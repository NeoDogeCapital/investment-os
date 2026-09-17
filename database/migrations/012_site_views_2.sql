-- 012_site_views_2.sql — Performance series + model registry views for the
-- live site. Additive to 011; same access model.
-- Moves AUM into the database (was config/aum.json on Niko's Mac only), so
-- the site and cloud jobs share one source of truth for firm weights.

ALTER TABLE model_portfolios ADD COLUMN IF NOT EXISTS aum_usd numeric;

UPDATE model_portfolios SET aum_usd = v.aum FROM (VALUES
  ('Liquid Core', 60000000), ('Conservative Core', 37000000),
  ('Income Real Return', 127000000), ('Flex IRR', 19000000),
  ('Balanced Core', 60000000), ('Tax Aware Balanced', 29000000),
  ('Diversified Growth', 51000000)
) AS v(name, aum) WHERE model_portfolios.name = v.name;

CREATE OR REPLACE VIEW v_models AS
SELECT name, benchmark_label, inception_date, aum_usd
FROM model_portfolios
WHERE active AND name != 'IWP Model Portfolio' AND fn_is_approved();

CREATE OR REPLACE VIEW v_performance_history AS
SELECT model_name, snapshot_date, ytd_return, sharpe_ratio, max_drawdown,
       created_at AS computed_at
FROM analytics_snapshots WHERE fn_is_approved()
ORDER BY model_name, snapshot_date;

GRANT SELECT ON v_models, v_performance_history TO authenticated;
