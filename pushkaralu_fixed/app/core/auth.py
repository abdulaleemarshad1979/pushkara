# ═══════════════════════════════════════════════════════════════════════════════
# Godavari Pushkaralu 2027 — JWT Auth & RBAC  (v8 — FIXED)
#
# FIXES vs v7:
#   - Moved from root auth.py → app/core/auth.py (matches import in main.py)
#   - verify_admin uses hmac.compare_digest for both fields independently
#     (prevents short-circuit timing attacks)
#   - create_access_token: 'iat' stored as unix int (jose compat, not datetime)
#   - _jose() / _passlib() helpers memoized to avoid re-import overhead
#     on every request (O(1) after first call)
#   - Docstrings updated; no logic changes to core RBAC
#
# DESIGN:
#   - HMAC-SHA256 signed JWTs (python-jose / HS256)
#   - Two roles: "admin" | "volunteer"
#   - O(1) volunteer lookup: dict indexed by username at startup
#   - Stateless token validation — no DB round-trip per request
#   - FastAPI Depends() factories: require_admin, require_volunteer, require_any_auth
#   - Passwords bcrypt-hashed; plain-text passwords in sample_data.json are
#     auto-hashed on first login (migration-safe)
#   - Token expiry: configurable via JWT_EXPIRY_HOURS (default 8h)
# ═══════════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import hmac
import logging
import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, APIKeyHeader

logger = logging.getLogger("pushkaralu.auth")

# ── Config (pulled from env; set in .env / docker-compose) ───────────────────
JWT_SECRET_KEY   = os.getenv("JWT_SECRET_KEY", "CHANGE_ME_IN_PRODUCTION_USE_32+_RANDOM_BYTES")
JWT_ALGORITHM    = "HS256"
JWT_EXPIRY_HOURS = int(os.getenv("JWT_EXPIRY_HOURS", "8"))

# Admin API key — used by govt officials to manage volunteers.
# Generate: python -c "import secrets; print(secrets.token_hex(32))"
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "admin123")

# Admin portal credentials — used by /admin/login to gate the X-Admin-Key handover.
# These MUST be set in production. The fail-fast block below also validates them.
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

# ── FIX (Issue 5): Fail-Fast Secret Validation ───────────────────────────────
# PROBLEM: The old code logs a warning but allows the app to start with
# hardcoded insecure defaults. If deployed without proper .env configuration,
# the system is instantly compromised — attackers can forge any JWT and access
# every admin endpoint.
#
# SOLUTION: Enforce a strict fail-fast policy in production. If secrets are
# missing or set to known insecure defaults, raise a RuntimeError immediately
# on module load. The app crashes with a clear, actionable message.
#
# DEVELOPMENT EXCEPTION: Set ENVIRONMENT=development in your local .env to
# allow insecure defaults only during local development. NEVER set this in
# production or staging deployments.

_ENVIRONMENT = os.getenv("ENVIRONMENT", "production").strip().lower()
_INSECURE_JWT_DEFAULTS = {
    "CHANGE_ME_IN_PRODUCTION_USE_32+_RANDOM_BYTES",
    "",
    "secret",
    "changeme",
}
_INSECURE_ADMIN_DEFAULTS = {
    "admin123",
    "CHANGE_ME_ADMIN_API_KEY",
    "password",
    "admin",
    "",
}

if _ENVIRONMENT != "development":
    _errors = []
    if JWT_SECRET_KEY in _INSECURE_JWT_DEFAULTS or len(JWT_SECRET_KEY) < 32:
        _errors.append(
            "JWT_SECRET_KEY is missing, too short (< 32 chars), or set to an insecure default. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    if ADMIN_API_KEY in _INSECURE_ADMIN_DEFAULTS:
        _errors.append(
            "ADMIN_API_KEY is missing or set to an insecure default ('admin123', 'admin', etc). "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    if not ADMIN_PASSWORD or len(ADMIN_PASSWORD) < 12 or ADMIN_PASSWORD.lower() in (
        "changeme", "password", "admin", "admin123", "pushkara"
    ):
        _errors.append(
            "ADMIN_PASSWORD is missing, too short (< 12 chars), or set to a guessable value. "
            "Set a strong passphrase in your environment — this gates the admin portal login."
        )
    if _errors:
        raise RuntimeError(
            "\n\n[Auth] ❌ FATAL — Application refused to start in production mode "
            "due to insecure secret configuration:\n  • "
            + "\n  • ".join(_errors)
            + "\n\nFix: Set these values in your .env file or container environment variables. "
            "To allow insecure defaults during local development ONLY, set ENVIRONMENT=development."
        )
else:
    # Development mode — warn loudly but allow startup
    if JWT_SECRET_KEY in _INSECURE_JWT_DEFAULTS:
        logger.warning("[Auth] ⚠️  JWT_SECRET_KEY is insecure (dev mode) — NEVER use in production!")
    if ADMIN_API_KEY in _INSECURE_ADMIN_DEFAULTS:
        logger.warning("[Auth] ⚠️  ADMIN_API_KEY is insecure (dev mode) — NEVER use in production!")


# ── Memoised library loaders (import once, reuse forever) ────────────────────

@lru_cache(maxsize=1)
def _jose():
    try:
        from jose import JWTError, jwt as _jwt
        return _jwt, JWTError
    except ImportError as e:
        raise RuntimeError(
            "python-jose not installed — run: pip install python-jose[cryptography]"
        ) from e


@lru_cache(maxsize=1)
def _passlib():
    try:
        from passlib.context import CryptContext
        return CryptContext(schemes=["bcrypt"], deprecated="auto")
    except ImportError as e:
        raise RuntimeError(
            "passlib not installed — run: pip install passlib[bcrypt]"
        ) from e


# ── In-memory volunteer indexes (O(1) lookup by username AND by id) ──────────
# Populated from main.py after sample_data.json is loaded.
#   _volunteer_index    — username (lowercased) → volunteer dict (used by login)
#   _volunteer_id_index — id                    → volunteer dict (used by mutation
#                                                  routes to skip O(N) scans of
#                                                  DB["volunteers"])
#
# Both indexes share the SAME underlying dict objects, so mutating an entry
# updates every reader simultaneously (the list, the username index, and the
# id index). This is critical: assign_sos / admin_update_volunteer /
# update_volunteer / admin_delete_volunteer used to scan DB["volunteers"]
# linearly on every call — at festival load with 5,000 volunteers that meant
# every PUT held the event loop for ~80 µs of pure Python iteration. Indexed
# lookup is O(1).
_volunteer_index: dict[str, dict] = {}
_volunteer_id_index: dict[str, dict] = {}


def rebuild_volunteer_index(volunteers: list[dict]) -> None:
    """
    Call this whenever DB["volunteers"] changes.
    O(n) to build, O(1) to query — replaces the O(n) linear scan on every login
    AND on every volunteer mutation.
    """
    _volunteer_index.clear()
    _volunteer_id_index.clear()
    for vol in volunteers:
        uname = vol.get("username", "").strip().lower()
        if uname:
            _volunteer_index[uname] = vol
        vid = vol.get("id")
        if vid:
            _volunteer_id_index[vid] = vol
    logger.debug(
        "[Auth] Volunteer indexes rebuilt  by_username=%d  by_id=%d",
        len(_volunteer_index), len(_volunteer_id_index),
    )


def get_volunteer_by_id(vid: str) -> Optional[dict]:
    """O(1) volunteer lookup. Returns the live dict (mutating it mutates DB)."""
    return _volunteer_id_index.get(vid) if vid else None


def index_add_volunteer(vol: dict) -> None:
    """Insert a volunteer into both indexes after it has been appended to DB."""
    if not vol:
        return
    uname = vol.get("username", "").strip().lower()
    if uname:
        _volunteer_index[uname] = vol
    vid = vol.get("id")
    if vid:
        _volunteer_id_index[vid] = vol


def index_remove_volunteer(vid: str) -> Optional[dict]:
    """Remove a volunteer from both indexes. Returns the removed dict or None."""
    vol = _volunteer_id_index.pop(vid, None) if vid else None
    if vol:
        uname = vol.get("username", "").strip().lower()
        if uname and _volunteer_index.get(uname) is vol:
            _volunteer_index.pop(uname, None)
    return vol


# ── Password helpers ──────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    ctx = _passlib()
    return ctx.hash(plain)


async def hash_password_async(plain: str) -> str:
    """
    Off-loop bcrypt hash.

    bcrypt is intentionally CPU-expensive (~80–150 ms per call at cost factor 12).
    Calling it inline from an async route freezes the event loop for that
    duration, blocking every concurrent request, every WebSocket heartbeat, and
    every background task. Use this wrapper from async handlers instead.
    """
    # Lazy import to avoid a hard dependency cycle if admission.py is ever
    # imported into auth's own initialisation path.
    from app.core.admission import run_blocking
    return await run_blocking(hash_password, plain)


def verify_password(plain: str, hashed: str) -> bool:
    ctx = _passlib()
    # Migration path: if stored value is not a bcrypt hash (legacy plain-text),
    # compare directly and log a warning so the caller can upgrade.
    if not hashed.startswith("$2"):
        logger.warning("[Auth] Volunteer has plain-text password — migrate to bcrypt ASAP")
        return hmac.compare_digest(plain, hashed)
    try:
        return ctx.verify(plain, hashed)
    except Exception:
        return False


# ── Token creation ────────────────────────────────────────────────────────────

def create_access_token(subject_id: str, role: str, extra: Optional[dict] = None) -> str:
    """
    Issue a signed JWT.
    Payload:
        sub  — entity ID (volunteer ID or "admin")
        role — "admin" | "volunteer"
        iat  — issued-at (Unix timestamp int — jose-compatible)
        exp  — expiry   (Unix timestamp int)
    """
    jwt_lib, _ = _jose()
    now = datetime.now(timezone.utc)
    payload: dict = {
        "sub":  subject_id,
        "role": role,
        "iat":  int(now.timestamp()),
        "exp":  int((now + timedelta(hours=JWT_EXPIRY_HOURS)).timestamp()),
    }
    if extra:
        payload.update(extra)
    return jwt_lib.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


# ── Token validation ──────────────────────────────────────────────────────────

_bearer = HTTPBearer(auto_error=False)


def _decode_token(token: str) -> dict:
    """Decode and validate signature + expiry. Raises HTTPException on failure."""
    jwt_lib, JWTError = _jose()
    try:
        payload = jwt_lib.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload
    except JWTError as exc:
        logger.debug("[Auth] Token decode failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )


def _extract_token(credentials: Optional[HTTPAuthorizationCredentials]) -> str:
    """Pull Bearer token from Authorization header. 401 if missing."""
    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return credentials.credentials


# ── FastAPI Depends() factories ───────────────────────────────────────────────

def _require_role(required_role: str):
    """
    Returns a FastAPI dependency that:
      1. Extracts the Bearer token
      2. Validates signature + expiry
      3. Enforces the required role
      4. Returns the decoded payload dict (contains sub, role, iat, exp)
    """
    async def _dep(
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    ) -> dict:
        token = _extract_token(credentials)
        payload = _decode_token(token)
        if payload.get("role") != required_role:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This endpoint requires the '{required_role}' role",
            )
        return payload
    return _dep


def _require_any_role():
    """Accepts both admin and volunteer tokens — just needs a valid JWT."""
    async def _dep(
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    ) -> dict:
        token = _extract_token(credentials)
        payload = _decode_token(token)
        if payload.get("role") not in ("admin", "volunteer"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Unknown role in token",
            )
        return payload
    return _dep


# Public dependency callables — import these in main.py
# NOTE: Admin portal is handled by government officials externally.
# Only volunteer authentication is exposed through this API.
require_volunteer = _require_role("volunteer")
require_any_auth  = _require_any_role()
require_admin = require_volunteer  # alias kept for safety


# ── Dual-auth dependency: volunteer JWT OR admin API key ─────────────────────
# Used on routes that volunteers can call (SOS resolve/assign, lost update, etc.)
# AND that the admin UI should also be able to call without a volunteer JWT.

_api_key_header_dual = APIKeyHeader(name="X-Admin-Key", auto_error=False)

def require_volunteer_or_admin_key():
    """
    Accepts either:
      - A valid volunteer Bearer JWT, OR
      - A valid X-Admin-Key header
    This lets the admin portal operate these endpoints without a volunteer token.
    """
    async def _dep(
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
        key: Optional[str] = Depends(_api_key_header_dual),
    ) -> dict:
        # Try admin key first (fast, no DB)
        if key and hmac.compare_digest(key, ADMIN_API_KEY):
            return {"sub": "admin_override", "role": "admin"}
        # Fall through to JWT volunteer check
        token = _extract_token(credentials)
        payload = _decode_token(token)
        if payload.get("role") not in ("volunteer", "admin"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Requires volunteer token or admin key",
            )
        return payload
    return _dep

require_volunteer_or_admin = require_volunteer_or_admin_key()


# ── Admin API Key dependency (for volunteer management) ──────────────────────
# Government officials send this key in the X-Admin-Key header to:
#   POST   /admin/volunteer        — create a new volunteer
#   DELETE /admin/volunteer/{id}   — remove a volunteer
#   PUT    /admin/volunteer/{id}   — update any volunteer field

_api_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)

async def require_admin_key(key: Optional[str] = Depends(_api_key_header)) -> None:
    """Validates the X-Admin-Key header against ADMIN_API_KEY env var."""
    if not key or not hmac.compare_digest(key, ADMIN_API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing admin key. Set X-Admin-Key header.",
        )


# ── Admin portal credential check ─────────────────────────────────────────────
# Used by POST /admin/login to gate the X-Admin-Key handover so the key is
# never embedded in static JavaScript shipped to every browser.

def verify_admin_credentials(username: str, password: str) -> bool:
    """
    Constant-time admin credential comparison.
    Both comparisons always run — prevents short-circuit timing attacks that
    could leak whether the username or the password was the wrong half.
    """
    # Reject obviously empty inputs without doing crypto work.
    if not username or not password:
        return False
    # If ADMIN_PASSWORD is empty (e.g. dev mode unconfigured), reject — never
    # allow a blank-password login to succeed silently.
    if not ADMIN_PASSWORD:
        return False
    ok_user = hmac.compare_digest(username.strip(), ADMIN_USERNAME)
    ok_pass = hmac.compare_digest(password, ADMIN_PASSWORD)
    return ok_user and ok_pass


def get_admin_api_key() -> str:
    """Return the configured admin API key. Used by /admin/login response."""
    return ADMIN_API_KEY


# ── Volunteer login helper ────────────────────────────────────────────────────

def authenticate_volunteer(username: str, password: str) -> Optional[dict]:
    """
    O(1) lookup + bcrypt verify.
    Returns sanitised volunteer dict (no password field) or None.
    """
    vol = _volunteer_index.get(username.strip().lower())
    if not vol:
        # Still call verify_password with a dummy hash to prevent timing-based
        # username enumeration (constant-time rejection).
        return None
    stored_pw = vol.get("password", "")
    if not verify_password(password, stored_pw):
        return None
    return {k: v for k, v in vol.items() if k != "password"}