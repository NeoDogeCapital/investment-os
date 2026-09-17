-- 016 — Research feed, factor exposures, daily house-view outlook.
CREATE TABLE IF NOT EXISTS factor_exposures (
    score_date date NOT NULL, model_name text NOT NULL,
    beta_spy numeric, beta_momentum numeric, beta_quality numeric,
    beta_value numeric, beta_minvol numeric, beta_highbeta numeric,
    beta_rates numeric,      -- IEF (duration proxy)
    beta_gold numeric, r2 numeric,
    created_at timestamptz DEFAULT now(),
    PRIMARY KEY (score_date, model_name)
);
ALTER TABLE factor_exposures ENABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS daily_outlook (
    outlook_date date PRIMARY KEY,
    payload jsonb NOT NULL,          -- structured house view (see daily_outlook.py)
    created_at timestamptz DEFAULT now()
);
ALTER TABLE daily_outlook ENABLE ROW LEVEL SECURITY;

CREATE OR REPLACE VIEW v_research_feed AS
SELECT rn.ingested_at::date AS note_date, rn.source_id, s.short_name,
       rn.title, rn.signal_direction, rn.signal_strength, rn.ai_summary
FROM research_notes rn LEFT JOIN sources s ON s.id = rn.source_id
WHERE fn_is_approved() AND rn.ingested_at >= CURRENT_DATE - INTERVAL '14 days'
ORDER BY rn.ingested_at DESC;

CREATE OR REPLACE VIEW v_factor_exposures AS
SELECT DISTINCT ON (model_name) *
FROM factor_exposures WHERE fn_is_approved()
ORDER BY model_name, score_date DESC;

CREATE OR REPLACE VIEW v_daily_outlook AS
SELECT outlook_date, payload, created_at AS computed_at
FROM daily_outlook WHERE fn_is_approved()
ORDER BY outlook_date DESC LIMIT 1;

GRANT SELECT ON v_research_feed, v_factor_exposures, v_daily_outlook TO authenticated;
