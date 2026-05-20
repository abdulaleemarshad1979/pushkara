-- ═══════════════════════════════════════════════════════════════════════════════
-- Godavari Pushkaralu 2027 — PostgreSQL Schema  (v8)
--
-- FIXES vs v7 schema:
--   - sos_alerts: added updated_at column (pg_store UPDATE queries referenced it)
--   - issues:     added updated_at column (pg_store UPDATE queries referenced it)
--   - lost_persons: updated_at was present but trigger was missing from DO block —
--     added to the trigger loop
--   - Schema auto-applied by postgres via docker-entrypoint-initdb.d/
--     (added schema.sql mount to docker-compose.yml postgres service)
--
-- ARCHITECTURE:
--   - PostgreSQL is the source of truth for SOS, issues, lost persons (v7+).
--   - FastAPI reads from Redis cache; misses fall through to Postgres.
--   - All critical tables use UUID primary keys for distributed safety.
--   - JSONB payload columns preserve full event snapshots for analytics.
-- ═══════════════════════════════════════════════════════════════════════════════

-- ── Extensions ────────────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "pgcrypto";   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS "pg_trgm";    -- fuzzy text search on lost persons

-- ── Ghats (reference data — loaded from sample_data.json once) ───────────────
CREATE TABLE IF NOT EXISTS ghats (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    telugu_name     TEXT,
    description     TEXT,
    latitude        DOUBLE PRECISION NOT NULL,
    longitude       DOUBLE PRECISION NOT NULL,
    capacity        INTEGER NOT NULL DEFAULT 1000,
    current_count   INTEGER NOT NULL DEFAULT 0,
    crowd_level     TEXT NOT NULL DEFAULT 'low'
                        CHECK (crowd_level IN ('low','medium','high','critical')),
    bathing_timings TEXT,
    zone            TEXT,
    nearest_landmark TEXT,
    special_dates   JSONB,
    facilities      JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ── Volunteers ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS volunteers (
    id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    name            TEXT NOT NULL,
    username        TEXT UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,              -- bcrypt hash; NEVER plain text
    phone           TEXT,
    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION,
    zone            TEXT,
    status          TEXT NOT NULL DEFAULT 'available'
                        CHECK (status IN ('available','busy','offline')),
    assigned_issue  TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_volunteers_status ON volunteers (status);

-- ── Issues ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS issues (
    id                  TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    description         TEXT NOT NULL,
    category            TEXT NOT NULL DEFAULT 'general',
    image_url           TEXT,
    latitude            DOUBLE PRECISION NOT NULL,
    longitude           DOUBLE PRECISION NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending','in_progress','resolved')),
    assigned_volunteer  TEXT REFERENCES volunteers(id) ON DELETE SET NULL,
    user_name           TEXT NOT NULL DEFAULT 'Anonymous',
    resolved_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),   -- FIX: was missing in some schema versions
    payload             JSONB           -- full event snapshot for analytics
);
CREATE INDEX IF NOT EXISTS idx_issues_status     ON issues (status);
CREATE INDEX IF NOT EXISTS idx_issues_created_at ON issues (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_issues_category   ON issues (category);

-- ── SOS Alerts ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sos_alerts (
    id                  TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    user_name           TEXT NOT NULL DEFAULT 'Pilgrim',
    phone               TEXT,
    latitude            DOUBLE PRECISION NOT NULL,
    longitude           DOUBLE PRECISION NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active','assigned','resolved')),
    assigned_volunteer  TEXT REFERENCES volunteers(id) ON DELETE SET NULL,
    assigned_volunteer_name TEXT,
    resolved_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),   -- FIX: was missing in original schema
    payload             JSONB
);
CREATE INDEX IF NOT EXISTS idx_sos_status     ON sos_alerts (status);
CREATE INDEX IF NOT EXISTS idx_sos_created_at ON sos_alerts (created_at DESC);

-- ── Lost Persons ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS lost_persons (
    id                  TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    name                TEXT NOT NULL,
    age                 INTEGER,
    photo_url           TEXT,
    last_seen_location  TEXT,
    current_location    TEXT DEFAULT 'Unknown',
    contact_person      TEXT NOT NULL,
    contact_phone       TEXT NOT NULL,
    description         TEXT,
    status              TEXT NOT NULL DEFAULT 'missing'
                            CHECK (status IN ('missing','found','closed')),
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    payload             JSONB
);
CREATE INDEX IF NOT EXISTS idx_lost_status ON lost_persons (status);
-- trigram index for fuzzy name search
CREATE INDEX IF NOT EXISTS idx_lost_name_trgm ON lost_persons USING GIN (name gin_trgm_ops);

-- ── Facilities ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS facilities (
    id          TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,
    latitude    DOUBLE PRECISION NOT NULL,
    longitude   DOUBLE PRECISION NOT NULL,
    zone        TEXT,
    ghat_id     TEXT REFERENCES ghats(id) ON DELETE SET NULL,
    star_rating SMALLINT CHECK (star_rating BETWEEN 0 AND 3),  -- hotels only (0-3 stars)
    status      TEXT NOT NULL DEFAULT 'active',
    payload     JSONB,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_facilities_type ON facilities (type);
CREATE INDEX IF NOT EXISTS idx_facilities_ghat ON facilities (ghat_id);
-- Add star_rating column idempotently in case the table already exists
DO $$ BEGIN
  ALTER TABLE facilities ADD COLUMN IF NOT EXISTS star_rating SMALLINT CHECK (star_rating BETWEEN 0 AND 3);
EXCEPTION WHEN others THEN NULL;
END $$;

-- ── Transport Routes ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS transport_routes (
    id          TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,   -- bus | train | boat | shuttle
    from_loc    TEXT,
    to_loc      TEXT,
    schedule    JSONB,
    payload     JSONB,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_transport_type ON transport_routes (type);

-- ── Emergency Contacts ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS emergency_contacts (
    id          TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    name        TEXT NOT NULL,
    designation TEXT,
    department  TEXT,
    phone       TEXT NOT NULL,
    latitude    DOUBLE PRECISION,
    longitude   DOUBLE PRECISION,
    address     TEXT,
    category    TEXT NOT NULL DEFAULT 'other',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_contacts_category ON emergency_contacts (category);

-- ── Medical Facilities ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS medical_facilities (
    id          TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,
    latitude    DOUBLE PRECISION NOT NULL,
    longitude   DOUBLE PRECISION NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    beds        INTEGER DEFAULT 0,
    doctor      TEXT,
    phone       TEXT,
    zone        TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_medical_type ON medical_facilities (type);

-- ── Crowd Snapshots (time-series analytics) ───────────────────────────────────
CREATE TABLE IF NOT EXISTS crowd_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    ghat_id         TEXT NOT NULL REFERENCES ghats(id) ON DELETE CASCADE,
    crowd_level     TEXT,
    risk_score      DOUBLE PRECISION,
    estimated_count INTEGER,
    occupancy_pct   DOUBLE PRECISION,
    sources         JSONB,
    recorded_at     TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_crowd_ghat_time ON crowd_snapshots (ghat_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_crowd_time ON crowd_snapshots (recorded_at DESC);

-- ── App Events (audit log) ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS app_events (
    id          BIGSERIAL PRIMARY KEY,
    event_type  TEXT NOT NULL,
    entity_id   TEXT,
    payload     JSONB,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_events_type       ON app_events (event_type);
CREATE INDEX IF NOT EXISTS idx_events_created_at ON app_events (created_at DESC);

-- ── Helper: auto-update updated_at columns ────────────────────────────────────
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    tbl TEXT;
BEGIN
    -- FIX: added lost_persons to trigger loop (was in the table def but not the loop)
    FOREACH tbl IN ARRAY ARRAY['ghats','volunteers','issues','sos_alerts','lost_persons'] LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS trg_updated_at ON %I;
             CREATE TRIGGER trg_updated_at BEFORE UPDATE ON %I
             FOR EACH ROW EXECUTE FUNCTION update_updated_at();',
            tbl, tbl
        );
    END LOOP;
END;
$$;