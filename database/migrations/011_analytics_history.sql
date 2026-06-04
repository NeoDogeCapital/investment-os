-- 011_analytics_history.sql
-- Permanent analytics snapshots — every run creates a new row, never overwritten

-- ─── Per-model analytics snapshots ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS analytics_snapshots (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model_name              TEXT NOT NULL,
    portfolio_id            TEXT NOT NULL,
    snapshot_date           DATE NOT NULL DEFAULT CURRENT_DATE,
    period_label            TEXT NOT NULL DEFAULT 'YTD',  -- 'YTD 2026', 'Q1 2026', 'ITD', etc.
    benchmark_name          TEXT,

    -- Return metrics
    ytd_return              NUMERIC(10,6),
    ytd_vs_benchmark        NUMERIC(10,6),
    ytd_excess_return       NUMERIC(10,6),
    ytd_sharpe              NUMERIC(10,6),
    ytd_max_drawdown        NUMERIC(10,6),
    ytd_batting_average     NUMERIC(8,4),
    return_1m               NUMERIC(10,6),
    return_3m               NUMERIC(10,6),
    return_6m               NUMERIC(10,6),
    return_1y               NUMERIC(10,6),
    return_3y               NUMERIC(10,6),
    annualized_return_itd   NUMERIC(10,6),
    benchmark_ytd           NUMERIC(10,6),
    benchmark_1y            NUMERIC(10,6),
    benchmark_sharpe        NUMERIC(10,6),

    -- Risk metrics
    std_dev_annualized      NUMERIC(10,6),
    downside_deviation      NUMERIC(10,6),
    max_drawdown            NUMERIC(10,6),
    max_drawdown_duration   INTEGER,
    var_95                  NUMERIC(10,6),
    var_99                  NUMERIC(10,6),
    cvar_95                 NUMERIC(10,6),
    rolling_vol_30d         NUMERIC(10,6),
    rolling_vol_60d         NUMERIC(10,6),
    rolling_vol_90d         NUMERIC(10,6),

    -- Risk-adjusted
    sharpe_ratio            NUMERIC(10,6),
    sortino_ratio           NUMERIC(10,6),
    calmar_ratio            NUMERIC(10,6),
    information_ratio       NUMERIC(10,6),
    omega_ratio             NUMERIC(10,6),

    -- Batting stats
    batting_average         NUMERIC(8,4),
    up_capture              NUMERIC(10,6),
    down_capture            NUMERIC(10,6),
    updown_capture_ratio    NUMERIC(10,6),
    win_loss_ratio          NUMERIC(10,6),
    slugging_pct            NUMERIC(10,6),

    -- Portfolio construction
    num_holdings            INTEGER,
    effective_n_positions   NUMERIC(10,4),
    hhi_concentration       NUMERIC(10,6),
    beta_to_spy             NUMERIC(10,6),
    beta_to_agg             NUMERIC(10,6),

    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_as_model   ON analytics_snapshots (portfolio_id, snapshot_date DESC);
CREATE INDEX IF NOT EXISTS idx_as_date    ON analytics_snapshots (snapshot_date DESC);
CREATE INDEX IF NOT EXISTS idx_as_period  ON analytics_snapshots (period_label);

-- ─── Firm-wide snapshots ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS firm_wide_snapshots (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    snapshot_date           DATE NOT NULL DEFAULT CURRENT_DATE,
    models_included         INTEGER NOT NULL DEFAULT 7,

    -- Weighted firm-level metrics
    firm_ytd_return         NUMERIC(10,6),
    firm_ytd_vs_benchmark   NUMERIC(10,6),
    firm_sharpe             NUMERIC(10,6),
    firm_sortino            NUMERIC(10,6),
    firm_calmar             NUMERIC(10,6),
    firm_max_drawdown       NUMERIC(10,6),
    firm_batting_average    NUMERIC(8,4),
    firm_up_capture         NUMERIC(10,6),
    firm_down_capture       NUMERIC(10,6),

    -- Best / worst model this period
    best_model_ytd          TEXT,
    best_model_ytd_return   NUMERIC(10,6),
    worst_model_ytd         TEXT,
    worst_model_ytd_return  NUMERIC(10,6),

    -- Per-model YTD (JSONB for flexibility)
    model_ytd_returns       JSONB DEFAULT '{}',
    model_sharpes           JSONB DEFAULT '{}',

    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fws_date ON firm_wide_snapshots (snapshot_date DESC);

-- ─── Holdings snapshots ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS model_holdings_snapshot (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model_name          TEXT NOT NULL,
    portfolio_id        TEXT NOT NULL,
    snapshot_date       DATE NOT NULL DEFAULT CURRENT_DATE,
    ticker              TEXT NOT NULL,
    fund_name           TEXT,
    weight              NUMERIC(8,6),
    ytd_contribution    NUMERIC(10,6),   -- weight × position YTD return
    position_ytd_return NUMERIC(10,6),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mhs_model ON model_holdings_snapshot (portfolio_id, snapshot_date DESC);
CREATE INDEX IF NOT EXISTS idx_mhs_date  ON model_holdings_snapshot (snapshot_date DESC);
CREATE INDEX IF NOT EXISTS idx_mhs_tick  ON model_holdings_snapshot (ticker);
