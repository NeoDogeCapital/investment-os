#!/usr/bin/env python3
"""
factor_engine.py — Factor exposure regressions per model + FIRM.

Regresses each model's daily portfolio return (holdings-weighted) on factor-ETF
daily returns: SPY (market), MTUM, QUAL, VLUE, USMV, SPHB (each vs SPY, so
they read as tilts), IEF (duration), GLD (gold/real-asset).
Multivariate OLS over the trailing 252 sessions; betas + R^2 stored.
Ported conceptually from IC's factor_exposure.py.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_market_date import us_market_date  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import psycopg2  # noqa: E402

FACTORS = ["SPY", "MTUM", "QUAL", "VLUE", "USMV", "SPHB", "IEF", "GLD"]


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    stamp = us_market_date()

    cur.execute("""WITH latest AS (SELECT model_name, MAX(snapshot_date) sd
                     FROM model_holdings_snapshot GROUP BY model_name)
                   SELECT h.model_name, h.ticker, h.weight FROM model_holdings_snapshot h
                   JOIN latest l ON l.model_name=h.model_name AND l.sd=h.snapshot_date""")
    hold = pd.DataFrame(cur.fetchall(), columns=["model", "ticker", "w"])
    hold["w"] = hold["w"].astype(float)
    cur.execute("SELECT name, aum_usd FROM model_portfolios WHERE aum_usd IS NOT NULL")
    aum = {n: float(a) for n, a in cur.fetchall()}

    tickers = sorted(set(hold.ticker) | set(FACTORS))
    cur.execute("""SELECT ticker, price_date, close_price FROM ticker_price_history
                   WHERE ticker=ANY(%s) AND price_date >= CURRENT_DATE - INTERVAL '440 days'""",
                (tickers,))
    px = pd.DataFrame(cur.fetchall(), columns=["ticker", "date", "close"])
    C = px.pivot_table(index="date", columns="ticker", values="close").astype(float)
    C = C.where(C > 0).ffill()
    R = C.pct_change().replace([np.inf, -np.inf], np.nan)
    R = R.where(R.abs() < 0.5)          # drop impossible daily moves (bad ticks/splits)
    missing = [f for f in FACTORS if f not in R.columns]
    if missing:
        raise SystemExit(f"missing factor history: {missing}")

    F = R[FACTORS].dropna()
    # express style factors as tilts vs SPY; SPY stays as market beta
    X = pd.DataFrame({
        "spy": F["SPY"],
        "momentum": F["MTUM"] - F["SPY"], "quality": F["QUAL"] - F["SPY"],
        "value": F["VLUE"] - F["SPY"], "minvol": F["USMV"] - F["SPY"],
        "highbeta": F["SPHB"] - F["SPY"],
        "rates": F["IEF"], "gold": F["GLD"],
    }).replace([np.inf, -np.inf], np.nan).dropna()
    X = X[np.isfinite(X.values).all(axis=1)].tail(252)

    rows, firm_parts = [], []
    for model, grp in hold.groupby("model"):
        w = grp.set_index("ticker")["w"]
        held = [t for t in w.index if t in R.columns]
        pr = (R[held] * w[held]).sum(axis=1, min_count=max(1, len(held) // 2)).reindex(X.index).dropna()
        pr = pr[np.isfinite(pr.values)]
        if len(pr) < 60:
            print(f"  {model}: insufficient clean history ({len(pr)} sessions) - skipped")
            continue
        Xa = X.loc[pr.index]
        ok = np.isfinite(Xa.values).all(axis=1)
        Xa, pr = Xa[ok], pr[ok]
        if len(pr) < 60:
            print(f"  {model}: insufficient clean factor overlap - skipped"); continue
        A = np.column_stack([np.ones(len(Xa)), Xa.values])
        beta, res, *_ = np.linalg.lstsq(A, pr.values, rcond=None)
        pred = A @ beta
        ss_res = float(((pr.values - pred) ** 2).sum())
        ss_tot = float(((pr.values - pr.values.mean()) ** 2).sum())
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
        b = dict(zip(["const", "spy", "momentum", "quality", "value", "minvol",
                      "highbeta", "rates", "gold"], beta))
        rows.append((stamp, model, *[round(float(b[k]), 3) for k in
                     ("spy", "momentum", "quality", "value", "minvol", "highbeta",
                      "rates", "gold")], round(r2, 3)))
        firm_parts.append((aum.get(model, 0), b, r2))

    total = sum(a for a, _, _ in firm_parts) or 1
    fb = {k: sum(a * b[k] for a, b, _ in firm_parts) / total
          for k in ("spy", "momentum", "quality", "value", "minvol", "highbeta", "rates", "gold")}
    fr2 = sum(a * r for a, _, r in firm_parts) / total
    rows.append((stamp, "FIRM", *[round(float(fb[k]), 3) for k in
                 ("spy", "momentum", "quality", "value", "minvol", "highbeta",
                  "rates", "gold")], round(fr2, 3)))

    cur.execute("DELETE FROM factor_exposures WHERE score_date=%s", (stamp,))
    cur.executemany("""INSERT INTO factor_exposures
        (score_date, model_name, beta_spy, beta_momentum, beta_quality, beta_value,
         beta_minvol, beta_highbeta, beta_rates, beta_gold, r2)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
    conn.commit()
    for r in rows:
        print(f"  {r[1]:<22} mkt={r[2]:+.2f} mom={r[3]:+.2f} qual={r[4]:+.2f} "
              f"val={r[5]:+.2f} rates={r[8]:+.2f} gold={r[9]:+.2f} R2={r[10]:.2f}")
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
