# ═══════════════════════════════════════════════════════════════════════════════
# Godavari Pushkaralu 2027 — AI Stack Dump Predictor  (v1)
#
# Tracks RSS memory, thread count, and event-loop latency.
# Emits Pre-Stack-Dump Warning when:
#   - Event loop blocks > 50 ms
#   - RSS memory spikes > MEMORY_SPIKE_THRESHOLD_MB above baseline
# ═══════════════════════════════════════════════════════════════════════════════

import asyncio
import logging
import os
import time
import threading

logger = logging.getLogger("pushkaralu.ai_predictor")

MONITOR_INTERVAL_S          = float(os.getenv("PREDICTOR_INTERVAL_S", "5"))
LOOP_LAG_WARN_MS            = float(os.getenv("PREDICTOR_LOOP_LAG_MS", "50"))
MEMORY_SPIKE_THRESHOLD_MB   = float(os.getenv("PREDICTOR_MEM_SPIKE_MB", "200"))
THREAD_COUNT_WARN_THRESHOLD = int(os.getenv("PREDICTOR_THREAD_WARN", "80"))

try:
    import importlib.util as _ilu
    _PSUTIL_AVAILABLE = _ilu.find_spec("psutil") is not None
except Exception:
    _PSUTIL_AVAILABLE = False
if not _PSUTIL_AVAILABLE:
    logger.warning("[Predictor] psutil not installed — memory/thread metrics disabled")

_baseline_rss_mb: float = 0.0
_proc = None


def _get_process():
    global _proc
    if _PSUTIL_AVAILABLE and _proc is None:
        import psutil
        _proc = psutil.Process()
    return _proc


def _rss_mb() -> float:
    proc = _get_process()
    if proc is None:
        return 0.0
    try:
        return proc.memory_info().rss / (1024 * 1024)
    except Exception:
        return 0.0


def _thread_count() -> int:
    if not _PSUTIL_AVAILABLE:
        return threading.active_count()
    proc = _get_process()
    if proc is None:
        return threading.active_count()
    try:
        return proc.num_threads()
    except Exception:
        return threading.active_count()


async def _measure_loop_lag_ms() -> float:
    """
    Schedule a no-op callback via call_soon and measure how long
    the event loop took to execute it. A high value indicates the loop
    is blocked by a slow coroutine or sync call.
    """
    # FIX (A3): asyncio.get_event_loop() is deprecated inside an async
    # function (DeprecationWarning on 3.10+, raises on 3.12+).
    # We are guaranteed to have a running loop here, so use get_running_loop().
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    t0 = time.perf_counter()
    loop.call_soon(future.set_result, None)
    await future
    return (time.perf_counter() - t0) * 1000


async def _monitor_loop():
    global _baseline_rss_mb

    # Establish RSS baseline on first run
    await asyncio.sleep(2)
    _baseline_rss_mb = _rss_mb()
    logger.info(
        "[Predictor] Started  baseline_rss=%.1f MB  interval=%.0fs",
        _baseline_rss_mb, MONITOR_INTERVAL_S,
    )

    while True:
        await asyncio.sleep(MONITOR_INTERVAL_S)
        try:
            rss_mb       = _rss_mb()
            threads      = _thread_count()
            loop_lag_ms  = await _measure_loop_lag_ms()
            rss_delta_mb = rss_mb - _baseline_rss_mb

            # ── Anomaly detection ─────────────────────────────────────────────
            triggered = False

            if loop_lag_ms > LOOP_LAG_WARN_MS:
                logger.warning(
                    "[Predictor] Pre-Stack-Dump Warning — EVENT LOOP LAG  "
                    "lag=%.1f ms  threshold=%.0f ms  rss=%.1f MB  threads=%d",
                    loop_lag_ms, LOOP_LAG_WARN_MS, rss_mb, threads,
                )
                triggered = True

            if rss_delta_mb > MEMORY_SPIKE_THRESHOLD_MB:
                logger.warning(
                    "[Predictor] Pre-Stack-Dump Warning — MEMORY SPIKE  "
                    "rss=%.1f MB  delta=+%.1f MB  threshold=%.0f MB  threads=%d",
                    rss_mb, rss_delta_mb, MEMORY_SPIKE_THRESHOLD_MB, threads,
                )
                triggered = True

            if threads > THREAD_COUNT_WARN_THRESHOLD:
                logger.warning(
                    "[Predictor] Pre-Stack-Dump Warning — THREAD COUNT HIGH  "
                    "threads=%d  threshold=%d  rss=%.1f MB  lag=%.1f ms",
                    threads, THREAD_COUNT_WARN_THRESHOLD, rss_mb, loop_lag_ms,
                )
                triggered = True

            if not triggered:
                logger.debug(
                    "[Predictor] OK  rss=%.1f MB (+%.1f)  threads=%d  lag=%.2f ms",
                    rss_mb, rss_delta_mb, threads, loop_lag_ms,
                )

            # Drift baseline slowly to avoid false alarms after legitimate growth
            _baseline_rss_mb = _baseline_rss_mb * 0.99 + rss_mb * 0.01

        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.debug("[Predictor] Monitor error: %s", exc)


async def collect_telemetry() -> dict:
    """
    On-demand telemetry snapshot. Exposed via /metrics or /health extended.
    """
    rss_mb      = _rss_mb()
    threads     = _thread_count()
    loop_lag_ms = await _measure_loop_lag_ms()
    return {
        "rss_mb":           round(rss_mb, 2),
        "rss_delta_mb":     round(rss_mb - _baseline_rss_mb, 2),
        "thread_count":     threads,
        "loop_lag_ms":      round(loop_lag_ms, 3),
        "psutil_available": _PSUTIL_AVAILABLE,
    }


def start_monitor(app=None):
    """
    Launch the background monitor task.
    Called from FastAPI lifespan startup. `app` param accepted but unused
    (kept for forward compatibility with dependency injection patterns).
    """
    asyncio.create_task(_monitor_loop(), name="ai-stack-predictor")
    logger.info("[Predictor] Background monitor task scheduled")
