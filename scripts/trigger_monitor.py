#!/usr/bin/env python3
"""
trigger_monitor.py — Monitor open positions for invalidation condition triggers.

Runs on a schedule (or as a daemon). For each open position, uses Claude to assess
whether recent research signals any invalidation condition being met.
Fires alerts and logs to decision_journal.
"""

import os
import json
import time
import logging
from datetime import datetime, timezone, timedelta

import psycopg2
import psycopg2.extras
import anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

DB_URL = os.environ["DATABASE_URL"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
AI_MODEL = "claude-sonnet-4-6"

CHECK_INTERVAL_SECONDS = 3600  # 1 hour between full sweeps
RESEARCH_LOOKBACK_DAYS = 3     # recent research window for invalidation check


def get_db():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def fetch_open_positions(db_conn) -> list[dict]:
    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT id, ticker, direction, current_allocation,
                   thesis, invalidation_conditions, entry_date,
                   regime_at_entry, ladder_step
            FROM positions
            WHERE status = 'open'
              AND thesis_locked = TRUE
              AND invalidation_conditions IS NOT NULL
            ORDER BY current_allocation DESC
        """)
        return [dict(r) for r in cur.fetchall()]


def fetch_recent_research(db_conn, ticker: str, lookback_days: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT rn.source_id, rn.title, rn.ai_summary,
                   rn.signal_direction, rn.signal_strength,
                   rn.tickers_mentioned,
                   COALESCE(rn.published_at, rn.ingested_at) AS note_date,
                   s.short_name
            FROM research_notes rn
            JOIN sources s ON s.id = rn.source_id
            WHERE (
                %s = ANY(rn.tickers_mentioned)
                OR rn.layers_mentioned && ARRAY['macro_regime','global_liquidity','market_structure']
            )
            AND COALESCE(rn.published_at, rn.ingested_at) >= %s
            AND rn.ai_summary IS NOT NULL
            ORDER BY COALESCE(rn.published_at, rn.ingested_at) DESC
            LIMIT 20
        """, (ticker.upper(), cutoff))
        return [dict(r) for r in cur.fetchall()]


TREND_ARROW = {"IMPROVING": "↑", "STABLE": "→", "DETERIORATING": "↓"}
LABEL_ICON  = {"RISK_ON": "🟢", "NEUTRAL": "🟡", "RISK_OFF": "🔴"}

def fetch_regime_stack(db_conn) -> dict:
    """Fetch the current three-horizon regime stack."""
    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT short_term_score, short_term_label, short_term_confidence, short_term_trend,
                   medium_term_score, medium_term_label, medium_term_confidence, medium_term_trend,
                   long_term_score, long_term_label, long_term_confidence, long_term_trend,
                   long_term_cycle_position, stack_alignment,
                   max_tier_eligible, new_positions_allowed, short_vs_long_opposed,
                   stack_date, ai_interpretation
            FROM regime_stack WHERE is_current = TRUE LIMIT 1
        """)
        row = cur.fetchone()
        if row:
            return dict(row)
    # Fallback to legacy
    with db_conn.cursor() as cur:
        cur.execute("SELECT regime, composite_score, scored_at FROM regime_snapshots ORDER BY scored_at DESC LIMIT 1")
        row = cur.fetchone()
        if row:
            r = dict(row)
            return {"short_term_label": r["regime"], "medium_term_label": r["regime"],
                    "long_term_label": r["regime"], "medium_term_score": r["composite_score"],
                    "stack_alignment": "PARTIAL", "max_tier_eligible": 3}
    return {}

def fetch_latest_regime(db_conn) -> dict:
    """Legacy compat — returns medium-term from stack."""
    stack = fetch_regime_stack(db_conn)
    return {"regime": stack.get("medium_term_label"),
            "composite_score": stack.get("medium_term_score"),
            "scored_at": stack.get("stack_date")}

def print_stack_header(stack: dict):
    """Print regime stack summary banner to console."""
    def f(score): return f"{float(score):+.4f}" if score else "  n/a "
    def icon(lbl): return LABEL_ICON.get(lbl,"⚪")
    def arr(trend): return TREND_ARROW.get(trend,"→")
    log.info("┌─────────────────────────── REGIME STACK ───────────────────────────┐")
    log.info("│  SHORT  (7d):  %s %-9s %s  score=%s  conf=%s",
             icon(stack.get("short_term_label")),  stack.get("short_term_label","?"),
             arr(stack.get("short_term_trend")),   f(stack.get("short_term_score")),
             f"{float(stack.get('short_term_confidence') or 0):.0%}")
    log.info("│  MEDIUM (30d): %s %-9s %s  score=%s  conf=%s",
             icon(stack.get("medium_term_label")), stack.get("medium_term_label","?"),
             arr(stack.get("medium_term_trend")),  f(stack.get("medium_term_score")),
             f"{float(stack.get('medium_term_confidence') or 0):.0%}")
    log.info("│  LONG   (90d): %s %-9s %s  score=%s  cycle=%s",
             icon(stack.get("long_term_label")),   stack.get("long_term_label","?"),
             arr(stack.get("long_term_trend")),    f(stack.get("long_term_score")),
             stack.get("long_term_cycle_position","?"))
    log.info("│  Alignment: %-12s  Max Tier: %s/5  New Positions: %s",
             stack.get("stack_alignment","?"),
             stack.get("max_tier_eligible","?"),
             "YES" if stack.get("new_positions_allowed") else "NO")
    if stack.get("short_vs_long_opposed"):
        log.warning("│  ⚠️  ST OPPOSED TO LT — elevated uncertainty")
    log.info("└────────────────────────────────────────────────────────────────────┘")


def check_for_existing_alert(db_conn, position_id: str, hours: int = 24) -> bool:
    """Don't re-alert within X hours for the same position."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT id FROM alerts
            WHERE position_id = %s
              AND alert_type = 'invalidation'
              AND created_at >= %s
              AND acknowledged = FALSE
        """, (position_id, cutoff))
        return cur.fetchone() is not None


def assess_invalidation(
    client: anthropic.Anthropic,
    position: dict,
    recent_research: list[dict],
    regime: dict,
) -> dict:
    """Ask Claude whether the position's invalidation conditions are being triggered."""

    research_text = "\n".join([
        f"[{r['short_name']} | {r['note_date'].strftime('%Y-%m-%d')} | "
        f"signal={r['signal_direction']} {r.get('signal_strength', 0):+.2f}]\n"
        f"{r['ai_summary']}"
        for r in recent_research
    ]) or "No recent research found for this ticker."

    prompt = f"""You are a risk manager reviewing an open position for invalidation triggers.

POSITION:
- Ticker: {position['ticker']}
- Direction: {position['direction'].upper()}
- Allocation: {float(position['current_allocation']):.1%}
- Entry date: {position['entry_date']}
- Regime at entry: {position['regime_at_entry']}

THESIS:
{position['thesis']}

INVALIDATION CONDITIONS:
{position['invalidation_conditions']}

CURRENT REGIME: {regime.get('regime', 'UNKNOWN')} (score: {float(regime.get('composite_score') or 0):+.3f})

RECENT RESEARCH (last {RESEARCH_LOOKBACK_DAYS} days):
{research_text}

Assess whether ANY invalidation condition is being triggered or approaching.
Respond in JSON only:
{{
  "invalidation_triggered": true | false,
  "invalidation_risk": "high" | "medium" | "low",
  "triggered_conditions": ["list of specific conditions that appear triggered"],
  "warning_signals": ["list of early warning signs worth watching"],
  "recommendation": "hold" | "reduce" | "exit" | "monitor",
  "rationale": "2-3 sentence explanation"
}}"""

    response = client.messages.create(
        model=AI_MODEL,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        return json.loads(response.content[0].text)
    except (json.JSONDecodeError, IndexError):
        log.warning("Failed to parse AI assessment for %s", position["ticker"])
        return {
            "invalidation_triggered": False,
            "invalidation_risk": "low",
            "recommendation": "monitor",
            "rationale": "AI assessment failed to parse.",
        }


def fire_alert(db_conn, position: dict, assessment: dict):
    severity = "critical" if assessment.get("invalidation_triggered") else "warning"
    title = (
        f"{'⛔ INVALIDATION TRIGGERED' if assessment.get('invalidation_triggered') else '⚠️ Invalidation Risk'}: "
        f"{position['direction'].upper()} {position['ticker']}"
    )
    body = json.dumps({
        "risk_level": assessment.get("invalidation_risk"),
        "recommendation": assessment.get("recommendation"),
        "rationale": assessment.get("rationale"),
        "triggered_conditions": assessment.get("triggered_conditions", []),
        "warning_signals": assessment.get("warning_signals", []),
    }, indent=2)

    with db_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO alerts (alert_type, position_id, severity, title, body)
            VALUES ('invalidation', %s, %s, %s, %s)
        """, (str(position["id"]), severity, title, body))
    db_conn.commit()
    log.warning("%s | Risk=%s | Action=%s", title,
                assessment.get("invalidation_risk"), assessment.get("recommendation"))


def check_stale_research(db_conn):
    """Alert for sources with no research in their staleness window."""
    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT s.id, s.short_name, s.recency_half_life_days,
                   MAX(COALESCE(rn.published_at, rn.ingested_at)) AS last_note
            FROM sources s
            LEFT JOIN research_notes rn ON rn.source_id = s.id
            WHERE s.active = TRUE
            GROUP BY s.id, s.short_name, s.recency_half_life_days
        """)
        for row in cur.fetchall():
            if row["last_note"] is None:
                stale_days = 9999
            else:
                last = row["last_note"]
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                stale_days = (datetime.now(timezone.utc) - last).days

            threshold = row["recency_half_life_days"] * 3
            if stale_days > threshold:
                log.warning("Stale research: %s (last note %d days ago, threshold %d)",
                            row["short_name"], stale_days, threshold)
                cur.execute("""
                    INSERT INTO alerts (alert_type, source_id, severity, title, body)
                    SELECT 'stale_research', %s, 'warning',
                           %s, %s
                    WHERE NOT EXISTS (
                        SELECT 1 FROM alerts
                        WHERE source_id = %s AND alert_type = 'stale_research'
                          AND acknowledged = FALSE
                          AND created_at > NOW() - INTERVAL '48 hours'
                    )
                """, (
                    row["id"],
                    f"Stale research: {row['short_name']} ({stale_days}d since last note)",
                    f"Last note was {stale_days} days ago. Half-life: {row['recency_half_life_days']}d.",
                    row["id"],
                ))
    db_conn.commit()


def check_stack_changes(db_conn):
    """Alert if any horizon changed label or trend shifted to DETERIORATING."""
    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT short_term_label, medium_term_label, long_term_label,
                   short_term_trend, medium_term_trend, long_term_trend,
                   stack_alignment, max_tier_eligible, prior_stack_id
            FROM regime_stack WHERE is_current = TRUE LIMIT 1
        """)
        current = cur.fetchone()
        if not current or not current["prior_stack_id"]:
            return

        cur.execute("""
            SELECT short_term_label, medium_term_label, long_term_label
            FROM regime_stack WHERE id = %s LIMIT 1
        """, (current["prior_stack_id"],))
        prior = cur.fetchone()
        if not prior:
            return

    changes = []
    for horizon, curr_lbl, prior_lbl in [
        ("Short-term",  current["short_term_label"],  prior["short_term_label"]),
        ("Medium-term", current["medium_term_label"], prior["medium_term_label"]),
        ("Long-term",   current["long_term_label"],   prior["long_term_label"]),
    ]:
        if curr_lbl != prior_lbl:
            changes.append(f"{horizon} regime changed: {prior_lbl} → {curr_lbl}")

    for horizon, trend in [
        ("Short-term",  current["short_term_trend"]),
        ("Medium-term", current["medium_term_trend"]),
        ("Long-term",   current["long_term_trend"]),
    ]:
        if trend == "DETERIORATING":
            changes.append(f"{horizon} trend is DETERIORATING ↓")

    if changes:
        log.warning("⚠️  REGIME STACK CHANGES DETECTED:")
        for c in changes:
            log.warning("   • %s", c)
    else:
        log.info("Regime stack: no changes since last run")


def run_sweep(db_conn, client: anthropic.Anthropic):
    log.info("─── Starting invalidation sweep ───")
    stack     = fetch_regime_stack(db_conn)
    positions = fetch_open_positions(db_conn)
    regime    = fetch_latest_regime(db_conn)

    # Print stack header at top of every sweep
    print_stack_header(stack)
    check_stack_changes(db_conn)

    log.info("Open positions: %d | Max Tier: %s/5 | New Positions: %s",
             len(positions),
             stack.get("max_tier_eligible","?"),
             "YES" if stack.get("new_positions_allowed") else "NO")

    for pos in positions:
        ticker = pos["ticker"]
        log.info("Checking: %s %s @ %.1f%%",
                 pos["direction"].upper(), ticker, float(pos["current_allocation"]) * 100)

        if check_for_existing_alert(db_conn, str(pos["id"])):
            log.info("  Skipping — unacknowledged alert within 24h")
            continue

        research = fetch_recent_research(db_conn, ticker, RESEARCH_LOOKBACK_DAYS)
        assessment = assess_invalidation(client, pos, research, regime)

        risk = assessment.get("invalidation_risk", "low")
        triggered = assessment.get("invalidation_triggered", False)
        rec = assessment.get("recommendation", "monitor")

        log.info("  Risk=%s | Triggered=%s | Action=%s", risk, triggered, rec)

        if triggered or risk in ("high", "medium"):
            fire_alert(db_conn, pos, assessment)

    check_stale_research(db_conn)

    # ── Chart signals (last 24h) ──────────────────────────────────────────
    try:
        with db_conn.cursor() as cur:
            cur.execute("""
                SELECT ims.ticker, ims.technical_signal, ims.signal_strength,
                       ims.chart_type, ims.key_observation,
                       ims.regime_vote, ims.ai_processed_at,
                       COALESCE(s.short_name, ims.source_id) AS source_name
                FROM image_signals ims
                LEFT JOIN sources s ON s.id = ims.source_id
                WHERE ims.ai_processed_at >= NOW() - INTERVAL '24 hours'
                ORDER BY ims.ai_processed_at DESC
            """)
            chart_signals = cur.fetchall()

        if chart_signals:
            log.info("─── CHART SIGNALS (last 24h) ───")
            for cs in chart_signals:
                icon = {"BULLISH":"🟢","BEARISH":"🔴","NEUTRAL":"🟡"}.get(
                    cs["technical_signal"],"⚪")
                log.info("  %s %-6s  %-12s  %s  (vote=%+.1f)  %s",
                    icon,
                    cs["ticker"] or "?",
                    cs["source_name"] or "?",
                    cs["technical_signal"],
                    float(cs["regime_vote"] or 0),
                    (cs["key_observation"] or "")[:80],
                )
        else:
            log.info("─── CHART SIGNALS: none in last 24h ───")
    except Exception as e:
        log.debug("Could not load chart signals (run migration 010): %s", e)

    log.info("─── Sweep complete ───")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Position invalidation monitor")
    parser.add_argument("--once", action="store_true", help="Run one sweep and exit")
    args = parser.parse_args()

    db_conn = get_db()
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    if args.once:
        run_sweep(db_conn, client)
    else:
        log.info("Starting trigger monitor (interval=%ds)", CHECK_INTERVAL_SECONDS)
        while True:
            try:
                run_sweep(db_conn, client)
            except Exception as e:
                log.error("Sweep error: %s", e, exc_info=True)
            log.info("Sleeping %ds until next sweep...", CHECK_INTERVAL_SECONDS)
            time.sleep(CHECK_INTERVAL_SECONDS)

    db_conn.close()


if __name__ == "__main__":
    main()
