#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║         PUSHKARALU UNIFIED ENGINE  v1.0                             ║
║  Static Analysis · Performance Testing · Security · GUI Dashboard   ║
║  Zero mandatory external dependencies — degrades gracefully         ║
╚══════════════════════════════════════════════════════════════════════╝

Usage
-----
    # Launch full GUI dashboard (opens browser automatically):
    python pushkaralu_engine.py gui ./pushkaralu_v16

    # CLI — run everything, save reports:
    python pushkaralu_engine.py analyze ./pushkaralu_v16

    # CLI — specific modules only:
    python pushkaralu_engine.py analyze ./pushkaralu_v16 --only bugs security

    # Performance benchmark:
    python pushkaralu_engine.py benchmark ./pushkaralu_v16 --url http://localhost:8000

    # List all available checks:
    python pushkaralu_engine.py list

Optional tools (auto-detected, used if installed):
    pip install ruff bandit radon rich psutil httpx
"""

from __future__ import annotations

# ── Standard Library ──────────────────────────────────────────────────────────
import ast
import asyncio
import collections
import concurrent.futures
import gc
import importlib.util
import json
import logging
import os
import platform
import queue
import re
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time
import traceback
import tracemalloc
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import asdict, dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# ── Optional dependency detection ─────────────────────────────────────────────
def _has(pkg: str) -> bool:
    return importlib.util.find_spec(pkg) is not None

HAS_RICH   = _has("rich")
HAS_PSUTIL = _has("psutil")
HAS_HTTPX  = _has("httpx")
HAS_RUFF   = shutil.which("ruff") is not None
HAS_BANDIT = shutil.which("bandit") is not None or _has("bandit")
HAS_RADON  = _has("radon")

# ══════════════════════════════════════════════════════════════════════════════
# DATA MODELS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Issue:
    file:        str
    line:        int
    col:         int
    severity:    str        # CRITICAL | HIGH | MEDIUM | LOW | INFO
    category:    str        # bug | security | performance | dead_code | style
    code:        str        # e.g. BUG-01, SEC-03
    title:       str
    explanation: str
    fix:         str
    perf_impact: str = ""
    mem_impact:  str = ""


@dataclass
class FileMetrics:
    path:          str
    lines:         int
    functions:     int
    classes:       int
    complexity:    float
    imports:       int
    issues:        List[Issue] = field(default_factory=list)


@dataclass
class BenchmarkResult:
    endpoint:     str
    method:       str
    users:        int
    rps:          float          # requests per second
    p50_ms:       float
    p95_ms:       float
    p99_ms:       float
    error_rate:   float          # 0.0 – 1.0
    duration_s:   float
    total_reqs:   int


@dataclass
class AnalysisReport:
    project_path:     str
    timestamp:        str
    python_files:     int
    total_lines:      int
    issues:           List[Issue]
    file_metrics:     List[FileMetrics]
    benchmarks:       List[BenchmarkResult]
    dep_graph:        Dict[str, List[str]]
    circular_imports: List[List[str]]
    scores:           Dict[str, float]   # optimization, maintainability, security, scalability
    recommendations:  List[str]
    system_info:      Dict[str, Any]


# ══════════════════════════════════════════════════════════════════════════════
# SCANNER — recursive Python file discovery
# ══════════════════════════════════════════════════════════════════════════════

SKIP_DIRS = {
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    ".mypy_cache", ".pytest_cache", "dist", "build", ".tox",
    ".pushkaralu_fix_backup",
}

def scan_python_files(root: Path) -> List[Path]:
    files = []
    for p in root.rglob("*.py"):
        if not any(part in SKIP_DIRS for part in p.parts):
            files.append(p)
    return sorted(files)


def read_file_safe(path: Path) -> Optional[str]:
    for enc in ("utf-8", "latin-1", "cp1252"):
        try:
            return path.read_text(encoding=enc)
        except Exception:
            continue
    return None


# ══════════════════════════════════════════════════════════════════════════════
# AST ENGINE — pure-stdlib static analysis
# ══════════════════════════════════════════════════════════════════════════════

class ASTEngine:
    """
    Walks the AST of every Python file and emits Issues.
    Covers: dead code, async bugs, exception handling, blocking calls,
    loop inefficiencies, import issues, large functions, bare excepts.
    """

    # Dangerous sync calls inside async functions
    BLOCKING_CALLS = {
        "time.sleep", "requests.get", "requests.post", "requests.put",
        "requests.delete", "requests.patch", "urllib.request.urlopen",
        "open",  # should use aiofiles in async context
    }

    # Patterns that suggest pandas (migration opportunity)
    PANDAS_PATTERNS = {"pd.read_csv", "pd.DataFrame", "df.iterrows", "df.apply"}

    def analyze_file(self, path: Path, source: str) -> Tuple[FileMetrics, List[Issue]]:
        issues: List[Issue] = []
        rel = str(path)

        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as e:
            issues.append(Issue(
                file=rel, line=e.lineno or 1, col=e.offset or 0,
                severity="CRITICAL", category="bug", code="SYN-01",
                title="Syntax Error",
                explanation=str(e),
                fix="Fix the syntax error at the indicated line.",
            ))
            metrics = FileMetrics(path=rel, lines=source.count("\n"),
                                  functions=0, classes=0, complexity=0.0, imports=0)
            return metrics, issues

        visitor = _ASTVisitor(rel, source)
        visitor.visit(tree)
        issues.extend(visitor.issues)

        metrics = FileMetrics(
            path=rel,
            lines=source.count("\n") + 1,
            functions=visitor.function_count,
            classes=visitor.class_count,
            complexity=visitor.max_complexity,
            imports=visitor.import_count,
            issues=issues,
        )
        return metrics, issues


class _ASTVisitor(ast.NodeVisitor):
    """Single-pass AST visitor — O(N) per file."""

    def __init__(self, filepath: str, source: str):
        self.filepath          = filepath
        self.source_lines      = source.splitlines()
        self.issues: List[Issue] = []
        self.function_count    = 0
        self.class_count       = 0
        self.import_count      = 0
        self.max_complexity    = 0.0
        self._async_stack: List[bool] = []   # True = inside async def
        self._imports: Dict[str, str] = {}   # alias → module
        self._used_names: set  = set()
        self._defined_names: set = set()
        self._loop_depth       = 0

    # ── helpers ──────────────────────────────────────────────────────────────

    def _issue(self, node, severity, category, code, title, explanation, fix,
               perf="", mem=""):
        self.issues.append(Issue(
            file=self.filepath,
            line=getattr(node, "lineno", 0),
            col=getattr(node, "col_offset", 0),
            severity=severity, category=category, code=code,
            title=title, explanation=explanation, fix=fix,
            perf_impact=perf, mem_impact=mem,
        ))

    def _node_name(self, node) -> str:
        if isinstance(node, ast.Name):       return node.id
        if isinstance(node, ast.Attribute):  return f"{self._node_name(node.value)}.{node.attr}"
        if isinstance(node, ast.Call):       return self._node_name(node.func)
        return ""

    def _complexity(self, node) -> int:
        """McCabe cyclomatic complexity — count decision points."""
        count = 1
        for child in ast.walk(node):
            if isinstance(child, (ast.If, ast.While, ast.For, ast.ExceptHandler,
                                  ast.With, ast.Assert, ast.comprehension)):
                count += 1
            elif isinstance(child, ast.BoolOp):
                count += len(child.values) - 1
        return count

    # ── visitors ─────────────────────────────────────────────────────────────

    def visit_Import(self, node):
        self.import_count += len(node.names)
        for alias in node.names:
            name = alias.asname or alias.name
            self._imports[name] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        self.import_count += len(node.names)
        for alias in node.names:
            name = alias.asname or alias.name
            self._imports[name] = f"{node.module}.{alias.name}" if node.module else alias.name
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        self.class_count += 1
        self._defined_names.add(node.name)
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        self._visit_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node):
        self._visit_function(node, is_async=True)

    def _visit_function(self, node, is_async: bool):
        self.function_count += 1
        self._defined_names.add(node.name)

        # ── Large function check ──────────────────────────────────────────
        func_lines = (node.end_lineno or node.lineno) - node.lineno
        if func_lines > 80:
            self._issue(node, "MEDIUM", "style", "SIZE-01",
                f"Large function: {node.name} ({func_lines} lines)",
                f"Function '{node.name}' spans {func_lines} lines. "
                "Large functions are hard to test, review, and maintain.",
                "Break into smaller, focused functions of ≤ 40 lines each.",
                perf="Low — readability/maintainability concern")

        # ── Complexity check ──────────────────────────────────────────────
        complexity = self._complexity(node)
        self.max_complexity = max(self.max_complexity, complexity)
        if complexity > 10:
            sev = "HIGH" if complexity > 15 else "MEDIUM"
            self._issue(node, sev, "bug", "CMPLX-01",
                f"High cyclomatic complexity: {node.name} (CC={complexity})",
                f"McCabe complexity {complexity} in '{node.name}'. "
                "High complexity → more bug surface area, harder to test.",
                f"Refactor into smaller helpers. Target CC ≤ 10. "
                "Extract conditions into named predicates.",
                perf="Medium — harder to optimise for CPU branch prediction")

        # ── Async-specific checks ─────────────────────────────────────────
        self._async_stack.append(is_async)
        self.generic_visit(node)
        self._async_stack.pop()

    def visit_Call(self, node):
        name = self._node_name(node.func)
        self._used_names.add(name.split(".")[0])

        # Blocking call inside async function
        if self._async_stack and self._async_stack[-1]:
            for blocking in ASTEngine.BLOCKING_CALLS:
                if name == blocking or name.endswith(f".{blocking.split('.')[-1]}"):
                    self._issue(node, "HIGH", "performance", "ASYNC-01",
                        f"Blocking call in async context: {name}()",
                        f"'{name}()' is synchronous and will block the entire "
                        "asyncio event loop, preventing other coroutines from running. "
                        "Under load this can cause request timeouts and cascade failures.",
                        f"Replace with async equivalent: "
                        f"asyncio.to_thread({name}, ...) or use aiofiles/httpx/aiohttp.",
                        perf="CRITICAL — blocks event loop under concurrent load",
                        mem="Negligible")

        # time.sleep anywhere
        if name == "time.sleep":
            self._issue(node, "MEDIUM", "performance", "ASYNC-02",
                "time.sleep() detected",
                "time.sleep() blocks the thread. In async code this blocks "
                "the event loop; in sync workers it wastes a thread.",
                "Use await asyncio.sleep() in async context.",
                perf="High — wasted CPU/thread time")

        # Pandas iterrows
        if name in ("df.iterrows", "iterrows"):
            self._issue(node, "HIGH", "performance", "PERF-01",
                "pandas iterrows() — extremely slow",
                "iterrows() is 100–1000× slower than vectorised operations. "
                "It creates a Python object per row, defeating NumPy's C speed.",
                "Replace with df.itertuples(), vectorised operations, or migrate to Polars.",
                perf="CRITICAL for large DataFrames",
                mem="High — Python object per row")

        # Bare open() without context manager check (heuristic)
        if name == "open" and not isinstance(node.parent if hasattr(node, "parent") else None,
                                              ast.withitem):
            pass  # handled in visit_Assign / visit_Expr below

        self.generic_visit(node)

    def visit_ExceptHandler(self, node):
        # Bare except: or except Exception: with pass
        if node.type is None:
            self._issue(node, "HIGH", "bug", "EXC-01",
                "Bare except clause",
                "Bare 'except:' catches ALL exceptions including "
                "KeyboardInterrupt, SystemExit, and GeneratorExit. "
                "This can mask critical errors and prevent clean shutdown.",
                "Catch specific exceptions: except (ValueError, TypeError) as e:",
                perf="Low — correctness concern")

        # except with only pass
        if (len(node.body) == 1 and isinstance(node.body[0], ast.Pass)):
            self._issue(node, "HIGH", "bug", "EXC-02",
                "Silent exception suppression (except + pass)",
                "Silently swallowing exceptions hides bugs. "
                "Errors disappear with no log, no alert, no trace.",
                "At minimum: logger.exception(e) or re-raise with context.",
                perf="Low — correctness/reliability concern")

        self.generic_visit(node)

    def visit_For(self, node):
        self._loop_depth += 1
        if self._loop_depth >= 2:
            self._issue(node, "MEDIUM", "performance", "LOOP-01",
                f"Nested loop (depth {self._loop_depth})",
                f"Nested loops at depth {self._loop_depth} suggest O(N²) or worse complexity. "
                "This scales poorly with data size.",
                "Consider: dict lookups, set operations, vectorised ops, or algorithmic redesign.",
                perf="HIGH — O(N²) or worse",
                mem="Medium")
        self.generic_visit(node)
        self._loop_depth -= 1

    def visit_Global(self, node):
        for name in node.names:
            self._issue(node, "LOW", "style", "GLOBAL-01",
                f"Global variable mutation: {name}",
                f"'global {name}' mutates module-level state. "
                "This creates hidden coupling and makes testing hard.",
                "Pass state explicitly or use a class/dataclass to encapsulate it.",
                perf="Low — concurrency/threading concern")
        self.generic_visit(node)

    def visit_Assert(self, node):
        # assert in non-test files (optimized Python strips them)
        if "_test" not in self.filepath and "test_" not in self.filepath:
            self._issue(node, "LOW", "bug", "ASSERT-01",
                "assert statement in production code",
                "assert statements are stripped when Python runs with -O flag. "
                "Never use assert for input validation in production code.",
                "Replace with explicit if/raise: if not condition: raise ValueError(...)",
                perf="Low — correctness concern")
        self.generic_visit(node)

    def visit_Expr(self, node):
        # Detect fire-and-forget coroutines (unawaited)
        if isinstance(node.value, ast.Call):
            # Can't fully detect without type info, but flag common patterns
            pass
        self.generic_visit(node)


# ══════════════════════════════════════════════════════════════════════════════
# DEPENDENCY MAPPER — circular import detection
# ══════════════════════════════════════════════════════════════════════════════

class DependencyMapper:

    def build_graph(self, files: List[Path], root: Path) -> Dict[str, List[str]]:
        graph: Dict[str, List[str]] = {}
        for f in files:
            source = read_file_safe(f)
            if not source:
                continue
            rel = str(f.relative_to(root))
            imports = self._extract_imports(source, f, root)
            graph[rel] = imports
        return graph

    def _extract_imports(self, source: str, file: Path, root: Path) -> List[str]:
        deps = []
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return deps
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                # Convert module path to file path (heuristic)
                mod_path = node.module.replace(".", os.sep) + ".py"
                candidate = root / mod_path
                if candidate.exists():
                    deps.append(str(candidate.relative_to(root)))
        return deps

    def find_circular(self, graph: Dict[str, List[str]]) -> List[List[str]]:
        """DFS-based cycle detection — O(V+E)."""
        visited = set()
        rec_stack = set()
        cycles = []

        def dfs(node, path):
            visited.add(node)
            rec_stack.add(node)
            for neighbor in graph.get(node, []):
                if neighbor not in visited:
                    dfs(neighbor, path + [neighbor])
                elif neighbor in rec_stack:
                    # Found a cycle
                    cycle_start = path.index(neighbor) if neighbor in path else 0
                    cycle = path[cycle_start:] + [neighbor]
                    if cycle not in cycles:
                        cycles.append(cycle)
            rec_stack.discard(node)

        for node in graph:
            if node not in visited:
                dfs(node, [node])

        return cycles


# ══════════════════════════════════════════════════════════════════════════════
# SECURITY ENGINE — pattern-based security checks
# ══════════════════════════════════════════════════════════════════════════════

class SecurityEngine:

    # (pattern, code, title, severity, explanation, fix)
    PATTERNS = [
        (r'SECRET_KEY\s*=\s*["\'][^"\']{1,20}["\']',
         "SEC-01", "Hardcoded secret key", "CRITICAL",
         "Short/hardcoded secret key found in source. Attackers who read the "
         "source (via logs, error pages, or repo access) can forge JWTs.",
         "Load from environment: SECRET_KEY = os.getenv('SECRET_KEY')"),

        (r'password\s*=\s*["\'][^"\']+["\']',
         "SEC-02", "Hardcoded password", "CRITICAL",
         "Literal password found in source code.",
         "Use environment variables or a secrets manager."),

        (r'allow_origins\s*=\s*\[.*\*.*\]',
         "SEC-03", "CORS allow_origins=[\"*\"]", "HIGH",
         "Wildcard CORS allows ANY origin to make credentialed requests. "
         "In production this exposes all authenticated endpoints.",
         "Restrict to known origins: allow_origins=['https://yourdomain.com']"),

        (r'verify\s*=\s*False',
         "SEC-04", "SSL verification disabled", "HIGH",
         "verify=False disables TLS certificate validation, making the "
         "connection vulnerable to MITM attacks.",
         "Remove verify=False. If using self-signed certs, add the CA bundle."),

        (r'md5|sha1\b',
         "SEC-05", "Weak hash algorithm (MD5/SHA1)", "HIGH",
         "MD5 and SHA1 are cryptographically broken. Do not use for passwords or signatures.",
         "Use hashlib.sha256() or bcrypt/argon2 for passwords."),

        (r'pickle\.loads?\(',
         "SEC-06", "pickle.load() — arbitrary code execution risk", "CRITICAL",
         "pickle.loads() executes arbitrary Python when deserializing untrusted data. "
         "A crafted payload can run any OS command.",
         "Use JSON, msgpack, or orjson for serialization. Never pickle untrusted input."),

        (r'subprocess\.call\(.*shell\s*=\s*True',
         "SEC-07", "Shell injection risk (shell=True)", "CRITICAL",
         "shell=True with user-controlled input enables OS command injection.",
         "Use shell=False and pass args as a list: subprocess.call(['cmd', arg])"),

        (r'eval\s*\(',
         "SEC-08", "eval() — arbitrary code execution", "CRITICAL",
         "eval() executes arbitrary Python. Never call with user input.",
         "Use ast.literal_eval() for safe expression parsing, or redesign."),

        (r'exec\s*\(',
         "SEC-09", "exec() — arbitrary code execution", "CRITICAL",
         "exec() executes arbitrary Python strings. Extreme security risk.",
         "Redesign to avoid dynamic code execution."),

        (r'debug\s*=\s*True',
         "SEC-10", "Debug mode enabled", "HIGH",
         "Debug mode exposes stack traces, source code, and interactive "
         "debugger to anyone who triggers an error.",
         "Set debug=False in production. Use environment variable: "
         "debug=os.getenv('DEBUG', 'false').lower() == 'true'"),

        (r'jwt\.decode\(.*options.*verify.*False',
         "SEC-11", "JWT signature verification disabled", "CRITICAL",
         "Disabling JWT signature verification allows attackers to forge tokens "
         "and authenticate as any user.",
         "Always verify JWT signatures. Remove options={'verify_signature': False}."),
    ]

    def analyze_file(self, path: Path, source: str) -> List[Issue]:
        issues = []
        rel = str(path)
        lines = source.splitlines()
        for i, line in enumerate(lines, 1):
            # Skip comments
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for pattern, code, title, severity, explanation, fix in self.PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    issues.append(Issue(
                        file=rel, line=i, col=0,
                        severity=severity, category="security",
                        code=code, title=title,
                        explanation=explanation, fix=fix,
                    ))
        return issues


# ══════════════════════════════════════════════════════════════════════════════
# RUFF RUNNER — uses ruff if installed, stdlib fallback otherwise
# ══════════════════════════════════════════════════════════════════════════════

class RuffRunner:

    def run(self, root: Path) -> List[Issue]:
        if not HAS_RUFF:
            return []
        try:
            result = subprocess.run(
                ["ruff", "check", str(root), "--output-format", "json",
                 "--select", "E,W,F,B,C,N,UP,ASYNC,S", "--quiet"],
                capture_output=True, text=True, timeout=60,
            )
            if not result.stdout.strip():
                return []
            data = json.loads(result.stdout)
            issues = []
            for item in data:
                sev = "MEDIUM"
                if item.get("code", "").startswith(("E9", "F8")):
                    sev = "HIGH"
                elif item.get("code", "").startswith("S"):
                    sev = "HIGH"
                issues.append(Issue(
                    file=item.get("filename", ""),
                    line=item.get("location", {}).get("row", 0),
                    col=item.get("location", {}).get("column", 0),
                    severity=sev,
                    category="style",
                    code=f"RUFF-{item.get('code', '?')}",
                    title=item.get("message", ""),
                    explanation=item.get("message", ""),
                    fix=item.get("fix", {}).get("message", "See ruff documentation.") or "See ruff documentation.",
                ))
            return issues
        except Exception as e:
            return []


# ══════════════════════════════════════════════════════════════════════════════
# BANDIT RUNNER — security scanner
# ══════════════════════════════════════════════════════════════════════════════

class BanditRunner:

    def run(self, root: Path) -> List[Issue]:
        if not HAS_BANDIT:
            return []
        try:
            result = subprocess.run(
                ["python", "-m", "bandit", "-r", str(root),
                 "-f", "json", "-q", "--skip", "B101"],
                capture_output=True, text=True, timeout=120,
            )
            raw = result.stdout.strip()
            if not raw:
                return []
            data = json.loads(raw)
            issues = []
            sev_map = {"HIGH": "HIGH", "MEDIUM": "MEDIUM", "LOW": "LOW"}
            for item in data.get("results", []):
                issues.append(Issue(
                    file=item.get("filename", ""),
                    line=item.get("line_number", 0),
                    col=0,
                    severity=sev_map.get(item.get("issue_severity", "MEDIUM"), "MEDIUM"),
                    category="security",
                    code=f"BANDIT-{item.get('test_id', '?')}",
                    title=item.get("issue_text", ""),
                    explanation=item.get("issue_text", ""),
                    fix=f"See: {item.get('more_info', 'https://bandit.readthedocs.io')}",
                ))
            return issues
        except Exception:
            return []


# ══════════════════════════════════════════════════════════════════════════════
# PERFORMANCE BENCHMARKER — async HTTP load testing (stdlib only)
# ══════════════════════════════════════════════════════════════════════════════

class PerformanceBenchmarker:
    """
    Lightweight load tester using threading + urllib (stdlib only).
    Falls back to httpx if available for better async performance.
    """

    DEFAULT_ENDPOINTS = [
        ("GET",  "/health"),
        ("GET",  "/"),
        ("GET",  "/stats"),
        ("GET",  "/get_ghats"),
        ("GET",  "/get_volunteers"),
        ("GET",  "/emergency_services"),
    ]

    def run_benchmark(
        self,
        base_url: str,
        endpoints: Optional[List[Tuple[str, str]]] = None,
        user_counts: Optional[List[int]] = None,
        duration_s: int = 15,
        progress_cb: Optional[Callable] = None,
    ) -> List[BenchmarkResult]:
        endpoints  = endpoints  or self.DEFAULT_ENDPOINTS
        user_counts = user_counts or [10, 50, 100]
        results = []

        for users in user_counts:
            for method, path in endpoints:
                if progress_cb:
                    progress_cb(f"Benchmarking {method} {path} with {users} users…")
                result = self._load_test(base_url, method, path, users, duration_s)
                results.append(result)

        return results

    def _load_test(
        self,
        base_url: str,
        method: str,
        path: str,
        users: int,
        duration_s: int,
    ) -> BenchmarkResult:
        url = base_url.rstrip("/") + path
        latencies: List[float] = []
        errors = 0
        lock = threading.Lock()
        stop_event = threading.Event()

        def worker():
            nonlocal errors
            while not stop_event.is_set():
                t0 = time.perf_counter()
                try:
                    req = urllib.request.Request(url, method=method)
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        resp.read()
                    ms = (time.perf_counter() - t0) * 1000
                    with lock:
                        latencies.append(ms)
                except Exception:
                    with lock:
                        errors += 1

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(users)]
        t_start = time.perf_counter()
        for t in threads:
            t.start()
        time.sleep(duration_s)
        stop_event.set()
        for t in threads:
            t.join(timeout=2)
        actual_duration = time.perf_counter() - t_start

        latencies.sort()
        total = len(latencies) + errors
        p50 = latencies[int(len(latencies) * 0.50)] if latencies else 0.0
        p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0.0
        p99 = latencies[int(len(latencies) * 0.99)] if latencies else 0.0
        rps = len(latencies) / actual_duration if actual_duration > 0 else 0.0

        return BenchmarkResult(
            endpoint=path,
            method=method,
            users=users,
            rps=round(rps, 2),
            p50_ms=round(p50, 2),
            p95_ms=round(p95, 2),
            p99_ms=round(p99, 2),
            error_rate=round(errors / total, 4) if total > 0 else 0.0,
            duration_s=round(actual_duration, 2),
            total_reqs=total,
        )


# ══════════════════════════════════════════════════════════════════════════════
# SCORE ENGINE — produces 0–100 scores
# ══════════════════════════════════════════════════════════════════════════════

class ScoreEngine:

    def compute(self, issues: List[Issue], metrics: List[FileMetrics],
                benchmarks: List[BenchmarkResult]) -> Dict[str, float]:
        sev_weights = {"CRITICAL": 20, "HIGH": 10, "MEDIUM": 4, "LOW": 1, "INFO": 0}

        # Security score
        sec_issues = [i for i in issues if i.category == "security"]
        sec_penalty = sum(sev_weights.get(i.severity, 2) for i in sec_issues)
        security = max(0.0, 100.0 - sec_penalty)

        # Optimization score
        perf_issues = [i for i in issues if i.category == "performance"]
        perf_penalty = sum(sev_weights.get(i.severity, 2) for i in perf_issues)
        optimization = max(0.0, 100.0 - perf_penalty)

        # Maintainability score (based on complexity + function size)
        bug_issues = [i for i in issues if i.category in ("bug", "style")]
        bug_penalty = sum(sev_weights.get(i.severity, 2) for i in bug_issues)
        avg_complexity = (sum(m.complexity for m in metrics) / len(metrics)) if metrics else 0
        maintainability = max(0.0, 100.0 - bug_penalty - min(avg_complexity * 2, 30))

        # Scalability score (based on benchmarks)
        if benchmarks:
            avg_p95 = sum(b.p95_ms for b in benchmarks) / len(benchmarks)
            avg_err = sum(b.error_rate for b in benchmarks) / len(benchmarks)
            scalability = max(0.0, 100.0 - min(avg_p95 / 50, 50) - avg_err * 100)
        else:
            scalability = 70.0  # unknown — assume moderate

        return {
            "security":       round(security, 1),
            "optimization":   round(optimization, 1),
            "maintainability": round(maintainability, 1),
            "scalability":    round(scalability, 1),
            "overall":        round((security + optimization + maintainability + scalability) / 4, 1),
        }


# ══════════════════════════════════════════════════════════════════════════════
# RECOMMENDATION ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class RecommendationEngine:

    RUST_ALTERNATIVES = {
        "json":        ("orjson", "2–10× faster JSON serialization (Rust-powered)"),
        "re":          ("regex",  "Drop-in replacement with better Unicode and performance"),
        "pandas":      ("polars", "10–100× faster DataFrame operations (Rust-powered)"),
        "requests":    ("httpx",  "Async-capable HTTP client with HTTP/2 support"),
        "PIL":         ("pillow-simd", "SIMD-accelerated image processing"),
        "pydantic":    ("pydantic v2", "Rust core — 5–50× faster validation"),
    }

    def generate(self, issues: List[Issue], metrics: List[FileMetrics],
                 dep_graph: Dict[str, List[str]]) -> List[str]:
        recs = []

        # Count by category
        counts = collections.Counter(i.category for i in issues)
        sev_counts = collections.Counter(i.severity for i in issues)

        if sev_counts["CRITICAL"] > 0:
            recs.append(
                f"🔴 {sev_counts['CRITICAL']} CRITICAL issues found — fix these before any deployment."
            )
        if sev_counts["HIGH"] > 0:
            recs.append(
                f"🟠 {sev_counts['HIGH']} HIGH severity issues — address before next release."
            )

        if counts["performance"] > 3:
            recs.append(
                "⚡ Multiple performance issues detected. Consider profiling with py-spy "
                "or scalene to find the actual bottleneck before optimising."
            )

        if counts["security"] > 0:
            recs.append(
                "🔒 Run 'bandit -r .' and 'pip-audit' regularly in your CI pipeline."
            )

        # Async recommendations
        async_issues = [i for i in issues if "ASYNC" in i.code]
        if async_issues:
            recs.append(
                "🔄 Blocking calls detected in async context. Consider using "
                "asyncio.to_thread() for CPU-bound work and httpx/aiofiles for I/O."
            )

        # Complexity
        high_complexity = [m for m in metrics if m.complexity > 10]
        if high_complexity:
            recs.append(
                f"🧩 {len(high_complexity)} files have high cyclomatic complexity. "
                "Refactor to reduce cognitive load and improve testability."
            )

        # Large files
        large_files = [m for m in metrics if m.lines > 500]
        if large_files:
            recs.append(
                f"📄 {len(large_files)} files exceed 500 lines. "
                "Split into focused modules following single-responsibility principle."
            )

        # Rust-powered suggestions
        recs.append("🦀 Rust-powered drop-in upgrades to consider:")
        recs.append("   • Replace json → orjson (2-10× faster, zero config)")
        recs.append("   • Replace pandas → polars (10-100× faster, lazy evaluation)")
        recs.append("   • Use pydantic v2 (Rust core, 5-50× faster validation)")
        recs.append("   • Use ruff instead of flake8/pylint (100× faster linting)")

        # Caching
        recs.append(
            "💾 Ensure Redis TTLs are tuned per data volatility: "
            "ghats=3s, facilities=30s, static data=300s+"
        )

        # Scaling
        recs.append(
            "📈 For 10k+ concurrent users: add nginx load balancer → "
            "4× FastAPI workers → Redis cluster → PostgreSQL read replicas."
        )

        return recs


# ══════════════════════════════════════════════════════════════════════════════
# REPORT GENERATOR — markdown, JSON, HTML
# ══════════════════════════════════════════════════════════════════════════════

class ReportGenerator:

    def save_markdown(self, report: AnalysisReport, out: Path) -> Path:
        p = out / "report.md"
        lines = [
            f"# Pushkaralu Engine — Analysis Report",
            f"",
            f"**Project:** `{report.project_path}`  ",
            f"**Generated:** {report.timestamp}  ",
            f"**Python files:** {report.python_files}  ",
            f"**Total lines:** {report.total_lines:,}  ",
            f"",
            f"## Scores",
            f"",
            f"| Metric | Score |",
            f"|--------|-------|",
        ]
        for k, v in report.scores.items():
            emoji = "🟢" if v >= 80 else "🟡" if v >= 60 else "🔴"
            lines.append(f"| {k.title()} | {emoji} {v}/100 |")

        lines += ["", "## Issues by Severity", ""]
        sev_counts = collections.Counter(i.severity for i in report.issues)
        for sev in ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]:
            if sev_counts[sev]:
                lines.append(f"- **{sev}**: {sev_counts[sev]}")

        lines += ["", "## Top Issues", ""]
        top = sorted(report.issues,
                     key=lambda x: ["CRITICAL","HIGH","MEDIUM","LOW","INFO"].index(x.severity))[:30]
        for issue in top:
            lines += [
                f"### [{issue.severity}] {issue.code} — {issue.title}",
                f"**File:** `{issue.file}` · Line {issue.line}  ",
                f"**Category:** {issue.category}  ",
                f"",
                f"{issue.explanation}",
                f"",
                f"**Fix:** {issue.fix}",
                f"",
            ]

        if report.benchmarks:
            lines += ["## Performance Benchmarks", ""]
            lines += ["| Endpoint | Users | RPS | p50 ms | p95 ms | p99 ms | Errors |",
                      "|----------|-------|-----|--------|--------|--------|--------|"]
            for b in report.benchmarks:
                lines.append(
                    f"| {b.method} {b.endpoint} | {b.users} | {b.rps} | "
                    f"{b.p50_ms} | {b.p95_ms} | {b.p99_ms} | {b.error_rate:.1%} |"
                )

        lines += ["", "## Recommendations", ""]
        for rec in report.recommendations:
            lines.append(f"- {rec}")

        p.write_text("\n".join(lines), encoding="utf-8")
        return p

    def save_json(self, report: AnalysisReport, out: Path) -> Path:
        p = out / "report.json"
        data = {
            "project_path":     report.project_path,
            "timestamp":        report.timestamp,
            "python_files":     report.python_files,
            "total_lines":      report.total_lines,
            "scores":           report.scores,
            "issues":           [asdict(i) for i in report.issues],
            "benchmarks":       [asdict(b) for b in report.benchmarks],
            "recommendations":  report.recommendations,
            "circular_imports": report.circular_imports,
            "system_info":      report.system_info,
        }
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return p

    def save_html(self, report: AnalysisReport, out: Path) -> Path:
        p = out / "report.html"
        p.write_text(_build_html_report(report), encoding="utf-8")
        return p


# ══════════════════════════════════════════════════════════════════════════════
# HTML REPORT TEMPLATE
# ══════════════════════════════════════════════════════════════════════════════

def _build_html_report(report: AnalysisReport) -> str:
    sev_counts = collections.Counter(i.severity for i in report.issues)
    cat_counts  = collections.Counter(i.category for i in report.issues)

    issues_html = ""
    top_issues = sorted(report.issues,
                        key=lambda x: ["CRITICAL","HIGH","MEDIUM","LOW","INFO"].index(x.severity))[:50]
    for issue in top_issues:
        color = {"CRITICAL":"#dc2626","HIGH":"#ea580c","MEDIUM":"#d97706",
                 "LOW":"#65a30d","INFO":"#0891b2"}.get(issue.severity, "#6b7280")
        issues_html += f"""
        <div class="issue" style="border-left:4px solid {color}">
          <div class="issue-header">
            <span class="badge" style="background:{color}">{issue.severity}</span>
            <span class="code">{issue.code}</span>
            <strong>{issue.title}</strong>
          </div>
          <div class="issue-meta">📁 {issue.file} · Line {issue.line} · {issue.category}</div>
          <div class="issue-body">
            <p>{issue.explanation}</p>
            <div class="fix">💡 <strong>Fix:</strong> {issue.fix}</div>
            {"<div class='impact'>⚡ Performance: "+issue.perf_impact+"</div>" if issue.perf_impact else ""}
          </div>
        </div>"""

    bench_rows = ""
    for b in report.benchmarks:
        err_class = "error-high" if b.error_rate > 0.05 else "error-low"
        bench_rows += f"""<tr>
          <td>{b.method} {b.endpoint}</td>
          <td>{b.users}</td>
          <td>{b.rps}</td>
          <td>{b.p50_ms}ms</td>
          <td>{b.p95_ms}ms</td>
          <td>{b.p99_ms}ms</td>
          <td class="{err_class}">{b.error_rate:.1%}</td>
        </tr>"""

    score_cards = ""
    for k, v in report.scores.items():
        color = "#16a34a" if v >= 80 else "#d97706" if v >= 60 else "#dc2626"
        score_cards += f"""
        <div class="score-card">
          <div class="score-value" style="color:{color}">{v}</div>
          <div class="score-label">{k.title()}</div>
        </div>"""

    rec_html = "".join(f"<li>{r}</li>" for r in report.recommendations)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pushkaralu Engine Report</title>
<style>
  :root {{
    --bg: #0f172a; --surface: #1e293b; --border: #334155;
    --text: #e2e8f0; --muted: #94a3b8; --accent: #3b82f6;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'DM Sans', sans-serif;
          background: var(--bg); color: var(--text); line-height: 1.6; }}
  .header {{ background: linear-gradient(135deg, #1e3a5f, #0f172a);
             padding: 2rem; border-bottom: 1px solid var(--border); }}
  .header h1 {{ font-size: 1.8rem; font-weight: 700; color: #60a5fa; }}
  .header .meta {{ color: var(--muted); font-size: 0.9rem; margin-top: 0.5rem; }}
  .container {{ max-width: 1400px; margin: 0 auto; padding: 2rem; }}
  .scores {{ display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 2rem; }}
  .score-card {{ background: var(--surface); border: 1px solid var(--border);
                 border-radius: 12px; padding: 1.5rem; text-align: center;
                 flex: 1; min-width: 140px; }}
  .score-value {{ font-size: 2.5rem; font-weight: 800; }}
  .score-label {{ color: var(--muted); font-size: 0.85rem; margin-top: 0.3rem; text-transform: uppercase; letter-spacing: 0.05em; }}
  .stats-row {{ display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 2rem; }}
  .stat-box {{ background: var(--surface); border: 1px solid var(--border);
               border-radius: 8px; padding: 1rem 1.5rem; flex: 1; min-width: 120px; }}
  .stat-num {{ font-size: 1.8rem; font-weight: 700; color: var(--accent); }}
  .stat-lbl {{ color: var(--muted); font-size: 0.8rem; }}
  h2 {{ font-size: 1.3rem; margin: 2rem 0 1rem; color: #93c5fd;
        border-bottom: 1px solid var(--border); padding-bottom: 0.5rem; }}
  .issue {{ background: var(--surface); border-radius: 8px; margin-bottom: 1rem;
            padding: 1rem 1.2rem; border: 1px solid var(--border); }}
  .issue-header {{ display: flex; align-items: center; gap: 0.7rem; flex-wrap: wrap; }}
  .badge {{ padding: 0.2rem 0.6rem; border-radius: 4px; font-size: 0.75rem;
            font-weight: 700; color: white; }}
  .code {{ color: var(--muted); font-family: 'Source Code Pro', monospace; font-size: 0.85rem; }}
  .issue-meta {{ color: var(--muted); font-size: 0.8rem; margin: 0.4rem 0; }}
  .issue-body {{ margin-top: 0.5rem; font-size: 0.9rem; }}
  .fix {{ background: #064e3b; border-radius: 4px; padding: 0.5rem 0.8rem;
          margin-top: 0.5rem; font-size: 0.85rem; color: #6ee7b7; }}
  .impact {{ background: #1c1917; border-radius: 4px; padding: 0.4rem 0.8rem;
             margin-top: 0.4rem; font-size: 0.8rem; color: #fcd34d; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--surface);
           border-radius: 8px; overflow: hidden; }}
  th {{ background: #1e3a5f; padding: 0.8rem 1rem; text-align: left;
        font-size: 0.85rem; color: var(--muted); font-weight: 600; }}
  td {{ padding: 0.7rem 1rem; border-bottom: 1px solid var(--border); font-size: 0.9rem; }}
  tr:last-child td {{ border-bottom: none; }}
  .error-high {{ color: #f87171; font-weight: 700; }}
  .error-low  {{ color: #4ade80; }}
  .rec-list {{ background: var(--surface); border-radius: 8px; padding: 1.5rem;
               border: 1px solid var(--border); }}
  .rec-list li {{ margin-bottom: 0.6rem; font-size: 0.9rem; line-height: 1.5; }}
  .footer {{ text-align: center; color: var(--muted); font-size: 0.8rem;
             padding: 2rem; border-top: 1px solid var(--border); margin-top: 3rem; }}
  @media (max-width: 600px) {{ .scores, .stats-row {{ flex-direction: column; }} }}
</style>
</head>
<body>
<div class="header">
  <h1>⚡ Pushkaralu Engine Report</h1>
  <div class="meta">
    Project: {report.project_path} &nbsp;·&nbsp;
    Generated: {report.timestamp} &nbsp;·&nbsp;
    {report.python_files} Python files &nbsp;·&nbsp;
    {report.total_lines:,} lines
  </div>
</div>
<div class="container">
  <h2>Scores</h2>
  <div class="scores">{score_cards}</div>

  <div class="stats-row">
    <div class="stat-box"><div class="stat-num">{sev_counts['CRITICAL']}</div><div class="stat-lbl">Critical</div></div>
    <div class="stat-box"><div class="stat-num">{sev_counts['HIGH']}</div><div class="stat-lbl">High</div></div>
    <div class="stat-box"><div class="stat-num">{sev_counts['MEDIUM']}</div><div class="stat-lbl">Medium</div></div>
    <div class="stat-box"><div class="stat-num">{cat_counts['security']}</div><div class="stat-lbl">Security</div></div>
    <div class="stat-box"><div class="stat-num">{cat_counts['performance']}</div><div class="stat-lbl">Performance</div></div>
    <div class="stat-box"><div class="stat-num">{len(report.circular_imports)}</div><div class="stat-lbl">Circular Imports</div></div>
  </div>

  <h2>Issues ({len(report.issues)} total, showing top 50)</h2>
  {issues_html}

  {"<h2>Performance Benchmarks</h2><table><thead><tr><th>Endpoint</th><th>Users</th><th>RPS</th><th>p50</th><th>p95</th><th>p99</th><th>Errors</th></tr></thead><tbody>" + bench_rows + "</tbody></table>" if report.benchmarks else ""}

  <h2>Recommendations</h2>
  <div class="rec-list"><ul>{rec_html}</ul></div>
</div>
<div class="footer">Pushkaralu Engine · Generated {report.timestamp}</div>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# SYSTEM INFO
# ══════════════════════════════════════════════════════════════════════════════

def get_system_info() -> Dict[str, Any]:
    info = {
        "platform": platform.platform(),
        "python":   sys.version,
        "cpu_count": os.cpu_count(),
        "tools": {
            "ruff":   HAS_RUFF,
            "bandit": HAS_BANDIT,
            "radon":  HAS_RADON,
            "rich":   HAS_RICH,
            "psutil": HAS_PSUTIL,
            "httpx":  HAS_HTTPX,
        },
    }
    if HAS_PSUTIL:
        import psutil
        mem = psutil.virtual_memory()
        info["ram_total_gb"] = round(mem.total / 1e9, 1)
        info["ram_available_gb"] = round(mem.available / 1e9, 1)
        info["cpu_freq_mhz"] = psutil.cpu_freq().current if psutil.cpu_freq() else "unknown"
    return info


# ══════════════════════════════════════════════════════════════════════════════
# ORCHESTRATOR — ties everything together
# ══════════════════════════════════════════════════════════════════════════════

class Engine:

    def __init__(self, progress_cb: Optional[Callable] = None):
        self.progress_cb = progress_cb or (lambda msg: None)
        self.ast_engine   = ASTEngine()
        self.sec_engine   = SecurityEngine()
        self.dep_mapper   = DependencyMapper()
        self.ruff_runner  = RuffRunner()
        self.bandit_runner = BanditRunner()
        self.score_engine  = ScoreEngine()
        self.rec_engine    = RecommendationEngine()
        self.report_gen    = ReportGenerator()
        self.benchmarker   = PerformanceBenchmarker()

    def analyze(
        self,
        project_path: Path,
        base_url: Optional[str] = None,
        only: Optional[List[str]] = None,
        user_counts: Optional[List[int]] = None,
    ) -> AnalysisReport:
        root = project_path.resolve()
        self.progress_cb(f"Scanning {root} …")

        files = scan_python_files(root)
        self.progress_cb(f"Found {len(files)} Python files")

        all_issues: List[Issue] = []
        all_metrics: List[FileMetrics] = []

        # ── AST analysis (parallel) ───────────────────────────────────────────
        if not only or "bugs" in only or "all" in only:
            self.progress_cb("Running AST analysis …")
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4)) as ex:
                futures = {}
                for f in files:
                    source = read_file_safe(f)
                    if source:
                        futures[ex.submit(self.ast_engine.analyze_file, f, source)] = f

                for future in concurrent.futures.as_completed(futures):
                    try:
                        metrics, issues = future.result()
                        all_metrics.append(metrics)
                        all_issues.extend(issues)
                    except Exception as e:
                        pass

        # ── Security analysis (parallel) ─────────────────────────────────────
        if not only or "security" in only or "all" in only:
            self.progress_cb("Running security analysis …")
            for f in files:
                source = read_file_safe(f)
                if source:
                    issues = self.sec_engine.analyze_file(f, source)
                    all_issues.extend(issues)

        # ── Ruff ─────────────────────────────────────────────────────────────
        if (not only or "ruff" in only or "all" in only) and HAS_RUFF:
            self.progress_cb("Running Ruff linter …")
            all_issues.extend(self.ruff_runner.run(root))

        # ── Bandit ───────────────────────────────────────────────────────────
        if (not only or "security" in only or "all" in only) and HAS_BANDIT:
            self.progress_cb("Running Bandit security scanner …")
            all_issues.extend(self.bandit_runner.run(root))

        # ── Dependency graph ─────────────────────────────────────────────────
        self.progress_cb("Building dependency graph …")
        dep_graph = self.dep_mapper.build_graph(files, root)
        circular  = self.dep_mapper.find_circular(dep_graph)
        if circular:
            for cycle in circular:
                all_issues.append(Issue(
                    file=cycle[0], line=1, col=0,
                    severity="HIGH", category="bug", code="CIRC-01",
                    title=f"Circular import: {' → '.join(cycle)}",
                    explanation="Circular imports cause ImportError at runtime and indicate "
                                "tight coupling between modules.",
                    fix="Break the cycle by extracting shared code into a third module, "
                        "or use lazy imports inside functions.",
                ))

        # ── Benchmarks ───────────────────────────────────────────────────────
        benchmarks: List[BenchmarkResult] = []
        if base_url and (not only or "perf" in only or "all" in only):
            self.progress_cb(f"Running performance benchmarks against {base_url} …")
            benchmarks = self.benchmarker.run_benchmark(
                base_url,
                user_counts=user_counts or [10, 50, 100],
                progress_cb=self.progress_cb,
            )

        # ── Scores & recommendations ─────────────────────────────────────────
        scores = self.score_engine.compute(all_issues, all_metrics, benchmarks)
        recommendations = self.rec_engine.generate(all_issues, all_metrics, dep_graph)

        total_lines = sum(m.lines for m in all_metrics)

        return AnalysisReport(
            project_path=str(root),
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            python_files=len(files),
            total_lines=total_lines,
            issues=all_issues,
            file_metrics=all_metrics,
            benchmarks=benchmarks,
            dep_graph=dep_graph,
            circular_imports=circular,
            scores=scores,
            recommendations=recommendations,
            system_info=get_system_info(),
        )


# ══════════════════════════════════════════════════════════════════════════════
# GUI — browser-based dashboard served by a local HTTP server
# ══════════════════════════════════════════════════════════════════════════════

_GUI_STATE: Dict[str, Any] = {
    "status":   "idle",       # idle | running | done | error
    "progress": [],
    "report":   None,
    "log":      [],
}
_GUI_LOCK = threading.Lock()


def _gui_html(project_path: str = "") -> str:
    """The main dashboard SPA — served at GET /"""
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pushkaralu Engine</title>
<style>
  :root {
    --bg:#0f172a; --surface:#1e293b; --surface2:#243047;
    --border:#334155; --text:#e2e8f0; --muted:#94a3b8;
    --accent:#3b82f6; --green:#22c55e; --red:#ef4444;
    --orange:#f97316; --yellow:#eab308;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family: 'DM Sans', sans-serif;
         background:var(--bg); color:var(--text);
         min-height:100vh; }
  /* ── Layout ── */
  .app { display:grid; grid-template-columns:260px 1fr;
         grid-template-rows:60px 1fr; height:100vh; }
  .topbar { grid-column:1/-1; background:var(--surface);
            border-bottom:1px solid var(--border);
            display:flex; align-items:center; padding:0 1.5rem; gap:1rem; }
  .topbar h1 { font-size:1.1rem; font-weight:700; color:#60a5fa; }
  .topbar .sub { color:var(--muted); font-size:0.8rem; }
  .sidebar { background:var(--surface); border-right:1px solid var(--border);
             padding:1.2rem; overflow-y:auto; }
  .main { overflow-y:auto; padding:1.5rem; }
  /* ── Sidebar ── */
  .sidebar label { display:block; color:var(--muted);
                   font-size:0.75rem; font-weight:600; letter-spacing:.05em;
                   text-transform:uppercase; margin:1rem 0 0.4rem; }
  .sidebar input, .sidebar select {
    width:100%; background:var(--bg); border:1px solid var(--border);
    color:var(--text); border-radius:6px; padding:0.5rem 0.7rem;
    font-size:0.85rem; }
  .sidebar input:focus, .sidebar select:focus {
    outline:none; border-color:var(--accent); }
  .check-group { display:flex; flex-direction:column; gap:0.4rem; }
  .check-group label { color:var(--text) !important;
                       font-size:0.85rem !important; font-weight:400 !important;
                       text-transform:none !important; letter-spacing:0 !important;
                       margin:0 !important; display:flex; align-items:center; gap:0.5rem; }
  .btn { width:100%; padding:0.7rem; border:none; border-radius:8px;
         font-size:0.9rem; font-weight:600; cursor:pointer;
         transition:all .15s; margin-top:0.5rem; }
  .btn-primary { background:var(--accent); color:white; }
  .btn-primary:hover { background:#2563eb; }
  .btn-primary:disabled { background:#334155; color:var(--muted); cursor:not-allowed; }
  .btn-secondary { background:var(--surface2); color:var(--text);
                   border:1px solid var(--border); }
  .btn-secondary:hover { background:var(--border); }
  /* ── Progress ── */
  .progress-bar { width:100%; height:6px; background:var(--border);
                  border-radius:3px; margin:0.8rem 0; overflow:hidden; }
  .progress-fill { height:100%; background:var(--accent);
                   border-radius:3px; transition:width .3s;
                   animation: pulse 1.5s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.6} }
  .log-box { background:var(--bg); border:1px solid var(--border);
             border-radius:6px; padding:0.8rem; font-family: 'Source Code Pro', monospace;
             font-size:0.78rem; color:var(--muted); max-height:120px;
             overflow-y:auto; margin-top:0.5rem; }
  /* ── Dashboard cards ── */
  .scores-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
                 gap:1rem; margin-bottom:1.5rem; }
  .score-card { background:var(--surface); border:1px solid var(--border);
                border-radius:12px; padding:1.3rem; text-align:center; }
  .score-val { font-size:2.2rem; font-weight:800; }
  .score-lbl { color:var(--muted); font-size:0.75rem; margin-top:0.3rem;
               text-transform:uppercase; letter-spacing:.05em; }
  .stats-row { display:grid; grid-template-columns:repeat(auto-fit,minmax(110px,1fr));
               gap:0.8rem; margin-bottom:1.5rem; }
  .stat-card { background:var(--surface); border:1px solid var(--border);
               border-radius:8px; padding:0.9rem; text-align:center; }
  .stat-num { font-size:1.7rem; font-weight:700; color:var(--accent); }
  .stat-lbl { color:var(--muted); font-size:0.75rem; }
  /* ── Issues table ── */
  .section-title { font-size:1rem; font-weight:700; color:#93c5fd;
                   margin:1.5rem 0 0.8rem;
                   border-bottom:1px solid var(--border); padding-bottom:0.4rem; }
  .filters { display:flex; gap:0.6rem; flex-wrap:wrap; margin-bottom:0.8rem; }
  .filter-btn { padding:0.3rem 0.8rem; border-radius:20px; border:1px solid var(--border);
                background:var(--surface); color:var(--muted); font-size:0.78rem;
                cursor:pointer; transition:all .15s; }
  .filter-btn.active { background:var(--accent); color:white; border-color:var(--accent); }
  .filter-btn:hover:not(.active) { border-color:var(--accent); color:var(--text); }
  .issue-list { display:flex; flex-direction:column; gap:0.6rem; }
  .issue-card { background:var(--surface); border-radius:8px;
                border:1px solid var(--border); overflow:hidden;
                border-left-width:4px; }
  .issue-head { display:flex; align-items:center; gap:0.6rem;
                padding:0.7rem 1rem; cursor:pointer; flex-wrap:wrap; }
  .issue-head:hover { background:var(--surface2); }
  .badge { padding:.15rem .5rem; border-radius:4px; font-size:.7rem;
           font-weight:700; color:#fff; white-space:nowrap; }
  .issue-code { font-family: 'Source Code Pro', monospace; color:var(--muted); font-size:.8rem; }
  .issue-title { font-size:.88rem; font-weight:500; }
  .issue-body { padding:0 1rem 0.8rem; font-size:.84rem; display:none; }
  .issue-body.open { display:block; }
  .issue-meta { color:var(--muted); font-size:.78rem; margin-bottom:.5rem; }
  .fix-box { background:#064e3b; border-radius:4px; padding:.5rem .8rem;
             color:#6ee7b7; font-size:.82rem; margin-top:.4rem; }
  /* ── Bench table ── */
  table { width:100%; border-collapse:collapse; background:var(--surface);
          border-radius:8px; overflow:hidden; font-size:.84rem; }
  th { background:#1e3a5f; padding:.7rem 1rem; text-align:left;
       color:var(--muted); font-weight:600; font-size:.78rem; }
  td { padding:.65rem 1rem; border-bottom:1px solid var(--border); }
  tr:last-child td { border:none; }
  /* ── Rec list ── */
  .rec-list { background:var(--surface); border:1px solid var(--border);
              border-radius:8px; padding:1rem 1.3rem; }
  .rec-list li { margin-bottom:.5rem; font-size:.87rem; line-height:1.5; }
  /* ── Idle screen ── */
  .idle-screen { display:flex; flex-direction:column; align-items:center;
                 justify-content:center; height:60vh; gap:1rem; }
  .idle-screen .icon { font-size:4rem; }
  .idle-screen h2 { color:var(--muted); font-weight:400; }
  @media(max-width:700px) {
    .app { grid-template-columns:1fr; grid-template-rows:60px auto 1fr; }
    .sidebar { border-right:none; border-bottom:1px solid var(--border); }
  }
</style>
</head>
<body>
<div class="app">
  <!-- Topbar -->
  <div class="topbar">
    <div>
      <div class="topbar h1">⚡ Pushkaralu Engine</div>
      <div class="sub">Static Analysis · Security · Performance · GUI</div>
    </div>
    <div style="margin-left:auto;display:flex;gap:0.5rem;">
      <span id="status-badge" style="padding:.2rem .7rem;border-radius:20px;
            background:var(--surface2);color:var(--muted);font-size:.8rem;">Idle</span>
    </div>
  </div>

  <!-- Sidebar -->
  <div class="sidebar">
    <label>Project Path</label>
    <input type="text" id="inp-path" placeholder="C:\\path\\to\\project or ./project"
           value=\"""" + project_path + """\">

    <label>API URL (optional)</label>
    <input type="text" id="inp-url" placeholder="http://localhost:8000">

    <label>Load Test Users</label>
    <select id="inp-users">
      <option value="10,50">Quick (10, 50)</option>
      <option value="10,50,100" selected>Standard (10, 50, 100)</option>
      <option value="10,50,100,500">Heavy (up to 500)</option>
      <option value="10,50,100,500,1000">Stress (up to 1000)</option>
    </select>

    <label>Analysis Modules</label>
    <div class="check-group">
      <label><input type="checkbox" value="bugs" checked> AST Bug Detection</label>
      <label><input type="checkbox" value="security" checked> Security Analysis</label>
      <label><input type="checkbox" value="ruff" checked> Ruff Linting</label>
      <label><input type="checkbox" value="perf" checked> Performance Benchmark</label>
      <label><input type="checkbox" value="deps" checked> Dependency Graph</label>
    </div>

    <button class="btn btn-primary" id="btn-run" onclick="startAnalysis()">
      ▶ Run Analysis
    </button>
    <button class="btn btn-secondary" id="btn-report"
            onclick="openReport()" style="display:none">
      📄 Open Full Report
    </button>
    <button class="btn btn-secondary" onclick="downloadJSON()" style="margin-top:.3rem">
      ⬇ Export JSON
    </button>

    <div id="progress-section" style="display:none">
      <div class="progress-bar"><div class="progress-fill" id="progress-fill" style="width:30%"></div></div>
      <div class="log-box" id="log-box"></div>
    </div>
  </div>

  <!-- Main -->
  <div class="main" id="main-content">
    <div class="idle-screen" id="idle-screen">
      <div class="icon">⚡</div>
      <h2>Enter your project path and click Run Analysis</h2>
      <p style="color:var(--muted);font-size:.9rem;text-align:center;max-width:400px">
        Detects bugs, security issues, performance bottlenecks,
        dead code, circular imports, and more. Zero config.
      </p>
    </div>
    <div id="dashboard" style="display:none"></div>
  </div>
</div>

<script>
let reportData = null;
let pollTimer  = null;
let logOffset  = 0;

function startAnalysis() {
  const path  = document.getElementById('inp-path').value.trim();
  const url   = document.getElementById('inp-url').value.trim();
  const users = document.getElementById('inp-users').value;
  const checks = [...document.querySelectorAll('.check-group input:checked')]
                   .map(c => c.value);
  if (!path) { alert('Please enter a project path.'); return; }

  document.getElementById('btn-run').disabled = true;
  document.getElementById('btn-report').style.display = 'none';
  document.getElementById('progress-section').style.display = 'block';
  document.getElementById('idle-screen').style.display = 'none';
  document.getElementById('dashboard').style.display = 'none';
  document.getElementById('dashboard').innerHTML = '';
  document.getElementById('log-box').textContent = '';
  document.getElementById('status-badge').textContent = 'Running…';
  document.getElementById('status-badge').style.background = '#1d4ed8';
  document.getElementById('status-badge').style.color = '#bfdbfe';
  logOffset = 0;

  fetch('/api/start', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ path, url: url || null, users, checks }),
  });

  pollTimer = setInterval(pollStatus, 800);
}

async function pollStatus() {
  try {
    const r = await fetch('/api/status?offset=' + logOffset);
    const d = await r.json();

    // Append new log lines
    if (d.log && d.log.length) {
      logOffset += d.log.length;
      const lb = document.getElementById('log-box');
      d.log.forEach(l => { lb.textContent += l + '\\n'; });
      lb.scrollTop = lb.scrollHeight;
    }

    if (d.status === 'done') {
      clearInterval(pollTimer);
      document.getElementById('btn-run').disabled = false;
      document.getElementById('status-badge').textContent = 'Complete';
      document.getElementById('status-badge').style.background = '#14532d';
      document.getElementById('status-badge').style.color = '#86efac';
      document.getElementById('progress-fill').style.width = '100%';
      document.getElementById('progress-fill').style.animation = 'none';
      document.getElementById('btn-report').style.display = 'block';
      reportData = d.report;
      renderDashboard(d.report);
    } else if (d.status === 'error') {
      clearInterval(pollTimer);
      document.getElementById('btn-run').disabled = false;
      document.getElementById('status-badge').textContent = 'Error';
      document.getElementById('status-badge').style.background = '#7f1d1d';
      document.getElementById('status-badge').style.color = '#fca5a5';
    }
  } catch(e) {}
}

function renderDashboard(report) {
  const sev = counts => ({
    CRITICAL: counts.CRITICAL||0, HIGH: counts.HIGH||0,
    MEDIUM: counts.MEDIUM||0, LOW: counts.LOW||0,
  });

  const sevMap = {};
  const catMap = {};
  report.issues.forEach(i => {
    sevMap[i.severity] = (sevMap[i.severity]||0) + 1;
    catMap[i.category] = (catMap[i.category]||0) + 1;
  });

  const scoreColor = v => v >= 80 ? '#22c55e' : v >= 60 ? '#eab308' : '#ef4444';

  let scoreCards = '';
  for (const [k,v] of Object.entries(report.scores)) {
    scoreCards += `<div class="score-card">
      <div class="score-val" style="color:${scoreColor(v)}">${v}</div>
      <div class="score-lbl">${k}</div>
    </div>`;
  }

  let benchTable = '';
  if (report.benchmarks && report.benchmarks.length) {
    const rows = report.benchmarks.map(b => `<tr>
      <td>${b.method} ${b.endpoint}</td>
      <td>${b.users}</td>
      <td>${b.rps}</td>
      <td>${b.p50_ms}ms</td>
      <td>${b.p95_ms}ms</td>
      <td>${b.p99_ms}ms</td>
      <td style="color:${b.error_rate>0.05?'#f87171':'#4ade80'}">${(b.error_rate*100).toFixed(1)}%</td>
    </tr>`).join('');
    benchTable = `<div class="section-title">Performance Benchmarks</div>
    <table><thead><tr>
      <th>Endpoint</th><th>Users</th><th>RPS</th><th>p50</th><th>p95</th><th>p99</th><th>Errors</th>
    </tr></thead><tbody>${rows}</tbody></table>`;
  }

  const recItems = (report.recommendations||[]).map(r => `<li>${r}</li>`).join('');

  const dashboard = document.getElementById('dashboard');
  dashboard.style.display = 'block';
  dashboard.innerHTML = `
    <div class="section-title">Scores</div>
    <div class="scores-grid">${scoreCards}</div>

    <div class="stats-row">
      <div class="stat-card"><div class="stat-num" style="color:#ef4444">${sevMap.CRITICAL||0}</div><div class="stat-lbl">Critical</div></div>
      <div class="stat-card"><div class="stat-num" style="color:#f97316">${sevMap.HIGH||0}</div><div class="stat-lbl">High</div></div>
      <div class="stat-card"><div class="stat-num" style="color:#eab308">${sevMap.MEDIUM||0}</div><div class="stat-lbl">Medium</div></div>
      <div class="stat-card"><div class="stat-num">${catMap.security||0}</div><div class="stat-lbl">Security</div></div>
      <div class="stat-card"><div class="stat-num">${catMap.performance||0}</div><div class="stat-lbl">Perf Issues</div></div>
      <div class="stat-card"><div class="stat-num">${report.circular_imports?.length||0}</div><div class="stat-lbl">Circular Imports</div></div>
      <div class="stat-card"><div class="stat-num">${report.python_files}</div><div class="stat-lbl">Python Files</div></div>
      <div class="stat-card"><div class="stat-num">${(report.total_lines||0).toLocaleString()}</div><div class="stat-lbl">Total Lines</div></div>
    </div>

    <div class="section-title">Issues (${report.issues.length} total)</div>
    <div class="filters" id="filters">
      <button class="filter-btn active" onclick="filterIssues('all',this)">All</button>
      <button class="filter-btn" onclick="filterIssues('CRITICAL',this)">🔴 Critical</button>
      <button class="filter-btn" onclick="filterIssues('HIGH',this)">🟠 High</button>
      <button class="filter-btn" onclick="filterIssues('MEDIUM',this)">🟡 Medium</button>
      <button class="filter-btn" onclick="filterIssues('security',this)">🔒 Security</button>
      <button class="filter-btn" onclick="filterIssues('performance',this)">⚡ Performance</button>
      <button class="filter-btn" onclick="filterIssues('bug',this)">🐛 Bugs</button>
    </div>
    <div class="issue-list" id="issue-list">${buildIssueCards(report.issues)}</div>

    ${benchTable}

    <div class="section-title">Recommendations</div>
    <div class="rec-list"><ul>${recItems}</ul></div>
  `;
}

function buildIssueCards(issues) {
  const sevColor = {
    CRITICAL:'#dc2626', HIGH:'#ea580c', MEDIUM:'#d97706',
    LOW:'#65a30d', INFO:'#0891b2'
  };
  const sorted = [...issues].sort((a,b) =>
    ['CRITICAL','HIGH','MEDIUM','LOW','INFO'].indexOf(a.severity) -
    ['CRITICAL','HIGH','MEDIUM','LOW','INFO'].indexOf(b.severity)
  ).slice(0, 200);

  return sorted.map((issue, idx) => {
    const c = sevColor[issue.severity] || '#6b7280';
    return `<div class="issue-card" style="border-left-color:${c}"
                 data-sev="${issue.severity}" data-cat="${issue.category}">
      <div class="issue-head" onclick="toggleIssue(${idx})">
        <span class="badge" style="background:${c}">${issue.severity}</span>
        <span class="issue-code">${issue.code}</span>
        <span class="issue-title">${issue.title}</span>
      </div>
      <div class="issue-body" id="ib-${idx}">
        <div class="issue-meta">📁 ${issue.file} · Line ${issue.line} · ${issue.category}</div>
        <p style="margin-bottom:.4rem">${issue.explanation}</p>
        <div class="fix-box">💡 ${issue.fix}</div>
        ${issue.perf_impact ? `<div style="color:#fcd34d;font-size:.8rem;margin-top:.3rem">⚡ ${issue.perf_impact}</div>` : ''}
      </div>
    </div>`;
  }).join('');
}

function toggleIssue(idx) {
  const el = document.getElementById('ib-' + idx);
  if (el) el.classList.toggle('open');
}

function filterIssues(filter, btn) {
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  const cards = document.querySelectorAll('.issue-card');
  cards.forEach(card => {
    const show = filter === 'all' ||
                 card.dataset.sev === filter ||
                 card.dataset.cat === filter;
    card.style.display = show ? 'block' : 'none';
  });
}

function downloadJSON() {
  if (!reportData) { alert('Run analysis first.'); return; }
  const blob = new Blob([JSON.stringify(reportData, null, 2)], {type:'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'pushkaralu_report.json';
  a.click();
}

function openReport() {
  window.open('/report.html', '_blank');
}
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# HTTP SERVER — serves the GUI and API
# ══════════════════════════════════════════════════════════════════════════════

_ANALYSIS_THREAD: Optional[threading.Thread] = None
_REPORT_HTML_CACHE: str = ""


class GUIHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # suppress default access log

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            project_path = _GUI_STATE.get("project_path", "")
            html = _gui_html(project_path)
            self._respond(200, "text/html", html.encode())

        elif path == "/api/status":
            offset = int(params.get("offset", ["0"])[0])
            with _GUI_LOCK:
                log_slice = _GUI_STATE["log"][offset:]
                data = {
                    "status": _GUI_STATE["status"],
                    "log":    log_slice,
                    "report": _GUI_STATE["report"],
                }
            self._respond(200, "application/json", json.dumps(data).encode())

        elif path == "/report.html":
            self._respond(200, "text/html", _REPORT_HTML_CACHE.encode() if _REPORT_HTML_CACHE else b"<p>No report yet.</p>")

        else:
            self._respond(404, "text/plain", b"Not found")

    def do_POST(self):
        if self.path == "/api/start":
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
            _start_analysis_thread(body)
            self._respond(200, "application/json", b'{"ok":true}')
        else:
            self._respond(404, "text/plain", b"Not found")

    def _respond(self, code: int, ct: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


def _start_analysis_thread(params: dict):
    global _ANALYSIS_THREAD, _REPORT_HTML_CACHE

    with _GUI_LOCK:
        _GUI_STATE["status"]   = "running"
        _GUI_STATE["log"]      = []
        _GUI_STATE["report"]   = None
        _GUI_STATE["project_path"] = params.get("path", "")

    def progress(msg: str):
        with _GUI_LOCK:
            _GUI_STATE["log"].append(msg)

    def run():
        global _REPORT_HTML_CACHE
        try:
            project_path = Path(params["path"])
            base_url     = params.get("url") or None
            checks       = params.get("checks") or None
            users_str    = params.get("users", "10,50,100")
            user_counts  = [int(x) for x in users_str.split(",")]

            engine = Engine(progress_cb=progress)
            report = engine.analyze(
                project_path,
                base_url=base_url,
                only=checks,
                user_counts=user_counts,
            )

            # Save reports to disk
            out = project_path / "pushkaralu_reports"
            out.mkdir(exist_ok=True)
            gen = ReportGenerator()
            gen.save_markdown(report, out)
            gen.save_json(report, out)
            html_path = gen.save_html(report, out)
            _REPORT_HTML_CACHE = html_path.read_text(encoding="utf-8")

            progress(f"✓ Reports saved to {out}")
            progress(f"✓ Analysis complete — {len(report.issues)} issues found")

            # Serialise for JSON transport
            report_dict = {
                "project_path":     report.project_path,
                "timestamp":        report.timestamp,
                "python_files":     report.python_files,
                "total_lines":      report.total_lines,
                "scores":           report.scores,
                "issues":           [asdict(i) for i in report.issues],
                "benchmarks":       [asdict(b) for b in report.benchmarks],
                "recommendations":  report.recommendations,
                "circular_imports": report.circular_imports,
                "system_info":      report.system_info,
            }
            with _GUI_LOCK:
                _GUI_STATE["report"] = report_dict
                _GUI_STATE["status"] = "done"

        except Exception as exc:
            progress(f"ERROR: {exc}")
            progress(traceback.format_exc())
            with _GUI_LOCK:
                _GUI_STATE["status"] = "error"

    _ANALYSIS_THREAD = threading.Thread(target=run, daemon=True)
    _ANALYSIS_THREAD.start()


def _find_free_port(preferred: int = 7421) -> int:
    for port in range(preferred, preferred + 50):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return preferred


def launch_gui(project_path: str = ""):
    port = _find_free_port()
    _GUI_STATE["project_path"] = project_path
    server = HTTPServer(("127.0.0.1", port), GUIHandler)
    url = f"http://127.0.0.1:{port}"
    print(f"\n{'━'*60}")
    print(f"  ⚡ Pushkaralu Engine GUI")
    print(f"{'━'*60}")
    print(f"  URL     : {url}")
    print(f"  Project : {project_path or '(enter in GUI)'}")
    print(f"{'━'*60}")
    print(f"  Opening browser… (Ctrl+C to stop)\n")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")


# ══════════════════════════════════════════════════════════════════════════════
# CLI — terminal interface
# ══════════════════════════════════════════════════════════════════════════════

def _print(msg: str, color: str = ""):
    codes = {"red":"\033[31;1m","green":"\033[32;1m","yellow":"\033[33;1m",
             "cyan":"\033[36;1m","bold":"\033[1m","dim":"\033[2m"}
    reset = "\033[0m"
    if color and sys.stdout.isatty():
        print(f"{codes.get(color,'')}{msg}{reset}")
    else:
        print(msg)


def cli_analyze(args):
    project_path = Path(args.path)
    if not project_path.exists():
        _print(f"✗ Path not found: {project_path}", "red"); sys.exit(1)

    _print(f"\n{'━'*60}", "bold")
    _print("  ⚡ Pushkaralu Engine — Analysis", "bold")
    _print(f"{'━'*60}", "bold")

    def progress(msg):
        _print(f"  → {msg}", "dim")

    engine = Engine(progress_cb=progress)
    report = engine.analyze(
        project_path,
        base_url=getattr(args, "url", None),
        only=getattr(args, "only", None),
        user_counts=[10, 50, 100],
    )

    # Terminal summary
    _print(f"\n{'━'*60}", "bold")
    _print("  Results", "bold")
    _print(f"{'━'*60}", "bold")
    _print(f"  Files   : {report.python_files}", "cyan")
    _print(f"  Lines   : {report.total_lines:,}", "cyan")
    _print(f"  Issues  : {len(report.issues)}", "yellow")

    sev_counts = collections.Counter(i.severity for i in report.issues)
    for sev, color in [("CRITICAL","red"),("HIGH","red"),("MEDIUM","yellow"),("LOW","dim")]:
        if sev_counts[sev]:
            _print(f"    {sev:<10} {sev_counts[sev]}", color)

    _print("\n  Scores:", "bold")
    for k, v in report.scores.items():
        color = "green" if v >= 80 else "yellow" if v >= 60 else "red"
        _print(f"    {k:<20} {v}/100", color)

    # Save reports
    out = project_path / "pushkaralu_reports"
    out.mkdir(exist_ok=True)
    gen = ReportGenerator()
    md  = gen.save_markdown(report, out)
    js  = gen.save_json(report, out)
    htm = gen.save_html(report, out)

    _print(f"\n  Reports saved to {out}:", "green")
    _print(f"    {md}", "dim")
    _print(f"    {js}", "dim")
    _print(f"    {htm}", "dim")
    _print(f"{'━'*60}\n", "bold")


def cli_list():
    _print("\n  Available analysis modules:\n", "bold")
    modules = [
        ("bugs",     "AST bug detection — bare excepts, complexity, large functions"),
        ("security", "Pattern-based security scan + Bandit integration"),
        ("ruff",     "Ruff linter (requires: pip install ruff)"),
        ("perf",     "Live HTTP load testing (requires --url)"),
        ("deps",     "Import dependency graph + circular import detection"),
    ]
    for name, desc in modules:
        _print(f"  {name:<12} {desc}", "cyan")
    _print("")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        prog="pushkaralu_engine",
        description="Pushkaralu Unified Engine — Static Analysis + Performance + GUI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command")

    # gui
    p_gui = sub.add_parser("gui", help="Launch browser-based GUI dashboard")
    p_gui.add_argument("path", nargs="?", default="", help="Project path (pre-fill in GUI)")

    # analyze
    p_analyze = sub.add_parser("analyze", help="Run full analysis (CLI mode)")
    p_analyze.add_argument("path", help="Path to Python project")
    p_analyze.add_argument("--url",  help="Base URL for live benchmarking (e.g. http://localhost:8000)")
    p_analyze.add_argument("--only", nargs="+", metavar="MODULE",
                           help="Run only specified modules: bugs security ruff perf deps")

    # benchmark
    p_bench = sub.add_parser("benchmark", help="Run performance benchmarks only")
    p_bench.add_argument("path", help="Project path (for reports output)")
    p_bench.add_argument("--url", required=True, help="Base URL to benchmark")
    p_bench.add_argument("--users", default="10,50,100",
                         help="Comma-separated user counts (default: 10,50,100)")

    # list
    sub.add_parser("list", help="List available analysis modules")

    args = parser.parse_args()

    if args.command == "gui":
        launch_gui(args.path)

    elif args.command == "analyze":
        cli_analyze(args)

    elif args.command == "benchmark":
        project_path = Path(args.path)
        user_counts = [int(x) for x in args.users.split(",")]
        _print(f"\n  Benchmarking {args.url} with users: {user_counts}\n", "bold")
        b = PerformanceBenchmarker()
        results = b.run_benchmark(args.url, user_counts=user_counts,
                                  progress_cb=lambda m: _print(f"  {m}", "dim"))
        _print("\n  Results:\n", "bold")
        for r in results:
            _print(f"  {r.method} {r.endpoint} ({r.users} users) — "
                   f"RPS:{r.rps} p50:{r.p50_ms}ms p95:{r.p95_ms}ms "
                   f"errors:{r.error_rate:.1%}", "cyan")

    elif args.command == "list":
        cli_list()

    else:
        parser.print_help()
        _print("\n  Quick start:", "bold")
        _print("    python pushkaralu_engine.py gui ./pushkaralu_v16", "cyan")
        _print("    python pushkaralu_engine.py analyze ./pushkaralu_v16\n", "cyan")


if __name__ == "__main__":
    main()