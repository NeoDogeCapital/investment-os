#!/usr/bin/env python3
"""
exit_gate.py — Validate a proposed exit or trim against hard/soft rules.

Complements decision_gate.py (entry). Every exit check is permanently logged
to the decision_journal and a position_event is written.

Exit types:
  trim  — partial reduction, position remains open
  exit  — full close, position marked closed

Usage:
    python exit_gate.py --ticker BFGIX --exit-type trim --from-alloc 0.20 --to-alloc 0.12 \
        --reason "Taking partial profits on SpaceX IPO appreciation inside BFGIX; reducing concentration risk at Tier 1 LATE cycle" \
        --rotation-into "CPAI" \
        [--position-id <uuid>]
        [--exit-price <float>]
        [--override-doc '<text>']
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

DB_URL             = os.environ["DATABASE_URL"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
AI_MODEL           = "claude-sonnet-4-6"

ALLOCATION_LADDER  = [0.00, 0.02, 0.04, 0.06, 0.08, 0.10, 0.12]

# Set to False to downgrade ladder violations from hard-block to soft-flag.
# Use when trimming legacy off-rung positions (e.g. 5% positions that predate the ladder).
LADDER_HARD = True

# Exit reasons that are regime-aligned (no soft flag needed)
REGIME_ALIGNED_REASONS = {
    "stop_loss",
    "invalidation_triggered",
    "regime_risk_off",
    "concentration_reduction",
    "profit_taking_late_cycle",
    "rotation",
}


def get_db():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def get_regime_stack(db_conn) -> dict:
    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT id, stack_date,
                   short_term_score, short_term_label, short_term_confidence, short_term_trend,
                   medium_term_score, medium_term_label, medium_term_confidence, medium_term_trend,
                   long_term_score,  long_term_label,  long_term_confidence,  long_term_trend,
                   long_term_cycle_position, stack_alignment,
                   max_tier_eligible, new_positions_allowed, reduce_existing_flag,
                   short_vs_long_opposed
            FROM regime_stack WHERE is_current = TRUE LIMIT 1
        """)
        row = cur.fetchone()
        if row:
            return dict(row)
    return {}


def get_position(db_conn, ticker: str, position_id: Optional[str]) -> Optional[dict]:
    with db_conn.cursor() as cur:
        if position_id:
            cur.execute(
                "SELECT * FROM positions WHERE id = %s LIMIT 1", (position_id,)
            )
        else:
            cur.execute(
                """SELECT * FROM positions
                   WHERE ticker = %s AND status = 'open'
                   ORDER BY created_at DESC LIMIT 1""",
                (ticker,),
            )
        row = cur.fetchone()
        return dict(row) if row else None


# ─── Hard rules ──────────────────────────────────────────────────────────────

def check_hard_rules(
    exit_type: str,
    from_alloc: float,
    to_alloc: float,
    reason: str,
    position: Optional[dict],
):
    """Returns (hard_failures, ladder_violations). hard_failures blocks the gate."""
    failures = []
    ladder_violations = []

    # 1. Reason required
    if not reason or len(reason.strip()) < 20:
        failures.append(
            "REASON_REQUIRED: A documented exit reason (>=20 chars) is required for every exit"
        )

    # 2. to_alloc must be less than from_alloc
    if to_alloc >= from_alloc:
        failures.append(
            f"INVALID_DIRECTION: to-alloc ({to_alloc:.1%}) must be less than from-alloc ({from_alloc:.1%})"
        )

    # 3. Full exit must go to zero
    if exit_type == "exit" and to_alloc != 0.00:
        failures.append(
            f"EXIT_MUST_BE_ZERO: exit type is 'exit' (full close) but to-alloc is {to_alloc:.1%}. "
            "Use exit-type 'trim' for partial reductions."
        )

    # 4. Trim must leave a valid rung or zero
    if exit_type == "trim" and to_alloc != 0.00:
        valid = set(ALLOCATION_LADDER)
        if not any(abs(to_alloc - v) < 0.001 for v in valid):
            ladder_violations.append(
                f"LADDER_INVALID: to-alloc {to_alloc:.1%} is not a valid ladder step. "
                f"Valid: {[f'{s:.0%}' for s in ALLOCATION_LADDER]}"
            )

    if ladder_violations and LADDER_HARD:
        failures.extend(ladder_violations)

    # 5. to_alloc cannot exceed max
    if to_alloc > 0.12:
        failures.append(
            f"MAX_ALLOCATION: to-alloc {to_alloc:.1%} exceeds maximum 12%"
        )

    # 6. Position must exist and be open (if position record provided)
    if position is not None:
        status = position.get("status")
        if status != "open":
            failures.append(
                f"POSITION_NOT_OPEN: Position {position.get('ticker')} has status '{status}'. "
                "Can only exit/trim open positions."
            )
        recorded_alloc = float(position.get("current_allocation") or 0)
        if abs(recorded_alloc - from_alloc) > 0.02:
            failures.append(
                f"ALLOC_MISMATCH: from-alloc ({from_alloc:.1%}) does not match recorded "
                f"position allocation ({recorded_alloc:.1%}). Verify before proceeding."
            )

    return failures, ladder_violations


# ─── Soft rules ──────────────────────────────────────────────────────────────

def check_soft_rules(
    exit_type: str,
    from_alloc: float,
    to_alloc: float,
    reason: str,
    rotation_into: Optional[str],
    stack: dict,
    position: Optional[dict],
    ladder_violations: list = None,
) -> list[str]:
    flags = []

    # When LADDER_HARD=False, ladder violations surface here as soft flags
    if not LADDER_HARD and ladder_violations:
        for v in ladder_violations:
            flags.append(f"LADDER_SOFT: {v} — document rationale to proceed.")

    st_label  = stack.get("short_term_label",  "UNKNOWN")
    mt_label  = stack.get("medium_term_label", "UNKNOWN")
    lt_label  = stack.get("long_term_label",   "UNKNOWN")
    mt_score  = float(stack.get("medium_term_score") or 0)
    cycle     = stack.get("long_term_cycle_position", "UNKNOWN")
    reduce_flag = stack.get("reduce_existing_flag", False)

    pct_reduced = (from_alloc - to_alloc) / from_alloc if from_alloc > 0 else 0

    # Soft: exiting a long when regime is strongly RISK_ON (leaving money on the table)
    if exit_type == "exit" and lt_label == "RISK_ON" and mt_label == "RISK_ON":
        flags.append(
            f"EXITING_IN_RISK_ON: Full exit while both medium ({mt_label}) and long ({lt_label}) "
            "horizons are RISK_ON. Document why you're closing vs. holding or trimming."
        )

    # Soft: large trim (>50%) without RISK_OFF or LATE cycle justification
    if pct_reduced > 0.50 and lt_label not in ("RISK_OFF",) and cycle not in ("LATE", "CONTRACTION"):
        flags.append(
            f"LARGE_TRIM: Reducing by {pct_reduced:.0%} of position outside RISK_OFF or LATE "
            "cycle context. Document the catalyst for this size of reduction."
        )

    # Soft: rotating into new position while regime is Defensive (Tier 1 of 3)
    if rotation_into and stack.get("max_tier_eligible", 2) <= 1:
        flags.append(
            f"ROTATION_AT_TIER_1: Proceeds are rotating into {rotation_into} while the regime "
            "is Tier 1/3 (Defensive). The new position entry is capped at 4%. Document that "
            "you understand it cannot be sized up until the regime improves."
        )

    # Soft: regime is telling you to reduce (affirming — log as confirmation not flag)
    if reduce_flag:
        log.info("ℹ️  Stack reduce_existing_flag=TRUE — regime is actively calling for reduction. "
                 "This exit is regime-aligned.")

    # Soft: no thesis was locked on the original position
    if position and not position.get("thesis_locked"):
        flags.append(
            "THESIS_NEVER_LOCKED: The original position has no locked thesis on record. "
            "Document the original entry rationale alongside this exit reason."
        )

    # Soft: exit_type is full exit but trim would be more ladder-appropriate
    if exit_type == "exit" and from_alloc > 0.04 and lt_label != "RISK_OFF":
        flags.append(
            f"FULL_EXIT_LARGE_POSITION: Full exit of a {from_alloc:.1%} position without "
            "RISK_OFF long-term regime. Consider a trim to the next ladder rung first "
            "unless invalidation has been triggered."
        )

    return flags


# ─── AI assessment ───────────────────────────────────────────────────────────

def ai_exit_assessment(
    client, ticker, exit_type, from_alloc, to_alloc, reason,
    rotation_into, stack, hard_failures, soft_flags, position
):
    thesis = (position or {}).get("thesis", "No locked thesis on record")
    invalidation = (position or {}).get("invalidation_conditions", "No invalidation conditions on record")
    regime_at_entry = (position or {}).get("regime_at_entry", "Unknown")

    prompt = f"""You are a risk manager reviewing a proposed exit/trim against the current three-horizon regime stack.

PROPOSED EXIT:
  Ticker:       {ticker}
  Exit type:    {exit_type.upper()}
  From alloc:   {from_alloc:.1%}  →  To alloc: {to_alloc:.1%}  (reducing by {from_alloc - to_alloc:.1%})
  Reason:       {reason}
  Rotating into: {rotation_into or "nothing (cash)"}

ORIGINAL POSITION:
  Thesis:       {thesis}
  Invalidation: {invalidation}
  Regime at entry: {regime_at_entry}

CURRENT REGIME STACK:
  Short-Term:  {stack.get('short_term_label','?')}  ({float(stack.get('short_term_score') or 0):+.3f})  trend={stack.get('short_term_trend','?')}
  Medium-Term: {stack.get('medium_term_label','?')} ({float(stack.get('medium_term_score') or 0):+.3f})  trend={stack.get('medium_term_trend','?')}
  Long-Term:   {stack.get('long_term_label','?')}  ({float(stack.get('long_term_score') or 0):+.3f})  trend={stack.get('long_term_trend','?')}
  Alignment:   {stack.get('stack_alignment','?')}  |  Max Tier: {stack.get('max_tier_eligible','?')}  |  Cycle: {stack.get('long_term_cycle_position','?')}
  Reduce flag: {stack.get('reduce_existing_flag', False)}

Hard rule failures (BLOCKING): {json.dumps(hard_failures)}
Soft flags (need doc): {json.dumps(soft_flags)}

Provide a 3-point assessment:
1. Is this exit/trim regime-aligned and thesis-consistent?
2. Key risk of exiting now vs. holding (opportunity cost or timing risk)?
3. If rotating proceeds, is the destination appropriate given the current stack?"""

    return client.messages.create(
        model=AI_MODEL, max_tokens=350,
        messages=[{"role": "user", "content": prompt}]
    ).content[0].text.strip()


# ─── DB writes ───────────────────────────────────────────────────────────────

def log_to_journal(
    db_conn, ticker, exit_type, from_alloc, to_alloc,
    reason, rotation_into, stack, hard_failures, soft_flags,
    gate_passed, position_id=None, override_doc=None
):
    regime_label = stack.get("medium_term_label")
    regime_score = stack.get("medium_term_score")

    with db_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO decision_journal (
                decision_type, ticker, position_id,
                proposed_allocation, proposed_direction,
                gate_passed, hard_rule_failures, soft_rule_flags,
                regime_at_decision, regime_score,
                thesis_summary, decision_rationale, soft_override_doc
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            exit_type,
            ticker,
            str(position_id) if position_id else None,
            to_alloc,
            "long",
            gate_passed,
            json.dumps(hard_failures),
            json.dumps(soft_flags),
            regime_label,
            regime_score,
            reason[:500] if reason else None,
            f"EXIT: {from_alloc:.1%} → {to_alloc:.1%}. Rotating into: {rotation_into or 'cash'}",
            override_doc,
        ))
    db_conn.commit()


def write_position_event(
    db_conn, position_id, exit_type, from_alloc, to_alloc,
    exit_price, stack, reason, gate_passed, hard_failures
):
    if not position_id:
        return

    event_type = "close" if exit_type == "exit" else "trim"
    regime_label = stack.get("medium_term_label")

    with db_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO position_events (
                position_id, event_type,
                from_allocation, to_allocation,
                price, regime_at_event,
                rationale, gate_passed, gate_failures
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            str(position_id),
            event_type,
            from_alloc, to_alloc,
            exit_price,
            regime_label,
            reason[:500] if reason else None,
            gate_passed,
            json.dumps(hard_failures),
        ))

        # Update position record
        if exit_type == "exit":
            cur.execute("""
                UPDATE positions
                SET status = 'closed',
                    current_allocation = 0,
                    exit_date = NOW()::date,
                    exit_price = %s,
                    updated_at = NOW()
                WHERE id = %s
            """, (exit_price, str(position_id)))
        else:
            # Find new ladder step
            new_step = 0
            for i, step in enumerate(ALLOCATION_LADDER):
                if abs(step - to_alloc) < 0.001:
                    new_step = i
                    break
            cur.execute("""
                UPDATE positions
                SET current_allocation = %s,
                    ladder_step = %s,
                    updated_at = NOW()
                WHERE id = %s
            """, (to_alloc, new_step, str(position_id)))

    db_conn.commit()


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Investment exit/trim gate checker")
    parser.add_argument("--ticker",        required=True,
                        help="Ticker or fund name being exited")
    parser.add_argument("--exit-type",     required=True, choices=["trim", "exit"],
                        help="'trim' = partial reduction; 'exit' = full close")
    parser.add_argument("--from-alloc",    required=True, type=float,
                        help="Current allocation before exit (e.g. 0.20 for 20%%)")
    parser.add_argument("--to-alloc",      required=True, type=float,
                        help="Target allocation after exit (0.00 for full exit)")
    parser.add_argument("--reason",        required=True,
                        help="Documented reason for this exit/trim")
    parser.add_argument("--rotation-into", default=None,
                        help="Ticker/fund receiving the proceeds (if any)")
    parser.add_argument("--position-id",   default=None,
                        help="UUID of the position record (optional — looks up by ticker if omitted)")
    parser.add_argument("--exit-price",    default=None, type=float,
                        help="Price at exit (for PnL tracking)")
    parser.add_argument("--override-doc",  default=None,
                        help="Required text if overriding a soft rule flag")
    parser.add_argument("--ladder-soft",   action="store_true",
                        help="Downgrade ladder violations to soft flags (use for off-rung legacy positions)")
    args = parser.parse_args()

    if args.ladder_soft:
        global LADDER_HARD
        LADDER_HARD = False

    db_conn  = get_db()
    client   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    stack    = get_regime_stack(db_conn)
    position = get_position(db_conn, args.ticker, args.position_id)

    log.info("═══════════════════════════════════════════════════════════")
    log.info("EXIT GATE: %s %s  %.1f%% → %.1f%%  (reducing %.1f%%)",
             args.exit_type.upper(), args.ticker,
             args.from_alloc * 100, args.to_alloc * 100,
             (args.from_alloc - args.to_alloc) * 100)
    if args.rotation_into:
        log.info("  Rotating proceeds into: %s", args.rotation_into)
    log.info("  SHORT  (7d):  %-9s  score=%s  trend=%s",
             stack.get("short_term_label", "?"),
             f"{float(stack.get('short_term_score') or 0):+.4f}",
             stack.get("short_term_trend", "?"))
    log.info("  MEDIUM (30d): %-9s  score=%s  trend=%s",
             stack.get("medium_term_label", "?"),
             f"{float(stack.get('medium_term_score') or 0):+.4f}",
             stack.get("medium_term_trend", "?"))
    log.info("  LONG   (90d): %-9s  score=%s  trend=%s",
             stack.get("long_term_label", "?"),
             f"{float(stack.get('long_term_score') or 0):+.4f}",
             stack.get("long_term_trend", "?"))
    log.info("  Alignment: %-12s  Max Tier: %s  Cycle: %s  Reduce flag: %s",
             stack.get("stack_alignment", "?"),
             stack.get("max_tier_eligible", "?"),
             stack.get("long_term_cycle_position", "?"),
             stack.get("reduce_existing_flag", False))
    if position:
        log.info("  Position on record: alloc=%.1f%%  status=%s  thesis_locked=%s",
                 float(position.get("current_allocation") or 0) * 100,
                 position.get("status"),
                 position.get("thesis_locked"))
    else:
        log.info("  Position: not found in DB (manual position or untracked)")
    log.info("═══════════════════════════════════════════════════════════")

    hard_failures, ladder_violations = check_hard_rules(
        args.exit_type, args.from_alloc, args.to_alloc, args.reason, position
    )
    soft_flags = check_soft_rules(
        args.exit_type, args.from_alloc, args.to_alloc, args.reason,
        args.rotation_into, stack, position,
        ladder_violations=ladder_violations
    )

    gate_passed = len(hard_failures) == 0

    if hard_failures:
        log.error("❌ EXIT GATE BLOCKED — Hard rule violations:")
        for f in hard_failures:
            log.error("   • %s", f)
    else:
        log.info("✅ Hard rules: PASSED")

    if soft_flags:
        log.warning("⚠️  Soft rule flags (document to proceed):")
        for f in soft_flags:
            log.warning("   • %s", f)
        if not args.override_doc:
            log.warning("   → Pass --override-doc '<text>' to acknowledge and proceed")
    else:
        log.info("✅ Soft rules: No flags")

    assessment = ai_exit_assessment(
        client, args.ticker, args.exit_type,
        args.from_alloc, args.to_alloc, args.reason,
        args.rotation_into, stack, hard_failures, soft_flags, position
    )
    log.info("\n─── Risk Manager Assessment ───")
    log.info(assessment)

    pos_id = args.position_id or (str(position["id"]) if position else None)

    log_to_journal(
        db_conn, args.ticker, args.exit_type,
        args.from_alloc, args.to_alloc, args.reason,
        args.rotation_into, stack, hard_failures, soft_flags,
        gate_passed, position_id=pos_id, override_doc=args.override_doc
    )

    if gate_passed and pos_id:
        write_position_event(
            db_conn, pos_id, args.exit_type,
            args.from_alloc, args.to_alloc,
            args.exit_price, stack, args.reason,
            gate_passed, hard_failures
        )

    log.info("\nDecision logged to journal.")
    db_conn.close()

    if not gate_passed:
        log.error("\n⛔ EXIT BLOCKED. Resolve hard rule violations before proceeding.")
        sys.exit(1)
    elif soft_flags and not args.override_doc:
        log.warning("\n⚠️  Soft flags present. Provide --override-doc to confirm this is intentional.")
        sys.exit(2)
    else:
        log.info("\n🟢 EXIT GATE PASSED. Proceed with %s.", args.exit_type)
        sys.exit(0)


if __name__ == "__main__":
    main()
