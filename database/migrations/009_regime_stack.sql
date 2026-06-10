-- 009_regime_stack.sql
-- Three-horizon regime stack: Short-Term (7d), Medium-Term (30d), Long-Term (90d)

-- ─── Main stack table ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS regime_stack (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    stack_date                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Short-Term horizon (7-day lookback)
    short_term_score            NUMERIC(5,4),
    short_term_label            TEXT CHECK (short_term_label IN ('RISK_ON','NEUTRAL','RISK_OFF')),
    short_term_confidence       NUMERIC(4,3),
    short_term_trend            TEXT CHECK (short_term_trend IN ('IMPROVING','STABLE','DETERIORATING')),
    short_term_notes_used       INTEGER DEFAULT 0,
    short_term_sources          TEXT[] DEFAULT '{}',

    -- Medium-Term horizon (30-day lookback)
    medium_term_score           NUMERIC(5,4),
    medium_term_label           TEXT CHECK (medium_term_label IN ('RISK_ON','NEUTRAL','RISK_OFF')),
    medium_term_confidence      NUMERIC(4,3),
    medium_term_trend           TEXT CHECK (medium_term_trend IN ('IMPROVING','STABLE','DETERIORATING')),
    medium_term_notes_used      INTEGER DEFAULT 0,
    medium_term_sources         TEXT[] DEFAULT '{}',

    -- Long-Term horizon (90-day lookback)
    long_term_score             NUMERIC(5,4),
    long_term_label             TEXT CHECK (long_term_label IN ('RISK_ON','NEUTRAL','RISK_OFF')),
    long_term_confidence        NUMERIC(4,3),
    long_term_trend             TEXT CHECK (long_term_trend IN ('IMPROVING','STABLE','DETERIORATING')),
    long_term_cycle_position    TEXT CHECK (long_term_cycle_position IN ('EARLY','MID','LATE')),
    long_term_notes_used        INTEGER DEFAULT 0,
    long_term_sources           TEXT[] DEFAULT '{}',

    -- Stack synthesis
    stack_alignment             TEXT NOT NULL CHECK (stack_alignment IN ('FULL','PARTIAL','DIVERGENT','OPPOSED')),
    max_tier_eligible           INTEGER NOT NULL CHECK (max_tier_eligible BETWEEN 0 AND 5),
    new_positions_allowed       BOOLEAN NOT NULL DEFAULT TRUE,
    reduce_existing_flag        BOOLEAN NOT NULL DEFAULT FALSE,
    short_vs_long_opposed       BOOLEAN NOT NULL DEFAULT FALSE,

    -- Dimension scores per horizon (JSON for compactness)
    short_term_dims             JSONB DEFAULT '{}',
    medium_term_dims            JSONB DEFAULT '{}',
    long_term_dims              JSONB DEFAULT '{}',

    -- AI narrative
    ai_interpretation           TEXT,
    ai_model                    TEXT,

    -- State
    is_current                  BOOLEAN NOT NULL DEFAULT TRUE,
    prior_stack_id              UUID REFERENCES regime_stack(id),

    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Only one current stack at a time
CREATE UNIQUE INDEX IF NOT EXISTS idx_regime_stack_current
    ON regime_stack (is_current) WHERE is_current = TRUE;

CREATE INDEX IF NOT EXISTS idx_regime_stack_date ON regime_stack (stack_date DESC);

-- ─── Per-source votes per horizon ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS regime_stack_source_votes (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    stack_id            UUID NOT NULL REFERENCES regime_stack(id) ON DELETE CASCADE,
    source_id           TEXT NOT NULL,
    horizon             TEXT NOT NULL CHECK (horizon IN ('short','medium','long')),
    composite_score     NUMERIC(5,4),
    score_macro_regime  NUMERIC(5,4),
    score_micro_levels  NUMERIC(5,4),
    score_options_flow  NUMERIC(5,4),
    score_timing        NUMERIC(5,4),
    decay_factor        NUMERIC(5,4),
    notes_count         INTEGER DEFAULT 0,
    most_recent_note_at TIMESTAMPTZ,
    label_vote          TEXT CHECK (label_vote IN ('RISK_ON','NEUTRAL','RISK_OFF'))
);

CREATE INDEX IF NOT EXISTS idx_rssv_stack   ON regime_stack_source_votes (stack_id);
CREATE INDEX IF NOT EXISTS idx_rssv_horizon ON regime_stack_source_votes (horizon);
CREATE INDEX IF NOT EXISTS idx_rssv_source  ON regime_stack_source_votes (source_id);

-- ─── Row Level Security ─────────────────────────────────────────────────────
ALTER TABLE public.regime_stack ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.regime_stack_source_votes ENABLE ROW LEVEL SECURITY;
