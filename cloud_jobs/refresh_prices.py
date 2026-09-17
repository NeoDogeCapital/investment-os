#!/usr/bin/env python3
"""
refresh_prices.py — Cloud price refresh (GitHub Actions, market hours).

Fetches current-session prices for every ticker in the live books and writes
them to ticker_price_history stamped with us_market_date(). Idempotent per
(ticker, date): re-runs update the same row, so a 30-minute cadence keeps the
session row live without duplicating.

Env: DATABASE_URL (Supabase POOLER host — direct host is IPv6-only and
unreachable from Actions runners).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from lib_market_date import us_market_date, is_market_hours  # noqa: E402

import psycopg2  # noqa: E402
import yfinance as yf  # noqa: E402

FORCE = "--force" in sys.argv  # post-close chain calls with --force


def log_run(cur, status, detail):
    cur.execute(
        "INSERT INTO job_runs (job_name, market_date, status, detail) VALUES (%s,%s,%s,%s)",
        ("refresh_prices", us_market_date(), status, detail[:500]),
    )


def main():
    if not is_market_hours() and not FORCE:
        print("outside market hours; skipping (use --force to override)")
        return

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    try:
        cur.execute(
            """SELECT DISTINCT ticker FROM model_holdings_snapshot h
               WHERE ticker != 'CASH' AND snapshot_date = (
                 SELECT MAX(snapshot_date) FROM model_holdings_snapshot
                 WHERE model_name = h.model_name)"""
        )
        tickers = sorted(r[0] for r in cur.fetchall())
        stamp = us_market_date()
        px = yf.download(tickers, period="5d", auto_adjust=False,
                         progress=False, group_by="ticker")

        wrote, missing = 0, []
        for t in tickers:
            try:
                df = px[t].dropna(subset=["Close"])
                if df.empty:
                    missing.append(t)
                    continue
                row = df.iloc[-1]
                cur.execute(
                    """INSERT INTO ticker_price_history
                       (ticker, price_date, open_price, high_price, low_price,
                        close_price, adj_close, volume, source, fetched_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'yfinance-cloud',NOW())
                       ON CONFLICT (ticker, price_date) DO UPDATE SET
                         close_price = EXCLUDED.close_price,
                         high_price  = GREATEST(ticker_price_history.high_price, EXCLUDED.high_price),
                         low_price   = LEAST(ticker_price_history.low_price, EXCLUDED.low_price),
                         adj_close   = EXCLUDED.adj_close,
                         volume      = EXCLUDED.volume,
                         fetched_at  = NOW()""",
                    (t, stamp, float(row["Open"]), float(row["High"]),
                     float(row["Low"]), float(row["Close"]),
                     float(row.get("Adj Close", row["Close"])),
                     int(row.get("Volume", 0) or 0)),
                )
                wrote += 1
            except Exception as e:  # one bad ticker must not sink the run
                missing.append(f"{t}({e})")

        detail = f"wrote {wrote}/{len(tickers)}" + (f"; missing: {', '.join(missing[:8])}" if missing else "")
        log_run(cur, "ok" if wrote else "error", detail)
        conn.commit()
        print(detail)
    except Exception as e:
        conn.rollback()
        log_run(cur, "error", str(e))
        conn.commit()
        raise
    finally:
        cur.close(); conn.close()


if __name__ == "__main__":
    main()
