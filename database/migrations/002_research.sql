-- 002_research.sql
-- Research notes ingested from Obsidian vault

CREATE TABLE IF NOT EXISTS research_notes (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id           TEXT NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
    title               TEXT NOT NULL,
    content             TEXT NOT NULL,
    raw_file_path       TEXT,                     -- path within Obsidian vault
    note_type           TEXT NOT NULL DEFAULT 'research'
                            CHECK (note_type IN ('research', 'tweet', 'summary', 'alert')),
    -- Analyst's directional signal extracted from note
    signal_direction    TEXT CHECK (signal_direction IN ('bullish', 'bearish', 'neutral', 'mixed')),
    signal_strength     NUMERIC(3,2) CHECK (signal_strength BETWEEN -1.0 AND 1.0),
    -- Dimensional scores (AI-extracted, -1.0 to +1.0)
    score_macro_regime  NUMERIC(3,2) CHECK (score_macro_regime BETWEEN -1.0 AND 1.0),
    score_micro_levels  NUMERIC(3,2) CHECK (score_micro_levels BETWEEN -1.0 AND 1.0),
    score_options_flow  NUMERIC(3,2) CHECK (score_options_flow BETWEEN -1.0 AND 1.0),
    score_timing        NUMERIC(3,2) CHECK (score_timing BETWEEN -1.0 AND 1.0),
    -- Analytical layer tags
    layers_mentioned    TEXT[] DEFAULT '{}',
    -- Tickers/assets mentioned
    tickers_mentioned   TEXT[] DEFAULT '{}',
    themes              TEXT[] DEFAULT '{}',
    -- AI processing metadata
    ai_summary          TEXT,
    ai_processed_at     TIMESTAMPTZ,
    ai_model            TEXT,
    -- Timestamps
    published_at        TIMESTAMPTZ,              -- when the analyst published (if known)
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_research_source     ON research_notes (source_id);
CREATE INDEX IF NOT EXISTS idx_research_ingested   ON research_notes (ingested_at DESC);
CREATE INDEX IF NOT EXISTS idx_research_published  ON research_notes (published_at DESC);
CREATE INDEX IF NOT EXISTS idx_research_tickers    ON research_notes USING GIN (tickers_mentioned);
CREATE INDEX IF NOT EXISTS idx_research_layers     ON research_notes USING GIN (layers_mentioned);
CREATE INDEX IF NOT EXISTS idx_research_signal     ON research_notes (signal_direction, signal_strength);
