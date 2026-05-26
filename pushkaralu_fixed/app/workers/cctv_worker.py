"""
Godavari Pushkaralu 2027 — CCTV Worker  (v6 — Throughput-optimized)

THROUGHPUT FIX vs v5:
  - cv2.VideoCapture / cap.read() / cv2.resize and YOLO inference were called
    SYNCHRONOUSLY inside async coroutines. With N cameras sharing a single
    event loop, every frame on every camera serialised behind the slowest
    one — past ~1 camera the per-camera FPS collapsed.
  - Both the OpenCV I/O path and the YOLO inference path are now offloaded
    to a bounded ThreadPoolExecutor via app.core.admission.run_blocking.
  - httpx.AsyncClient is now a singleton from app.core.http_client to keep a
    warm connection pool to the API instead of reconnecting per camera.
  - Camera failures now use exponential backoff — a dead camera no longer
    consumes an event loop slot at full FPS.
"""
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass

import httpx

# Allow `from app.core...` imports when run as `python -m app.workers.cctv_worker`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

logger = logging.getLogger("pushkaralu.cctv_worker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

API_BASE         = os.getenv("API_BASE", "http://localhost:8000")
INGEST_ENDPOINT  = f"{API_BASE}/crowd/ingest/cctv"
TARGET_FPS       = float(os.getenv("CCTV_FPS", "1.5"))
FRAME_MAX_PX     = int(os.getenv("FRAME_MAX_PX", "640"))
MODEL_NAME       = os.getenv("YOLO_MODEL", "yolov8n.pt")
CONFIDENCE       = float(os.getenv("YOLO_CONFIDENCE", "0.45"))
PERSON_CLASS_ID  = 0
DEFAULT_FRAME_AREA = float(os.getenv("DEFAULT_FRAME_AREA_SQM", "500.0"))

CAMERA_CONFIG = json.loads(os.getenv("CAMERA_CONFIG", json.dumps([
    {"camera_id": "cam-g01-01", "ghat_id": "g01", "source": os.getenv("CAM_G01_01", "mock"), "area_sqm": 500.0},
    {"camera_id": "cam-g01-02", "ghat_id": "g01", "source": os.getenv("CAM_G01_02", "mock"), "area_sqm": 450.0},
    {"camera_id": "cam-g02-01", "ghat_id": "g02", "source": os.getenv("CAM_G02_01", "mock"), "area_sqm": 600.0},
    {"camera_id": "cam-g06-01", "ghat_id": "g06", "source": os.getenv("CAM_G06_01", "mock"), "area_sqm": 550.0},
])))


def _generate_mock_frame():
    try:
        import numpy as np
        return np.zeros((640, 640, 3), dtype="uint8")
    except ImportError:
        return None


class YOLODetector:
    _model = None
    _available = None

    @classmethod
    def _try_load(cls):
        if cls._available is not None:
            return cls._available
        try:
            from ultralytics import YOLO
            cls._model = YOLO(MODEL_NAME)
            logger.info("[YOLO] Loaded: %s", MODEL_NAME)
            cls._available = True
        except Exception as exc:
            logger.warning("[YOLO] Unavailable (%s) — mock counts", exc)
            cls._available = False
        return cls._available

    @classmethod
    def count_persons(cls, frame) -> int:
        if not cls._try_load() or frame is None:
            import random
            return random.randint(100, 400)
        try:
            results = cls._model(frame, classes=[PERSON_CLASS_ID], conf=CONFIDENCE, verbose=False)
            return sum(len(r.boxes) for r in results)
        except Exception as exc:
            logger.debug("[YOLO] Inference error: %s", exc)
            return 0


def read_frame(source: str):
    if source == "mock":
        return _generate_mock_frame()
    try:
        import cv2
        cap = cv2.VideoCapture(source)
        ret, frame = cap.read()
        cap.release()
        if not ret or frame is None:
            return None
        h, w = frame.shape[:2]
        scale = FRAME_MAX_PX / max(h, w)
        if scale < 1.0:
            frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        return frame
    except Exception as exc:
        logger.debug("[Camera] Read error (source=%s): %s", source[:40], exc)
        return None


@dataclass
class CameraState:
    camera_id: str
    ghat_id: str
    source: str
    area_sqm: float
    last_count: int = 0
    fail_count: int = 0
    backoff_until: float = 0.0
    MAX_FAILS = 10
    MAX_BACKOFF_S = 30.0

    async def process_and_post(self, client: httpx.AsyncClient):
        # Honour exponential backoff — when a camera has failed many frames
        # in a row, stop hammering its endpoint at full FPS. Saves CPU and
        # avoids retry-storming a dead RTSP feed.
        now = time.monotonic()
        if now < self.backoff_until:
            return

        # Offload the blocking OpenCV I/O to the shared thread pool so the
        # event loop stays free for every other camera coroutine.
        from app.core.admission import run_blocking
        try:
            frame = await asyncio.wait_for(
                run_blocking(read_frame, self.source),
                timeout=4.0,
            )
        except asyncio.TimeoutError:
            frame = None

        if frame is None:
            self.fail_count += 1
            if self.fail_count == self.MAX_FAILS:
                logger.warning("[Camera] %s offline for %d frames — backing off",
                               self.camera_id, self.MAX_FAILS)
            if self.fail_count >= self.MAX_FAILS:
                # Exponential backoff capped at MAX_BACKOFF_S
                wait = min(self.MAX_BACKOFF_S,
                           1.5 ** min(self.fail_count - self.MAX_FAILS, 8))
                self.backoff_until = now + wait
            person_count = self.last_count
        else:
            self.fail_count = 0
            # YOLO inference is synchronous CPU-bound — also offload.
            person_count = await run_blocking(YOLODetector.count_persons, frame)
            self.last_count = person_count

        payload = {
            "ghat_id": self.ghat_id,
            "person_count": person_count,
            "frame_area_sq_m": self.area_sqm,
            "camera_id": self.camera_id,
            "timestamp": time.time(),
        }
        try:
            resp = await client.post(INGEST_ENDPOINT, json=payload, timeout=5.0)
            if resp.status_code != 200:
                logger.debug("[Camera] API rejected: %s", resp.text[:100])
        except Exception as exc:
            logger.debug("[Camera] API unreachable: %s", exc)


async def run_camera(cam_cfg: dict):
    state = CameraState(
        camera_id=cam_cfg["camera_id"],
        ghat_id=cam_cfg["ghat_id"],
        source=cam_cfg["source"],
        area_sqm=cam_cfg.get("area_sqm", DEFAULT_FRAME_AREA),
    )
    interval = 1.0 / TARGET_FPS
    logger.info("[Camera] Starting %s → ghat %s (%.1f FPS)", state.camera_id, state.ghat_id, TARGET_FPS)

    # Singleton httpx client shared across every camera coroutine. Each
    # `async with httpx.AsyncClient()` previously cost a full TCP+TLS dance
    # to the API on every iteration once the connection idled out.
    from app.core.http_client import cctv_ingest_client
    client = await cctv_ingest_client()
    while True:
        t0 = time.monotonic()
        try:
            await state.process_and_post(client)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.debug("[Camera] iteration error: %s", exc)
        elapsed = time.monotonic() - t0
        await asyncio.sleep(max(0, interval - elapsed))


async def main():
    logger.info("[CCTVWorker] Starting %d cameras  API=%s  FPS=%.1f", len(CAMERA_CONFIG), API_BASE, TARGET_FPS)
    tasks = [asyncio.create_task(run_camera(cam)) for cam in CAMERA_CONFIG]
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
