-- 007_learning.sql
-- Learning system: track source accuracy, thesis outcomes, and model calibration

-- ─── Source accuracy tracking ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS source_accuracy (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id           TEXT NOT NULL REFERENCES sources(id),
    evaluation_period   TEXT NOT NULL,          -- e.g. '2025-Q1', '2025-06', 'trailing-90d'
    -- Prediction accuracy by dimension
    accuracy_macro_regime   NUMERIC(5,4),       -- 0.0–1.0
    accuracy_micro_levels   NUMERIC(5,4),
    accuracy_options_flow   NUMERIC(5,4),
    accuracy_timing         NUMERIC(5,4),
    -- Overall
    accuracy_overall        NUMERIC(5,4),
    notes_evaluated         INTEGER NOT NULL DEFAULT 0,
    -- Weight adjustment suggestion
    suggested_weight_adj    NUMERIC(5,4),       -- positive = upweight, negative = downweight
    adjustment_applied      BOOLEAN NOT NULL DEFAULT FALSE,
    evaluated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_source_acc_source ON source_accuracy (source_id);
CREATE INDEX IF NOT EXISTS idx_source_acc_period ON source_accuracy (evaluation_period);

-- ─── Thesis outcome tracking ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS thesis_outcomes (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    position_id         UUID NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
    -- Original thesis elements
    thesis_text         TEXT NOT NULL,
    invalidation_text   TEXT,
    -- Outcome
    thesis_validated    BOOLEAN,                -- NULL = still open, TRUE = worked, FALSE = failed
    invalidation_triggered  BOOLEAN DEFAULT FALSE,
    invalidation_detail TEXT,
    -- Performance
    realized_pnl_pct    NUMERIC(8,4),
    max_drawdown_pct    NUMERIC(8,4),
    hold_days           INTEGER,
    -- Regime accuracy
    regime_at_entry     TEXT,
    regime_was_correct  BOOLEAN,
    -- Source accuracy linkage: which sources supported this thesis
    supporting_sources  TEXT[] DEFAULT '{}',
    -- AI-generated post-mortem
    post_mortem         TEXT,
    post_mortem_at      TIMESTAMPTZ,
    -- Timestamps
    evaluated_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_thesis_outcomes_pos    ON thesis_outcomes (position_id);
CREATE INDEX IF NOT EXISTS idx_thesis_outcomes_result ON thesis_outcomes (thesis_validated);

-- ─── Model calibration snapshots ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS model_calibration (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    calibrated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Regime threshold calibration
    risk_on_threshold   NUMERIC(5,4) NOT NULL DEFAULT 0.40,
    risk_off_threshold  NUMERIC(5,4) NOT NULL DEFAULT -0.40,
    -- Source weight adjustments applied this calibration
    weight_adjustments  JSONB DEFAULT '{}',
    -- Calibration basis
    positions_evaluated INTEGER NOT NULL DEFAULT 0,
    win_rate            NUMERIC(5,4),
    avg_pnl_pct         NUMERIC(8,4),
    regime_accuracy     NUMERIC(5,4),
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS idx_model_cal_at ON model_calibration (calibrated_at DESC);

-- ─── Alerts / trigger log ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS alerts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_type      TEXT NOT NULL CHECK (alert_type IN (
                        'invalidation', 'regime_change', 'stale_research',
                        'ladder_eligible', 'thesis_check', 'custom')),
    position_id     UUID REFERENCES positions(id) ON DELETE SET NULL,
    source_id       TEXT REFERENCES sources(id) ON DELETE SET NULL,
    severity        TEXT NOT NULL DEFAULT 'info'
                        CHECK (severity IN ('info', 'warning', 'critical')),
    title           TEXT NOT NULL,
    body            TEXT,
    acknowledged    BOOLEAN NOT NULL DEFAULT FALSE,
    acknowledged_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_alerts_unacked    ON alerts (acknowledged) WHERE acknowledged = FALSE;
CREATE INDEX IF NOT EXISTS idx_alerts_position   ON alerts (position_id);
CREATE INDEX IF NOT EXISTS idx_alerts_created    ON alerts (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_severity   ON alerts (severity);

-- ─── Row Level Security ─────────────────────────────────────────────────────
ALTER TABLE public.source_accuracy ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.thesis_outcomes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.model_calibration ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.alerts ENABLE ROW LEVEL SECURITY;
