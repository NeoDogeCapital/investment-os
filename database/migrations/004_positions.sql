-- 004_positions.sql
-- Positions and model rules (rules are hardcoded — never bypass)

-- ─── MODEL RULES (reference table, immutable) ────────────────────────────────
CREATE TABLE IF NOT EXISTS model_rules (
    rule_id         TEXT PRIMARY KEY,
    rule_type       TEXT NOT NULL CHECK (rule_type IN ('hard', 'soft')),
    description     TEXT NOT NULL,
    value           JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO model_rules (rule_id, rule_type, description, value) VALUES
('max_allocation',      'hard', 'Maximum allocation per position',          '"0.12"'),
('min_initiation',      'hard', 'Minimum initiation size',                  '"0.02"'),
('allocation_ladder',   'hard', 'Allowed allocation steps (sequential)',    '[0.02, 0.04, 0.07, 0.10, 0.12]'),
('require_thesis',      'hard', 'Every trade requires a locked thesis',     '"true"'),
('require_invalidation','hard', 'Every trade requires invalidation conditions', '"true"'),
('no_skip_ladder',      'hard', 'Allocation ladder steps cannot be skipped','"true"'),
('soft_override_doc',   'soft', 'Soft rule overrides require written documentation', '"true"')
ON CONFLICT (rule_id) DO NOTHING;

-- ─── POSITIONS ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS positions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticker                  TEXT NOT NULL,
    name                    TEXT,
    direction               TEXT NOT NULL CHECK (direction IN ('long', 'short')),
    status                  TEXT NOT NULL DEFAULT 'pending'
                                CHECK (status IN ('pending', 'open', 'closed', 'invalidated')),
    -- Allocation (as fraction of portfolio, e.g. 0.04 = 4%)
    current_allocation      NUMERIC(5,4) NOT NULL DEFAULT 0.00
                                CHECK (current_allocation >= 0 AND current_allocation <= 0.12),
    target_allocation       NUMERIC(5,4)
                                CHECK (target_allocation IS NULL OR (target_allocation >= 0.02 AND target_allocation <= 0.12)),
    -- Ladder tracking
    ladder_step             INTEGER NOT NULL DEFAULT 0
                                CHECK (ladder_step BETWEEN 0 AND 4),
                                -- 0=not yet initiated, 1=2%, 2=4%, 3=7%, 4=10%, 5=12%
    -- Entry details
    entry_price             NUMERIC(12,4),
    entry_date              DATE,
    -- Exit details
    exit_price              NUMERIC(12,4),
    exit_date               DATE,
    realized_pnl_pct        NUMERIC(8,4),
    -- Regime at entry
    regime_at_entry         TEXT CHECK (regime_at_entry IN ('RISK_ON', 'NEUTRAL', 'RISK_OFF')),
    regime_score_at_entry   NUMERIC(5,4),
    -- Thesis (REQUIRED — hard rule)
    thesis_locked           BOOLEAN NOT NULL DEFAULT FALSE,
    thesis                  TEXT,
    thesis_locked_at        TIMESTAMPTZ,
    -- Invalidation conditions (REQUIRED — hard rule)
    invalidation_conditions TEXT,
    invalidation_locked_at  TIMESTAMPTZ,
    -- Asset metadata
    asset_class             TEXT,
    sector                  TEXT,
    -- Soft rule override (if any soft rule was overridden for this position)
    soft_rule_override      BOOLEAN NOT NULL DEFAULT FALSE,
    soft_rule_override_doc  TEXT,
    -- Timestamps
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_positions_status   ON positions (status);
CREATE INDEX IF NOT EXISTS idx_positions_ticker   ON positions (ticker);
CREATE INDEX IF NOT EXISTS idx_positions_entry    ON positions (entry_date DESC);

-- ─── POSITION SIZING EVENTS ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS position_events (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    position_id         UUID NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
    event_type          TEXT NOT NULL CHECK (event_type IN (
                            'initiation', 'add', 'trim', 'close', 'stop', 'invalidation')),
    from_allocation     NUMERIC(5,4) NOT NULL,
    to_allocation       NUMERIC(5,4) NOT NULL,
    from_ladder_step    INTEGER,
    to_ladder_step      INTEGER,
    price               NUMERIC(12,4),
    regime_at_event     TEXT CHECK (regime_at_event IN ('RISK_ON', 'NEUTRAL', 'RISK_OFF')),
    rationale           TEXT,
    -- Gate check results at time of event
    gate_passed         BOOLEAN,
    gate_failures       JSONB DEFAULT '[]',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_position_events_pos ON position_events (position_id);
CREATE INDEX IF NOT EXISTS idx_position_events_ts  ON position_events (created_at DESC);

-- ─── Row Level Security ─────────────────────────────────────────────────────
ALTER TABLE public.model_rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.positions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.position_events ENABLE ROW LEVEL SECURITY;
