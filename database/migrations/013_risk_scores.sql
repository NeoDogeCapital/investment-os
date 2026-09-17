-- 013_risk_scores.sql — PRISM-style risk scores table + site view.
CREATE TABLE IF NOT EXISTS risk_scores (
    score_date      date NOT NULL,
    model_name      text NOT NULL,
    risk_score      numeric,        -- 1..10 composite
    s_volatility    numeric, s_var numeric, s_concentration numeric, s_structure numeric,
    ann_volatility  numeric,        -- recency-weighted, annualized
    var95_daily     numeric,        -- fraction of NAV
    var95_dollars   numeric,
    tail_freq       numeric,        -- share of days beyond 2 sigma
    top_position_w  numeric, hhi numeric, effective_n numeric, cash_weight numeric,
    aum_usd         numeric,
    created_at      timestamptz DEFAULT now(),
    PRIMARY KEY (score_date, model_name)
);
ALTER TABLE risk_scores ENABLE ROW LEVEL SECURITY;

CREATE OR REPLACE VIEW v_risk AS
SELECT DISTINCT ON (model_name) score_date, model_name, risk_score,
       s_volatility, s_var, s_concentration, s_structure,
       ann_volatility, var95_daily, var95_dollars, tail_freq,
       top_position_w, hhi, effective_n, cash_weight, aum_usd,
       created_at AS computed_at
FROM risk_scores WHERE fn_is_approved()
ORDER BY model_name, score_date DESC;

GRANT SELECT ON v_risk TO authenticated;
