#!/usr/bin/env python3
"""
decision_gate.py — Validate a proposed trade against all hard and soft rules.

Hard rules BLOCK entry. Soft rules FLAG for documentation.
Every gate check is permanently logged to the decision_journal.

Usage:
    python decision_gate.py --ticker SPY --direction long --allocation 0.04 \
        --thesis "SPY breakout above 200d MA in RISK_ON regime" \
        --invalidation "Close below 200d MA on weekly" \
        [--position-id <uuid>]  # if adding to existing position
"""

import os
import sys
import json
import argparse
import logging
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
import anthropic
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

DB_URL = os.environ["DATABASE_URL"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
AI_MODEL = "claude-sonnet-4-6"

# ─── Hardcoded model rules (mirrors DB model_rules + triggers) ─────────────────
ALLOCATION_LADDER = [0.00, 0.02, 0.04, 0.06, 0.08, 0.10, 0.12]
MAX_ALLOCATION = 0.12
MIN_INITIATION = 0.02

# Set to False to downgrade ladder violations from hard-block to soft-flag.
# Use when executing rotations or off-rung legacy positions that don't fit the ladder.
LADDER_HARD = True

# Set to False to downgrade tier-cap violations from hard-block to soft-flag.
# Use for funded rotations where gross equity exposure is unchanged (selling X to buy Y).
TIER_CAP_HARD = True


def get_db():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def get_regime_stack(db_conn) -> dict:
    """Pull the current three-horizon regime stack. Falls back to legacy snapshot."""
    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT id, stack_date,
                   short_term_score, short_term_label, short_term_confidence, short_term_trend,
                   medium_term_score, medium_term_label, medium_term_confidence, medium_term_trend,
                   long_term_score, long_term_label, long_term_confidence, long_term_trend,
                   long_term_cycle_position, stack_alignment,
                   max_tier_eligible, new_positions_allowed, reduce_existing_flag,
                   short_vs_long_opposed
            FROM regime_stack WHERE is_current = TRUE LIMIT 1
        """)
        row = cur.fetchone()
        if row:
            return dict(row)

    # Legacy fallback
    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT id, regime, composite_score, confidence, scored_at
            FROM regime_snapshots ORDER BY scored_at DESC LIMIT 1
        """)
        row = cur.fetchone()
        if row:
            r = dict(row)
            return {
                "short_term_label":    r.get("regime"),
                "short_term_score":    r.get("composite_score"),
                "short_term_confidence": r.get("confidence"),
                "medium_term_label":   r.get("regime"),
                "medium_term_score":   r.get("composite_score"),
                "long_term_label":     r.get("regime"),
                "long_term_score":     r.get("composite_score"),
                "stack_alignment":     "PARTIAL",
                "max_tier_eligible":   2,
                "new_positions_allowed": True,
                "short_vs_long_opposed": False,
                "_legacy": True,
            }
    return {}


def get_latest_regime(db_conn) -> dict:
    """Legacy helper — returns medium-term from stack or snapshot."""
    stack = get_regime_stack(db_conn)
    if not stack:
        return {}
    return {
        "regime":          stack.get("medium_term_label"),
        "composite_score": stack.get("medium_term_score"),
        "confidence":      stack.get("medium_term_confidence"),
        "id":              stack.get("id"),
    }


def get_existing_position(db_conn, ticker: str, direction: str) -> Optional[dict]:
    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT * FROM positions
            WHERE ticker = %s AND direction = %s AND status = 'open'
            ORDER BY created_at DESC LIMIT 1
        """, (ticker, direction))
        row = cur.fetchone()
        return dict(row) if row else None


def check_hard_rules(
    ticker: str,
    direction: str,
    proposed_allocation: float,
    thesis: Optional[str],
    invalidation: Optional[str],
    existing_position: Optional[dict],
):
    """Returns (hard_failures, ladder_violations). hard_failures blocks the gate."""
    failures = []
    ladder_violations = []

    # 1. Max allocation
    if proposed_allocation > MAX_ALLOCATION:
        failures.append(
            f"MAX_ALLOCATION: {proposed_allocation:.1%} exceeds maximum {MAX_ALLOCATION:.1%}"
        )

    # 2. Min initiation
    current_alloc = float(existing_position["current_allocation"]) if existing_position else 0.0
    if current_alloc == 0.0 and proposed_allocation < MIN_INITIATION:
        failures.append(
            f"MIN_INITIATION: {proposed_allocation:.1%} below minimum initiation size {MIN_INITIATION:.1%}"
        )

    # 3. Allocation ladder (no skipping when adding)
    ladder_violations = []
    if proposed_allocation > current_alloc:
        def nearest_step(val):
            return min(ALLOCATION_LADDER, key=lambda x: abs(x - val))

        cur_step_idx = ALLOCATION_LADDER.index(nearest_step(current_alloc)) if current_alloc in ALLOCATION_LADDER else -1
        prop_step_idx = -1
        for i, step in enumerate(ALLOCATION_LADDER):
            if abs(step - proposed_allocation) < 0.001:
                prop_step_idx = i
                break

        if prop_step_idx == -1:
            ladder_violations.append(
                f"LADDER_INVALID: {proposed_allocation:.1%} is not a valid ladder step. "
                f"Valid: {[f'{s:.0%}' for s in ALLOCATION_LADDER[1:]]}"
            )
        elif cur_step_idx != -1 and prop_step_idx > cur_step_idx + 1:
            from_step = ALLOCATION_LADDER[cur_step_idx]
            to_step = ALLOCATION_LADDER[prop_step_idx]
            ladder_violations.append(
                f"LADDER_SKIP: Cannot jump from {from_step:.1%} to {to_step:.1%}. "
                f"Next step is {ALLOCATION_LADDER[cur_step_idx + 1]:.1%}"
            )

    if ladder_violations and LADDER_HARD:
        failures.extend(ladder_violations)

    # 4. Thesis required
    if not thesis or len(thesis.strip()) < 20:
        failures.append(
            "THESIS_REQUIRED: A locked thesis of at least 20 characters is required before entry"
        )

    # 5. Invalidation conditions required
    if not invalidation or len(invalidation.strip()) < 20:
        failures.append(
            "INVALIDATION_REQUIRED: Documented invalidation conditions (>=20 chars) are required"
        )

    return failures, ladder_violations


def check_hard_rules_stack(
    proposed_allocation: float,
    stack: dict,
    existing_position,
):
    """Returns (hard_failures, tier_violations). hard_failures blocks the gate."""
    failures = []
    tier_violations = []
    max_tier = stack.get("max_tier_eligible", 2)
    new_allowed = stack.get("new_positions_allowed", True)

    # 3-tier caps (2026-08-10 redesign): Tier 1 Defensive / 2 Neutral / 3 Overweight.
    # Legacy 0-5 tiers from older stacks map onto the same scale.
    TIER_CAPS = {0: 0.00, 1: 0.04, 2: 0.08, 3: 0.12, 4: 0.12, 5: 0.12}
    max_alloc = TIER_CAPS.get(max_tier, 0.08)

    current_alloc = float(existing_position["current_allocation"]) if existing_position else 0.0
    is_new = current_alloc == 0.0
    if is_new and not new_allowed:
        failures.append(
            f"STACK_BLOCKS_NEW: Regime stack is deep risk-off — "
            f"no new positions allowed per regime stack rules."
        )

    if proposed_allocation > max_alloc + 1e-9:
        tier_violations.append(
            f"TIER_CAP_EXCEEDED: Proposed allocation {proposed_allocation:.1%} exceeds the "
            f"Tier {max_tier}/3 cap of {max_alloc:.0%}. Use --tier-soft for funded rotations "
            f"where gross exposure is unchanged."
        )

    if tier_violations and TIER_CAP_HARD:
        failures.extend(tier_violations)

    return failures, tier_violations


def check_soft_rules(
    stack: dict,
    proposed_allocation: float,
    direction: str,
    existing_position,
    ladder_violations: list = None,
    tier_violations: list = None,
) -> list:
    """Soft rule flags based on three-horizon stack. Require documentation to proceed."""
    flags = []

    # When LADDER_HARD=False, ladder violations surface here as soft flags
    if not LADDER_HARD and ladder_violations:
        for v in ladder_violations:
            flags.append(f"LADDER_SOFT: {v} — document rationale to proceed.")

    # When TIER_CAP_HARD=False, tier violations surface here as soft flags
    if not TIER_CAP_HARD and tier_violations:
        for v in tier_violations:
            flags.append(f"TIER_CAP_SOFT: {v}")

    st_label = stack.get("short_term_label",  "UNKNOWN")
    mt_label = stack.get("medium_term_label", "UNKNOWN")
    lt_label = stack.get("long_term_label",   "UNKNOWN")
    st_score = float(stack.get("short_term_score")  or 0)
    mt_score = float(stack.get("medium_term_score") or 0)
    lt_score = float(stack.get("long_term_score")   or 0)
    st_conf  = float(stack.get("short_term_confidence")  or 0)
    mt_conf  = float(stack.get("medium_term_confidence") or 0)
    alignment = stack.get("stack_alignment", "UNKNOWN")
    opposed   = stack.get("short_vs_long_opposed", False)

    # Soft: initiating long when medium-term is RISK_OFF
    if direction == "long" and mt_label == "RISK_OFF":
        flags.append(
            f"MT_REGIME_MISMATCH: Initiating LONG with medium-term RISK_OFF "
            f"(score={mt_score:+.3f}). Document why this trade is appropriate."
        )

    # Soft: short vs long-term RISK_ON
    if direction == "short" and lt_label == "RISK_ON":
        flags.append(
            f"LT_REGIME_MISMATCH: Initiating SHORT with long-term RISK_ON "
            f"(score={lt_score:+.3f}). Document your contrarian thesis."
        )

    # Soft: short-term opposed to long-term
    if opposed:
        flags.append(
            f"ST_LT_OPPOSED: Short-term ({st_label}) is opposed to long-term ({lt_label}). "
            "Elevated uncertainty — document your horizon rationale."
        )

    # Soft: DIVERGENT or OPPOSED stack alignment
    if alignment in ("DIVERGENT", "OPPOSED"):
        flags.append(
            f"DIVERGENT_STACK: Stack alignment is {alignment} "
            f"(ST={st_label}, MT={mt_label}, LT={lt_label}). "
            "Smaller sizing is recommended until horizons converge."
        )

    # Soft: low medium-term confidence
    if mt_conf < 0.35:
        flags.append(
            f"LOW_MT_CONFIDENCE: Medium-term confidence is {mt_conf:.0%}. "
            "Add more research from core sources before sizing up."
        )

    # Soft: large initiation
    current_alloc = float(existing_position["current_allocation"]) if existing_position else 0.0
    if proposed_allocation >= 0.07 and current_alloc == 0.0:
        flags.append(
            f"LARGE_INITIATION: Initiating at {proposed_allocation:.1%} skips early ladder rungs. "
            "Document conviction rationale."
        )

    return flags


def ai_gate_assessment(client, ticker, direction, allocation, thesis,
                        invalidation, stack, hard_failures, soft_flags):
    prompt = f"""You are a risk manager reviewing a proposed trade against a three-horizon regime stack.

PROPOSED TRADE:
  Ticker: {ticker}  |  Direction: {direction.upper()}  |  Allocation: {allocation:.1%}
  Thesis: {thesis}
  Invalidation: {invalidation}

REGIME STACK:
  Short-Term:  {stack.get('short_term_label','?')}  ({float(stack.get('short_term_score') or 0):+.3f})  trend={stack.get('short_term_trend','?')}
  Medium-Term: {stack.get('medium_term_label','?')} ({float(stack.get('medium_term_score') or 0):+.3f})  trend={stack.get('medium_term_trend','?')}
  Long-Term:   {stack.get('long_term_label','?')}  ({float(stack.get('long_term_score') or 0):+.3f})  trend={stack.get('long_term_trend','?')}
  Alignment: {stack.get('stack_alignment','?')}  |  Max Tier: {stack.get('max_tier_eligible','?')}  |  Cycle: {stack.get('long_term_cycle_position','?')}

Hard rule failures (BLOCKING): {json.dumps(hard_failures)}
Soft flags (need doc): {json.dumps(soft_flags)}

2-3 sentence assessment:
1. Is this trade aligned with the stack and thesis?
2. Key risk given the stack divergence or alignment?
3. Fastest invalidation signal to watch?"""

    return client.messages.create(
        model=AI_MODEL, max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    ).content[0].text.strip()


def log_to_journal(db_conn, ticker: str, direction: str, allocation: float,
                   thesis: str, invalidation: str, regime: dict,
                   hard_failures: list, soft_flags: list,
                   gate_passed: bool, position_id=None, override_doc=None):
    with db_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO decision_journal (
                decision_type, ticker, position_id,
                proposed_allocation, proposed_direction,
                gate_passed, hard_rule_failures, soft_rule_flags,
                regime_at_decision, regime_score, regime_snapshot_id,
                thesis_summary, soft_override_doc
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            "entry" if not position_id else "add",
            ticker, position_id,
            allocation, direction,
            gate_passed,
            json.dumps(hard_failures),
            json.dumps(soft_flags),
            regime.get("regime"), regime.get("composite_score"),
            None,  # regime_snapshot_id: regime_stack uses a separate table, FK not applicable
            thesis[:500] if thesis else None,
            override_doc,
        ))
    db_conn.commit()


def main():
    parser = argparse.ArgumentParser(description="Investment decision gate checker")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--direction", required=True, choices=["long", "short"])
    parser.add_argument("--allocation", required=True, type=float,
                        help="Proposed allocation as decimal (e.g. 0.04 for 4%%)")
    parser.add_argument("--thesis", required=True, help="Locked thesis statement")
    parser.add_argument("--invalidation", required=True, help="Invalidation conditions")
    parser.add_argument("--position-id", default=None, help="UUID of existing position (if adding)")
    parser.add_argument("--override-doc", default=None, help="Soft rule override documentation")
    parser.add_argument("--ladder-soft", action="store_true",
                        help="Downgrade ladder violations to soft flags (use for off-rung legacy positions)")
    parser.add_argument("--tier-soft", action="store_true",
                        help="Downgrade tier-cap violations to soft flags (use for funded rotations where gross exposure is unchanged)")
    args = parser.parse_args()

    if args.ladder_soft:
        global LADDER_HARD
        LADDER_HARD = False
    if args.tier_soft:
        global TIER_CAP_HARD
        TIER_CAP_HARD = False

    db_conn  = get_db()
    client   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    stack    = get_regime_stack(db_conn)
    existing = get_existing_position(db_conn, args.ticker, args.direction)

    log.info("═══════════════════════════════════════════════════════════")
    log.info("DECISION GATE: %s %s @ %.1f%%",
             args.direction.upper(), args.ticker, args.allocation * 100)
    log.info("  SHORT  (7d):  %-9s  score=%s  trend=%s",
             stack.get("short_term_label","?"),
             f"{float(stack.get('short_term_score') or 0):+.4f}",
             stack.get("short_term_trend","?"))
    log.info("  MEDIUM (30d): %-9s  score=%s  trend=%s",
             stack.get("medium_term_label","?"),
             f"{float(stack.get('medium_term_score') or 0):+.4f}",
             stack.get("medium_term_trend","?"))
    log.info("  LONG   (90d): %-9s  score=%s  trend=%s",
             stack.get("long_term_label","?"),
             f"{float(stack.get('long_term_score') or 0):+.4f}",
             stack.get("long_term_trend","?"))
    log.info("  Alignment: %-12s  Max Tier: %s  Cycle: %s",
             stack.get("stack_alignment","?"),
             stack.get("max_tier_eligible","?"),
             stack.get("long_term_cycle_position","?"))
    log.info("═══════════════════════════════════════════════════════════")

    # Hard rules: model rules + stack tier cap
    hard_failures, ladder_violations = check_hard_rules(
        args.ticker, args.direction, args.allocation,
        args.thesis, args.invalidation, existing
    )
    stack_failures, tier_violations = check_hard_rules_stack(args.allocation, stack, existing)
    hard_failures += stack_failures
    soft_flags = check_soft_rules(stack, args.allocation, args.direction, existing,
                                  ladder_violations=ladder_violations,
                                  tier_violations=tier_violations)

    gate_passed = len(hard_failures) == 0

    if stack.get("short_vs_long_opposed"):
        log.warning("⚠️  SHORT-TERM OPPOSED TO LONG-TERM — elevated uncertainty")

    if hard_failures:
        log.error("❌ GATE BLOCKED — Hard rule violations:")
        for f in hard_failures:
            log.error("   • %s", f)
    else:
        log.info("✅ Hard rules: PASSED")

    if soft_flags:
        log.warning("⚠️  Soft rule flags (document to proceed):")
        for f in soft_flags:
            log.warning("   • %s", f)
        if not args.override_doc:
            log.warning("   → Pass --override-doc '<text>' to acknowledge")
    else:
        log.info("✅ Soft rules: No flags")

    assessment = ai_gate_assessment(
        client, args.ticker, args.direction, args.allocation,
        args.thesis, args.invalidation, stack, hard_failures, soft_flags
    )
    log.info("\n─── Risk Manager Assessment ───")
    log.info(assessment)

    regime = get_latest_regime(db_conn)
    log_to_journal(
        db_conn, args.ticker, args.direction, args.allocation,
        args.thesis, args.invalidation, regime,
        hard_failures, soft_flags, gate_passed,
        position_id=args.position_id,
        override_doc=args.override_doc,
    )
    log.info("\nDecision logged to journal.")

    db_conn.close()

    if not gate_passed:
        log.error("\n⛔ ENTRY BLOCKED. Resolve hard rule violations before proceeding.")
        sys.exit(1)
    elif soft_flags and not args.override_doc:
        log.warning("\n⚠️  Soft flags present. Provide --override-doc to confirm this is intentional.")
        sys.exit(2)
    else:
        log.info("\n🟢 GATE PASSED. Proceed with entry.")
        sys.exit(0)


if __name__ == "__main__":
    main()
