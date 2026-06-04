#!/usr/bin/env python3
"""
analytics_engine.py — Comprehensive portfolio analytics for all 7 IWP models.

Usage:
    python scripts/analytics_engine.py --all
    python scripts/analytics_engine.py --model "Diversified Growth"
    python scripts/analytics_engine.py --stress
    python scripts/analytics_engine.py --refresh-prices
    python scripts/analytics_engine.py --ytd
"""

import os, sys, json, math, warnings, argparse, logging
from datetime import date, datetime, timedelta
from pathlib import Path
from collections import defaultdict

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

import numpy as np
import pandas as pd
from scipy import stats
import psycopg2, psycopg2.extras
import yfinance as yf
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
import plotly.io as pio

PROJECT_DIR = Path(__file__).parent.parent

def load_env():
    for line in (PROJECT_DIR / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))
load_env()

DB_URL      = os.environ["DATABASE_URL"]
REPORTS_DIR = PROJECT_DIR / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

NAVY   = "#faf9f4"
NAVY2  = "#3a1f1d"
NAVY3  = "#8f3d2c"
GOLD   = "#8f3d2c"
GOLD2  = "#3a1f1d"
WHITE  = "#3a1f1d"
GRAY   = "#7a6055"
GREEN  = "#2e7d4f"
YELLOW = "#8a6e2a"
RED    = "#8f3d2c"
BLUE   = "#3d6080"

# ── Benchmark definitions per model ──────────────────────────────────────────
MODEL_BENCHMARKS = {
    "liquid_core":        [("AGG", 0.60), ("SPY", 0.40)],
    "conservative_core":  [("AGG", 0.80), ("SPY", 0.20)],
    "income_real_return": [("AGG", 0.50), ("SPY", 0.30), ("TIP", 0.20)],
    "flex_irr":           [("AGG", 0.50), ("SPY", 0.30), ("TIP", 0.20)],
    "balanced_core":      [("SPY", 0.60), ("AGG", 0.40)],
    "tax_aware_balanced": [("MUB", 0.60), ("SPY", 0.40)],
    "diversified_growth": [("SPY", 1.00)],
    "iwp_main":           [("SPY", 0.60), ("AGG", 0.40)],
    "__firmwide__":       [("SPY", 0.60), ("AGG", 0.40)],
}

STRESS_SCENARIOS = {
    "COVID Crash":       (date(2020, 2, 19), date(2020, 3, 23)),
    "2022 Rate Spike":   (date(2022, 1,  1), date(2022, 12, 31)),
    "2022 Q1":           (date(2022, 1,  1), date(2022, 3,  31)),
    "2020 Recovery":     (date(2020, 3, 23), date(2020, 12, 31)),
}

PRICE_START = date(2020, 1, 1)

# ═════════════════════════════════════════════════════════════════════════════
# DB helpers
# ═════════════════════════════════════════════════════════════════════════════

def get_db():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def run_migration(db):
    sql = (PROJECT_DIR / "database/migrations/009_analytics.sql").read_text()
    try:
        with db.cursor() as cur: cur.execute(sql)
        db.commit(); log.info("Migration 009 OK")
    except Exception as e:
        db.rollback()
        if "already exists" in str(e): log.info("Analytics tables already exist")
        else: raise

def load_portfolios(db):
    with db.cursor() as cur:
        cur.execute("""SELECT id, name, benchmark_ticker, allocation_profile
                       FROM model_portfolios WHERE id != 'iwp_main' ORDER BY name""")
        return cur.fetchall()

def load_allocations(db, portfolio_id):
    """Return DataFrame: snapshot_date, ticker, weight — sorted ascending."""
    with db.cursor() as cur:
        cur.execute("""SELECT snapshot_date, ticker, weight FROM model_allocations
                       WHERE portfolio_id=%s ORDER BY snapshot_date, ticker""",
                    (portfolio_id,))
        rows = cur.fetchall()
    if not rows: return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"])
    return df

# ═════════════════════════════════════════════════════════════════════════════
# Price fetching
# ═════════════════════════════════════════════════════════════════════════════

ALWAYS_FETCH = ["SPY","AGG","TIP","MUB","GLD","^IRX"]

def get_all_tickers(db):
    with db.cursor() as cur:
        cur.execute("SELECT DISTINCT ticker FROM model_allocations WHERE ticker != 'CASH'")
        tickers = {r["ticker"] for r in cur.fetchall()}
    tickers.update(ALWAYS_FETCH)
    for bm_list in MODEL_BENCHMARKS.values():
        for t, _ in bm_list: tickers.add(t)
    return sorted(tickers)

def fetch_prices(db, tickers, force_refresh=False):
    """Pull daily OHLCV from yfinance, cache to ticker_price_history. Returns price DataFrame."""
    if not force_refresh:
        with db.cursor() as cur:
            cur.execute("SELECT MAX(price_date) FROM ticker_price_history")
            r = cur.fetchone()
            last = r["max"] if r and r["max"] else None
        if last and (date.today() - last).days <= 1:
            log.info("Prices up to date (%s) — loading from cache", last)
            return _load_prices_from_db(db, tickers)

    log.info("Fetching prices for %d tickers from %s…", len(tickers), PRICE_START)
    missing = []
    frames  = {}

    # Batch download — yfinance handles this well
    batch = [t for t in tickers if t != "CASH"]
    try:
        raw = yf.download(batch, start=str(PRICE_START),
                          end=str(date.today() + timedelta(days=1)),
                          auto_adjust=True, progress=False, threads=True)
    except Exception as e:
        log.error("yfinance batch download failed: %s", e)
        return pd.DataFrame()

    if raw.empty:
        log.warning("yfinance returned empty DataFrame")
        return pd.DataFrame()

    close = raw["Close"] if "Close" in raw.columns else raw
    if isinstance(close, pd.Series): close = close.to_frame()

    rows_to_insert = []
    for ticker in batch:
        col = ticker if ticker in close.columns else None
        # Handle multi-level columns (single ticker case)
        if col is None:
            log.warning("  %-10s NO DATA from yfinance — skipping", ticker)
            missing.append(ticker)
            continue
        series = close[col].dropna()
        if series.empty:
            log.warning("  %-10s empty series — skipping", ticker)
            missing.append(ticker)
            continue
        prev = None
        for idx, price in series.items():
            d = idx.date() if hasattr(idx, "date") else idx
            ret = (price - prev) / prev if prev and prev > 0 else None
            rows_to_insert.append((ticker, d, float(price), float(ret) if ret is not None else None))
            prev = float(price)
        log.info("  %-12s %d days", ticker, len(series))

    if rows_to_insert:
        with db.cursor() as cur:
            psycopg2.extras.execute_values(cur, """
                INSERT INTO ticker_price_history (ticker, price_date, close_price, daily_return)
                VALUES %s ON CONFLICT (ticker, price_date)
                DO UPDATE SET close_price=EXCLUDED.close_price, daily_return=EXCLUDED.daily_return,
                              fetched_at=NOW()
            """, rows_to_insert, page_size=2000)
        db.commit()
        log.info("Cached %d price rows. Missing: %s", len(rows_to_insert), missing or "none")

    return _load_prices_from_db(db, tickers)

def _load_prices_from_db(db, tickers):
    with db.cursor() as cur:
        cur.execute("""SELECT ticker, price_date, close_price FROM ticker_price_history
                       WHERE ticker = ANY(%s) AND price_date >= %s ORDER BY price_date""",
                    (list(tickers), PRICE_START))
        rows = cur.fetchall()
    if not rows: return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    df["price_date"]  = pd.to_datetime(df["price_date"])
    df["close_price"] = df["close_price"].astype(float)
    pivot = df.pivot(index="price_date", columns="ticker", values="close_price")
    pivot.columns.name = None
    return pivot.astype(float)

# ═════════════════════════════════════════════════════════════════════════════
# Risk-free rate
# ═════════════════════════════════════════════════════════════════════════════

def get_risk_free_rate(prices_df):
    """^IRX is annualized %. Convert to daily and monthly."""
    if "^IRX" not in prices_df.columns:
        return 0.04  # fallback 4%
    irx = prices_df["^IRX"].dropna() / 100
    return float(irx.iloc[-1]) if not irx.empty else 0.04

# ═════════════════════════════════════════════════════════════════════════════
# Model return series construction
# ═════════════════════════════════════════════════════════════════════════════

def build_model_returns(portfolio_id, alloc_df, prices_df):
    """
    Build daily model return series.
    Uses the most recent snapshot weight as of each date.
    Returns daily return Series indexed by date.
    """
    if alloc_df.empty or prices_df.empty:
        return pd.Series(dtype=float)

    dates = prices_df.index
    snap_dates = sorted(alloc_df["snapshot_date"].unique())

    daily_returns = []
    for i in range(1, len(dates)):
        d = dates[i]
        # Find most recent snapshot ≤ d
        active_snap = None
        for sd in snap_dates:
            if sd <= d: active_snap = sd
            else: break
        if active_snap is None: continue

        holdings = alloc_df[alloc_df["snapshot_date"] == active_snap]
        r = 0.0
        total_w = 0.0
        for _, row in holdings.iterrows():
            ticker = str(row["ticker"])
            w = float(row["weight"])
            if ticker == "CASH": continue
            if ticker not in prices_df.columns: continue
            p0 = prices_df[ticker].iloc[i-1]
            p1 = prices_df[ticker].iloc[i]
            p0, p1 = float(p0), float(p1)
            if pd.isna(p0) or pd.isna(p1) or p0 <= 0: continue
            r += w * (p1 - p0) / p0
            total_w += w

        # Scale for missing coverage (CASH fills the rest at 0%)
        daily_returns.append((d, r))

    if not daily_returns: return pd.Series(dtype=float)
    s = pd.Series([r for _, r in daily_returns],
                  index=pd.DatetimeIndex([d for d, _ in daily_returns]))
    return s

def build_benchmark_returns(portfolio_id, prices_df):
    """Build benchmark daily return series for a given model."""
    bm_components = MODEL_BENCHMARKS.get(portfolio_id, [("SPY", 0.60), ("AGG", 0.40)])
    ret = pd.Series(0.0, index=prices_df.index)
    total_w = 0
    for ticker, w in bm_components:
        if ticker not in prices_df.columns:
            log.warning("Benchmark ticker %s not in prices — skipping", ticker)
            continue
        p = prices_df[ticker].pct_change()
        ret = ret + w * p.reindex(ret.index, fill_value=0)
        total_w += w
    if total_w > 0 and total_w < 0.99:
        ret = ret / total_w
    return ret.dropna()

# ═════════════════════════════════════════════════════════════════════════════
# Core analytics functions (all on monthly returns for consistency)
# ═════════════════════════════════════════════════════════════════════════════

def to_monthly(daily: pd.Series) -> pd.Series:
    return (1 + daily).resample("ME").prod() - 1

def total_return(monthly: pd.Series) -> float:
    if monthly.empty: return np.nan
    return float((1 + monthly).prod() - 1)

def annualized_return(monthly: pd.Series) -> float:
    n = len(monthly)
    if n == 0: return np.nan
    tr = (1 + monthly).prod()
    return float(tr ** (12 / n) - 1)

def annualized_vol(monthly: pd.Series) -> float:
    if len(monthly) < 2: return np.nan
    return float(monthly.std() * np.sqrt(12))

def downside_deviation(monthly: pd.Series, threshold=0.0) -> float:
    neg = monthly[monthly < threshold] - threshold
    if len(neg) < 2: return np.nan
    return float(np.sqrt((neg**2).mean()) * np.sqrt(12))

def max_drawdown(monthly: pd.Series):
    if monthly.empty: return np.nan, 0
    cum = (1 + monthly).cumprod()
    roll_max = cum.cummax()
    dd = (cum - roll_max) / roll_max
    mdd = float(dd.min())
    # Duration: find longest stretch underwater
    underwater = (dd < 0)
    max_dur = 0
    cur_dur = 0
    for v in underwater:
        if v: cur_dur += 1
        else: cur_dur = 0
        max_dur = max(max_dur, cur_dur)
    return mdd, max_dur

def var_cvar(monthly: pd.Series, confidence=0.95):
    if len(monthly) < 5: return np.nan, np.nan
    sorted_r = np.sort(monthly)
    idx = int((1 - confidence) * len(sorted_r))
    var  = float(sorted_r[idx])
    cvar = float(sorted_r[:max(idx,1)].mean())
    return var, cvar

def sharpe(monthly: pd.Series, rf_annual=0.04) -> float:
    if len(monthly) < 2: return np.nan
    rf_monthly = (1 + rf_annual) ** (1/12) - 1
    excess = monthly - rf_monthly
    if excess.std() == 0: return np.nan
    return float(excess.mean() / excess.std() * np.sqrt(12))

def sortino(monthly: pd.Series, rf_annual=0.04) -> float:
    if len(monthly) < 2: return np.nan
    rf_monthly = (1 + rf_annual) ** (1/12) - 1
    excess = monthly - rf_monthly
    dd = downside_deviation(monthly)
    if dd is None or dd == 0 or np.isnan(dd): return np.nan
    return float(annualized_return(monthly) / dd)

def calmar(monthly: pd.Series) -> float:
    ann = annualized_return(monthly)
    mdd, _ = max_drawdown(monthly)
    if mdd == 0 or np.isnan(mdd): return np.nan
    return float(ann / abs(mdd))

def information_ratio(model: pd.Series, bench: pd.Series) -> float:
    aligned = pd.concat([model, bench], axis=1).dropna()
    if len(aligned) < 2: return np.nan
    excess = aligned.iloc[:,0] - aligned.iloc[:,1]
    if excess.std() == 0: return np.nan
    return float(excess.mean() / excess.std() * np.sqrt(12))

def omega_ratio(monthly: pd.Series, threshold=0.0) -> float:
    gains  = monthly[monthly > threshold] - threshold
    losses = threshold - monthly[monthly < threshold]
    if losses.sum() == 0: return np.nan
    return float(gains.sum() / losses.sum())

def batting_average(monthly: pd.Series) -> float:
    if monthly.empty: return np.nan
    return float((monthly > 0).mean())

def up_down_capture(model: pd.Series, bench: pd.Series):
    aligned = pd.concat([model, bench], axis=1).dropna()
    aligned.columns = ["m","b"]
    up_b   = aligned[aligned["b"] > 0]
    down_b = aligned[aligned["b"] < 0]
    up_cap   = ((1+up_b["m"]).prod()   / (1+up_b["b"]).prod())   ** (12/max(len(up_b),1))   - 1 if len(up_b)   > 0 else np.nan
    down_cap = ((1+down_b["m"]).prod() / (1+down_b["b"]).prod()) ** (12/max(len(down_b),1)) - 1 if len(down_b) > 0 else np.nan
    # Standard formula: geometric avg return in up/down markets
    up_cap2   = float(up_b["m"].mean()   / up_b["b"].mean())   if len(up_b)   > 0 and up_b["b"].mean()   != 0 else np.nan
    down_cap2 = float(down_b["m"].mean() / down_b["b"].mean()) if len(down_b) > 0 and down_b["b"].mean() != 0 else np.nan
    ratio = up_cap2 / down_cap2 if (down_cap2 and down_cap2 != 0 and not np.isnan(down_cap2)) else np.nan
    return up_cap2, down_cap2, ratio

def slugging(monthly: pd.Series) -> float:
    wins  = monthly[monthly > 0]
    losses = monthly[monthly < 0]
    if losses.empty or losses.mean() == 0: return np.nan
    return float(wins.mean() / abs(losses.mean())) if not wins.empty else np.nan

def win_loss_ratio(monthly: pd.Series):
    wins   = (monthly > 0).sum()
    losses = (monthly < 0).sum()
    return float(wins / losses) if losses > 0 else np.nan

def beta(model_daily: pd.Series, factor_daily: pd.Series) -> float:
    aligned = pd.concat([model_daily, factor_daily], axis=1).dropna()
    if len(aligned) < 10: return np.nan
    cov = np.cov(aligned.iloc[:,0], aligned.iloc[:,1])
    var = np.var(aligned.iloc[:,1])
    return float(cov[0,1] / var) if var > 0 else np.nan

def hhi(weights: list) -> float:
    w = np.array(weights)
    return float((w**2).sum())

def effective_n(weights: list) -> float:
    h = hhi(weights)
    return float(1/h) if h > 0 else np.nan

def calendar_year_returns(monthly: pd.Series) -> dict:
    out = {}
    for yr, grp in monthly.groupby(monthly.index.year):
        out[str(yr)] = round(float((1+grp).prod() - 1), 6)
    return out

def quarterly_extremes(monthly: pd.Series):
    q = (1 + monthly).resample("QE").prod() - 1
    if q.empty: return np.nan, "", np.nan, ""
    best_q  = q.idxmax(); best_v  = float(q.max())
    worst_q = q.idxmin(); worst_v = float(q.min())
    def qlabel(d): return f"{d.year} Q{(d.month-1)//3+1}"
    return best_v, qlabel(best_q), worst_v, qlabel(worst_q)

def rolling_vol(daily: pd.Series, window=60) -> pd.Series:
    return daily.rolling(window).std() * np.sqrt(252)

def rolling_sharpe(daily: pd.Series, rf_daily=0.04/252, window=252) -> pd.Series:
    excess = daily - rf_daily
    return excess.rolling(window).mean() / daily.rolling(window).std() * np.sqrt(252)

# ═════════════════════════════════════════════════════════════════════════════
# Full analytics computation for one model over one period
# ═════════════════════════════════════════════════════════════════════════════

def compute_analytics(portfolio_id, model_daily, bench_daily, prices_df,
                      start, end, label, rf_annual=0.04):
    m = model_daily[(model_daily.index >= pd.Timestamp(start)) &
                    (model_daily.index <= pd.Timestamp(end))].copy()
    b = bench_daily[(bench_daily.index >= pd.Timestamp(start)) &
                    (bench_daily.index <= pd.Timestamp(end))].copy()

    mm = to_monthly(m)
    bm = to_monthly(b)

    if mm.empty:
        return {}

    mdd_val, mdd_dur   = max_drawdown(mm)
    var95, cvar95       = var_cvar(mm, 0.95)
    var99, cvar99       = var_cvar(mm, 0.99)
    up_c, down_c, udc   = up_down_capture(mm, bm)
    bq_v, bq_l, wq_v, wq_l = quarterly_extremes(mm)

    bm_tr  = total_return(bm)
    m_tr   = total_return(mm)
    spy_d  = bench_daily[bench_daily.index >= pd.Timestamp(start)]

    spy_m = to_monthly(prices_df["SPY"].pct_change()
                       if "SPY" in prices_df.columns else pd.Series(dtype=float))
    spy_m = spy_m[(spy_m.index >= pd.Timestamp(start)) & (spy_m.index <= pd.Timestamp(end))]

    best_m_date  = mm.idxmax() if not mm.empty else None
    worst_m_date = mm.idxmin() if not mm.empty else None

    return {
        "portfolio_id":          portfolio_id,
        "period_label":          label,
        "period_start":          start,
        "period_end":            end,
        "data_points":           len(mm),
        "total_return":          m_tr,
        "annualized_return":     annualized_return(mm),
        "benchmark_total_return":bm_tr,
        "benchmark_annualized":  annualized_return(bm),
        "excess_return":         (m_tr - bm_tr) if not np.isnan(m_tr) and not np.isnan(bm_tr) else np.nan,
        "information_ratio":     information_ratio(mm, bm),
        "volatility_ann":        annualized_vol(mm),
        "downside_deviation":    downside_deviation(mm),
        "max_drawdown":          mdd_val,
        "max_drawdown_duration_days": mdd_dur,
        "var_95":                var95,
        "var_99":                var99,
        "cvar_95":               cvar95,
        "cvar_99":               cvar99,
        "sharpe_ratio":          sharpe(mm, rf_annual),
        "sortino_ratio":         sortino(mm, rf_annual),
        "calmar_ratio":          calmar(mm),
        "omega_ratio":           omega_ratio(mm),
        "batting_average":       batting_average(mm),
        "up_capture":            up_c,
        "down_capture":          down_c,
        "up_down_capture":       udc,
        "win_loss_ratio":        win_loss_ratio(mm),
        "slugging_pct":          slugging(mm),
        "best_month":            float(mm.max()) if not mm.empty else np.nan,
        "best_month_date":       best_m_date.date() if best_m_date is not None else None,
        "worst_month":           float(mm.min()) if not mm.empty else np.nan,
        "worst_month_date":      worst_m_date.date() if worst_m_date is not None else None,
        "best_quarter":          bq_v,
        "best_quarter_label":    bq_l,
        "worst_quarter":         wq_v,
        "worst_quarter_label":   wq_l,
        "calendar_year_returns": json.dumps(calendar_year_returns(mm)),
        "risk_free_rate_avg":    rf_annual,
    }

def compute_ytd(portfolio_id, model_daily, bench_daily, prices_df, rf_annual):
    ytd_start = date(date.today().year, 1, 1)
    ytd_end   = date.today()
    prior_start = date(date.today().year - 1, 1, 1)
    prior_end   = date(date.today().year - 1, date.today().month, date.today().day)

    m_ytd = model_daily[model_daily.index >= pd.Timestamp(ytd_start)]
    b_ytd = bench_daily[bench_daily.index >= pd.Timestamp(ytd_start)]
    mm = to_monthly(m_ytd); bm = to_monthly(b_ytd)

    m_prior = model_daily[(model_daily.index >= pd.Timestamp(prior_start)) &
                          (model_daily.index <= pd.Timestamp(prior_end))]
    mm_prior = to_monthly(m_prior)

    mdd_ytd, _ = max_drawdown(mm)
    spy_ytd = prices_df["SPY"].pct_change() if "SPY" in prices_df.columns else pd.Series(dtype=float)
    spy_ytd_m = to_monthly(spy_ytd[spy_ytd.index >= pd.Timestamp(ytd_start)])

    ytd_r  = total_return(mm)
    ytd_br = total_return(bm)

    base = compute_analytics(portfolio_id, model_daily, bench_daily, prices_df,
                             ytd_start, ytd_end, "YTD", rf_annual)
    base.update({
        "ytd_return":          ytd_r,
        "ytd_benchmark_return": ytd_br,
        "ytd_excess":          (ytd_r - ytd_br) if not (np.isnan(ytd_r) or np.isnan(ytd_br)) else np.nan,
        "ytd_sharpe":          sharpe(mm, rf_annual),
        "ytd_max_drawdown":    mdd_ytd,
        "ytd_batting_average": batting_average(mm),
        "ytd_best_month":      float(mm.max()) if not mm.empty else np.nan,
        "ytd_worst_month":     float(mm.min()) if not mm.empty else np.nan,
        "ytd_vs_prior_year":   (ytd_r - total_return(mm_prior)) if not mm_prior.empty else np.nan,
    })
    return base

def compute_rolling_returns(model_daily, prices_df, end_date):
    def _r(months):
        start = pd.Timestamp(end_date) - pd.DateOffset(months=months)
        sub = model_daily[model_daily.index >= start]
        return total_return(to_monthly(sub))
    return {
        "return_1m":  _r(1),
        "return_3m":  _r(3),
        "return_6m":  _r(6),
        "return_1y":  _r(12),
        "return_3y_ann": annualized_return(to_monthly(
            model_daily[model_daily.index >= pd.Timestamp(end_date) - pd.DateOffset(years=3)]
        )),
    }

def compute_construction(portfolio_id, alloc_df, prices_df, model_daily):
    latest = alloc_df["snapshot_date"].max()
    holdings = alloc_df[alloc_df["snapshot_date"] == latest]
    weights = [float(r["weight"]) for _, r in holdings.iterrows() if r["ticker"] != "CASH"]
    if not weights: return {}

    spy_d = prices_df["SPY"].pct_change().dropna() if "SPY" in prices_df.columns else pd.Series()
    agg_d = prices_df["AGG"].pct_change().dropna() if "AGG" in prices_df.columns else pd.Series()
    gld_d = prices_df["GLD"].pct_change().dropna() if "GLD" in prices_df.columns else pd.Series()

    return {
        "hhi_concentration":     hhi(weights),
        "effective_n_positions": effective_n(weights),
        "beta_spy":              beta(model_daily, spy_d),
        "beta_agg":              beta(model_daily, agg_d),
        "beta_gld":              beta(model_daily, gld_d),
    }

def compute_stress(portfolio_id, model_daily, bench_daily, prices_df, db):
    results = []
    spy_d = prices_df["SPY"].pct_change() if "SPY" in prices_df.columns else pd.Series()
    for name, (s, e) in STRESS_SCENARIOS.items():
        m = model_daily[(model_daily.index >= pd.Timestamp(s)) &
                        (model_daily.index <= pd.Timestamp(e))]
        b = bench_daily[(bench_daily.index >= pd.Timestamp(s)) &
                        (bench_daily.index <= pd.Timestamp(e))]
        spy = spy_d[(spy_d.index >= pd.Timestamp(s)) & (spy_d.index <= pd.Timestamp(e))]
        if m.empty: continue
        mm = to_monthly(m); bm = to_monthly(b); spy_m = to_monthly(spy)
        mdd, _ = max_drawdown(mm)
        row = {
            "portfolio_id":     portfolio_id,
            "scenario_name":    name,
            "scenario_start":   s,
            "scenario_end":     e,
            "model_return":     total_return(mm),
            "benchmark_return": total_return(bm),
            "spy_return":       total_return(spy_m),
            "excess_return":    (total_return(mm) - total_return(bm)),
            "max_drawdown":     mdd,
            "volatility":       annualized_vol(mm),
        }
        results.append(row)
        with db.cursor() as cur:
            cur.execute("""
                INSERT INTO stress_test_results
                    (portfolio_id, scenario_name, scenario_start, scenario_end,
                     model_return, benchmark_return, spy_return, excess_return,
                     max_drawdown, volatility)
                VALUES (%(portfolio_id)s,%(scenario_name)s,%(scenario_start)s,%(scenario_end)s,
                        %(model_return)s,%(benchmark_return)s,%(spy_return)s,%(excess_return)s,
                        %(max_drawdown)s,%(volatility)s)
                ON CONFLICT (portfolio_id, scenario_name) DO UPDATE SET
                    model_return=EXCLUDED.model_return, benchmark_return=EXCLUDED.benchmark_return,
                    spy_return=EXCLUDED.spy_return, excess_return=EXCLUDED.excess_return,
                    max_drawdown=EXCLUDED.max_drawdown, volatility=EXCLUDED.volatility,
                    computed_at=NOW()
            """, row)
        db.commit()
    return results

def compute_regime(portfolio_id, model_daily, bench_daily, db):
    with db.cursor() as cur:
        cur.execute("""SELECT regime, scored_at FROM regime_snapshots ORDER BY scored_at""")
        snaps = cur.fetchall()
    if not snaps: return []

    # Build regime timeline (strip tz so we can compare with tz-naive index)
    def to_ts(dt):
        ts = pd.Timestamp(dt)
        return ts.tz_localize(None) if ts.tzinfo is None else ts.tz_convert(None)

    regime_periods = []
    for i, s in enumerate(snaps):
        start = to_ts(s["scored_at"])
        end   = to_ts(snaps[i+1]["scored_at"]) if i+1 < len(snaps) else pd.Timestamp.now()
        regime_periods.append((s["regime"], start, end))

    results = []
    for regime, rs, re in regime_periods:
        m = model_daily[(model_daily.index >= rs) & (model_daily.index < re)]
        b = bench_daily[(bench_daily.index >= rs) & (bench_daily.index < re)]
        if m.empty: continue
        mm = to_monthly(m); bm = to_monthly(b)
        mdd, _ = max_drawdown(mm)
        row = {
            "portfolio_id":      portfolio_id,
            "regime":            regime,
            "calc_date":         date.today(),
            "periods_count":     len(mm),
            "total_days":        (re - rs).days,
            "avg_monthly_return":float(mm.mean()) if not mm.empty else None,
            "ann_return":        annualized_return(mm),
            "ann_volatility":    annualized_vol(mm),
            "sharpe_ratio":      sharpe(mm),
            "max_drawdown":      mdd,
            "batting_average":   batting_average(mm),
            "benchmark_avg_return": float(bm.mean()) if not bm.empty else None,
            "excess_vs_benchmark": (float(mm.mean()) - float(bm.mean()))
                                   if not mm.empty and not bm.empty else None,
        }
        results.append(row)

    # Aggregate by regime
    by_regime = defaultdict(list)
    for r in results:
        by_regime[r["regime"]].append(r)

    for regime, rows in by_regime.items():
        all_mm = pd.concat([model_daily[(model_daily.index >= to_ts(snaps[i]["scored_at"])) &
                                        (model_daily.index < to_ts(
                                            snaps[i+1]["scored_at"] if i+1 < len(snaps) else datetime.now()
                                        ))]
                            for i, s in enumerate(snaps) if s["regime"] == regime], axis=0)
        if all_mm.empty: continue
        mm = to_monthly(all_mm)
        mdd, _ = max_drawdown(mm)
        agg = {
            "portfolio_id":      portfolio_id,
            "regime":            regime,
            "calc_date":         date.today(),
            "periods_count":     len(mm),
            "total_days":        sum(r["total_days"] for r in rows),
            "avg_monthly_return": float(mm.mean()),
            "ann_return":        annualized_return(mm),
            "ann_volatility":    annualized_vol(mm),
            "sharpe_ratio":      sharpe(mm),
            "max_drawdown":      mdd,
            "batting_average":   batting_average(mm),
        }
        with db.cursor() as cur:
            cur.execute("""
                INSERT INTO regime_performance
                    (portfolio_id, regime, calc_date, periods_count, total_days,
                     avg_monthly_return, ann_return, ann_volatility, sharpe_ratio,
                     max_drawdown, batting_average)
                VALUES (%(portfolio_id)s,%(regime)s,%(calc_date)s,%(periods_count)s,%(total_days)s,
                        %(avg_monthly_return)s,%(ann_return)s,%(ann_volatility)s,%(sharpe_ratio)s,
                        %(max_drawdown)s,%(batting_average)s)
                ON CONFLICT (portfolio_id, regime, calc_date) DO UPDATE SET
                    periods_count=EXCLUDED.periods_count, avg_monthly_return=EXCLUDED.avg_monthly_return,
                    ann_return=EXCLUDED.ann_return, ann_volatility=EXCLUDED.ann_volatility,
                    sharpe_ratio=EXCLUDED.sharpe_ratio, max_drawdown=EXCLUDED.max_drawdown,
                    batting_average=EXCLUDED.batting_average, computed_at=NOW()
            """, agg)
        db.commit()
    return by_regime

def save_analytics(db, a, calc_date):
    a = {k: (None if isinstance(v, float) and np.isnan(v) else v) for k, v in a.items()}
    cols = [k for k in a if k not in ("portfolio_id","period_label","period_start","period_end")]
    with db.cursor() as cur:
        cur.execute(f"""
            INSERT INTO model_analytics (portfolio_id, calc_date, period_start, period_end, period_label,
                {', '.join(cols)})
            VALUES (%(portfolio_id)s, %(calc_date)s, %(period_start)s, %(period_end)s, %(period_label)s,
                {', '.join(f'%({c})s' for c in cols)})
            ON CONFLICT (portfolio_id, calc_date, period_label) DO UPDATE SET
                {', '.join(f'{c}=EXCLUDED.{c}' for c in cols)},
                computed_at=NOW()
        """, {**a, "calc_date": calc_date})
    db.commit()

# ═════════════════════════════════════════════════════════════════════════════
# HTML Report Generation
# ═════════════════════════════════════════════════════════════════════════════

PLOTLY_CONFIG = dict(displayModeBar=False, responsive=True)
PLOTLY_THEME  = dict(
    paper_bgcolor=NAVY2, plot_bgcolor=NAVY,
    font=dict(family="Segoe UI, system-ui, sans-serif", color=WHITE, size=11),
    xaxis=dict(gridcolor=NAVY3, showgrid=True),
    yaxis=dict(gridcolor=NAVY3, showgrid=True),
    margin=dict(l=50, r=20, t=40, b=40),
)

def p(v, decimals=2, pct=True, plus=True):
    if v is None or (isinstance(v, float) and np.isnan(v)): return "—"
    val = float(v) * 100 if pct else float(v)
    sign = "+" if plus and val > 0 else ""
    return f"{sign}{val:.{decimals}f}{'%' if pct else 'x'}"

def pcolor(v, invert=False):
    if v is None or (isinstance(v, float) and np.isnan(v)): return GRAY
    good = float(v) > 0
    if invert: good = not good
    return GREEN if good else RED

def metric_row(label, val, pct=True, plus=True, invert=False, note=""):
    color = pcolor(val, invert)
    return f"""<tr>
      <td style="color:{GRAY};padding:7px 12px;border-bottom:1px solid {NAVY3};">{label}</td>
      <td style="color:{color};font-weight:600;padding:7px 12px;border-bottom:1px solid {NAVY3};text-align:right;">
        {p(val, pct=pct, plus=plus)}
      </td>
      {'<td style="color:'+GRAY+';font-size:11px;padding:7px 8px;border-bottom:1px solid '+NAVY3+';">'+note+'</td>' if note else ''}
    </tr>"""

def build_cumulative_chart(model_daily, bench_daily, prices_df, model_name, bench_label):
    mm = to_monthly(model_daily)
    bm = to_monthly(bench_daily)
    spy_m = to_monthly(prices_df["SPY"].pct_change()) if "SPY" in prices_df.columns else pd.Series()

    cum_m   = (1 + mm).cumprod() - 1
    cum_b   = (1 + bm).cumprod() - 1
    cum_spy = (1 + spy_m).cumprod() - 1

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=cum_m.index,   y=cum_m*100,   name=model_name,  line=dict(color=GOLD, width=2.5)))
    fig.add_trace(go.Scatter(x=cum_b.index,   y=cum_b*100,   name=bench_label, line=dict(color=BLUE, width=1.8, dash="dot")))
    fig.add_trace(go.Scatter(x=cum_spy.index, y=cum_spy*100, name="SPY",        line=dict(color=GRAY, width=1.2, dash="dash")))
    fig.add_hline(y=0, line_color=NAVY3, line_width=1)
    fig.update_layout(**PLOTLY_THEME, title=dict(text="Cumulative Return", font=dict(color=GOLD, size=13)),
                      yaxis_ticksuffix="%", legend=dict(bgcolor=NAVY2, bordercolor=NAVY3))
    return pio.to_html(fig, full_html=False, config=PLOTLY_CONFIG, include_plotlyjs=False)

def build_drawdown_chart(model_daily, model_name):
    mm  = to_monthly(model_daily)
    cum = (1 + mm).cumprod()
    dd  = (cum - cum.cummax()) / cum.cummax() * 100
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=dd.index, y=dd, fill="tozeroy", name="Drawdown",
                             line=dict(color=RED, width=1.5), fillcolor='rgba(231,76,60,0.15)'))
    fig.update_layout(**PLOTLY_THEME, title=dict(text="Drawdown", font=dict(color=GOLD, size=13)),
                      yaxis_ticksuffix="%")
    return pio.to_html(fig, full_html=False, config=PLOTLY_CONFIG, include_plotlyjs=False)

def build_rolling_sharpe_chart(model_daily, model_name, rf_annual=0.04):
    rs = rolling_sharpe(model_daily, rf_daily=rf_annual/252, window=252).dropna()
    rs_m = rs.resample("ME").last()
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=rs_m.index, y=rs_m, name="Rolling 12M Sharpe",
                             line=dict(color=GOLD, width=2)))
    fig.add_hline(y=0, line_color=GRAY, line_width=1, line_dash="dash")
    fig.add_hline(y=1, line_color=GREEN, line_width=1, line_dash="dot")
    fig.update_layout(**PLOTLY_THEME, title=dict(text="Rolling 12-Month Sharpe", font=dict(color=GOLD, size=13)))
    return pio.to_html(fig, full_html=False, config=PLOTLY_CONFIG, include_plotlyjs=False)

def build_monthly_heatmap(model_daily, model_name):
    mm = to_monthly(model_daily)
    df = mm.to_frame("ret")
    df["year"]  = df.index.year
    df["month"] = df.index.month
    pivot = df.pivot(index="year", columns="month", values="ret") * 100

    month_names = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    pivot.columns = [month_names[c-1] for c in pivot.columns]

    fig = go.Figure(go.Heatmap(
        z=pivot.values, x=list(pivot.columns), y=[str(y) for y in pivot.index],
        colorscale=[[0, RED], [0.5, NAVY2], [1, GREEN]],
        zmid=0, text=[[f"{v:.1f}%" if not np.isnan(v) else "" for v in row] for row in pivot.values],
        texttemplate="%{text}", showscale=True,
        colorbar=dict(ticksuffix="%", outlinecolor=NAVY3),
    ))
    theme = {k:v for k,v in PLOTLY_THEME.items() if k != "yaxis"}
    fig.update_layout(**theme, title=dict(text="Monthly Return Heatmap", font=dict(color=GOLD, size=13)),
                      yaxis=dict(autorange="reversed", gridcolor=NAVY3))
    return pio.to_html(fig, full_html=False, config=PLOTLY_CONFIG, include_plotlyjs=False)

def build_correlation_heatmap(portfolio_id, alloc_df, prices_df):
    latest = alloc_df["snapshot_date"].max()
    tickers = [r["ticker"] for _, r in alloc_df[alloc_df["snapshot_date"]==latest].iterrows()
               if r["ticker"] != "CASH" and r["ticker"] in prices_df.columns]
    if len(tickers) < 2: return ""
    rets = prices_df[tickers].pct_change().dropna()
    corr = rets.corr()
    fig = go.Figure(go.Heatmap(
        z=corr.values, x=corr.columns.tolist(), y=corr.index.tolist(),
        colorscale=[[0, BLUE], [0.5, NAVY2], [1, RED]], zmin=-1, zmax=1,
        text=[[f"{v:.2f}" for v in row] for row in corr.values],
        texttemplate="%{text}", showscale=True,
    ))
    fig.update_layout(**PLOTLY_THEME, title=dict(text="Holdings Correlation Matrix", font=dict(color=GOLD, size=13)))
    return pio.to_html(fig, full_html=False, config=PLOTLY_CONFIG, include_plotlyjs=False)

def build_return_histogram(model_daily, bench_daily, model_name):
    mm = to_monthly(model_daily) * 100
    bm = to_monthly(bench_daily) * 100
    fig = go.Figure()
    fig.add_trace(go.Histogram(x=bm, name="Benchmark", opacity=0.5,
                               marker_color=BLUE, nbinsx=24))
    fig.add_trace(go.Histogram(x=mm, name=model_name, opacity=0.7,
                               marker_color=GOLD, nbinsx=24))
    fig.update_layout(**PLOTLY_THEME, barmode="overlay",
                      title=dict(text="Monthly Return Distribution", font=dict(color=GOLD, size=13)),
                      xaxis_ticksuffix="%")
    return pio.to_html(fig, full_html=False, config=PLOTLY_CONFIG, include_plotlyjs=False)

def build_rolling_returns_chart(model_daily, model_name):
    mm   = to_monthly(model_daily)
    r12  = mm.rolling(12).apply(lambda x: (1+x).prod()-1) * 100
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=r12.index, y=r12, name="Rolling 12M Return",
                             line=dict(color=GOLD, width=2), fill="tozeroy",
                             fillcolor='rgba(201,168,76,0.15)'))
    fig.add_hline(y=0, line_color=GRAY, line_width=1)
    fig.update_layout(**PLOTLY_THEME, yaxis_ticksuffix="%",
                      title=dict(text="Rolling 12-Month Return", font=dict(color=GOLD, size=13)))
    return pio.to_html(fig, full_html=False, config=PLOTLY_CONFIG, include_plotlyjs=False)

# ─── CSS / HTML shared pieces ─────────────────────────────────────────────────

BASE_CSS = f"""
*, *::before, *::after {{ box-sizing: border-box; margin:0; padding:0; }}
body {{ background:{NAVY}; color:{WHITE}; font-family:'Segoe UI',system-ui,sans-serif; font-size:14px; line-height:1.6; }}
.page-header {{ background:linear-gradient(135deg,{NAVY2},{NAVY3}); border-bottom:2px solid {GOLD};
                padding:24px 36px 18px; display:flex; justify-content:space-between; align-items:flex-start; }}
.page-header h1 {{ font-size:20px; font-weight:800; color:{GOLD}; letter-spacing:.06em; text-transform:uppercase; }}
.page-header p  {{ color:{GRAY}; font-size:12px; margin-top:3px; }}
.grid-2 {{ display:grid; grid-template-columns:1fr 1fr; gap:18px; }}
.grid-3 {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:18px; }}
.grid-4 {{ display:grid; grid-template-columns:repeat(4,1fr); gap:18px; }}
.full   {{ grid-column:1/-1; }}
.half   {{ grid-column:span 2; }}
.content {{ padding:20px 28px; }}
.card {{ background:{NAVY2}; border:1px solid {NAVY3}; border-radius:8px; padding:18px 20px; margin-bottom:18px; }}
.card-title {{ font-size:10px; font-weight:700; letter-spacing:.12em; text-transform:uppercase;
               color:{GOLD}; border-bottom:1px solid {NAVY3}; padding-bottom:8px; margin-bottom:14px; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th {{ text-align:left; padding:7px 12px; font-size:10px; font-weight:700; letter-spacing:.1em;
      text-transform:uppercase; color:{GOLD}; border-bottom:1px solid {NAVY3}; }}
td {{ padding:7px 12px; border-bottom:1px solid {NAVY3}; vertical-align:top; }}
tr:last-child td {{ border-bottom:none; }}
tr:hover td {{ background:{NAVY3}33; }}
.tag {{ display:inline-block; padding:2px 8px; border-radius:3px; font-size:10px;
        font-weight:700; letter-spacing:.06em; text-transform:uppercase; }}
.ytd-scorecard {{ display:grid; grid-template-columns:repeat(5,1fr); gap:0; background:{NAVY2};
                  border:1px solid {GOLD}44; border-radius:8px; overflow:hidden; margin-bottom:18px; }}
.ytd-box {{ padding:14px 16px; border-right:1px solid {NAVY3}; }}
.ytd-box:last-child {{ border-right:none; }}
.ytd-label {{ font-size:9px; text-transform:uppercase; letter-spacing:.1em; color:{GRAY}; margin-bottom:5px; }}
.ytd-value {{ font-size:22px; font-weight:800; }}
.page-footer {{ border-top:1px solid {NAVY3}; padding:14px 28px; display:flex;
                justify-content:space-between; font-size:11px; color:{GRAY}; margin-top:30px; }}
.nav {{ background:{NAVY2}; border-bottom:1px solid {NAVY3}; padding:8px 28px;
        display:flex; gap:12px; font-size:12px; }}
.nav a {{ color:{GRAY}; text-decoration:none; padding:4px 10px; border-radius:4px; }}
.nav a:hover {{ color:{GOLD}; background:{NAVY3}; }}
.nav a.active {{ color:{GOLD}; border:1px solid {GOLD}44; }}
"""

PLOTLYJS = "<script src='https://cdn.plot.ly/plotly-2.35.2.min.js'></script>"

def nav_html(pages, current):
    links = " ".join(
        f'<a href="{slug}.html" class="{"active" if name==current else ""}">{name}</a>'
        for name, slug in pages
    )
    return f'<div class="nav">{links}</div>'

def ytd_scorecard(a):
    def box(label, val, color=None, pct=True, plus=True, invert=False):
        c = color or pcolor(val, invert)
        return f'''<div class="ytd-box">
          <div class="ytd-label">{label}</div>
          <div class="ytd-value" style="color:{c}">{p(val, pct=pct, plus=plus)}</div>
        </div>'''
    if not a: return '<div class="ytd-scorecard"><div class="ytd-box" style="grid-column:1/-1;color:#8899aa;">No YTD data yet</div></div>'
    return f'''<div class="ytd-scorecard">
      {box("YTD Return",       a.get("ytd_return"))}
      {box("vs Benchmark",     a.get("ytd_excess"))}
      {box("YTD Sharpe",       a.get("ytd_sharpe"), pct=False, plus=True)}
      {box("YTD Max DD",       a.get("ytd_max_drawdown"), invert=True)}
      {box("Batting Avg",      a.get("ytd_batting_average"), color=GOLD)}
    </div>'''

def analytics_table(a, stress=None, regime=None):
    if not a: return '<p style="color:#8899aa;">No analytics computed yet.</p>'
    cal = json.loads(a.get("calendar_year_returns") or "{}") if isinstance(a.get("calendar_year_returns"), str) else {}

    def section(title):
        return f'<tr><td colspan="3" style="background:{NAVY3};color:{GOLD};font-weight:700;font-size:10px;letter-spacing:.1em;padding:8px 12px;text-transform:uppercase;">{title}</td></tr>'

    rows = f"""
    {section("Returns")}
    {metric_row("Total Return (ITD)",      a.get("total_return"))}
    {metric_row("Annualized Return",        a.get("annualized_return"))}
    {metric_row("Benchmark Return (ITD)",   a.get("benchmark_total_return"), plus=False)}
    {metric_row("Excess Return",            a.get("excess_return"))}
    {metric_row("1-Month Return",           a.get("return_1m"))}
    {metric_row("3-Month Return",           a.get("return_3m"))}
    {metric_row("6-Month Return",           a.get("return_6m"))}
    {metric_row("1-Year Return",            a.get("return_1y"))}
    {metric_row("3-Year Annualized",        a.get("return_3y_ann"))}
    {section("Calendar Year Returns")}
    {''.join(metric_row(yr, v) for yr,v in sorted(cal.items()))}
    {metric_row("Best Month Ever",          a.get("best_month"),  note=str(a.get("best_month_date","")))}
    {metric_row("Worst Month Ever",         a.get("worst_month"), note=str(a.get("worst_month_date","")))}
    {metric_row("Best Quarter Ever",        a.get("best_quarter"),  note=str(a.get("best_quarter_label","")))}
    {metric_row("Worst Quarter Ever",       a.get("worst_quarter"), note=str(a.get("worst_quarter_label","")))}
    {section("Risk")}
    {metric_row("Annualized Volatility",    a.get("volatility_ann"))}
    {metric_row("Downside Deviation",       a.get("downside_deviation"))}
    {metric_row("Max Drawdown",             a.get("max_drawdown"), invert=True)}
    {metric_row("Max DD Duration (months)", a.get("max_drawdown_duration_days"), pct=False, note="months")}
    {metric_row("VaR 95%",                  a.get("var_95"), invert=True)}
    {metric_row("VaR 99%",                  a.get("var_99"), invert=True)}
    {metric_row("CVaR 95%",                 a.get("cvar_95"), invert=True)}
    {metric_row("CVaR 99%",                 a.get("cvar_99"), invert=True)}
    {section("Risk-Adjusted Returns")}
    {metric_row("Sharpe Ratio",             a.get("sharpe_ratio"), pct=False)}
    {metric_row("Sortino Ratio",            a.get("sortino_ratio"), pct=False)}
    {metric_row("Calmar Ratio",             a.get("calmar_ratio"), pct=False)}
    {metric_row("Omega Ratio",              a.get("omega_ratio"), pct=False)}
    {metric_row("Information Ratio",        a.get("information_ratio"), pct=False)}
    {section("Batting Statistics")}
    {metric_row("Batting Average",          a.get("batting_average"))}
    {metric_row("Up Capture Ratio",         a.get("up_capture"), pct=False, note="> 1.0 ideal")}
    {metric_row("Down Capture Ratio",       a.get("down_capture"), pct=False, invert=True, note="< 1.0 ideal")}
    {metric_row("Up/Down Capture",          a.get("up_down_capture"), pct=False, note="> 1.0 ideal")}
    {metric_row("Win/Loss Ratio",           a.get("win_loss_ratio"), pct=False)}
    {metric_row("Slugging %",               a.get("slugging_pct"), pct=False)}
    {section("Portfolio Construction")}
    {metric_row("Beta to SPY",              a.get("beta_spy"), pct=False, plus=False)}
    {metric_row("Beta to AGG",              a.get("beta_agg"), pct=False, plus=False)}
    {metric_row("Beta to GLD",              a.get("beta_gld"), pct=False, plus=False)}
    {metric_row("HHI Concentration",        a.get("hhi_concentration"), pct=False, plus=False, note="lower=diversified")}
    {metric_row("Effective # Positions",    a.get("effective_n_positions"), pct=False, plus=False)}
    """

    if stress:
        rows += section("Stress Tests")
        for s in stress:
            rows += metric_row(s["scenario_name"], s.get("model_return"),
                               note=f"bench={p(s.get('benchmark_return'))} | excess={p(s.get('excess_return'))}")

    if regime:
        rows += section("Regime-Adjusted Performance")
        for r_name, r_data in regime.items():
            if not r_data: continue
            first = r_data[0]
            rows += metric_row(f"{r_name} Avg Monthly",   first.get("avg_monthly_return"),
                               note=f"n={first.get('periods_count','?')} months")
            rows += metric_row(f"{r_name} Ann Return",    first.get("ann_return"))
            rows += metric_row(f"{r_name} Sharpe",        first.get("sharpe_ratio"), pct=False)

    return f'<table><thead><tr><th>Metric</th><th style="text-align:right;">Value</th><th>Note</th></tr></thead><tbody>{rows}</tbody></table>'

# ═════════════════════════════════════════════════════════════════════════════
# Single model HTML page
# ═════════════════════════════════════════════════════════════════════════════

def build_model_page(portfolio, model_daily, bench_daily, prices_df, alloc_df,
                     analytics_itd, analytics_ytd, stress_results, regime_data, pages):
    name  = portfolio["name"]
    pid   = portfolio["id"]
    alloc = portfolio.get("allocation_profile","")
    bench_label = " / ".join(f"{w*100:.0f}% {t}" for t, w in MODEL_BENCHMARKS.get(pid, [("SPY",1.0)]))

    ytd_card     = ytd_scorecard(analytics_ytd)
    analytics_tbl = analytics_table(analytics_itd, stress_results, regime_data)

    cum_chart    = build_cumulative_chart(model_daily, bench_daily, prices_df, name, bench_label)
    dd_chart     = build_drawdown_chart(model_daily, name)
    rs_chart     = build_rolling_sharpe_chart(model_daily, name)
    rr_chart     = build_rolling_returns_chart(model_daily, name)
    heat_chart   = build_monthly_heatmap(model_daily, name)
    corr_chart   = build_correlation_heatmap(pid, alloc_df, prices_df)
    hist_chart   = build_return_histogram(model_daily, bench_daily, name)

    date_str = date.today().strftime("%B %d, %Y")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>IWP — {name} Analytics</title>
  {PLOTLYJS}
  <style>{BASE_CSS}</style>
</head>
<body>
<div class="page-header">
  <div>
    <h1>IWP — {name}</h1>
    <p>{alloc} &nbsp;·&nbsp; Benchmark: {bench_label} &nbsp;·&nbsp; {date_str}</p>
  </div>
  <div style="text-align:right;color:{GRAY};font-size:12px;">Analytics Engine v1.0</div>
</div>
{nav_html(pages, name)}

<div class="content">
  <div class="card-title" style="margin-bottom:8px;">YTD Scorecard — {date.today().year}</div>
  {ytd_card}

  <div class="grid-2">
    <div>
      <div class="card">
        <div class="card-title">Comprehensive Analytics — Inception to Date</div>
        {analytics_tbl}
      </div>
    </div>
    <div>
      <div class="card">{cum_chart}</div>
      <div class="card">{dd_chart}</div>
      <div class="card">{rs_chart}</div>
      <div class="card">{rr_chart}</div>
    </div>
  </div>

  <div class="grid-2">
    <div class="card">{heat_chart}</div>
    <div class="card">{hist_chart}</div>
    {"<div class='card full'>" + corr_chart + "</div>" if corr_chart else ""}
  </div>
</div>

<div class="page-footer">
  <div><strong style="color:{GOLD}">Confidential</strong> &nbsp;·&nbsp; IWP Analytics</div>
  <div>Generated {date_str} &nbsp;·&nbsp; Prices via yfinance</div>
</div>
</body>
</html>"""

# ═════════════════════════════════════════════════════════════════════════════
# Comparison + Firm-wide pages
# ═════════════════════════════════════════════════════════════════════════════

def build_comparison_page(all_results, pages):
    date_str = date.today().strftime("%B %d, %Y")
    METRICS = [
        ("Model",               lambda a,p: f'<a href="analytics-{p["id"].replace("_","-")}.html" style="color:{GOLD};font-weight:700;">{p["name"]}</a>', False),
        ("YTD",                 lambda a,p: p(a.get("ytd_return"))           if a else "—", True),
        ("1Y",                  lambda a,p: p(a.get("return_1y"))            if a else "—", True),
        ("3Y Ann",              lambda a,p: p(a.get("return_3y_ann"))        if a else "—", True),
        ("Sharpe",              lambda a,p: p(a.get("sharpe_ratio"),pct=False) if a else "—", True),
        ("Sortino",             lambda a,p: p(a.get("sortino_ratio"),pct=False) if a else "—", True),
        ("Calmar",              lambda a,p: p(a.get("calmar_ratio"),pct=False) if a else "—", True),
        ("Max DD",              lambda a,p: p(a.get("max_drawdown"))         if a else "—", True),
        ("Up/Down Capture",     lambda a,p: p(a.get("up_down_capture"),pct=False) if a else "—", True),
        ("Batting Avg",         lambda a,p: p(a.get("batting_average"))      if a else "—", True),
    ]
    header = "".join(f"<th>{m[0]}</th>" for m in METRICS)
    body = ""
    for portfolio, a in all_results:
        def fmt(fn, show_color):
            try:
                val = fn(a, portfolio)
                return f'<td style="text-align:right;">{val}</td>'
            except: return "<td>—</td>"
        body += "<tr>" + "".join(
            f'<td>{METRICS[0][1](a, portfolio)}</td>' if i == 0
            else fmt(METRICS[i][1], METRICS[i][2])
            for i in range(len(METRICS))
        ) + "</tr>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <title>IWP — Model Comparison</title>
  {PLOTLYJS}
  <style>{BASE_CSS} th,td{{text-align:right;}} th:first-child,td:first-child{{text-align:left;}}</style>
</head>
<body>
<div class="page-header">
  <div><h1>IWP — Model Comparison</h1><p>All 7 models · {date_str}</p></div>
</div>
{nav_html(pages, "Comparison")}
<div class="content">
  <div class="card">
    <div class="card-title">Side-by-Side: All 7 Models</div>
    <table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table>
  </div>
</div>
<div class="page-footer">
  <div><strong style="color:{GOLD}">Confidential</strong> &nbsp;·&nbsp; IWP Analytics</div>
  <div>Generated {date_str}</div>
</div>
</body></html>"""

def build_firmwide_page(all_results, prices_df, pages, stress_all, db):
    date_str = date.today().strftime("%B %d, %Y")

    # Firm-wide common holdings
    with db.cursor() as cur:
        cur.execute("""
            SELECT ma.ticker, ma.instrument_name,
                   COUNT(DISTINCT ma.portfolio_id) AS in_models,
                   AVG(ma.weight) AS avg_weight,
                   SUM(ma.weight) AS total_weight
            FROM model_allocations ma
            WHERE ma.snapshot_date IN (
                SELECT MAX(snapshot_date) FROM model_allocations GROUP BY portfolio_id
            ) AND ma.ticker != 'CASH'
            GROUP BY ma.ticker, ma.instrument_name
            ORDER BY total_weight DESC LIMIT 30
        """)
        common = cur.fetchall()

    holdings_rows = "".join(
        f'<tr><td><strong>{r["ticker"]}</strong></td><td style="color:{GRAY}">{r["instrument_name"] or ""}</td>'
        f'<td style="text-align:right;">{int(r["in_models"])}/7</td>'
        f'<td style="text-align:right;color:{GOLD};">{float(r["total_weight"])*100:.1f}%</td>'
        f'<td style="text-align:right;">{float(r["avg_weight"])*100:.1f}%</td></tr>'
        for r in common
    )

    # Model correlation heatmap
    model_rets = {}
    for portfolio, a in all_results:
        if "model_daily" in portfolio: model_rets[portfolio["name"]] = portfolio["model_daily"]

    corr_chart = ""
    if len(model_rets) >= 2:
        frames = {name: to_monthly(s) for name, s in model_rets.items()}
        aligned = pd.DataFrame(frames).dropna()
        if not aligned.empty:
            corr = aligned.corr()
            fig = go.Figure(go.Heatmap(
                z=corr.values, x=corr.columns.tolist(), y=corr.index.tolist(),
                colorscale=[[0, BLUE],[0.5,NAVY2],[1,RED]], zmin=-1, zmax=1,
                text=[[f"{v:.2f}" for v in row] for row in corr.values],
                texttemplate="%{text}", showscale=True,
            ))
            fig.update_layout(**PLOTLY_THEME, title=dict(text="Model Correlation Matrix", font=dict(color=GOLD,size=13)))
            corr_chart = f'<div class="card">{pio.to_html(fig,full_html=False,config=PLOTLY_CONFIG,include_plotlyjs=False)}</div>'

    # Stress test summary
    stress_rows = ""
    if stress_all:
        scenarios = list({s["scenario_name"] for s in stress_all})
        stress_rows = "<table><thead><tr><th>Model</th>" + "".join(f"<th>{s}</th>" for s in scenarios) + "</tr></thead><tbody>"
        by_model = defaultdict(dict)
        for s in stress_all:
            by_model[s["portfolio_id"]][s["scenario_name"]] = s["model_return"]
        for pid, scen in by_model.items():
            name = next((r["name"] for r,_ in all_results if r["id"]==pid), pid)
            stress_rows += f"<tr><td><strong>{name}</strong></td>"
            for sc in scenarios:
                v = scen.get(sc)
                c = pcolor(v)
                stress_rows += f'<td style="text-align:right;color:{c};">{p(v) if v is not None else "—"}</td>'
            stress_rows += "</tr>"
        stress_rows += "</tbody></table>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <title>IWP — Firm-Wide Analytics</title>
  {PLOTLYJS}
  <style>{BASE_CSS}</style>
</head>
<body>
<div class="page-header">
  <div><h1>IWP — Firm-Wide Analytics</h1><p>{date_str}</p></div>
</div>
{nav_html(pages, "Firm-Wide")}
<div class="content">
  <div class="grid-2">
    <div class="card">
      <div class="card-title">Common Holdings (All Models)</div>
      <table>
        <thead><tr><th>Ticker</th><th>Name</th><th>Models</th><th>Total Weight</th><th>Avg Weight</th></tr></thead>
        <tbody>{holdings_rows}</tbody>
      </table>
    </div>
    <div>
      {corr_chart}
    </div>
  </div>
  {"<div class='card'><div class='card-title'>Stress Test Results</div>" + stress_rows + "</div>" if stress_rows else ""}
</div>
<div class="page-footer">
  <div><strong style="color:{GOLD}">Confidential</strong> &nbsp;·&nbsp; IWP Analytics</div>
  <div>Generated {date_str}</div>
</div>
</body></html>"""

# ═════════════════════════════════════════════════════════════════════════════
# Main orchestrator
# ═════════════════════════════════════════════════════════════════════════════

def run_model(portfolio, prices_df, db, gen_html=True, pages=None):
    pid  = portfolio["id"]
    name = portfolio["name"]
    log.info("═══ %s ═══", name)

    alloc_df = load_allocations(db, pid)
    if alloc_df.empty:
        log.warning("No allocations for %s — skipping", name); return None, None, None, []

    model_daily = build_model_returns(pid, alloc_df, prices_df)
    bench_daily = build_benchmark_returns(pid, prices_df)

    if model_daily.empty:
        log.warning("Could not build return series for %s", name); return None, None, None, []

    # Align on common dates
    aligned = pd.concat([model_daily, bench_daily], axis=1).dropna()
    model_daily = aligned.iloc[:,0]
    bench_daily = aligned.iloc[:,1]

    rf = get_risk_free_rate(prices_df)
    log.info("  Risk-free rate: %.2f%%  |  %d trading days", rf*100, len(model_daily))

    today      = date.today()
    itd_start  = model_daily.index[0].date()

    # ITD analytics
    a_itd  = compute_analytics(pid, model_daily, bench_daily, prices_df, itd_start, today, "ITD", rf)
    a_itd.update(compute_rolling_returns(model_daily, prices_df, today))
    a_itd.update(compute_construction(pid, alloc_df, prices_df, model_daily))
    save_analytics(db, {**a_itd, "period_start": itd_start, "period_end": today}, today)

    # YTD analytics
    a_ytd = compute_ytd(pid, model_daily, bench_daily, prices_df, rf)
    a_ytd.update(compute_rolling_returns(model_daily, prices_df, today))
    a_ytd.update(compute_construction(pid, alloc_df, prices_df, model_daily))
    save_analytics(db, {**a_ytd, "period_start": date(today.year,1,1), "period_end": today}, today)

    # Calendar years
    for yr in range(2020, today.year + 1):
        yr_start = date(yr, 1, 1)
        yr_end   = date(yr, 12, 31) if yr < today.year else today
        a_yr = compute_analytics(pid, model_daily, bench_daily, prices_df, yr_start, yr_end, str(yr), rf)
        if a_yr: save_analytics(db, {**a_yr, "period_start": yr_start, "period_end": yr_end}, today)

    # Stress tests
    stress = compute_stress(pid, model_daily, bench_daily, prices_df, db)

    # Regime
    regime = compute_regime(pid, model_daily, bench_daily, db)

    # Print summary
    tr   = a_itd.get("total_return",0) or 0
    sh   = a_itd.get("sharpe_ratio")
    mdd  = a_itd.get("max_drawdown",0) or 0
    ytd  = a_ytd.get("ytd_return",0) or 0
    xsyt = a_ytd.get("ytd_excess",0) or 0
    log.info("  ITD=%+.1f%%  Sharpe=%.2f  MaxDD=%.1f%%  YTD=%+.1f%%  YTD-alpha=%+.1f%%",
             tr*100, sh or 0, mdd*100, ytd*100, (xsyt or 0)*100)

    portfolio["model_daily"] = model_daily  # store for firm-wide

    if gen_html and pages:
        html = build_model_page(portfolio, model_daily, bench_daily, prices_df, alloc_df,
                                a_itd, a_ytd, stress, regime, pages)
        slug = f"analytics-{pid.replace('_','-')}"
        path = REPORTS_DIR / f"{slug}.html"
        path.write_text(html, encoding="utf-8")
        log.info("  → %s", path.name)

    return a_itd, a_ytd, alloc_df, stress

# ═════════════════════════════════════════════════════════════════════════════
# Entry point
# ═════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all",            action="store_true",  help="Run all models + firm-wide")
    parser.add_argument("--model",          type=str, default=None, help="Run single model by name")
    parser.add_argument("--stress",         action="store_true",  help="Stress tests only")
    parser.add_argument("--ytd",            action="store_true",  help="YTD analytics only")
    parser.add_argument("--refresh-prices", action="store_true",  help="Force re-download all prices")
    args = parser.parse_args()

    db = get_db()
    run_migration(db)

    portfolios = load_portfolios(db)
    log.info("Loaded %d portfolios", len(portfolios))

    # Filter if --model specified
    if args.model:
        portfolios = [p for p in portfolios if p["name"].lower() == args.model.lower()]
        if not portfolios:
            log.error("Model '%s' not found. Available: %s", args.model,
                      [p["name"] for p in load_portfolios(db)])
            sys.exit(1)

    # Get all tickers and fetch prices
    all_tickers = get_all_tickers(db)
    prices_df   = fetch_prices(db, all_tickers, force_refresh=args.refresh_prices)
    if prices_df.empty:
        log.error("No price data available. Check yfinance connectivity."); sys.exit(1)

    log.info("Price matrix: %d dates × %d tickers", *prices_df.shape)

    # Build nav for HTML
    pages = [(p["name"], f"analytics-{p['id'].replace('_','-')}") for p in portfolios]
    pages += [("Comparison", "analytics-comparison"), ("Firm-Wide", "analytics-firmwide")]

    all_results = []
    stress_all  = []

    for portfolio in portfolios:
        a_itd, a_ytd, alloc_df, stress = run_model(
            portfolio, prices_df, db,
            gen_html=(args.all or args.model is not None),
            pages=pages
        )
        all_results.append((portfolio, a_itd))
        stress_all.extend(stress or [])

    # Comparison + firm-wide pages (only when running --all)
    if args.all and len(portfolios) > 1:
        log.info("Building comparison page…")
        comp_html = build_comparison_page(all_results, pages)
        (REPORTS_DIR / "analytics-comparison.html").write_text(comp_html, encoding="utf-8")

        log.info("Building firm-wide page…")
        fw_html = build_firmwide_page(all_results, prices_df, pages, stress_all, db)
        (REPORTS_DIR / "analytics-firmwide.html").write_text(fw_html, encoding="utf-8")

    db.close()

    log.info("\n✅  Done. Open with:")
    if args.all:
        log.info("  open ~/Documents/investment-os/reports/analytics-comparison.html")
    if args.model:
        pid = portfolios[0]["id"]
        log.info("  open ~/Documents/investment-os/reports/analytics-%s.html",
                 pid.replace("_","-"))


if __name__ == "__main__":
    main()
