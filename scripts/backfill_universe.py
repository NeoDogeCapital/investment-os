#!/usr/bin/env python3
"""backfill_universe.py — Resumable 2y price backfill for universe tickers lacking history.
Commits per batch so an interruption loses at most one batch. Deactivates tickers
that return no data (delisted / unsupported symbols) so the engines stop retrying them."""
import os, time, warnings
warnings.filterwarnings("ignore")
import psycopg2, yfinance as yf

SQL = """INSERT INTO ticker_price_history
  (ticker,price_date,open_price,high_price,low_price,close_price,adj_close,volume,source,fetched_at)
  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'yfinance-universe',NOW()) ON CONFLICT (ticker,price_date) DO NOTHING"""

conn = psycopg2.connect(os.environ["DATABASE_URL"]); cur = conn.cursor()
cur.execute("UPDATE universe SET active=false, notes='unresolvable symbol' WHERE ticker LIKE '%%\\_%%' AND active")
conn.commit()
cur.execute("""SELECT u.ticker FROM universe u
  LEFT JOIN (SELECT ticker, COUNT(*) n FROM ticker_price_history GROUP BY 1) p ON p.ticker=u.ticker
  WHERE u.active AND COALESCE(p.n,0) < 60 ORDER BY 1""")
need = [r[0] for r in cur.fetchall()]
print(f"backfilling {len(need)} tickers", flush=True)
ok, bad = 0, []
for i in range(0, len(need), 15):
    chunk = need[i:i+15]; rows = []
    try:
        px = yf.download(chunk, period="2y", auto_adjust=False, progress=False,
                         group_by="ticker", threads=False, timeout=30)
    except Exception:
        bad += chunk; continue
    for t in chunk:
        try:
            df = px[t].dropna(subset=["Close"])
            if len(df) < 60: bad.append(t); continue
            rows += [(t, d.date(), float(r["Open"]), float(r["High"]), float(r["Low"]),
                      float(r["Close"]), float(r.get("Adj Close", r["Close"])),
                      int(r.get("Volume", 0) or 0)) for d, r in df.iterrows()]
            ok += 1
        except Exception:
            bad.append(t)
    for j in range(0, len(rows), 2000):
        cur.executemany(SQL, rows[j:j+2000]); conn.commit()
    print(f"  batch {i//15+1}: +{len(rows)} bars (resolved so far {ok})", flush=True)
    time.sleep(0.3)
if bad:
    cur.execute("UPDATE universe SET active=false, notes='no price data from yfinance' WHERE ticker=ANY(%s)", (bad,))
    conn.commit()
print(f"DONE: resolved {ok}, deactivated {len(bad)}: {sorted(bad)[:25]}", flush=True)
cur.close(); conn.close()
