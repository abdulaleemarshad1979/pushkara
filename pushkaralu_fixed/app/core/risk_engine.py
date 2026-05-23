# ═══════════════════════════════════════════════════════════════════════════════
# Godavari Pushkaralu 2027 — Crowd Risk Engine
#
# DESIGN PHILOSOPHY:
#   - No ML, no heavy models, no GPU required
#   - Every calculation is O(1) time and O(1) space
#   - Uses: occupancy ratio + trend + time-of-day + source fusion
#   - final_density = 0.75 * vision_density + 0.25 * telecom_density
#   - Risk levels map to existing system's crowd_level strings (low/medium/high/critical)
#     → ZERO frontend changes required
#
# FAULT TOLERANCE:
#   - CCTV fails → use last known vision value (stale-ok for up to 5 min)
#   - Telecom fails → ignore (weight falls back to vision only)
#   - Both fail → use occupancy from DB ghat.current_count / ghat.capacity
# ═══════════════════════════════════════════════════════════════════════════════

import time
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger("pushkaralu.risk")

# ── Thresholds (tunable via env without code change) ────────────────────────
# Occupancy ratio → crowd level string (matches existing DB schema)
THRESHOLD_LOW      = 0.40   # < 40%  → "low"
THRESHOLD_MEDIUM   = 0.65   # 40-65% → "medium"
THRESHOLD_HIGH     = 0.85   # 65-85% → "high"
# anything ≥ 85%            → "critical"

# Trend weight: how much the recent change affects risk
TREND_WEIGHT       = 0.15   # 15% of final score from trend

# Time-of-day surge multiplier (peak bathing hours boost sensitivity)
PEAK_HOURS = {5, 6, 7, 8, 16, 17, 18, 19}   # 5–8 AM and 4–7 PM
PEAK_MULTIPLIER    = 1.10   # 10% uplift during peak hours
OFF_PEAK_MULT      = 1.00

# Stale data timeout: if last reading is older than this, mark source as stale
VISION_STALE_SECS  = 300    # 5 minutes
TELECOM_STALE_SECS = 120    # 2 minutes


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class VisionReading:
    """Structured data from CCTV/YOLO — never send raw video."""
    ghat_id: str
    person_count: int           # raw head count from YOLO
    frame_area_sq_m: float      # calibrated coverage area of the camera frame
    timestamp: float = field(default_factory=time.time)

    @property
    def density_per_sqm(self) -> float:
        if self.frame_area_sq_m <= 0:
            return 0.0
        return self.person_count / self.frame_area_sq_m

    @property
    def is_stale(self) -> bool:
        return (time.time() - self.timestamp) > VISION_STALE_SECS


@dataclass
class TelecomReading:
    """Aggregated tower-level data — never raw subscriber data."""
    ghat_id: str
    active_devices: int         # unique devices on nearest tower
    tower_baseline: int         # typical off-festival device count for calibration
    timestamp: float = field(default_factory=time.time)

    @property
    def normalised_density(self) -> float:
        """0.0–1.0: how far above baseline the tower is seeing."""
        if self.tower_baseline <= 0:
            return 0.0
        ratio = self.active_devices / self.tower_baseline
        return min(ratio / 5.0, 1.0)    # cap at 5× baseline = 1.0

    @property
    def is_stale(self) -> bool:
        return (time.time() - self.timestamp) > TELECOM_STALE_SECS


@dataclass
class GhatState:
    """
    Runtime state for one ghat.  Stored PER INSTANCE — do NOT share across
    processes.  Redis is the source of truth; this is a local computation cache.
    """
    ghat_id: str
    capacity: int
    # Latest readings
    last_vision:  Optional[VisionReading]  = None
    last_telecom: Optional[TelecomReading] = None
    # Fallback: direct count from DB/admin input
    db_count:     int   = 0
    db_level:     str   = "low"
    # History for trend (ring buffer, max 10 readings)
    _density_history: list = field(default_factory=list)
    MAX_HIST = 10

    def push_density(self, density: float):
        self._density_history.append(density)
        if len(self._density_history) > self.MAX_HIST:
            self._density_history.pop(0)

    @property
    def trend(self) -> float:
        """
        Linear trend of density over last N readings.
        Positive → crowd growing.  Negative → crowd shrinking.
        Range: roughly -1.0 to +1.0.
        O(N) where N ≤ 10, effectively O(1).
        """
        h = self._density_history
        if len(h) < 2:
            return 0.0
        # Simple first-difference average
        diffs = [h[i] - h[i - 1] for i in range(1, len(h))]
        return sum(diffs) / len(diffs)


# ── Core risk computation ─────────────────────────────────────────────────────

class RiskEngine:
    """
    Stateless computation methods — all inputs are passed as arguments.
    Can be called from any async worker or sync context.
    No I/O, no network calls, no ML inference.
    All methods: O(1) time, O(1) space.
    """

    @staticmethod
    def fuse_density(
        vision_density: Optional[float],
        telecom_density: Optional[float],
        db_occupancy: float,
    ) -> float:
        """
        Sensor fusion with spec weights:  0.75 vision + 0.25 telecom
        Falls back gracefully when sources are unavailable.
        """
        if vision_density is not None and telecom_density is not None:
            return 0.75 * vision_density + 0.25 * telecom_density
        if vision_density is not None:
            return vision_density          # telecom unavailable — use vision only
        if telecom_density is not None:
            return 0.40 * telecom_density + 0.60 * db_occupancy  # partial fusion
        return db_occupancy                # both sensors down — use DB count

    @staticmethod
    def time_multiplier() -> float:
        """Return peak-hour multiplier based on current hour (IST)."""
        hour = datetime.utcnow().hour + 5  # rough IST offset (UTC+5:30)
        hour = hour % 24
        return PEAK_MULTIPLIER if hour in PEAK_HOURS else OFF_PEAK_MULT

    @staticmethod
    def compute_risk_score(
        raw_density: float,
        trend: float,
        capacity: int,
        current_count: int,
    ) -> float:
        """
        Final risk score in [0.0, 1.0].
        Combines: occupancy ratio + trend influence + time-of-day.
        All arithmetic — no external calls.
        """
        # Base occupancy ratio (0–1)
        if capacity > 0:
            occupancy = min(current_count / capacity, 1.0)
        else:
            occupancy = raw_density   # fallback if capacity unknown

        # Blend sensor density with occupancy
        base = 0.70 * occupancy + 0.30 * raw_density

        # Trend: growing crowd increases risk, shrinking decreases it
        trend_factor = max(-0.20, min(0.20, trend * TREND_WEIGHT))
        adjusted = base + trend_factor

        # Time-of-day multiplier
        adjusted *= RiskEngine.time_multiplier()

        return max(0.0, min(1.0, adjusted))

    @staticmethod
    def score_to_level(score: float) -> str:
        """
        Map risk score → crowd_level string.
        These EXACTLY match the existing DB schema strings — no frontend changes.
        """
        if score < THRESHOLD_LOW:
            return "low"
        if score < THRESHOLD_MEDIUM:
            return "medium"
        if score < THRESHOLD_HIGH:
            return "high"
        return "critical"

    @staticmethod
    def score_to_colour(level: str) -> str:
        return {
            "low":      "#22c55e",   # green
            "medium":   "#f59e0b",   # amber
            "high":     "#ef4444",   # red
            "critical": "#7c3aed",   # purple (maximum danger)
        }.get(level, "#6b7280")

    @staticmethod
    def should_alert(score: float, previous_score: float) -> bool:
        """
        Trigger an alert if:
        1. Score just crossed into "high" or "critical" territory, OR
        2. Score jumped > 15% in one step (sudden surge)
        """
        level_now = RiskEngine.score_to_level(score)
        level_prev = RiskEngine.score_to_level(previous_score)
        if level_now in ("high", "critical") and level_prev in ("low", "medium"):
            return True
        if (score - previous_score) >= 0.15:
            return True
        return False

    @classmethod
    def evaluate(
        cls,
        state: GhatState,
        vision:  Optional[VisionReading] = None,
        telecom: Optional[TelecomReading] = None,
    ) -> dict:
        """
        Main entry point.  Accepts latest readings, returns a complete risk payload.
        This is the ONLY function callers should use.

        Returns a dict that is directly broadcast over WebSocket and stored in Redis.
        """
        # ── Extract sensor values (with stale-check fallbacks) ──────────────
        vision_density  = None
        telecom_density = None

        if vision and not vision.is_stale:
            # Normalise person density to a 0–1 scale:
            # assume a ghat is "full" at 4 persons/m² (Fruin Level F — stampede risk)
            vision_density = min(vision.density_per_sqm / 4.0, 1.0)
        elif state.last_vision and not state.last_vision.is_stale:
            vision_density = min(state.last_vision.density_per_sqm / 4.0, 1.0)

        if telecom and not telecom.is_stale:
            telecom_density = telecom.normalised_density
        elif state.last_telecom and not state.last_telecom.is_stale:
            telecom_density = state.last_telecom.normalised_density

        # DB occupancy (always available as final fallback)
        db_occupancy = (state.db_count / state.capacity) if state.capacity > 0 else 0.0

        # ── Fuse signals ────────────────────────────────────────────────────
        fused_density = cls.fuse_density(vision_density, telecom_density, db_occupancy)

        # ── Update trend history ────────────────────────────────────────────
        state.push_density(fused_density)
        trend = state.trend

        # ── Compute risk score ──────────────────────────────────────────────
        estimated_count = int(fused_density * state.capacity) if state.capacity > 0 else state.db_count
        score = cls.compute_risk_score(fused_density, trend, state.capacity, estimated_count)
        level = cls.score_to_level(score)

        # ── Save latest readings back to state ──────────────────────────────
        if vision:
            state.last_vision = vision
        if telecom:
            state.last_telecom = telecom

        # ── Build broadcast payload ─────────────────────────────────────────
        return {
            "ghat_id":          state.ghat_id,
            "crowd_level":      level,                   # matches existing DB field name
            "risk_score":       round(score, 3),
            "fused_density":    round(fused_density, 3),
            "trend":            round(trend, 4),
            "estimated_count":  estimated_count,
            "capacity":         state.capacity,
            "occupancy_pct":    round(fused_density * 100, 1),
            "colour":           cls.score_to_colour(level),
            "sources": {
                "vision":  vision_density is not None,
                "telecom": telecom_density is not None,
                "db":      True,
            },
            "peak_hour":        RiskEngine.time_multiplier() > 1.0,
            "timestamp":        time.time(),
        }


# ── Convenience: evaluate from raw dicts (used by API endpoints) ──────────────

def evaluate_from_dicts(
    ghat: dict,
    vision_data:  Optional[dict] = None,
    telecom_data: Optional[dict] = None,
    density_history: list = None,
) -> dict:
    """
    Adapter so the FastAPI layer doesn't need to import dataclasses.
    Accepts plain dicts (as stored in Redis / DB).
    """
    state = GhatState(
        ghat_id  = ghat["id"],
        capacity = ghat.get("capacity", 1000),
        db_count = ghat.get("current_count", 0),
        db_level = ghat.get("crowd_level", "low"),
    )

    # Replay history if provided (e.g., loaded from Redis ring buffer)
    if density_history:
        for h in density_history[-GhatState.MAX_HIST:]:
            state._density_history.append(h.get("fused_density", 0.0))

    vision = None
    if vision_data:
        vision = VisionReading(
            ghat_id        = ghat["id"],
            person_count   = vision_data.get("person_count", 0),
            frame_area_sq_m= vision_data.get("frame_area_sq_m", 500.0),
            timestamp      = vision_data.get("timestamp", time.time()),
        )

    telecom = None
    if telecom_data:
        telecom = TelecomReading(
            ghat_id        = ghat["id"],
            active_devices = telecom_data.get("active_devices", 0),
            tower_baseline = telecom_data.get("tower_baseline", 1000),
            timestamp      = telecom_data.get("timestamp", time.time()),
        )

    return RiskEngine.evaluate(state, vision, telecom)


# ═══════════════════════════════════════════════════════════════════════════════
# SCALE IMPROVEMENTS — added for 10-lakh pilgrim scenarios
# ═══════════════════════════════════════════════════════════════════════════════

class AdaptiveThresholds:
    """
    Tighten risk thresholds on known high-traffic festival days.
    On Maha Pushkar (Day 1) and Karthika Pournami bathing dates, the system
    lowers the 'medium' and 'high' triggers so warnings fire earlier.

    No DB or network calls — pure date arithmetic (O(1)).
    """

    # Known Pushkaralu 2027 high-traffic dates (YYYY-MM-DD)
    # Maha Pushkar = first 2 days, Ardha Pushkar = days 5-6, Uttara = last 2 days
    HIGH_TRAFFIC_DATES = {
        "2027-07-25", "2027-07-26",  # Maha Pushkar (Day 1-2) — highest footfall
        "2027-07-29", "2027-07-30",  # Ardha Pushkar
        "2027-08-03", "2027-08-04",  # Uttara Pushkar (final days)
    }

    @classmethod
    def get_thresholds(cls, date_str: str = None) -> tuple[float, float, float]:
        """
        Returns (low, medium, high) threshold tuple for current date.
        Normal days: (0.40, 0.65, 0.85)
        High-traffic: (0.30, 0.55, 0.75) — triggers 10 pts earlier
        """
        from datetime import date
        today = date_str or date.today().isoformat()
        if today in cls.HIGH_TRAFFIC_DATES:
            return (0.30, 0.55, 0.75)
        return (THRESHOLD_LOW, THRESHOLD_MEDIUM, THRESHOLD_HIGH)

    @classmethod
    def score_to_level_adaptive(cls, score: float, date_str: str = None) -> str:
        low, medium, high = cls.get_thresholds(date_str)
        if score < low:    return "low"
        if score < medium: return "medium"
        if score < high:   return "high"
        return "critical"


class SurgeDetector:
    """
    Ring-buffer based per-ghat surge detector.
    Fires when crowd density jumps > SURGE_THRESHOLD in a short window.
    Used for inter-ghat flow management: divert pilgrims to less-busy ghats.

    All operations O(1) per call (fixed ring buffer size).
    """
    SURGE_THRESHOLD   = 0.20   # 20% density jump in one reading window
    SPIKE_WINDOW      = 3      # check across last 3 readings

    def __init__(self):
        self._histories: dict[str, list] = {}  # ghat_id → recent density list

    def update(self, ghat_id: str, density: float) -> bool:
        """
        Push new density reading.  Returns True if a surge is detected.
        """
        h = self._histories.setdefault(ghat_id, [])
        h.append(density)
        if len(h) > self.SPIKE_WINDOW + 1:
            h.pop(0)
        if len(h) < 2:
            return False
        # Maximum single-step jump in the recent window
        max_jump = max(h[i] - h[i-1] for i in range(1, len(h)))
        return max_jump >= self.SURGE_THRESHOLD

    def clear(self, ghat_id: str):
        self._histories.pop(ghat_id, None)


# Singleton for the broadcast loop to use
_surge_detector = SurgeDetector()


def evaluate_from_dicts_adaptive(
    ghat: dict,
    vision_data:  Optional[dict] = None,
    telecom_data: Optional[dict] = None,
    density_history: list = None,
    date_str: str = None,
) -> dict:
    """
    Like evaluate_from_dicts() but uses adaptive thresholds for festival days.
    Drop-in replacement — same return format.
    """
    result = evaluate_from_dicts(ghat, vision_data, telecom_data, density_history)

    # Override level with adaptive threshold
    adaptive_level = AdaptiveThresholds.score_to_level_adaptive(result["risk_score"], date_str)
    result["crowd_level"] = adaptive_level
    result["colour"]      = RiskEngine.score_to_colour(adaptive_level)

    # Add surge flag
    surge = _surge_detector.update(ghat["id"], result["fused_density"])
    result["surge_detected"] = surge

    return result
