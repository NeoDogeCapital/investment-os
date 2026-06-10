-- 008_models.sql
-- Model portfolio history, performance tracking, and benchmarks

-- ─── Model portfolios ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS model_portfolios (
    id              TEXT PRIMARY KEY,           -- e.g. 'iwp_main'
    name            TEXT NOT NULL,
    description     TEXT,
    inception_date  DATE,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO model_portfolios (id, name, description, inception_date)
VALUES ('iwp_main', 'IWP Model Portfolio',
        'Primary IWP model allocation history, imported from iwp models old.csv',
        '2020-02-03')
ON CONFLICT (id) DO NOTHING;

-- ─── Allocation snapshots ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS model_allocations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    portfolio_id    TEXT NOT NULL REFERENCES model_portfolios(id) ON DELETE CASCADE,
    snapshot_date   DATE NOT NULL,
    ticker          TEXT NOT NULL,
    instrument_name TEXT,
    weight          NUMERIC(8,6) NOT NULL CHECK (weight >= 0 AND weight <= 1),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_model_alloc_unique
    ON model_allocations (portfolio_id, snapshot_date, ticker);

CREATE INDEX IF NOT EXISTS idx_model_alloc_date
    ON model_allocations (portfolio_id, snapshot_date DESC);

CREATE INDEX IF NOT EXISTS idx_model_alloc_ticker
    ON model_allocations (ticker);

-- ─── Ticker price cache ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS price_cache (
    ticker          TEXT NOT NULL,
    price_date      DATE NOT NULL,
    close_price     NUMERIC(14,6) NOT NULL,
    source          TEXT NOT NULL DEFAULT 'yfinance',
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ticker, price_date)
);

CREATE INDEX IF NOT EXISTS idx_price_cache_ticker ON price_cache (ticker, price_date DESC);

-- ─── Snapshot performance ─────────────────────────────────────────────────────
-- One row per snapshot period (from this snapshot_date to the next snapshot_date)
CREATE TABLE IF NOT EXISTS model_snapshot_performance (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    portfolio_id        TEXT NOT NULL REFERENCES model_portfolios(id),
    snapshot_date       DATE NOT NULL,          -- start of this period
    next_snapshot_date  DATE,                   -- end of period (null = current/open)
    period_days         INTEGER,
    -- Portfolio return this period
    period_return       NUMERIC(10,6),          -- e.g. 0.0342 = +3.42%
    -- Benchmark returns same period
    spy_return          NUMERIC(10,6),
    agg_return          NUMERIC(10,6),          -- BND as bond proxy
    blend_6040_return   NUMERIC(10,6),          -- 60% SPY + 40% BND
    -- vs benchmark
    excess_vs_spy       NUMERIC(10,6),
    excess_vs_6040      NUMERIC(10,6),
    -- Cumulative from inception
    cumulative_return   NUMERIC(10,6),
    spy_cumulative      NUMERIC(10,6),
    blend_cumulative    NUMERIC(10,6),
    -- Regime at snapshot
    regime_at_snapshot  TEXT,
    -- Which tickers had prices available
    tickers_priced      INTEGER,
    tickers_total       INTEGER,
    weight_covered      NUMERIC(8,6),           -- fraction of portfolio with price data
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (portfolio_id, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_snap_perf_date ON model_snapshot_performance (portfolio_id, snapshot_date DESC);

-- ─── Position-level returns ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS model_position_returns (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    portfolio_id    TEXT NOT NULL REFERENCES model_portfolios(id),
    snapshot_date   DATE NOT NULL,
    ticker          TEXT NOT NULL,
    weight          NUMERIC(8,6),
    price_start     NUMERIC(14,6),
    price_end       NUMERIC(14,6),
    position_return NUMERIC(10,6),              -- raw return for this ticker
    contribution    NUMERIC(10,6),              -- weight × return
    UNIQUE (portfolio_id, snapshot_date, ticker)
);

CREATE INDEX IF NOT EXISTS idx_pos_ret_date   ON model_position_returns (portfolio_id, snapshot_date DESC);
CREATE INDEX IF NOT EXISTS idx_pos_ret_ticker ON model_position_returns (ticker);

-- ─── Row Level Security ─────────────────────────────────────────────────────
ALTER TABLE public.model_portfolios ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.model_allocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.price_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.model_snapshot_performance ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.model_position_returns ENABLE ROW LEVEL SECURITY;
