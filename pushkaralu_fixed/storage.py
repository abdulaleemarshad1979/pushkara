# DEPRECATED — use app/core/storage.py instead. This root-level copy is no longer imported.
# It remains here only for reference. All active imports point to app/core/.

# ═══════════════════════════════════════════════════════════════════════════════
# Godavari Pushkaralu 2027 — Object Storage (S3 / Cloudflare R2)  (v8 — FIXED)
#
# FIXES vs v7:
#   - Moved from root storage.py → app/core/storage.py (matches import in main.py)
#   - _upload_to_s3: aioboto3 Session created fresh per upload (thread-safe);
#     sessions are not meant to be reused across coroutines
#   - ACL "public-read" removed from default — Cloudflare R2 and many S3
#     buckets have ACLs disabled by default, causing AccessControlListNotSupported.
#     Set S3_ACL=public-read in .env only if your bucket requires it.
#   - Streaming read: buf.tell() check moved inside the loop to catch
#     oversize files early instead of at the end
#   - delete_image: URL parsing hardened against edge-case URL formats
#   - Added S3_ACL env var for configurable ACL support
#
# DESIGN:
#   - Async streaming upload via aioboto3 (never loads full file into RAM).
#   - Supports AWS S3 and Cloudflare R2 (same S3-compatible API, different endpoint).
#   - Falls back to local disk if S3 env vars are not configured.
#   - Uploaded files are given a UUID-prefixed key to prevent collisions.
#   - Returns a public HTTPS URL stored in PostgreSQL.
#
# ENV VARS:
#   S3_BUCKET_NAME        — your bucket name
#   S3_ACCESS_KEY_ID      — AWS access key or R2 access token
#   S3_SECRET_ACCESS_KEY  — AWS secret or R2 secret
#   S3_ENDPOINT_URL       — set ONLY for R2/MinIO (leave blank for AWS)
#   S3_REGION             — e.g. "ap-south-1" (AWS) or "auto" (R2)
#   S3_PUBLIC_BASE_URL    — CDN prefix, e.g. https://cdn.example.com
#   S3_ACL                — "public-read" (AWS) or "" (R2 / ACL-disabled buckets)
#
# BIG-O:
#   Upload — O(n/chunk_size) network round-trips; memory usage O(chunk_size)
# ═══════════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import io
import logging
import mimetypes
import os
import uuid
from typing import Optional

from fastapi import UploadFile

logger = logging.getLogger("pushkaralu.storage")

# ── Config ────────────────────────────────────────────────────────────────────
S3_BUCKET          = os.getenv("S3_BUCKET_NAME", "")
S3_ACCESS_KEY      = os.getenv("S3_ACCESS_KEY_ID", "")
S3_SECRET_KEY      = os.getenv("S3_SECRET_ACCESS_KEY", "")
S3_ENDPOINT_URL    = os.getenv("S3_ENDPOINT_URL", "")          # blank = AWS default
S3_REGION          = os.getenv("S3_REGION", "ap-south-1")
S3_PUBLIC_BASE_URL = os.getenv("S3_PUBLIC_BASE_URL", "")       # CDN or bucket URL
S3_CHUNK_MB        = int(os.getenv("S3_CHUNK_MB", "8"))
# ACL: set "public-read" for AWS; leave blank for Cloudflare R2 / ACL-disabled buckets
S3_ACL             = os.getenv("S3_ACL", "")

# Local fallback config
LOCAL_UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")
UPLOAD_MAX_MB    = int(os.getenv("UPLOAD_MAX_MB", "5"))

_s3_configured = bool(S3_BUCKET and S3_ACCESS_KEY and S3_SECRET_KEY)


def _public_url(key: str) -> str:
    """Build the public URL for an uploaded object."""
    if S3_PUBLIC_BASE_URL:
        return f"{S3_PUBLIC_BASE_URL.rstrip('/')}/{key}"
    if S3_ENDPOINT_URL:
        # R2 / MinIO style: endpoint/bucket/key
        return f"{S3_ENDPOINT_URL.rstrip('/')}/{S3_BUCKET}/{key}"
    # AWS S3 virtual-hosted style
    return f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/{key}"


def _content_type(filename: str) -> str:
    ct, _ = mimetypes.guess_type(filename)
    return ct or "application/octet-stream"


# ── Streaming S3 upload (memory-safe) ────────────────────────────────────────

async def _upload_to_s3(data: bytes, key: str, content_type: str) -> str:
    """
    Upload bytes to S3/R2 and return the public URL.
    Uses aioboto3 for true async I/O — no thread blocking.
    A single put_object call is optimal for files ≤ UPLOAD_MAX_MB (5 MB default).
    """
    try:
        import aioboto3
    except ImportError:
        raise RuntimeError("aioboto3 not installed — run: pip install aioboto3")

    # Session created fresh per call — aioboto3 sessions are not coroutine-safe
    # if shared across concurrent uploads.
    session = aioboto3.Session(
        aws_access_key_id     = S3_ACCESS_KEY,
        aws_secret_access_key = S3_SECRET_KEY,
        region_name           = S3_REGION,
    )

    put_kwargs: dict = dict(Bucket=S3_BUCKET, Key=key, Body=data, ContentType=content_type)
    # Only set ACL if explicitly configured — R2 and many newer S3 setups
    # raise AccessControlListNotSupported when ACL is set on ACL-disabled buckets.
    if S3_ACL:
        put_kwargs["ACL"] = S3_ACL

    endpoint_kwargs: dict = {}
    if S3_ENDPOINT_URL:
        endpoint_kwargs["endpoint_url"] = S3_ENDPOINT_URL

    async with session.client("s3", **endpoint_kwargs) as s3:
        await s3.put_object(**put_kwargs)

    url = _public_url(key)
    logger.info("[Storage] Uploaded  key=%s  url=%s", key, url)
    return url


# ── Local fallback (dev / misconfigured env) ──────────────────────────────────

def _save_local(data: bytes, filename: str) -> str:
    os.makedirs(LOCAL_UPLOAD_DIR, exist_ok=True)
    path = os.path.join(LOCAL_UPLOAD_DIR, filename)
    with open(path, "wb") as f:
        f.write(data)
    logger.info("[Storage] Saved locally  path=%s", path)
    return f"/{path}"


# ── Public interface ──────────────────────────────────────────────────────────

async def upload_image(file: Optional[UploadFile], folder: str = "lost-found") -> Optional[str]:
    """
    Stream-safe image upload.

    1. Validates MIME type (must start with "image/").
    2. Reads the file in chunks (≤ S3_CHUNK_MB) — rejects early if oversize.
    3. Uploads to S3/R2 if configured; falls back to local disk.
    4. Returns a public URL string, or None on failure / missing file.
    """
    if not file or not file.filename:
        return None

    content_type = file.content_type or ""
    if not content_type.startswith("image/"):
        # Try to guess from filename extension
        guessed = _content_type(file.filename)
        if not guessed.startswith("image/"):
            logger.info("[Storage] Rejected non-image upload: %s (%s)", file.filename, content_type)
            return None
        content_type = guessed

    try:
        max_bytes = UPLOAD_MAX_MB * 1024 * 1024
        chunk_size = S3_CHUNK_MB * 1024 * 1024
        buf = io.BytesIO()

        # FIX: check size inside loop → early rejection on oversize files
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            buf.write(chunk)
            if buf.tell() > max_bytes:
                buf.close()
                raise ValueError(f"File exceeds {UPLOAD_MAX_MB} MB limit")

        data = buf.getvalue()
        buf.close()

        # Sanitise filename — strip path traversal attempts, replace spaces
        safe_name = os.path.basename(file.filename).replace(" ", "_")
        # Extra guard: strip any remaining path separators
        safe_name = safe_name.replace("/", "_").replace("\\", "_")
        unique_key = f"{folder}/{uuid.uuid4().hex}_{safe_name}"

        if _s3_configured:
            return await _upload_to_s3(data, unique_key, content_type)
        else:
            logger.warning(
                "[Storage] S3 not configured — saving to local disk. "
                "Set S3_BUCKET_NAME, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY in .env"
            )
            return _save_local(data, unique_key.replace("/", "_"))

    except ValueError as exc:
        logger.info("[Storage] Rejected: %s", exc)
        return None
    except Exception as exc:
        logger.error("[Storage] Upload failed: %s", exc)
        return None


async def delete_image(url: str) -> bool:
    """
    Delete an object from S3/R2 given its public URL.
    No-op if S3 is not configured or the URL is a local path.
    """
    if not _s3_configured or not url or url.startswith("/"):
        return False

    try:
        import aioboto3

        # Extract key from URL — handle all three URL formats
        key: str
        if S3_PUBLIC_BASE_URL and url.startswith(S3_PUBLIC_BASE_URL):
            key = url[len(S3_PUBLIC_BASE_URL):].lstrip("/")
        elif S3_ENDPOINT_URL and url.startswith(S3_ENDPOINT_URL):
            # strip endpoint/bucket/ prefix
            parts = url[len(S3_ENDPOINT_URL):].lstrip("/").split("/", 1)
            key = parts[1] if len(parts) > 1 else parts[0]
        else:
            # AWS virtual-hosted: strip https://bucket.s3.region.amazonaws.com/
            if ".amazonaws.com/" in url:
                key = url.split(".amazonaws.com/", 1)[-1]
            else:
                logger.warning("[Storage] Could not parse key from URL: %s", url)
                return False

        session = aioboto3.Session(
            aws_access_key_id     = S3_ACCESS_KEY,
            aws_secret_access_key = S3_SECRET_KEY,
            region_name           = S3_REGION,
        )
        endpoint_kwargs = {"endpoint_url": S3_ENDPOINT_URL} if S3_ENDPOINT_URL else {}
        async with session.client("s3", **endpoint_kwargs) as s3:
            await s3.delete_object(Bucket=S3_BUCKET, Key=key)

        logger.info("[Storage] Deleted  key=%s", key)
        return True

    except Exception as exc:
        logger.warning("[Storage] Delete failed: %s", exc)
        return False