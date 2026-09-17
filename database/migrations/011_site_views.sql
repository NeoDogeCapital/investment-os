-- 011_site_views.sql — Purpose-built read-only views for the live site,
-- allowlist table, and the policies that gate them.
--
-- The browser (Supabase JS, publishable key + Auth session) may touch ONLY
-- these views. Raw tables stay closed (see 010). Every view carries a
-- computed_at so each tab can display data freshness.
--
-- Access model: security_invoker = false (definer) views owned by postgres,
-- SELECT granted to authenticated only, and each view filters through
-- fn_is_approved() so only allowlisted auth users get rows.
-- APPLY AFTER 010. Requires owner approval (production change).

-- ── Allowlist ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS approved_users (
    user_id    uuid PRIMARY KEY,          -- auth.users.id
    email      text NOT NULL,
    label      text,                      -- 'niko', 'scott', ...
    created_at timestamptz DEFAULT now()
);
ALTER TABLE approved_users ENABLE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION fn_is_approved() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS
$$ SELECT EXISTS (SELECT 1 FROM approved_users WHERE user_id = auth.uid()) $$;

-- job heartbeat (written by cloud jobs via service role; read via view below)
CREATE TABLE IF NOT EXISTS job_runs (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_name    text NOT NULL,
    market_date date NOT NULL,
    status      text NOT NULL CHECK (status IN ('ok','error')),
    detail      text,
    run_at      timestamptz DEFAULT now()
);
ALTER TABLE job_runs ENABLE ROW LEVEL SECURITY;

-- memo content moves into the DB so the site can render it live
CREATE TABLE IF NOT EXISTS regime_memos (
    memo_date   date PRIMARY KEY,
    html        text NOT NULL,
    created_at  timestamptz DEFAULT now()
);
ALTER TABLE regime_memos ENABLE ROW LEVEL SECURITY;

-- ── Views ──────────────────────────────────────────────────────────────────

CREATE OR REPLACE VIEW v_latest_holdings AS
WITH latest AS (
  SELECT model_name, MAX(snapshot_date) AS sd
  FROM model_holdings_snapshot GROUP BY model_name
)
SELECT h.model_name, h.ticker, h.fund_name, h.weight,
       h.snapshot_date AS as_of, h.created_at AS computed_at
FROM model_holdings_snapshot h
JOIN latest l ON l.model_name = h.model_name AND l.sd = h.snapshot_date
WHERE fn_is_approved();

CREATE OR REPLACE VIEW v_regime_current AS
SELECT stack_date, short_term_score, short_term_label, short_term_trend,
       short_term_confidence, medium_term_score, medium_term_label,
       medium_term_trend, medium_term_confidence, long_term_score,
       long_term_label, long_term_trend, long_term_confidence,
       stack_alignment, max_tier_eligible, long_term_cycle_position,
       ai_interpretation, created_at AS computed_at
FROM regime_stack WHERE is_current = TRUE AND fn_is_approved();

CREATE OR REPLACE VIEW v_regime_history AS
SELECT stack_date, short_term_score, medium_term_score, long_term_score,
       stack_alignment, max_tier_eligible, created_at AS computed_at
FROM regime_stack WHERE fn_is_approved()
ORDER BY stack_date;

CREATE OR REPLACE VIEW v_model_analytics AS
SELECT DISTINCT ON (model_name)
       model_name, snapshot_date AS as_of, ytd_return, sharpe_ratio,
       sortino_ratio, max_drawdown, std_dev_annualized, batting_average,
       slugging_pct, return_1m, return_3m, return_1y, beta_to_spy,
       created_at AS computed_at
FROM analytics_snapshots WHERE fn_is_approved()
ORDER BY model_name, snapshot_date DESC, created_at DESC;

CREATE OR REPLACE VIEW v_latest_prices AS
SELECT DISTINCT ON (ticker) ticker, price_date AS as_of, close_price,
       daily_return, fetched_at AS computed_at
FROM ticker_price_history WHERE fn_is_approved()
ORDER BY ticker, price_date DESC;

CREATE OR REPLACE VIEW v_trade_log AS
SELECT created_at::date AS trade_date, ticker, proposed_direction, decision_type,
       proposed_allocation, thesis_summary, gate_passed
FROM decision_journal WHERE fn_is_approved()
ORDER BY created_at DESC;

CREATE OR REPLACE VIEW v_latest_memo AS
SELECT memo_date, html, created_at AS computed_at
FROM regime_memos WHERE fn_is_approved()
ORDER BY memo_date DESC LIMIT 1;

CREATE OR REPLACE VIEW v_job_health AS
SELECT DISTINCT ON (job_name) job_name, market_date, status, detail, run_at
FROM job_runs WHERE fn_is_approved()
ORDER BY job_name, run_at DESC;

-- ── Grants: authenticated may SELECT the views; anon gets nothing ──────────
GRANT SELECT ON v_latest_holdings, v_regime_current, v_regime_history,
               v_model_analytics, v_latest_prices, v_trade_log,
               v_latest_memo, v_job_health
TO authenticated;

-- Column names verified against live schema 2026-09-17.
