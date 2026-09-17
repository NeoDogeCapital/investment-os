#!/usr/bin/env python3
"""
risk_engine.py — PRISM-style portfolio risk scoring (1-10) per model + firm.

Modeled on StratiFi PRISM 2.0's four pillars, adapted to available data:
  1. Volatility  — recency-weighted (last 12m counts ~2x older data)
  2. VaR         — 95% daily VaR (historical) + tail-event frequency, in % and $
  3. Concentration — top-position weight, HHI, effective N (ticker-level;
       intra-fund look-through requires fund holdings data we do not have — flagged)
  4. Security classification — hedged/buffer, income-overlay, alternative,
       bond, cash and real-asset sleeves carry different risk multipliers;
       a hedged-equity ETF is not open beta and is not scored as such.

Writes one row per model + one FIRM row to risk_scores. Run post-close.
"""

import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_market_date import us_market_date  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import psycopg2  # noqa: E402

# ── Security classification (PRISM pillar 4). Multiplier scales the equity-risk
#    contribution of the sleeve; 1.0 = full open equity risk.
CLASS_MAP = {
    "hedged_equity":  (0.60, ["KSPY", "HEQT", "QNZIX", "QDSIX", "BDMIX"]),
    "income_overlay": (0.80, ["SPYI", "FEOE", "CPAI", "TIBIX", "FEPI"]),
    "core_equity":    (1.00, ["PRWCX", "SGOIX", "HOBIX", "APDTX", "APDPX", "FWD", "SHLD"]),
    "intl_em":        (1.10, ["CGXU", "EMEQ", "EMEQ.L"]),
    "alternative":    (0.55, ["AIWEX", "VOLT", "RSNYX", "QCFIX", "MAGRX", "BFGIX"]),
    "real_asset":     (0.70, ["PHYS", "INFL", "GLD"]),
    "bond":           (0.35, ["AGGH", "LCTIX", "BUXX", "CLOX"]),
    "cash_like":      (0.05, ["USFR", "CASH", "SGOV", "BIL"]),
}
TICKER_CLASS = {t: (name, mult) for name, (mult, ts) in CLASS_MAP.items() for t in ts}


def recency_weighted_vol(returns: pd.Series) -> float:
    """Annualized daily vol with the last 252 sessions weighted 2x vs older."""
    r = returns.dropna()
    if len(r) < 40:
        return float("nan")
    w = np.ones(len(r))
    w[-252:] = 2.0
    mu = np.average(r, weights=w)
    var = np.average((r - mu) ** 2, weights=w)
    return float(np.sqrt(var) * np.sqrt(252))


def hist_var(returns: pd.Series, q=0.05):
    r = returns.dropna()
    if len(r) < 60:
        return float("nan"), float("nan")
    var = float(-np.quantile(r, q))
    tail_freq = float((r < -2 * r.std()).mean())          # PRISM tail-frequency idea
    return var, tail_freq


def scale(x, lo, hi):
    """Map x in [lo,hi] -> 1..10, clipped."""
    if x != x:
        return None
    return float(np.clip(1 + 9 * (x - lo) / (hi - lo), 1, 10))


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    stamp = us_market_date()

    cur.execute("""WITH latest AS (SELECT model_name, MAX(snapshot_date) sd
                     FROM model_holdings_snapshot GROUP BY model_name)
                   SELECT h.model_name, h.ticker, h.weight
                   FROM model_holdings_snapshot h
                   JOIN latest l ON l.model_name=h.model_name AND l.sd=h.snapshot_date""")
    hold = pd.DataFrame(cur.fetchall(), columns=["model", "ticker", "w"])
    hold["w"] = hold["w"].astype(float)

    cur.execute("SELECT name, aum_usd FROM model_portfolios WHERE aum_usd IS NOT NULL")
    aum = dict(cur.fetchall())

    tickers = [t for t in hold.ticker.unique() if t != "CASH"]
    cur.execute("""SELECT ticker, price_date, daily_return FROM ticker_price_history
                   WHERE ticker = ANY(%s) AND price_date >= %s""",
                (tickers, stamp - timedelta(days=900)))
    px = pd.DataFrame(cur.fetchall(), columns=["ticker", "date", "ret"])
    rets = px.pivot_table(index="date", columns="ticker", values="ret", aggfunc="last").astype(float)

    rows = []
    for model, grp in hold.groupby("model"):
        w = grp.set_index("ticker")["w"]
        held = [t for t in w.index if t in rets.columns]
        pr = (rets[held] * w[held]).sum(axis=1, min_count=max(1, len(held) // 2)).dropna()

        vol = recency_weighted_vol(pr)
        var95, tail = hist_var(pr)
        noncash = w[w.index != "CASH"]
        top_w = float(noncash.max()) if len(noncash) else 0.0
        hhi = float((noncash ** 2).sum())
        eff_n = 1.0 / hhi if hhi > 0 else 0.0
        struct = float(sum(w.get(t, 0) * TICKER_CLASS.get(t, ("core_equity", 1.0))[1]
                           for t in w.index))
        cash_w = float(w.get("CASH", 0.0))

        s_vol = scale(vol, 0.02, 0.25)
        s_var = scale(var95, 0.002, 0.030)
        s_con = scale(0.6 * top_w + 0.4 * hhi, 0.05, 0.30)
        s_str = scale(struct, 0.10, 1.00)
        parts = [(s_vol, .35), (s_var, .25), (s_con, .15), (s_str, .25)]
        avail = [(v, wt) for v, wt in parts if v is not None]
        score = sum(v * wt for v, wt in avail) / sum(wt for _, wt in avail)

        model_aum = float(aum.get(model, 0))
        var_dollars = var95 * model_aum if var95 == var95 else None
        rows.append((stamp, model, round(score, 2),
                     round(s_vol, 2) if s_vol else None, round(s_var, 2) if s_var else None,
                     round(s_con, 2) if s_con else None, round(s_str, 2) if s_str else None,
                     round(vol, 4) if vol == vol else None,
                     round(var95, 5) if var95 == var95 else None,
                     round(var_dollars) if var_dollars else None,
                     round(tail, 4) if tail == tail else None,
                     round(top_w, 4), round(hhi, 4), round(eff_n, 1), round(cash_w, 4),
                     model_aum))

    # FIRM row: AUM-weighted composite + firm-level dollar VaR
    total_aum = sum(r[-1] for r in rows) or 1
    firm_score = sum(r[2] * r[-1] for r in rows) / total_aum
    firm_var_d = sum((r[9] or 0) for r in rows)
    firm_cash = sum(r[14] * r[-1] for r in rows) / total_aum
    rows.append((stamp, "FIRM", round(firm_score, 2), None, None, None, None,
                 None, None, round(firm_var_d), None, None, None, None,
                 round(firm_cash, 4), total_aum))

    cur.execute("DELETE FROM risk_scores WHERE score_date=%s", (stamp,))
    cur.executemany("""INSERT INTO risk_scores
        (score_date, model_name, risk_score, s_volatility, s_var, s_concentration,
         s_structure, ann_volatility, var95_daily, var95_dollars, tail_freq,
         top_position_w, hhi, effective_n, cash_weight, aum_usd)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
    conn.commit()
    for r in rows:
        print(f"  {r[1]:<22} score={r[2]}  VaR95$={r[9] if r[9] else '—'}")
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
