"""
Smoke tests for the audit & performance fix pass.
Runs without Redis or Postgres — uses lightweight stubs for fastapi/httpx/pydantic
when those libraries are not installed locally so the test still verifies the
real behaviour of the patched helpers.

Verifies:
  A1: find_nearest_volunteer / safe_volunteers strip password & password_hash
  A2: orchestrator uses get_running_loop (source-level check)
  A3: ai_predictor uses get_running_loop (source-level check)
  A4: chat._real_client_ip honours X-Forwarded-For
  A5: chat._cache_set bounds dict size under sustained load
  A6: chat._get_cached_system_prompt reuses the prompt for same DB shape
  A7: admin_create_volunteer hashes the password ONCE (single bcrypt cost)
  A8: get_app_data / get_hospitals etc. wrap responses with cache_get/cache_set
  A9: admin.html startCrowdPoll skips poll while WS is open

Exit 0 on success, non-zero on any failure.
"""
from __future__ import annotations

import importlib
import os
import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ENVIRONMENT", "development")

failures = []


def check(label: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}{(' — ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(label + (f": {detail}" if detail else ""))


def stub_modules_if_missing():
    """Install just-enough stubs so chat.py loads when fastapi/httpx/pydantic
    are unavailable in the sandbox. We only need APIRouter / HTTPException /
    Request / BaseModel and an httpx symbol — the helpers under test never
    call any of these classes. The stubs are no-ops."""
    def need(name): 
        try:
            importlib.import_module(name); return False
        except Exception:
            return True

    if need("fastapi"):
        fa = types.ModuleType("fastapi")
        class _APIRouter:
            def __init__(self, *a, **k): pass
            def get(self, *a, **k):  return lambda fn: fn
            def post(self, *a, **k): return lambda fn: fn
        class _HTTPException(Exception):
            def __init__(self, status_code=500, detail=""):
                self.status_code, self.detail = status_code, detail
                super().__init__(detail)
        class _Request: pass
        fa.APIRouter   = _APIRouter
        fa.HTTPException = _HTTPException
        fa.Request     = _Request
        sys.modules["fastapi"] = fa

    if need("httpx"):
        hx = types.ModuleType("httpx")
        class _AsyncClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self):  return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **k):
                raise RuntimeError("network not available in smoke test")
        hx.AsyncClient = _AsyncClient
        hx.TimeoutException = type("TimeoutException", (Exception,), {})
        sys.modules["httpx"] = hx

    if need("pydantic"):
        pd = types.ModuleType("pydantic")
        class _BaseModel:
            def __init__(self, **kw):
                for k, v in kw.items(): setattr(self, k, v)
            @classmethod
            def model_validate(cls, d): return cls(**d)
        pd.BaseModel = _BaseModel
        sys.modules["pydantic"] = pd


# ── A2/A3/A7/A8/A9 — pure source-level checks (no imports needed) ──────────
def code_lines(text: str) -> str:
    """Return only non-comment Python lines so substrings inside comments
    (e.g. references to a deprecated API in an explanatory note) are ignored."""
    out = []
    for ln in text.splitlines():
        stripped = ln.lstrip()
        if stripped.startswith("#"):
            continue
        # also strip trailing inline comments (cheap heuristic)
        out.append(ln.split("#")[0])
    return "\n".join(out)


print("\n=== A2 — Orchestrator uses get_running_loop ===")
src_full = (ROOT / "app/healing/orchestrator.py").read_text()
src      = code_lines(src_full)
check("no deprecated get_event_loop() in orchestrator code", "asyncio.get_event_loop()" not in src)
check("uses get_running_loop()",                             "asyncio.get_running_loop()" in src)

print("\n=== A3 — ai_predictor uses get_running_loop ===")
src_full = (ROOT / "app/core/ai_predictor.py").read_text()
src      = code_lines(src_full)
check("no deprecated get_event_loop() in ai_predictor code", "asyncio.get_event_loop()" not in src)
check("uses get_running_loop()",                             "asyncio.get_running_loop()" in src)

print("\n=== A7 — admin_create_volunteer hashes password ONCE ===")
src_full = (ROOT / "main.py").read_text()
src      = src_full
admin_block_start = src.find("async def admin_create_volunteer")
admin_block_end   = src.find("async def admin_update_volunteer")
admin_block       = src[admin_block_start:admin_block_end]
admin_code        = code_lines(admin_block)
check("hash_password called exactly once in admin_create_volunteer code",
      admin_code.count("hash_password(password)") == 1,
      f"count={admin_code.count('hash_password(password)')}")
check("password_hash and password set to same variable",
      'pw_hashed = hash_password(password)' in admin_block
      and '"password_hash": pw_hashed' in admin_block
      and '"password":      pw_hashed' in admin_block)

print("\n=== A8 — caching wrappers around static endpoints ===")
for ep in ("get_app_data", "get_hospitals", "get_police", "get_hotels", "get_tourism", "get_poojas"):
    pos = src.find(f"async def {ep}(")
    fn = src[pos:pos + 800] if pos >= 0 else ""
    check(f"{ep} hits cache_get",      "cache_get(" in fn)
    check(f"{ep} populates cache_set", "cache_set(" in fn)

print("\n=== A9 — admin.html skips /crowd/risk/all poll while WS is open ===")
ah = (ROOT / "dashboards/admin.html").read_text()
check("startCrowdPoll guards on ws.readyState",
      "ws.readyState === WebSocket.OPEN" in ah and "_crowdPollTimer = setInterval(" in ah)


# ── A1 — verify the sanitiser strips both password fields ─────────────────
print("\n=== A1 — Volunteer password/password_hash never leaks (source check) ===")
check("_sanitize_volunteer helper defined in main.py",
      "def _sanitize_volunteer" in src and 'k not in ("password", "password_hash")' in src)
check("safe_volunteers calls _sanitize_volunteer",
      "_sanitize_volunteer(vol) for vol in DB" in src)
check("find_nearest_volunteer returns sanitised record",
      "return _sanitize_volunteer(nearest)" in src)


# ── A4/A5/A6 — runtime tests against the patched chat module ──────────────
print("\n=== A4/A5/A6 — chat.py runtime helpers ===")
stub_modules_if_missing()
try:
    import chat as C  # type: ignore
except Exception as exc:
    check("chat module imports cleanly under stubs", False, repr(exc))
else:
    # A4
    def fake_request(headers: dict, host: str = "127.0.0.1"):
        r = MagicMock(); r.headers = headers
        r.client = MagicMock(host=host); return r

    ip = C._real_client_ip(fake_request({"x-forwarded-for": "203.0.113.5, 10.0.0.1"}, "172.18.0.4"))
    check("A4: XFF first entry preferred",        ip == "203.0.113.5", f"got {ip!r}")
    ip = C._real_client_ip(fake_request({"x-real-ip": "198.51.100.7"}, "172.18.0.4"))
    check("A4: X-Real-IP fallback",               ip == "198.51.100.7", f"got {ip!r}")
    ip = C._real_client_ip(fake_request({}, "127.0.0.1"))
    check("A4: client.host fallback",             ip == "127.0.0.1", f"got {ip!r}")

    # A5
    C._cache.clear()
    for i in range(800):
        C._cache_set(f"k{i}", f"v{i}")
    check("A5: cache size bounded ≤500 under fresh-only load", len(C._cache) <= 500, f"size={len(C._cache)}")
    C._cache.clear()

    # A6
    C._system_prompt_cache = None
    db = {
        "ghats": [{"name": "G1", "id": "g1", "telugu_name": "", "zone": "Z", "crowd_level": "low",
                   "current_count": 1, "capacity": 100, "bathing_timings": "x", "nearest_landmark": "",
                   "special_dates": [], "facilities": []}],
        "transport_routes": [], "facilities": [], "poojas": [], "hotels": [], "hospitals": [],
        "helplines": {},
    }
    t0 = time.perf_counter(); p1 = C._get_cached_system_prompt(db); t1 = time.perf_counter()
    t2 = time.perf_counter(); p2 = C._get_cached_system_prompt(db); t3 = time.perf_counter()
    check("A6: cache returns identical content", p1 == p2)
    check("A6: cached call is faster than build (or equal)", (t3 - t2) <= max(t1 - t0, 1e-6))
    db2 = dict(db); db2["ghats"] = db["ghats"] + [
        {"name": "G2", "id": "g2", "telugu_name": "", "zone": "Z", "crowd_level": "low",
         "current_count": 1, "capacity": 100, "bathing_timings": "x", "nearest_landmark": "",
         "special_dates": [], "facilities": []}
    ]
    p3 = C._get_cached_system_prompt(db2)
    check("A6: cache busts when DB shape changes", p1 != p3)


print()
if failures:
    print(f"FAIL — {len(failures)} smoke check(s) failed:")
    for f in failures:
        print("   -", f)
    sys.exit(1)
print("PASS — all smoke checks passed.")
sys.exit(0)
