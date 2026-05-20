# ═══════════════════════════════════════════════════════════════════════════════
# Godavari Pushkaralu 2027 — FastAPI Dockerfile  (v5)
# PHASE 4: Production multi-stage build
#
# Optimizations:
#   - python:3.11-slim base (smaller image)
#   - pip install with --no-cache-dir
#   - Non-root user (security hardening)
#   - .dockerignore excludes __pycache__, *.pyc, etc.
#   - uvicorn with --workers 1 (multiple processes via Docker replicas, not threads)
# ═══════════════════════════════════════════════════════════════════════════════

FROM python:3.11-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt


# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

WORKDIR /app

# Security: run as non-root
RUN groupadd -r pushkaralu && useradd -r -g pushkaralu pushkaralu

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin/uvicorn /usr/local/bin/uvicorn

# Copy application code
COPY --chown=pushkaralu:pushkaralu . .

# Create uploads directory with correct ownership
RUN mkdir -p /app/uploads && chown pushkaralu:pushkaralu /app/uploads

# Install curl for health checks
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

USER pushkaralu

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -sf http://localhost:8000/health || exit 1

# Use 1 worker per container — scale via Docker replicas
CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--loop", "uvloop", \
     "--http", "httptools", \
     "--log-level", "info", \
     "--access-log"]
