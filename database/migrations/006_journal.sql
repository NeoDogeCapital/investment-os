-- 006_journal.sql
-- Decision journal: every gate check and trade decision is logged permanently

CREATE TABLE IF NOT EXISTS decision_journal (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- What decision was being made
    decision_type       TEXT NOT NULL CHECK (decision_type IN (
                            'entry', 'add', 'trim', 'exit', 'pass', 'regime_change')),
    ticker              TEXT,
    position_id         UUID REFERENCES positions(id) ON DELETE SET NULL,
    -- Proposed action
    proposed_allocation NUMERIC(5,4),
    proposed_direction  TEXT CHECK (proposed_direction IN ('long', 'short')),
    -- Gate results
    gate_passed         BOOLEAN NOT NULL,
    hard_rule_failures  JSONB NOT NULL DEFAULT '[]',
    soft_rule_flags     JSONB NOT NULL DEFAULT '[]',
    -- Regime context at decision time
    regime_at_decision  TEXT CHECK (regime_at_decision IN ('RISK_ON', 'NEUTRAL', 'RISK_OFF')),
    regime_score        NUMERIC(5,4),
    regime_snapshot_id  UUID REFERENCES regime_snapshots(id) ON DELETE SET NULL,
    -- Rationale
    thesis_summary      TEXT,
    decision_rationale  TEXT,
    soft_override_doc   TEXT,       -- required if overriding a soft rule
    -- Outcome tracking (filled in later)
    outcome_tracked     BOOLEAN NOT NULL DEFAULT FALSE,
    outcome_notes       TEXT,
    outcome_updated_at  TIMESTAMPTZ,
    -- Metadata
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_journal_ticker      ON decision_journal (ticker);
CREATE INDEX IF NOT EXISTS idx_journal_position    ON decision_journal (position_id);
CREATE INDEX IF NOT EXISTS idx_journal_created     ON decision_journal (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_journal_gate_passed ON decision_journal (gate_passed);
CREATE INDEX IF NOT EXISTS idx_journal_type        ON decision_journal (decision_type);

-- ─── Regime change log ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS regime_change_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_regime     TEXT CHECK (from_regime IN ('RISK_ON', 'NEUTRAL', 'RISK_OFF')),
    to_regime       TEXT NOT NULL CHECK (to_regime IN ('RISK_ON', 'NEUTRAL', 'RISK_OFF')),
    from_score      NUMERIC(5,4),
    to_score        NUMERIC(5,4) NOT NULL,
    snapshot_id     UUID NOT NULL REFERENCES regime_snapshots(id),
    narrative       TEXT,
    changed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_regime_change_log_at ON regime_change_log (changed_at DESC);

-- ─── Row Level Security ─────────────────────────────────────────────────────
ALTER TABLE public.decision_journal ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.regime_change_log ENABLE ROW LEVEL SECURITY;
