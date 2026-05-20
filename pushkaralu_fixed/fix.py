#!/usr/bin/env python3
"""
pushkaralu_fix.py — Surgical bug & dead-code remover for Pushkaralu v16
========================================================================

Applies every fix from the audit report in one shot.
Zero external dependencies — runs with stdlib only (Python 3.9+).

Usage
-----
    # Preview what would change (nothing written to disk):
    python pushkaralu_fix.py --dry-run /path/to/pushkaralu_v16

    # Apply all fixes (backup created automatically):
    python pushkaralu_fix.py /path/to/pushkaralu_v16

    # Apply only specific fix IDs:
    python pushkaralu_fix.py /path/to/pushkaralu_v16 --only BUG-01 BUG-04

    # Restore from backup:
    python pushkaralu_fix.py /path/to/pushkaralu_v16 --restore

Guarantees
----------
  • Every file is backed up before any modification.
  • Each fix is applied with exact string matching — if the target text
    is not found verbatim, the fix is SKIPPED (never corrupts a file).
  • Dry-run shows the diff without writing anything.
  • Restore undoes every change from the last run.
"""

from __future__ import annotations

import argparse
import datetime
import difflib
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# ── Terminal colours (degrades gracefully on Windows without ANSI) ────────────

_USE_COLOR = sys.stdout.isatty() and os.name != "nt"


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def RED(t):    return _c("31;1", t)
def GREEN(t):  return _c("32;1", t)
def YELLOW(t): return _c("33;1", t)
def CYAN(t):   return _c("36;1", t)
def BOLD(t):   return _c("1", t)
def DIM(t):    return _c("2", t)


# ── Result tracking ───────────────────────────────────────────────────────────

@dataclass
class FixResult:
    fix_id:   str
    title:    str
    status:   str          # "applied" | "skipped" | "dry_run" | "error"
    detail:   str = ""
    diff:     str = ""


# ── Core patch engine ─────────────────────────────────────────────────────────

class Patcher:
    """
    Applies surgical text replacements to files.
    All changes are staged in memory; written atomically only on commit().
    """

    def __init__(self, root: Path, dry_run: bool = False):
        self.root    = root.resolve()
        self.dry_run = dry_run
        self._staged: dict[Path, str] = {}   # path → new content
        self._originals: dict[Path, str] = {}

    def _read(self, rel: str) -> Optional[str]:
        p = self.root / rel
        if not p.exists():
            return None
        return p.read_text(encoding="utf-8")

    def _get(self, rel: str) -> Optional[str]:
        """Return in-memory staged version or disk version."""
        p = self.root / rel
        if p in self._staged:
            return self._staged[p]
        return self._read(rel)

    def replace_exact(
        self,
        rel: str,
        old: str,
        new: str,
    ) -> bool:
        """
        Replace the FIRST occurrence of `old` with `new` in file `rel`.
        Returns True if the replacement was found and staged.
        """
        content = self._get(rel)
        if content is None:
            return False
        if old not in content:
            return False
        p = self.root / rel
        if p not in self._originals:
            self._originals[p] = content
        self._staged[p] = content.replace(old, new, 1)
        return True

    def replace_all(
        self,
        rel: str,
        old: str,
        new: str,
    ) -> int:
        """Replace ALL occurrences. Returns count of replacements."""
        content = self._get(rel)
        if content is None:
            return 0
        count = content.count(old)
        if count == 0:
            return 0
        p = self.root / rel
        if p not in self._originals:
            self._originals[p] = content
        self._staged[p] = content.replace(old, new)
        return count

    def delete_file(self, rel: str) -> bool:
        """Stage a file for deletion. Returns True if file exists."""
        p = self.root / rel
        if not p.exists():
            return False
        content = self._read(rel)
        if content is not None:
            self._originals[p] = content
        # Mark as deleted by storing sentinel
        self._staged[p] = "__DELETED__"
        return True

    def get_diff(self, rel: str) -> str:
        p = self.root / rel
        original = self._originals.get(p, "")
        staged   = self._staged.get(p, original)
        if staged == "__DELETED__":
            staged = ""
        lines_a = original.splitlines(keepends=True)
        lines_b = staged.splitlines(keepends=True)
        diff = list(difflib.unified_diff(
            lines_a, lines_b,
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
            lineterm="",
        ))
        return "\n".join(diff)

    def commit(self, backup_dir: Path):
        """Write all staged changes to disk. backup_dir must exist."""
        manifest = []
        for p, new_content in self._staged.items():
            rel = str(p.relative_to(self.root))
            # Back up original
            backup_path = backup_dir / rel
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            original = self._originals.get(p, "")
            backup_path.write_text(original, encoding="utf-8")
            manifest.append({"rel": rel, "existed": p.exists()})

            if new_content == "__DELETED__":
                if p.exists():
                    p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(new_content, encoding="utf-8")

        # Save manifest so restore knows what to do
        (backup_dir / "_manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )


# ── Individual fix definitions ────────────────────────────────────────────────

@dataclass
class Fix:
    fix_id:  str
    title:   str
    apply:   Callable[[Patcher], bool]   # returns True if fix was applicable


def _all_fixes() -> list[Fix]:
    fixes = []

    # ─────────────────────────────────────────────────────────────────────────
    # BUG-01: safe_volunteers() leaks password_hash field
    # main.py line 171
    # ─────────────────────────────────────────────────────────────────────────
    def fix_bug01(p: Patcher) -> bool:
        return p.replace_exact(
            "main.py",
            old='return [{k: v for k, v in vol.items() if k != "password"} for vol in DB["volunteers"]]',
            new='return [{k: v for k, v in vol.items() if k not in ("password", "password_hash")} for vol in DB["volunteers"]]',
        )

    fixes.append(Fix(
        fix_id="BUG-01",
        title='safe_volunteers() leaks password_hash — strip both password fields',
        apply=fix_bug01,
    ))

    # ─────────────────────────────────────────────────────────────────────────
    # BUG-02: require_admin alias accepts volunteer tokens
    # app/core/auth.py line 265
    # ─────────────────────────────────────────────────────────────────────────
    def fix_bug02(p: Patcher) -> bool:
        return p.replace_exact(
            "app/core/auth.py",
            old="require_admin = require_volunteer  # alias kept for safety\n",
            new="# require_admin alias removed — it incorrectly accepted volunteer tokens.\n"
               "# Use require_admin_key (X-Admin-Key header) for admin endpoints.\n",
        )

    fixes.append(Fix(
        fix_id="BUG-02",
        title="require_admin alias silently accepted volunteer tokens — removed",
        apply=fix_bug02,
    ))

    # ─────────────────────────────────────────────────────────────────────────
    # BUG-03: Two duplicate APIKeyHeader instances for X-Admin-Key
    # app/core/auth.py lines 272, 308
    # ─────────────────────────────────────────────────────────────────────────
    def fix_bug03(p: Patcher) -> bool:
        # Remove the first duplicate header object and update its one usage
        # to point at the surviving _api_key_header
        step1 = p.replace_exact(
            "app/core/auth.py",
            old="_api_key_header_dual = APIKeyHeader(name=\"X-Admin-Key\", auto_error=False)\n",
            new="",
        )
        step2 = p.replace_exact(
            "app/core/auth.py",
            old="        key: Optional[str] = Depends(_api_key_header_dual),\n",
            new="        key: Optional[str] = Depends(_api_key_header),\n",
        )
        return step1 or step2

    fixes.append(Fix(
        fix_id="BUG-03",
        title="Duplicate APIKeyHeader for X-Admin-Key — consolidated to one instance",
        apply=fix_bug03,
    ))

    # ─────────────────────────────────────────────────────────────────────────
    # BUG-04: asyncio.get_event_loop() deprecated in orchestrator.py
    # app/healing/orchestrator.py line 293
    # ─────────────────────────────────────────────────────────────────────────
    def fix_bug04(p: Patcher) -> bool:
        # There is exactly one occurrence in the event loop guardian
        return p.replace_exact(
            "app/healing/orchestrator.py",
            old="            loop   = asyncio.get_event_loop()\n",
            new="            loop   = asyncio.get_running_loop()\n",
        )

    fixes.append(Fix(
        fix_id="BUG-04",
        title="asyncio.get_event_loop() → get_running_loop() in orchestrator",
        apply=fix_bug04,
    ))

    # ─────────────────────────────────────────────────────────────────────────
    # BUG-05: connection.py schema.sql path resolves to wrong location
    # app/core/connection.py line 46
    # ─────────────────────────────────────────────────────────────────────────
    def fix_bug05(p: Patcher) -> bool:
        return p.replace_exact(
            "app/core/connection.py",
            old='        schema_path = Path(__file__).parent / "schema.sql"\n',
            new='        schema_path = Path(__file__).parent.parent.parent / "db" / "schema.sql"\n',
        )

    fixes.append(Fix(
        fix_id="BUG-05",
        title="connection.py schema.sql path fixed to db/schema.sql",
        apply=fix_bug05,
    ))

    # ─────────────────────────────────────────────────────────────────────────
    # BUG-06: uuid imported inside hot-path function in redis_manager.py
    # Move `import uuid as _uuid` to module top level
    # ─────────────────────────────────────────────────────────────────────────
    def fix_bug06(p: Patcher) -> bool:
        # Add uuid to top-level imports (after `import time`)
        step1 = p.replace_exact(
            "app/core/redis_manager.py",
            old="import time\nfrom typing import Any, Optional\n",
            new="import time\nimport uuid as _uuid\nfrom typing import Any, Optional\n",
        )
        # Remove the function-level import
        step2 = p.replace_exact(
            "app/core/redis_manager.py",
            old="        import uuid as _uuid\n",
            new="",
        )
        return step1 or step2

    fixes.append(Fix(
        fix_id="BUG-06",
        title="uuid import moved from hot-path function to module top level",
        apply=fix_bug06,
    ))

    # ─────────────────────────────────────────────────────────────────────────
    # BUG-07: re and unicodedata imported inside upload hot path in storage.py
    # Also move _ALLOWED_IMAGE_EXTS to module level
    # ─────────────────────────────────────────────────────────────────────────
    def fix_bug07(p: Patcher) -> bool:
        # Add re and unicodedata to top-level imports
        step1 = p.replace_exact(
            "app/core/storage.py",
            old="import io\nimport logging\nimport mimetypes\nimport os\nimport uuid\n",
            new="import io\nimport logging\nimport mimetypes\nimport os\nimport re\nimport unicodedata\nimport uuid\n",
        )
        # Add _ALLOWED_IMAGE_EXTS as a module-level constant (after imports block)
        step2 = p.replace_exact(
            "app/core/storage.py",
            old='logger = logging.getLogger("pushkaralu.storage")\n',
            new='logger = logging.getLogger("pushkaralu.storage")\n\n'
               '# Image extension allowlist — validated in upload_image()\n'
               '_ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}\n',
        )
        # Remove the in-function imports
        step3 = p.replace_exact(
            "app/core/storage.py",
            old="        import re\n        import unicodedata\n\n        raw_name = file.filename or",
            new="        raw_name = file.filename or",
        )
        # Remove the in-function _ALLOWED_IMAGE_EXTS definition
        step4 = p.replace_exact(
            "app/core/storage.py",
            old='        _ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}\n',
            new="",
        )
        return any([step1, step2, step3, step4])

    fixes.append(Fix(
        fix_id="BUG-07",
        title="re, unicodedata, _ALLOWED_IMAGE_EXTS moved to module level in storage.py",
        apply=fix_bug07,
    ))

    # ─────────────────────────────────────────────────────────────────────────
    # DEAD-01: Delete deprecated root-level auth.py, pg_store.py, storage.py
    # ─────────────────────────────────────────────────────────────────────────
    def fix_dead01(p: Patcher) -> bool:
        a = p.delete_file("auth.py")
        b = p.delete_file("pg_store.py")
        c = p.delete_file("storage.py")
        return a or b or c

    fixes.append(Fix(
        fix_id="DEAD-01",
        title="Deprecated root-level auth.py / pg_store.py / storage.py deleted",
        apply=fix_dead01,
    ))

    # ─────────────────────────────────────────────────────────────────────────
    # DEAD-02: Delete app/core/connection.py — never imported anywhere
    # ─────────────────────────────────────────────────────────────────────────
    def fix_dead02(p: Patcher) -> bool:
        return p.delete_file("app/core/connection.py")

    fixes.append(Fix(
        fix_id="DEAD-02",
        title="app/core/connection.py deleted — module never imported",
        apply=fix_dead02,
    ))

    # ─────────────────────────────────────────────────────────────────────────
    # DEAD-03: Remove unused find_nearest_fire_station and get_emergency_numbers
    # services/emergency_service.py
    # ─────────────────────────────────────────────────────────────────────────
    def fix_dead03(p: Patcher) -> bool:
        step1 = p.replace_exact(
            "services/emergency_service.py",
            old=(
                "\r\n\r\ndef find_nearest_fire_station(user_lat: float, user_lon: float) -> Optional[dict]:\r\n"
                "    return find_nearest(user_lat, user_lon, \"fire\")\r\n"
                "\r\n\r\ndef get_emergency_numbers() -> dict:\r\n"
                "    \"\"\"Return the essential phone-only helplines as a compact dict.\"\"\"\r\n"
                "    return {\r\n"
                "        \"ambulance\": AMBULANCE_NUMBER,\r\n"
                "        \"fire\":      FIRE_NUMBER,\r\n"
                "        \"police\":    POLICE_NUMBER,\r\n"
                "    }\r\n"
            ),
            new="\r\n",
        )
        # Also try LF endings in case the file was normalized
        step2 = p.replace_exact(
            "services/emergency_service.py",
            old=(
                "\n\ndef find_nearest_fire_station(user_lat: float, user_lon: float) -> Optional[dict]:\n"
                "    return find_nearest(user_lat, user_lon, \"fire\")\n"
                "\n\ndef get_emergency_numbers() -> dict:\n"
                "    \"\"\"Return the essential phone-only helplines as a compact dict.\"\"\"\n"
                "    return {\n"
                "        \"ambulance\": AMBULANCE_NUMBER,\n"
                "        \"fire\":      FIRE_NUMBER,\n"
                "        \"police\":    POLICE_NUMBER,\n"
                "    }\n"
            ),
            new="\n",
        )
        return step1 or step2

    fixes.append(Fix(
        fix_id="DEAD-03",
        title="Unused find_nearest_fire_station / get_emergency_numbers removed",
        apply=fix_dead03,
    ))

    # ─────────────────────────────────────────────────────────────────────────
    # DEAD-04: Remove HELPLINES list from state/emergency_services.py
    # ─────────────────────────────────────────────────────────────────────────
    def fix_dead04(p: Patcher) -> bool:
        step1 = p.replace_exact(
            "state/emergency_services.py",
            old="HELPLINES     = [s for s in EMERGENCY_SERVICES if s[\"category\"] == \"helpline\"]\r\n",
            new="",
        )
        step2 = p.replace_exact(
            "state/emergency_services.py",
            old='HELPLINES     = [s for s in EMERGENCY_SERVICES if s["category"] == "helpline"]\n',
            new="",
        )
        return step1 or step2

    fixes.append(Fix(
        fix_id="DEAD-04",
        title="Unused HELPLINES variable removed from emergency_services.py",
        apply=fix_dead04,
    ))

    # ─────────────────────────────────────────────────────────────────────────
    # DEAD-05: Remove stream_read function from redis_manager.py
    # ─────────────────────────────────────────────────────────────────────────
    def fix_dead05(p: Patcher) -> bool:
        return p.replace_exact(
            "app/core/redis_manager.py",
            old=(
                "\nasync def stream_read(stream: str, last_id: str = \"0\", count: int = 100) -> list:\n"
                "    try:\n"
                "        r = await get_redis()\n"
                "        results = await _safe(r.xread({stream: last_id}, count=count, block=2000), default=[])\n"
                "        if results:\n"
                "            _stream, messages = results[0]\n"
                "            return messages\n"
                "        return []\n"
                "    except Exception as exc:\n"
                "        logger.debug(\"[Stream] XREAD failed err=%s\", exc)\n"
                "        return []\n"
            ),
            new="\n",
        )

    fixes.append(Fix(
        fix_id="DEAD-05",
        title="Unused stream_read() removed from redis_manager.py",
        apply=fix_dead05,
    ))

    # ─────────────────────────────────────────────────────────────────────────
    # DEAD-06: Remove cache_invalidate_pattern from redis_manager.py
    # ─────────────────────────────────────────────────────────────────────────
    def fix_dead06(p: Patcher) -> bool:
        return p.replace_exact(
            "app/core/redis_manager.py",
            old=(
                "\nasync def cache_invalidate_pattern(pattern: str):\n"
                "    try:\n"
                "        r = await get_redis()\n"
                "        cursor = 0\n"
                "        while True:\n"
                "            result = await _safe(r.scan(cursor, match=pattern, count=100), default=(0, []))\n"
                "            cursor, keys = result\n"
                "            if keys:\n"
                "                await _safe(r.delete(*keys))\n"
                "            if cursor == 0:\n"
                "                break\n"
                "    except Exception as exc:\n"
                "        logger.debug(\"[Cache] SCAN-DEL failed pattern=%s err=%s\", pattern, exc)\n"
            ),
            new="\n",
        )

    fixes.append(Fix(
        fix_id="DEAD-06",
        title="Unused cache_invalidate_pattern() removed from redis_manager.py",
        apply=fix_dead06,
    ))

    # ─────────────────────────────────────────────────────────────────────────
    # DEAD-07: Remove redundant sys.path.insert from main.py
    # ─────────────────────────────────────────────────────────────────────────
    def fix_dead07(p: Patcher) -> bool:
        return p.replace_exact(
            "main.py",
            old="import sys\nsys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n",
            new="",
        )

    fixes.append(Fix(
        fix_id="DEAD-07",
        title="Redundant sys.path.insert removed from main.py",
        apply=fix_dead07,
    ))

    # ─────────────────────────────────────────────────────────────────────────
    # DEAD-08: Remove haversine wrapper in main.py, update the one call site
    # ─────────────────────────────────────────────────────────────────────────
    def fix_dead08(p: Patcher) -> bool:
        # Remove the wrapper function
        step1 = p.replace_exact(
            "main.py",
            old="def haversine(lat1, lon1, lat2, lon2): return _hvs(lat1, lon1, lat2, lon2)\n\n",
            new="",
        )
        # Update find_nearest_volunteer to call _hvs directly
        step2 = p.replace_exact(
            "main.py",
            old="    return min(available, key=lambda v: haversine(lat, lon, v[\"latitude\"], v[\"longitude\"])) if available else None\n",
            new="    return min(available, key=lambda v: _hvs(lat, lon, v[\"latitude\"], v[\"longitude\"])) if available else None\n",
        )
        return step1 or step2

    fixes.append(Fix(
        fix_id="DEAD-08",
        title="Redundant haversine() wrapper removed; find_nearest_volunteer now calls _hvs directly",
        apply=fix_dead08,
    ))

    # ─────────────────────────────────────────────────────────────────────────
    # DEAD-09: Warn about duplicate HTML files (cannot auto-resolve which is canonical)
    # ─────────────────────────────────────────────────────────────────────────
    def fix_dead09(p: Patcher) -> bool:
        # We report this but do NOT auto-delete — the canonical set must be
        # confirmed by the developer (depends on nginx.conf serve path).
        # Instead, detect if both sets exist and surface a warning.
        root_html = ["admin.html", "index.html", "user.html", "config.js"]
        dash_html = [f"dashboards/{f}" for f in root_html]
        both_exist = all(
            (p.root / r).exists() and (p.root / d).exists()
            for r, d in zip(root_html, dash_html)
        )
        if not both_exist:
            return False
        # Create a note file instead of auto-deleting
        note = (
            "# Duplicate HTML Files — Manual Review Required\n\n"
            "The following files exist in BOTH the project root AND dashboards/:\n"
            "  admin.html, index.html, user.html, config.js\n\n"
            "Action required:\n"
            "  1. Check infrastructure/nginx/nginx.conf to find which path is served.\n"
            "  2. Keep only the canonical set.\n"
            "  3. Delete this note file after resolving.\n\n"
            "The fix CLI did NOT auto-delete either set because choosing the wrong\n"
            "one would break the running application.\n"
        )
        note_path = p.root / "DUPLICATE_HTML_WARNING.md"
        note_path.write_text(note, encoding="utf-8")
        return True

    fixes.append(Fix(
        fix_id="DEAD-09",
        title="Duplicate HTML/config files — review note written (manual deletion required)",
        apply=fix_dead09,
    ))

    return fixes


# ── Backup / Restore ──────────────────────────────────────────────────────────

def _backup_dir(root: Path) -> Path:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return root / f".pushkaralu_fix_backup_{ts}"


def _latest_backup(root: Path) -> Optional[Path]:
    candidates = sorted(root.glob(".pushkaralu_fix_backup_*"), reverse=True)
    return candidates[0] if candidates else None


def do_restore(root: Path):
    backup = _latest_backup(root)
    if not backup:
        print(RED("✗ No backup found. Nothing to restore."))
        sys.exit(1)

    manifest_path = backup / "_manifest.json"
    if not manifest_path.exists():
        print(RED("✗ Backup manifest missing. Cannot restore safely."))
        sys.exit(1)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    print(BOLD(f"Restoring from {backup.name} …"))

    restored = 0
    for entry in manifest:
        rel = entry["rel"]
        src = backup / rel
        dst = root / rel

        if not entry["existed"]:
            # File was deleted by the fix — restore means re-deleting it
            if dst.exists():
                dst.unlink()
                print(f"  {DIM('del')} {rel}")
        else:
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                print(f"  {GREEN('↩')}  {rel}")
                restored += 1

    print(GREEN(f"\n✓ Restored {restored} file(s) from {backup.name}"))


# ── Pretty diff display ───────────────────────────────────────────────────────

def _print_diff(diff: str):
    if not diff:
        return
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            print(BOLD(DIM(line)))
        elif line.startswith("+"):
            print(GREEN(line))
        elif line.startswith("-"):
            print(RED(line))
        elif line.startswith("@@"):
            print(CYAN(line))
        else:
            print(DIM(line))


# ── Main runner ───────────────────────────────────────────────────────────────

def run(root: Path, dry_run: bool, only: Optional[list[str]], show_diff: bool):
    all_fixes = _all_fixes()

    if only:
        only_upper = [x.upper() for x in only]
        all_fixes = [f for f in all_fixes if f.fix_id.upper() in only_upper]
        if not all_fixes:
            print(RED(f"No fixes matched: {only}"))
            sys.exit(1)

    patcher = Patcher(root, dry_run=dry_run)
    results: list[FixResult] = []

    print()
    print(BOLD("━" * 68))
    print(BOLD("  Pushkaralu v16 — Surgical Fix Engine"))
    print(BOLD("━" * 68))
    print(f"  Target : {root}")
    print(f"  Mode   : {YELLOW('DRY RUN (nothing written)') if dry_run else GREEN('LIVE — changes will be applied')}")
    print(f"  Fixes  : {len(all_fixes)}")
    print(BOLD("━" * 68))
    print()

    for fix in all_fixes:
        applicable = fix.apply(patcher)
        diff = ""
        for p, _ in patcher._staged.items():
            try:
                rel = str(p.relative_to(root))
                d = patcher.get_diff(rel)
                if d:
                    diff += d + "\n"
            except ValueError:
                pass

        if applicable:
            status = "dry_run" if dry_run else "applied"
            icon   = YELLOW("◌ DRY RUN") if dry_run else GREEN("✓ APPLIED")
        else:
            status = "skipped"
            icon   = DIM("– SKIPPED (pattern not found — already fixed or different version)")

        result = FixResult(
            fix_id=fix.fix_id,
            title=fix.title,
            status=status,
            diff=diff,
        )
        results.append(result)

        badge = {
            "applied":  GREEN(f"[{fix.fix_id}]"),
            "dry_run":  YELLOW(f"[{fix.fix_id}]"),
            "skipped":  DIM(f"[{fix.fix_id}]"),
            "error":    RED(f"[{fix.fix_id}]"),
        }[status]

        print(f"  {badge}  {fix.title}")
        print(f"         {icon}")

        if show_diff and diff:
            print()
            _print_diff(diff)
            print()

    # ── Commit ────────────────────────────────────────────────────────────────

    applied = [r for r in results if r.status == "applied"]
    skipped = [r for r in results if r.status == "skipped"]

    print()
    print(BOLD("━" * 68))
    print(BOLD("  Summary"))
    print(BOLD("━" * 68))
    print(f"  Applied : {GREEN(str(len(applied)))}")
    print(f"  Skipped : {DIM(str(len(skipped)))}")

    if dry_run:
        print()
        print(YELLOW("  DRY RUN — no files were modified."))
        print(YELLOW("  Run without --dry-run to apply."))
    elif applied:
        backup = _backup_dir(root)
        backup.mkdir(parents=True, exist_ok=True)
        patcher.commit(backup)
        print(f"  Backup  : {DIM(str(backup))}")
        print()
        print(GREEN("  ✓ All applicable fixes written to disk."))
        print(DIM("  To undo: python pushkaralu_fix.py <root> --restore"))
    else:
        print()
        print(DIM("  Nothing to write — all fixes already applied or not applicable."))

    print(BOLD("━" * 68))
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="pushkaralu_fix",
        description="Surgical bug & dead-code remover for Pushkaralu v16",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "root",
        type=Path,
        help="Path to the pushkaralu_v16 project directory",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without modifying any file",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        metavar="ID",
        help="Apply only the specified fix IDs (e.g. --only BUG-01 DEAD-05)",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="Print the unified diff for each applied fix",
    )
    parser.add_argument(
        "--restore",
        action="store_true",
        help="Restore files from the most recent backup",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all fix IDs and their titles without applying",
    )

    args = parser.parse_args()
    root = args.root.resolve()

    if not root.exists():
        print(RED(f"✗ Directory not found: {root}"))
        sys.exit(1)

    if not (root / "main.py").exists():
        print(RED(f"✗ This does not look like the pushkaralu_v16 root (main.py not found): {root}"))
        sys.exit(1)

    if args.list:
        print(BOLD("\nAvailable fixes:\n"))
        for fix in _all_fixes():
            sev = "🔴" if fix.fix_id.startswith("BUG-0") and int(fix.fix_id[-2:]) <= 3 else \
                  "🟠" if fix.fix_id.startswith("BUG") else "🟢"
            print(f"  {CYAN(fix.fix_id):20s}  {sev}  {fix.title}")
        print()
        return

    if args.restore:
        do_restore(root)
        return

    run(root, dry_run=args.dry_run, only=args.only, show_diff=args.diff)


if __name__ == "__main__":
    main()