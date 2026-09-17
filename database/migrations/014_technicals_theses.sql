-- 014_technicals_theses.sql — Fund/macro technicals, theses, price bars views.
CREATE TABLE IF NOT EXISTS fund_technicals (
    score_date date NOT NULL, ticker text NOT NULL, is_macro boolean DEFAULT false,
    price numeric, trend text, track_line numeric, ma50 numeric, ma200 numeric,
    rsi numeric, rsi_regime text, vol20 numeric,
    off_high_52w numeric, off_low_52w numeric,
    avwap_ytd numeric, avwap_52w_low numeric, rel_strength_63d numeric,
    range_1w_lo numeric, range_1w_hi numeric, range_1m_lo numeric, range_1m_hi numeric,
    pos_in_range_1m numeric, timing text,
    created_at timestamptz DEFAULT now(),
    PRIMARY KEY (score_date, ticker)
);
ALTER TABLE fund_technicals ENABLE ROW LEVEL SECURITY;

CREATE OR REPLACE VIEW v_technicals AS
SELECT DISTINCT ON (ticker) ticker, is_macro, price, trend, track_line, ma50, ma200,
       rsi, rsi_regime, vol20, off_high_52w, off_low_52w, avwap_ytd, avwap_52w_low,
       rel_strength_63d, range_1w_lo, range_1w_hi, range_1m_lo, range_1m_hi,
       pos_in_range_1m, timing, score_date, created_at AS computed_at
FROM fund_technicals WHERE fn_is_approved()
ORDER BY ticker, score_date DESC;

CREATE OR REPLACE VIEW v_price_bars AS
SELECT ticker, price_date, open_price, high_price, low_price, close_price, volume
FROM ticker_price_history
WHERE fn_is_approved() AND price_date >= CURRENT_DATE - INTERVAL '430 days';

CREATE OR REPLACE VIEW v_theses AS
SELECT DISTINCT ON (ticker) ticker, created_at::date AS thesis_date, decision_type,
       proposed_allocation, thesis_summary, decision_rationale, gate_passed
FROM decision_journal
WHERE fn_is_approved() AND thesis_summary IS NOT NULL
ORDER BY ticker, created_at DESC;

GRANT SELECT ON v_technicals, v_price_bars, v_theses TO authenticated;
