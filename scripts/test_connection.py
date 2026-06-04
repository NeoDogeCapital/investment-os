#!/usr/bin/env python3
"""
test_connection.py — Verify all IWP Investment OS dependencies.

Usage:
    python scripts/test_connection.py
"""

import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent

def load_env():
    env_file = PROJECT_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))

load_env()

PASS = "  ✓"
FAIL = "  ✗"
results = []

def check(label, fn):
    try:
        msg = fn()
        results.append((True, label, msg or ""))
        print(f"{PASS} {label}{(' — ' + msg) if msg else ''}")
    except Exception as e:
        results.append((False, label, str(e)))
        print(f"{FAIL} {label} — {e}")

print("\n" + "═" * 55)
print("  IWP Investment OS — Connection Check")
print("═" * 55)

# ── 1. Supabase connection ────────────────────────────────────
def check_supabase():
    import psycopg2
    db_url = os.environ.get("DATABASE_URL","")
    if not db_url:
        raise ValueError("DATABASE_URL not set in .env")
    conn = psycopg2.connect(db_url)
    with conn.cursor() as cur:
        cur.execute("SELECT version()")
        ver = cur.fetchone()[0].split(",")[0]
    conn.close()
    return ver

check("Supabase connection", check_supabase)

# ── 2. All required tables ────────────────────────────────────
REQUIRED_TABLES = [
    "sources","research_notes","regime_snapshots","regime_stack",
    "positions","model_rules","decision_journal","model_portfolios",
    "model_allocations","model_snapshot_performance","image_signals",
    "ticker_price_history","model_analytics",
]

def check_tables():
    import psycopg2
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    with conn.cursor() as cur:
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
        """)
        existing = {r[0] for r in cur.fetchall()}
    conn.close()
    missing = [t for t in REQUIRED_TABLES if t not in existing]
    if missing:
        raise ValueError(f"Missing tables: {missing}")
    return f"{len(existing)} tables found"

check("All DB tables exist", check_tables)

# ── 3. Sources seeded ─────────────────────────────────────────
def check_sources():
    import psycopg2
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM sources WHERE active=TRUE")
        n = cur.fetchone()[0]
    conn.close()
    if n < 10:
        raise ValueError(f"Only {n} active sources (expected ≥10)")
    return f"{n} active sources"

check("Sources seeded", check_sources)

# ── 4. Anthropic API ──────────────────────────────────────────
def check_anthropic():
    import anthropic
    key = os.environ.get("ANTHROPIC_API_KEY","")
    if not key or not key.startswith("sk-ant-"):
        raise ValueError("ANTHROPIC_API_KEY not set or malformed")
    client = anthropic.Anthropic(api_key=key)
    resp = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=10,
        messages=[{"role":"user","content":"Reply: OK"}]
    )
    return f"claude-sonnet-4-6 responded: {resp.content[0].text.strip()}"

check("Anthropic API", check_anthropic)

# ── 5. Obsidian vault ─────────────────────────────────────────
def check_vault():
    vault = os.environ.get("OBSIDIAN_VAULT_PATH","")
    if not vault:
        raise ValueError("OBSIDIAN_VAULT_PATH not set")
    p = Path(vault)
    if not p.exists():
        raise ValueError(f"Vault not found: {vault}")
    md_count = len(list(p.rglob("*.md")))
    return f"{md_count} notes at {vault}"

check("Obsidian vault found", check_vault)

# ── 6. Required scripts ───────────────────────────────────────
REQUIRED_SCRIPTS = [
    "vault_watcher.py","inbox_processor.py","image_analyzer.py",
    "regime_scanner.py","decision_gate.py","trigger_monitor.py",
    "generate_reports.py","analytics_engine.py","import_model_history.py",
]

def check_scripts():
    scripts_dir = PROJECT_DIR / "scripts"
    missing = [s for s in REQUIRED_SCRIPTS if not (scripts_dir / s).exists()]
    if missing:
        raise ValueError(f"Missing: {missing}")
    return f"all {len(REQUIRED_SCRIPTS)} scripts present"

check("Required scripts present", check_scripts)

# ── 7. Python dependencies ────────────────────────────────────
REQUIRED_PACKAGES = [
    "anthropic","psycopg2","yaml","watchdog",
    "yfinance","pandas","numpy","scipy","plotly","pdfplumber","openpyxl",
]

def check_packages():
    missing = []
    for pkg in REQUIRED_PACKAGES:
        try:
            __import__(pkg if pkg != "yaml" else "yaml")
        except ImportError:
            missing.append(pkg)
    if missing:
        raise ValueError(f"Missing packages: {missing}. Run: pip install -r requirements.txt")
    return f"all {len(REQUIRED_PACKAGES)} packages installed"

check("Python dependencies", check_packages)

# ── 8. Regime stack populated ─────────────────────────────────
def check_regime():
    import psycopg2
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    with conn.cursor() as cur:
        cur.execute("SELECT regime, composite_score FROM regime_snapshots ORDER BY scored_at DESC LIMIT 1")
        r = cur.fetchone()
    conn.close()
    if not r:
        raise ValueError("No regime snapshots — run regime_scanner.py first")
    return f"latest: {r[0]} ({float(r[1]):+.4f})"

check("Regime data present", check_regime)

# ── Summary ───────────────────────────────────────────────────
passed = sum(1 for ok,_,_ in results if ok)
total  = len(results)
print("\n" + "═" * 55)
if passed == total:
    print(f"  ✅  PASS — all {total} checks passed")
    print("  IWP Investment OS is fully operational.")
else:
    failed = [(l,m) for ok,l,m in results if not ok]
    print(f"  ❌  FAIL — {passed}/{total} checks passed")
    print("\n  Next steps:")
    for label, msg in failed:
        print(f"    • {label}: {msg}")
print("═" * 55 + "\n")

sys.exit(0 if passed == total else 1)
