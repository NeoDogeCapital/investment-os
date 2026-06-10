-- 009_analytics.sql
-- Analytics engine tables

-- ─── Daily price history (primary cache, replaces/extends price_cache) ────────
CREATE TABLE IF NOT EXISTS ticker_price_history (
    ticker          TEXT        NOT NULL,
    price_date      DATE        NOT NULL,
    open_price      NUMERIC(16,6),
    high_price      NUMERIC(16,6),
    low_price       NUMERIC(16,6),
    close_price     NUMERIC(16,6) NOT NULL,
    adj_close       NUMERIC(16,6),
    volume          BIGINT,
    daily_return    NUMERIC(12,8),   -- (close - prev_close) / prev_close
    source          TEXT NOT NULL DEFAULT 'yfinance',
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ticker, price_date)
);
CREATE INDEX IF NOT EXISTS idx_tph_ticker ON ticker_price_history (ticker, price_date DESC);
CREATE INDEX IF NOT EXISTS idx_tph_date   ON ticker_price_history (price_date DESC);

-- ─── Model daily return series ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS model_daily_returns (
    portfolio_id        TEXT    NOT NULL,
    return_date         DATE    NOT NULL,
    model_return        NUMERIC(12,8),   -- weighted model return
    benchmark_return    NUMERIC(12,8),   -- benchmark return same day
    excess_return       NUMERIC(12,8),
    spy_return          NUMERIC(12,8),
    agg_return          NUMERIC(12,8),
    risk_free_rate      NUMERIC(12,8),   -- annualized daily ^IRX / 252
    snapshot_date_used  DATE,            -- which allocation snapshot was active
    PRIMARY KEY (portfolio_id, return_date)
);
CREATE INDEX IF NOT EXISTS idx_mdr_pid  ON model_daily_returns (portfolio_id, return_date DESC);
CREATE INDEX IF NOT EXISTS idx_mdr_date ON model_daily_returns (return_date DESC);

-- ─── Comprehensive analytics results ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS model_analytics (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    portfolio_id                TEXT NOT NULL,
    calc_date                   DATE NOT NULL,
    period_start                DATE NOT NULL,
    period_end                  DATE NOT NULL,
    period_label                TEXT NOT NULL,   -- 'YTD','1Y','3Y','ITD','2024','stress_covid', etc.

    -- Returns
    total_return                NUMERIC(12,6),
    annualized_return           NUMERIC(12,6),
    benchmark_total_return      NUMERIC(12,6),
    benchmark_annualized        NUMERIC(12,6),
    excess_return               NUMERIC(12,6),
    information_ratio           NUMERIC(12,6),

    -- Rolling returns
    return_1m                   NUMERIC(12,6),
    return_3m                   NUMERIC(12,6),
    return_6m                   NUMERIC(12,6),
    return_1y                   NUMERIC(12,6),
    return_3y_ann               NUMERIC(12,6),

    -- Risk
    volatility_ann              NUMERIC(12,6),
    downside_deviation          NUMERIC(12,6),
    max_drawdown                NUMERIC(12,6),
    max_drawdown_duration_days  INTEGER,
    var_95                      NUMERIC(12,6),
    var_99                      NUMERIC(12,6),
    cvar_95                     NUMERIC(12,6),
    cvar_99                     NUMERIC(12,6),

    -- Risk-adjusted
    sharpe_ratio                NUMERIC(12,6),
    sortino_ratio               NUMERIC(12,6),
    calmar_ratio                NUMERIC(12,6),
    omega_ratio                 NUMERIC(12,6),

    -- Batting stats
    batting_average             NUMERIC(8,4),
    up_capture                  NUMERIC(12,6),
    down_capture                NUMERIC(12,6),
    up_down_capture             NUMERIC(12,6),
    win_loss_ratio              NUMERIC(12,6),
    slugging_pct                NUMERIC(12,6),

    -- Best / worst
    best_month                  NUMERIC(12,6),
    best_month_date             DATE,
    worst_month                 NUMERIC(12,6),
    worst_month_date            DATE,
    best_quarter                NUMERIC(12,6),
    best_quarter_label          TEXT,
    worst_quarter               NUMERIC(12,6),
    worst_quarter_label         TEXT,

    -- Portfolio construction
    hhi_concentration           NUMERIC(10,6),
    effective_n_positions       NUMERIC(10,4),
    beta_spy                    NUMERIC(10,6),
    beta_agg                    NUMERIC(10,6),
    beta_gld                    NUMERIC(10,6),

    -- Calendar year returns (stored as JSONB for flexibility)
    calendar_year_returns       JSONB,

    -- YTD-specific fields
    ytd_return                  NUMERIC(12,6),
    ytd_benchmark_return        NUMERIC(12,6),
    ytd_excess                  NUMERIC(12,6),
    ytd_sharpe                  NUMERIC(12,6),
    ytd_max_drawdown            NUMERIC(12,6),
    ytd_batting_average         NUMERIC(8,4),
    ytd_best_month              NUMERIC(12,6),
    ytd_worst_month             NUMERIC(12,6),
    ytd_vs_prior_year           NUMERIC(12,6),   -- same period prior year

    -- Metadata
    data_points                 INTEGER,
    tickers_priced              INTEGER,
    tickers_missing             TEXT[],
    risk_free_rate_avg          NUMERIC(12,6),
    computed_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (portfolio_id, calc_date, period_label)
);
CREATE INDEX IF NOT EXISTS idx_ma_pid    ON model_analytics (portfolio_id, calc_date DESC);
CREATE INDEX IF NOT EXISTS idx_ma_period ON model_analytics (period_label);

-- ─── Stress test results ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS stress_test_results (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    portfolio_id        TEXT NOT NULL,
    scenario_name       TEXT NOT NULL,
    scenario_start      DATE NOT NULL,
    scenario_end        DATE NOT NULL,
    model_return        NUMERIC(12,6),
    benchmark_return    NUMERIC(12,6),
    spy_return          NUMERIC(12,6),
    excess_return       NUMERIC(12,6),
    max_drawdown        NUMERIC(12,6),
    volatility          NUMERIC(12,6),
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (portfolio_id, scenario_name)
);
CREATE INDEX IF NOT EXISTS idx_str_pid ON stress_test_results (portfolio_id);
CREATE INDEX IF NOT EXISTS idx_str_scn ON stress_test_results (scenario_name);

-- ─── Regime performance ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS regime_performance (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    portfolio_id        TEXT NOT NULL,
    regime              TEXT NOT NULL CHECK (regime IN ('RISK_ON','NEUTRAL','RISK_OFF')),
    calc_date           DATE NOT NULL,
    periods_count       INTEGER,
    total_days          INTEGER,
    avg_monthly_return  NUMERIC(12,6),
    ann_return          NUMERIC(12,6),
    ann_volatility      NUMERIC(12,6),
    sharpe_ratio        NUMERIC(12,6),
    max_drawdown        NUMERIC(12,6),
    batting_average     NUMERIC(8,4),
    benchmark_avg_return NUMERIC(12,6),
    excess_vs_benchmark  NUMERIC(12,6),
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (portfolio_id, regime, calc_date)
);
CREATE INDEX IF NOT EXISTS idx_rp_pid ON regime_performance (portfolio_id);

-- ─── Row Level Security ─────────────────────────────────────────────────────
ALTER TABLE public.ticker_price_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.model_daily_returns ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.model_analytics ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stress_test_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.regime_performance ENABLE ROW LEVEL SECURITY;
