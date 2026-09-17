#!/usr/bin/env python3
"""
post_close.py — Post-close cloud chain (GitHub Actions, 4:45pm ET weekdays).

Order: final price refresh -> analytics engine -> regime scanner -> memo
generation -> memo stored to regime_memos. Everything the model computes,
recomputed nightly in the cloud, written to the database the site reads.

NOT in this chain (stays local, by design):
  - inbox_processor (needs the Obsidian vault on Niko's Mac)
  - trade gates / holdings changes (human decisions)

Env: DATABASE_URL (pooler host), ANTHROPIC_API_KEY.
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
sys.path.insert(0, SCRIPTS)
from lib_market_date import us_market_date  # noqa: E402

import psycopg2  # noqa: E402


def log_run(job, status, detail):
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO job_runs (job_name, market_date, status, detail) VALUES (%s,%s,%s,%s)",
        (job, us_market_date(), status, detail[:500]),
    )
    conn.commit(); cur.close(); conn.close()


def step(name, cmd):
    print(f"── {name} ──")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    tail = (r.stdout + r.stderr)[-400:]
    if r.returncode != 0:
        log_run(f"post_close:{name}", "error", tail)
        raise SystemExit(f"{name} failed:\n{tail}")
    log_run(f"post_close:{name}", "ok", tail)


def store_memo():
    """Take the memo HTML generate_reports just wrote and store it in the DB."""
    import glob
    memos = sorted(glob.glob(os.path.join(HERE, "..", "reports", "regime-memo-*.html")))
    if not memos:
        log_run("post_close:store_memo", "error", "no memo file produced")
        return
    latest = memos[-1]
    html = open(latest).read()
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO regime_memos (memo_date, html) VALUES (%s,%s)
           ON CONFLICT (memo_date) DO UPDATE SET html = EXCLUDED.html,
           created_at = NOW()""",
        (us_market_date(), html),
    )
    conn.commit(); cur.close(); conn.close()
    log_run("post_close:store_memo", "ok", os.path.basename(latest))


def main():
    py = sys.executable
    step("prices", [py, os.path.join(HERE, "refresh_prices.py"), "--force"])
    step("analytics", [py, os.path.join(SCRIPTS, "analytics_engine.py"), "--all"])
    step("scanner", [py, os.path.join(SCRIPTS, "regime_scanner.py")])
    step("risk", [py, os.path.join(SCRIPTS, "risk_engine.py")])
    step("technicals", [py, os.path.join(SCRIPTS, "technical_engine.py")])
    step("factors", [py, os.path.join(SCRIPTS, "factor_engine.py")])
    step("outlook", [py, os.path.join(SCRIPTS, "daily_outlook.py")])
    step("memo", [py, os.path.join(SCRIPTS, "generate_reports.py"), "--memo-only"])
    store_memo()
    log_run("post_close", "ok", "chain complete")
    print("post-close chain complete")


if __name__ == "__main__":
    main()
