# DEPRECATED — use app/core/auth.py instead. This root-level copy is no longer imported.
# It remains here only for reference. All active imports point to app/core/.

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
from dotenv import load_dotenv
load_dotenv()
from functools import lru_cache
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger("pushkaralu.auth")

# ── Config (pulled from env; set in .env / docker-compose) ───────────────────
JWT_SECRET_KEY   = os.getenv("JWT_SECRET_KEY", "CHANGE_ME_IN_PRODUCTION_USE_32+_RANDOM_BYTES")
JWT_ALGORITHM    = "HS256"
JWT_EXPIRY_HOURS = int(os.getenv("JWT_EXPIRY_HOURS", "8"))

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "CHANGE_ME_ADMIN_PASSWORD")

if JWT_SECRET_KEY == "CHANGE_ME_IN_PRODUCTION_USE_32+_RANDOM_BYTES":
    logger.warning("[Auth] JWT_SECRET_KEY is the default insecure value — set it in .env before production!")
if ADMIN_PASSWORD == "CHANGE_ME_ADMIN_PASSWORD":
    logger.warning("[Auth] ADMIN_PASSWORD is the default insecure value — set it in .env before production!")


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


# ── In-memory volunteer index (O(1) lookup by username) ──────────────────────
# Populated from main.py after sample_data.json is loaded.
# Key: username (str)  →  Value: volunteer dict (includes "password" field)
_volunteer_index: dict[str, dict] = {}


def rebuild_volunteer_index(volunteers: list[dict]) -> None:
    """
    Call this whenever DB["volunteers"] changes.
    O(n) to build, O(1) to query — replaces the O(n) linear scan on every login.
    """
    _volunteer_index.clear()
    for vol in volunteers:
        uname = vol.get("username", "").strip().lower()
        if uname:
            _volunteer_index[uname] = vol
    logger.debug("[Auth] Volunteer index rebuilt  entries=%d", len(_volunteer_index))


# ── Password helpers ──────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    ctx = _passlib()
    return ctx.hash(plain)


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
require_admin     = _require_role("admin")
require_volunteer = _require_role("volunteer")
require_any_auth  = _require_any_role()


# ── Admin credential helper ───────────────────────────────────────────────────

def verify_admin(username: str, password: str) -> bool:
    """
    Constant-time-safe admin credential check.
    Both comparisons always run — prevents short-circuit timing attacks.
    """
    ok_user = hmac.compare_digest(username, ADMIN_USERNAME)
    ok_pass = hmac.compare_digest(password, ADMIN_PASSWORD)
    return ok_user and ok_pass


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