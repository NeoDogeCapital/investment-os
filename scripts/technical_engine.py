#!/usr/bin/env python3
"""
technical_engine.py — Nightly technicals for every fund in the lineup plus the
macro watchlist. Ported conceptually from Integrity Compounders' engine,
adapted for a fund universe (mutual funds have NAV-only bars: no volume, no
intraday range — ATR degrades to return-vol and VWAPs to anchored averages).

Per ticker: trend state (200d anchor + 26d EMA track line with 1.5-ATR flip),
RSI(14) + regime, realized vol, % off 52w high/low, anchored VWAP/averages
(YTD and 52w-low), 63d relative strength vs SPY, calibrated risk ranges
(1w / 1m) with position-in-range, and a timing label:
  buy_zone | accumulate | neutral | wait_range_low | trim_zone | avoid_extended

Timing paces entries/exits; it never decides what is ownable (IC's rule).
Writes fund_technicals; run in the post-close chain.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_market_date import us_market_date  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import psycopg2  # noqa: E402

MACRO_WATCH = ["SPY", "QQQ", "IWM", "GLD", "USO", "UUP", "HYG", "IEF", "TLT",
               "XLY", "XLP", "SPHB", "SPLV", "EEM", "FXI", "^VIX", "^TNX"]


def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def compute(df, spy_close):
    """df: date-indexed OHLC(V) for one ticker, ascending."""
    c = df["close"].astype(float)
    if len(c) < 60:
        return None
    h = df["high"].astype(float).fillna(c)
    l = df["low"].astype(float).fillna(c)

    # ATR — degrade to return-vol-in-price for NAV-only funds
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = float(tr.rolling(14).mean().iloc[-1] or 0)
    ret_sd = float(c.pct_change().rolling(20).std().iloc[-1] or 0)
    if atr < c.iloc[-1] * 0.0015:                      # NAV-only: fake range
        atr = c.iloc[-1] * ret_sd * 1.4

    px = float(c.iloc[-1])
    ma50 = float(c.rolling(50).mean().iloc[-1])
    ma200 = float(c.rolling(200).mean().iloc[-1]) if len(c) >= 200 else None
    ema26 = c.ewm(span=26, adjust=False).mean()
    track = float(ema26.iloc[-1])

    # trend: 200d anchor when available, else 50d; track-line confirms
    anchor = ma200 if ma200 else ma50
    above = px > anchor
    slope = float(ema26.iloc[-1] - ema26.iloc[-10]) if len(ema26) > 10 else 0
    if above and px > track and slope > 0:   trend = "bullish"
    elif not above and px < track and slope < 0: trend = "bearish"
    else: trend = "neutral"

    r = float(rsi(c).iloc[-1] or 50)
    rsi_regime = "oversold" if r < 30 else "weak" if r < 45 else "neutral" if r < 60 else "strong" if r < 72 else "overbought"

    hi52 = float(c.tail(252).max()); lo52 = float(c.tail(252).min())
    off_hi = px / hi52 - 1; off_lo = px / lo52 - 1

    # anchored averages (volume-weighted when real volume exists)
    vol = df.get("volume", pd.Series(0, index=df.index)).astype(float)
    def anchored(start_idx):
        seg_c = c.loc[start_idx:]; seg_v = vol.loc[start_idx:]
        if seg_v.sum() > 1000:
            return float((seg_c * seg_v).sum() / seg_v.sum())
        return float(seg_c.mean())
    ytd_start = c.index[c.index.searchsorted(pd.Timestamp(f"{us_market_date().year}-01-01"))]
    avwap_ytd = anchored(ytd_start)
    avwap_lo = anchored(c.tail(252).idxmin())

    rs_63 = None
    if spy_close is not None and len(spy_close) > 63:
        a = c.reindex(spy_close.index).ffill()
        rs_63 = float((a.iloc[-1] / a.iloc[-63]) / (spy_close.iloc[-1] / spy_close.iloc[-63]) - 1)

    # risk ranges: close ± k·ATR·sqrt(sessions)
    rng = {}
    for label, sessions, k in (("1w", 5, 1.1), ("1m", 21, 1.0)):
        w = atr * k * np.sqrt(sessions)
        lo_b, hi_b = px - w, px + w
        rng[label] = (round(lo_b, 2), round(hi_b, 2))
    pos_1m = (px - rng["1m"][0]) / max(rng["1m"][1] - rng["1m"][0], 1e-9)

    ext = (px - track) / atr if atr > 0 else 0
    if trend == "bullish":
        timing = ("avoid_extended" if ext > 2.2 else
                  "trim_zone" if pos_1m > 0.8 else
                  "buy_zone" if pos_1m < 0.25 else
                  "accumulate" if pos_1m < 0.55 else "neutral")
    elif trend == "bearish":
        timing = "wait_range_low" if pos_1m > 0.35 else "buy_zone" if r < 32 else "wait_range_low"
    else:
        timing = "accumulate" if pos_1m < 0.3 and r < 45 else "trim_zone" if pos_1m > 0.85 else "neutral"

    return dict(price=round(px, 2), trend=trend, track_line=round(track, 2),
                ma50=round(ma50, 2), ma200=round(ma200, 2) if ma200 else None,
                rsi=round(r, 1), rsi_regime=rsi_regime,
                vol20=round(ret_sd * np.sqrt(252), 4),
                off_high_52w=round(off_hi, 4), off_low_52w=round(off_lo, 4),
                avwap_ytd=round(avwap_ytd, 2), avwap_52w_low=round(avwap_lo, 2),
                rel_strength_63d=round(rs_63, 4) if rs_63 is not None else None,
                range_1w_lo=rng["1w"][0], range_1w_hi=rng["1w"][1],
                range_1m_lo=rng["1m"][0], range_1m_hi=rng["1m"][1],
                pos_in_range_1m=round(float(np.clip(pos_1m, 0, 1)), 3),
                timing=timing)


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    stamp = us_market_date()

    cur.execute("SELECT ticker FROM universe WHERE active")
    universe = sorted({r[0] for r in cur.fetchall()} | set(MACRO_WATCH))

    cur.execute("""SELECT ticker, price_date, open_price, high_price, low_price,
                          close_price, volume
                   FROM ticker_price_history WHERE ticker = ANY(%s)
                   ORDER BY ticker, price_date""", (universe,))
    df = pd.DataFrame(cur.fetchall(),
                      columns=["ticker", "date", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["date"])

    spy = df[df.ticker == "SPY"].set_index("date")["close"].astype(float)
    spy = spy if len(spy) else None

    rows, skipped = [], []
    for t, g in df.groupby("ticker"):
        try:
            res = compute(g.set_index("date").sort_index(), spy)
            if res is None:
                skipped.append(t); continue
            is_macro = t in MACRO_WATCH
            clean = [None if v is None else (float(v) if isinstance(v,(int,float)) or hasattr(v,"item") else v) for v in res.values()]
            rows.append((stamp, t, is_macro, *clean))
        except Exception as e:
            skipped.append(f"{t}({e})")

    cols = ("score_date,ticker,is_macro,price,trend,track_line,ma50,ma200,rsi,rsi_regime,"
            "vol20,off_high_52w,off_low_52w,avwap_ytd,avwap_52w_low,rel_strength_63d,"
            "range_1w_lo,range_1w_hi,range_1m_lo,range_1m_hi,pos_in_range_1m,timing")
    cur.execute("DELETE FROM fund_technicals WHERE score_date=%s", (stamp,))
    cur.executemany(
        f"INSERT INTO fund_technicals ({cols}) VALUES ({','.join(['%s'] * 22)})", rows)
    conn.commit()
    print(f"technicals written: {len(rows)} tickers ({stamp}); skipped: {skipped or 'none'}")
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
