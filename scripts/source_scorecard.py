#!/usr/bin/env python3
"""
source_scorecard.py — Retroactive source skill scorecard (Regime Framework v2, Phase 1).

Scores every research source's directional calls against the IWP Risk Composite:
a 3-family, 6-instrument risk-on/risk-off proxy.

  Credit family : -1 * HY OAS 20d change (FRED BAMLH0A0HYM2), HYG-IEF daily return
  Equity family : SPHB-SPLV, XLY-XLP, SPY daily returns
  Macro family  : copper/gold (HG=F / GC=F) daily log change

Each leg is z-scored over the sample; families are averaged internally, then
equally weighted into the composite. A note's call "hits" if its signal sign
matches the sign of the cumulative composite over the forward horizon.

Skill baseline: naive persistence — predict the sign of the NEXT horizon from
the sign of the TRAILING horizon. Skill = hit_rate - baseline_hit_rate on the
same dates (positive = the source beats just extrapolating the recent tape).

Per framework rules: scores on fewer than 8 resolved claims are shown as
"insufficient sample", never as a number to rank on.
"""

import os
import sys
import io
import urllib.request
from datetime import date

import numpy as np
import pandas as pd
import psycopg2
import yfinance as yf

START = "2026-01-01"
HORIZONS = [5, 21]          # sessions; 63d unresolvable for most of a 3-month ledger
MIN_RESOLVED = 8

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=BAMLH0A0HYM2"


def fetch_composite():
    tickers = ["HYG", "IEF", "SPHB", "SPLV", "XLY", "XLP", "SPY", "HG=F", "GC=F"]
    px = yf.download(tickers, start=START, auto_adjust=True, progress=False)["Close"]
    px = px.ffill().dropna(how="all")

    import subprocess
    raw = subprocess.run(["curl", "-sL", "--max-time", "40", FRED_CSV],
                         capture_output=True, text=True, check=True).stdout
    oas = pd.read_csv(io.StringIO(raw))
    oas.columns = ["date", "oas"]
    oas["date"] = pd.to_datetime(oas["date"])
    oas = oas.set_index("date")["oas"].replace(".", np.nan).astype(float).ffill()
    oas = oas[oas.index >= START]

    z = lambda s: (s - s.mean()) / s.std()

    credit = pd.concat(
        [z(-oas.diff(20).reindex(px.index).ffill()),
         z(px["HYG"].pct_change() - px["IEF"].pct_change())], axis=1).mean(axis=1)
    equity = pd.concat(
        [z(px["SPHB"].pct_change() - px["SPLV"].pct_change()),
         z(px["XLY"].pct_change() - px["XLP"].pct_change()),
         z(px["SPY"].pct_change())], axis=1).mean(axis=1)
    macro = z(np.log(px["HG=F"] / px["GC=F"]).diff())

    comp = pd.concat([credit, equity, macro], axis=1, keys=["credit", "equity", "macro"])
    comp["composite"] = comp.mean(axis=1)
    return comp.dropna(subset=["composite"])


def forward_sign(comp, d, horizon):
    """Sign of cumulative composite over the `horizon` sessions AFTER date d.
    Returns (sign, resolved?)."""
    idx = comp.index.searchsorted(pd.Timestamp(d), side="right")
    fwd = comp["composite"].iloc[idx: idx + horizon]
    if len(fwd) < horizon:
        return 0, False
    return (1 if fwd.sum() > 0 else -1), True


def trailing_sign(comp, d, horizon):
    idx = comp.index.searchsorted(pd.Timestamp(d), side="right")
    tr = comp["composite"].iloc[max(0, idx - horizon): idx]
    if len(tr) < horizon:
        return 0
    return 1 if tr.sum() > 0 else -1


def main():
    comp = fetch_composite()
    print(f"Risk composite built: {comp.index[0].date()} → {comp.index[-1].date()} "
          f"({len(comp)} sessions)\n")

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("""
        SELECT source_id, ingested_at::date, signal_strength
        FROM research_notes
        WHERE signal_strength IS NOT NULL AND ABS(signal_strength) >= 0.25
        ORDER BY ingested_at
    """)
    notes = [(s, d, float(w)) for s, d, w in cur.fetchall()]
    cur.close(); conn.close()
    print(f"{len(notes)} directional claims (|strength| >= 0.25) in the ledger\n")

    for hz in HORIZONS:
        rows = {}
        base_hits = base_n = 0
        for src, d, w in notes:
            sign_fwd, ok = forward_sign(comp, d, hz)
            if not ok:
                continue
            call = 1 if w > 0 else -1
            r = rows.setdefault(src, {"n": 0, "hits": 0, "bhits": 0})
            r["n"] += 1
            r["hits"] += int(call == sign_fwd)
            tr = trailing_sign(comp, d, hz)
            r["bhits"] += int(tr != 0 and tr == sign_fwd)

        print(f"═══ Horizon: {hz} sessions ═══")
        print(f"{'source':<20}{'n':>4}{'hit%':>7}{'base%':>7}{'skill':>7}")
        ranked = sorted(rows.items(), key=lambda kv: -(kv[1]["hits"] / kv[1]["n"]))
        for src, r in ranked:
            hit = r["hits"] / r["n"] * 100
            base = r["bhits"] / r["n"] * 100
            skill = hit - base
            if r["n"] < MIN_RESOLVED:
                print(f"{src:<20}{r['n']:>4}   insufficient sample")
            else:
                print(f"{src:<20}{r['n']:>4}{hit:>6.0f}%{base:>6.0f}%{skill:>+6.0f}")
        tot_n = sum(r["n"] for r in rows.values())
        tot_h = sum(r["hits"] for r in rows.values())
        tot_b = sum(r["bhits"] for r in rows.values())
        print(f"{'ALL RESEARCH':<20}{tot_n:>4}{tot_h/tot_n*100:>6.0f}%"
              f"{tot_b/tot_n*100:>6.0f}%{(tot_h-tot_b)/tot_n*100:>+6.0f}\n")


if __name__ == "__main__":
    main()
