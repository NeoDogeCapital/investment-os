-- 010_image_signals.sql
-- Chart image analysis results from Claude vision

CREATE TABLE IF NOT EXISTS image_signals (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id           TEXT REFERENCES sources(id) ON DELETE SET NULL,
    image_path          TEXT NOT NULL,           -- relative path within vault
    note_path           TEXT,                    -- associated .md note path if any
    chart_type          TEXT CHECK (chart_type IN (
                            'price_chart','indicator','breadth',
                            'tweet','options_flow','macro_data','other')),
    ticker              TEXT,
    timeframe           TEXT CHECK (timeframe IN (
                            'intraday','daily','weekly','monthly','unknown')),
    technical_signal    TEXT CHECK (technical_signal IN ('BULLISH','NEUTRAL','BEARISH')),
    signal_strength     TEXT CHECK (signal_strength IN ('high','medium','low')),
    key_levels          JSONB DEFAULT '[]',
    indicators_shown    JSONB DEFAULT '[]',
    key_observation     TEXT,
    regime_implication  TEXT,
    action_implication  TEXT,
    regime_vote         NUMERIC(4,2),            -- -1.0 to +1.0 (70% weight vs text)
    asset_classes       TEXT[] DEFAULT '{}',
    regime_tags         TEXT[] DEFAULT '{}',
    raw_extraction      JSONB,                   -- full Claude response
    ai_model            TEXT,
    ai_processed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_img_source   ON image_signals (source_id);
CREATE INDEX IF NOT EXISTS idx_img_ticker   ON image_signals (ticker);
CREATE INDEX IF NOT EXISTS idx_img_signal   ON image_signals (technical_signal);
CREATE INDEX IF NOT EXISTS idx_img_created  ON image_signals (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_img_path     ON image_signals (image_path);

-- ─── Row Level Security ─────────────────────────────────────────────────────
ALTER TABLE public.image_signals ENABLE ROW LEVEL SECURITY;
