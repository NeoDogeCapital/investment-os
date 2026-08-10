#!/usr/bin/env python3
"""
regime_scanner.py — Three-horizon regime stack scanner.

Computes SHORT (7d), MEDIUM (30d), and LONG (90d) regime scores independently,
synthesises them into a stack alignment, derives max tier eligibility,
and persists to regime_stack + regime_stack_source_votes.

Also writes a legacy regime_snapshots row for backward compatibility.
"""

import os
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import psycopg2
import psycopg2.extras
import anthropic
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

DB_URL            = os.environ["DATABASE_URL"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
AI_MODEL          = "claude-sonnet-4-6"

_CFG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "sources.yaml")
with open(_CFG_PATH) as f:
    SOURCES_CONFIG = yaml.safe_load(f)

GLOBAL_DIM_WEIGHTS = SOURCES_CONFIG["dimension_global_weights"]
REGIME_THRESHOLDS  = SOURCES_CONFIG["scoring"]["regime_thresholds"]
RISK_ON_THRESH     = float(REGIME_THRESHOLDS["risk_on"])
RISK_OFF_THRESH    = float(REGIME_THRESHOLDS["risk_off"])

# ---------------------------------------------------------------------------
# Horizon definitions
# ---------------------------------------------------------------------------
HORIZONS = {
    "short": {
        "lookback_days":  7,
        "half_life_days": 3,
        "min_notes": 1,
        "source_multipliers": {
            # T3 demotion: spotgamma 2.0 → 0.9 (still relevant short-term but no longer dominant)
            "spotgamma": 0.9, "laduc": 1.6, "emerging_voice": 1.4,
            "market_commentary": 1.2, "hedgeye": 1.0, "42macro": 1.0,
            "dillian": 1.1, "mike_green": 0.9, "milton_berg": 0.9,
            "fftt": 0.6, "crossborder": 0.6, "investech": 0.5,
            "other_analyst": 0.8, "macro_data": 0.7,
        },
    },
    "medium": {
        "lookback_days":  30,
        "half_life_days": 14,
        "min_notes": 2,
        "source_multipliers": {
            "hedgeye": 1.8, "42macro": 1.7, "laduc": 1.4,
            "mike_green": 1.5, "dillian": 1.3,
            # T3 demotion: spotgamma 1.2 → 0.6
            "spotgamma": 0.6,
            "emerging_voice": 1.0, "market_commentary": 0.9,
            "milton_berg": 1.1, "fftt": 0.9, "crossborder": 0.9,
            "investech": 1.0, "other_analyst": 0.9, "macro_data": 1.1,
        },
    },
    "long": {
        "lookback_days":  90,
        "half_life_days": 45,
        "min_notes": 2,
        "source_multipliers": {
            "crossborder": 2.0, "fftt": 1.9, "milton_berg": 1.7,
            "investech": 1.6, "42macro": 1.5, "hedgeye": 1.1,
            "mike_green": 1.3, "laduc": 0.8, "dillian": 0.7,
            # T3 demotion: spotgamma 0.4 → 0.2 (negligible at 90d)
            "spotgamma": 0.2, "emerging_voice": 0.5,
            "market_commentary": 0.4, "other_analyst": 0.9, "macro_data": 1.2,
        },
    },
}

LIQUIDITY_SOURCES = {"crossborder", "fftt"}

TREND_ARROW = {"IMPROVING": "↑", "STABLE": "→", "DETERIORATING": "↓"}
LABEL_ICON  = {"RISK_ON": "🟢", "NEUTRAL": "🟡", "RISK_OFF": "🔴"}

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def run_migration(db):
    sql_path = os.path.join(os.path.dirname(__file__), "..",
                            "database", "migrations", "009_regime_stack.sql")
    with open(sql_path) as f:
        sql = f.read()
    try:
        with db.cursor() as cur:
            cur.execute(sql)
        db.commit()
        log.info("Migration 009_regime_stack OK")
    except Exception as e:
        db.rollback()
        if "already exists" in str(e):
            log.info("regime_stack tables already exist")
        else:
            raise

# ---------------------------------------------------------------------------
# Note fetching + image signal injection
# ---------------------------------------------------------------------------

def fetch_image_signals_as_notes(db, cutoff_date):
    """
    Pull recent image_signals and convert to pseudo-note dicts so the
    scoring engine can process them alongside text notes.
    Image signals are weighted at 70% (regime_vote already has 0.7 applied).
    Only included in the SHORT-TERM (7-day) horizon.
    """
    try:
        with db.cursor() as cur:
            cur.execute("""
                SELECT ims.source_id, ims.ticker, ims.technical_signal,
                       ims.regime_vote, ims.key_observation,
                       ims.chart_type, ims.ai_processed_at,
                       s.recency_half_life_days,
                       s.w_macro_regime, s.w_micro_levels,
                       s.w_options_flow, s.w_timing
                FROM image_signals ims
                LEFT JOIN sources s ON s.id = ims.source_id
                WHERE ims.ai_processed_at >= %s
                  AND ims.regime_vote IS NOT NULL
                ORDER BY ims.ai_processed_at DESC
            """, (cutoff_date,))
            rows = cur.fetchall()
    except Exception as e:
        log.debug("image_signals not available (may need migration 010): %s", e)
        return []

    pseudo_notes = []
    for r in rows:
        vote = float(r["regime_vote"] or 0)
        # Map regime_vote to dimensional scores
        # chart signals primarily inform micro_levels and options_flow
        chart_type = r.get("chart_type","")
        if chart_type == "options_flow":
            score_options = vote / 0.7  # undo the 70% discount for dimension
            score_micro   = vote / 0.7 * 0.5
            score_macro   = None
            score_timing  = vote / 0.7 * 0.3
        elif chart_type in ("breadth","indicator"):
            score_micro   = vote / 0.7
            score_timing  = vote / 0.7 * 0.5
            score_options = None
            score_macro   = vote / 0.7 * 0.2
        else:  # price_chart, tweet, other
            score_micro   = vote / 0.7 * 0.6
            score_timing  = vote / 0.7 * 0.4
            score_options = None
            score_macro   = vote / 0.7 * 0.2

        pseudo_notes.append({
            "id":               f"img_{r['ticker']}_{r['ai_processed_at']}",
            "source_id":        r["source_id"] or "market_commentary",
            "title":            f"[CHART] {r['ticker'] or 'unknown'} — {r['technical_signal']}",
            "score_macro_regime": score_macro,
            "score_micro_levels": score_micro,
            "score_options_flow": score_options,
            "score_timing":       score_timing,
            "layers_mentioned": [],
            "signal_direction": r["technical_signal"].lower() if r["technical_signal"] else "neutral",
            "signal_strength":  vote,
            "ai_summary":       r["key_observation"] or "",
            "published_at":     r["ai_processed_at"],
            "ingested_at":      r["ai_processed_at"],
            "recency_half_life_days": int(r["recency_half_life_days"] or 3),
            "w_macro_regime":   float(r["w_macro_regime"] or 0.15),
            "w_micro_levels":   float(r["w_micro_levels"] or 0.35),
            "w_options_flow":   float(r["w_options_flow"] or 0.30),
            "w_timing":         float(r["w_timing"] or 0.20),
            "_is_image":        True,
        })

    if pseudo_notes:
        log.info("Loaded %d image signal(s) as regime inputs (70%% weight)", len(pseudo_notes))
    return pseudo_notes


def fetch_notes(db, cutoff_date):
    with db.cursor() as cur:
        cur.execute("""
            SELECT rn.id, rn.source_id, rn.title,
                   rn.score_macro_regime, rn.score_micro_levels,
                   rn.score_options_flow, rn.score_timing,
                   rn.layers_mentioned, rn.signal_direction, rn.signal_strength,
                   rn.ai_summary, rn.published_at, rn.ingested_at,
                   s.recency_half_life_days,
                   s.w_macro_regime, s.w_micro_levels, s.w_options_flow, s.w_timing
            FROM research_notes rn
            JOIN sources s ON s.id = rn.source_id
            WHERE s.active = TRUE
              AND rn.ai_summary IS NOT NULL
              AND COALESCE(rn.published_at, rn.ingested_at) >= %s
            ORDER BY COALESCE(rn.published_at, rn.ingested_at) DESC
        """, (cutoff_date,))
        return [dict(r) for r in cur.fetchall()]

# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def decay(age_days, half_life):
    return 0.5 ** (age_days / max(half_life, 0.1))

def _weighted_composite(dim_scores):
    tw = 0.0; ts = 0.0
    for dim, gw in GLOBAL_DIM_WEIGHTS.items():
        gw = float(gw)
        s = dim_scores.get(dim)
        if s is not None:
            ts += s * gw; tw += gw
    return ts / tw if tw > 0 else None

def _classify(score):
    if score is None: return None
    if score > RISK_ON_THRESH:  return "RISK_ON"
    if score < RISK_OFF_THRESH: return "RISK_OFF"
    return "NEUTRAL"

def score_horizon(notes, horizon_cfg, now):
    lookback  = horizon_cfg["lookback_days"]
    half_life = horizon_cfg["half_life_days"]
    src_mults = horizon_cfg["source_multipliers"]
    cutoff    = now - timedelta(days=lookback)

    relevant = []
    for n in notes:
        ref = n["published_at"] or n["ingested_at"]
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        if ref >= cutoff:
            n = dict(n)
            n["_ref"] = ref
            relevant.append(n)

    if not relevant:
        return {"score": None, "label": None, "confidence": 0.0,
                "sources": {}, "dim_aggregates": {}, "notes_count": 0,
                "source_ids": [], "trend": "STABLE"}

    buckets = {}
    for n in relevant:
        sid  = n["source_id"]
        age  = (now - n["_ref"]).total_seconds() / 86400
        d    = decay(age, half_life)
        m    = src_mults.get(sid, 1.0)
        eff  = d * m

        if sid not in buckets:
            buckets[sid] = {
                "dim_sum":  {k: 0.0 for k in ("macro_regime","micro_levels","options_flow","timing")},
                "dim_wt":   {k: 0.0 for k in ("macro_regime","micro_levels","options_flow","timing")},
                "notes_count": 0, "decay_acc": 0.0, "most_recent": n["_ref"],
            }
        b = buckets[sid]
        b["notes_count"] += 1
        b["decay_acc"]   += eff
        if n["_ref"] > b["most_recent"]:
            b["most_recent"] = n["_ref"]

        for dim, sk, wk in [
            ("macro_regime","score_macro_regime","w_macro_regime"),
            ("micro_levels","score_micro_levels","w_micro_levels"),
            ("options_flow","score_options_flow","w_options_flow"),
            ("timing","score_timing","w_timing"),
        ]:
            sc = n.get(sk)
            if sc is not None:
                sw = float(n.get(wk) or 0.25)
                b["dim_sum"][dim] += float(sc) * eff * sw
                b["dim_wt"][dim]  += eff * sw

    source_scores = {}
    for sid, b in buckets.items():
        dims = {}
        for dim in ("macro_regime","micro_levels","options_flow","timing"):
            tw = b["dim_wt"][dim]
            dims[dim] = b["dim_sum"][dim] / tw if tw > 0 else None
        comp = _weighted_composite(dims)
        avg_d = b["decay_acc"] / max(b["notes_count"], 1)
        source_scores[sid] = {
            **dims, "composite": comp, "decay_factor": round(avg_d, 4),
            "notes_count": b["notes_count"], "most_recent": b["most_recent"],
            "label_vote": _classify(comp),
        }

    dim_agg = {d: [] for d in ("macro_regime","micro_levels","options_flow","timing")}
    for ss in source_scores.values():
        for dim in dim_agg:
            if ss[dim] is not None:
                dim_agg[dim].append(ss[dim])

    dim_aggregates = {d: (sum(v)/len(v) if v else None) for d,v in dim_agg.items()}
    composite      = _weighted_composite(dim_aggregates)

    n_src = len(source_scores)
    avg_decay_overall = (sum(ss["decay_factor"] for ss in source_scores.values()) / max(n_src, 1))
    # Confidence (2026-08-10 redesign): measures signal QUALITY, not just coverage.
    # agreement = how tightly sources cluster (low dispersion -> high agreement);
    # freshness = recency decay; coverage is a soft floor rather than the driver.
    # Old formula ((n_src/7) * decay) discounted unanimous-but-few readings as noise.
    comps = [ss["composite"] for ss in source_scores.values() if ss.get("composite") is not None]
    if len(comps) >= 2:
        mean_c = sum(comps) / len(comps)
        disp = (sum((c - mean_c) ** 2 for c in comps) / (len(comps) - 1)) ** 0.5
    else:
        disp = 0.35  # single source: neutral prior, neither penalize nor reward
    agreement  = max(0.0, 1.0 - disp / 0.6)
    coverage   = min(1.0, n_src / 5)
    confidence = round(min(1.0, agreement * avg_decay_overall * (0.6 + 0.4 * coverage)), 3)

    return {
        "score":          composite,
        "label":          _classify(composite),
        "confidence":     confidence,
        "sources":        source_scores,
        "dim_aggregates": dim_aggregates,
        "notes_count":    len(relevant),
        "source_ids":     list(source_scores.keys()),
        "trend":          "STABLE",   # filled in after prior lookup
    }

# ---------------------------------------------------------------------------
# Trend, cycle, alignment, tier
# ---------------------------------------------------------------------------

def calc_trend(current, prior):
    if current is None or prior is None: return "STABLE"
    delta = current - prior
    if delta >  0.10: return "IMPROVING"
    if delta < -0.10: return "DETERIORATING"
    return "STABLE"

def fetch_prior_stack(db):
    with db.cursor() as cur:
        cur.execute("""SELECT id, short_term_score, medium_term_score, long_term_score
                       FROM regime_stack WHERE is_current = TRUE LIMIT 1""")
        r = cur.fetchone()
        return dict(r) if r else None

def determine_cycle_position(long_r):
    liq_scores = []
    for sid in LIQUIDITY_SOURCES:
        ss = long_r["sources"].get(sid)
        if ss and ss.get("composite") is not None:
            liq_scores.append(ss["composite"])
    if not liq_scores:
        return "MID"
    avg_liq = sum(liq_scores) / len(liq_scores)
    trend   = long_r.get("trend", "STABLE")
    if avg_liq > 0.15 and trend == "IMPROVING":
        return "EARLY"
    elif avg_liq > 0.05:
        return "MID"
    else:
        return "LATE"

def compute_alignment(st, mt, lt):
    labels = [x for x in [st, mt, lt] if x]
    if len(labels) < 2:         return "PARTIAL"
    if len(set(labels)) == 1:   return "FULL"
    if (lt == st and lt != mt) or (lt == mt and lt != st): return "DIVERGENT"
    if lt and st and lt != st:  return "OPPOSED"
    return "PARTIAL"

# ── 3-tier score-driven eligibility (2026-08-10 redesign) ──────────────────
# Tier 1 = Defensive (minimum equity) · Tier 2 = Neutral · Tier 3 = Overweight.
# Driven by the continuous weighted stack score, not label combinations —
# the old +/-0.40 labels never fired in 59 scans, so tiers never moved.
STACK_W_MED, STACK_W_SHORT, STACK_W_LONG = 0.50, 0.30, 0.20
TIER_ENTER       = 0.12   # cross this to enter Tier 3 (mirrored for Tier 1)
TIER_ENTER_TREND = 0.08   # relaxed entry when short-term trend agrees
TIER_EXIT        = 0.05   # hysteresis: fall back inside this to leave an outer tier

# Old 0-5 tiers map onto the new scale for hysteresis continuity
_LEGACY_TIER_MAP = {0: 1, 1: 1, 2: 2, 3: 2, 4: 3, 5: 3}

def compute_tier_eligibility(short_r, medium_r, long_r, prior_tier=None):
    def sc(r): return float(r.get("score") or 0.0)
    st_lbl = short_r.get("label") or "NEUTRAL"
    lt_lbl = long_r.get("label") or "NEUTRAL"
    S = (STACK_W_MED * sc(medium_r) + STACK_W_SHORT * sc(short_r)
         + STACK_W_LONG * sc(long_r))

    st_trend = short_r.get("trend")
    up_gate = TIER_ENTER_TREND if st_trend == "IMPROVING"     else TIER_ENTER
    dn_gate = TIER_ENTER_TREND if st_trend == "DETERIORATING" else TIER_ENTER

    prior_tier = _LEGACY_TIER_MAP.get(prior_tier, prior_tier)
    if prior_tier == 3:
        tier = 3 if S > TIER_EXIT else (1 if S < -dn_gate else 2)
    elif prior_tier == 1:
        tier = 1 if S < -TIER_EXIT else (3 if S > up_gate else 2)
    else:
        tier = 3 if S > up_gate else (1 if S < -dn_gate else 2)

    opposed = (st_lbl == "RISK_ON" and lt_lbl == "RISK_OFF") or \
              (st_lbl == "RISK_OFF" and lt_lbl == "RISK_ON")
    reduce  = tier == 1 and S < -0.20
    return {"max_tier": tier, "stack_score": round(S, 4),
            "new_positions": not (tier == 1 and S < -0.25),
            "reduce": reduce, "opposed": opposed}

# ---------------------------------------------------------------------------
# AI narrative
# ---------------------------------------------------------------------------

def generate_interpretation(client, short_r, medium_r, long_r, tier, alignment, cycle_pos, all_notes):
    summaries = [n["ai_summary"] for n in all_notes if n.get("ai_summary")][:12]
    notes_text = "\n".join(f"- {s[:200]}" for s in summaries)

    def fmt_score(r):
        s = r.get("score")
        return f"{float(s):+.3f}" if s is not None else "n/a"

    prompt = f"""You are a macro strategist producing a three-horizon regime interpretation.

REGIME STACK:
  Short-Term  (7d):  {short_r.get('label','n/a')}   score={fmt_score(short_r)}  trend={short_r.get('trend','n/a')}  confidence={short_r.get('confidence',0):.0%}
  Medium-Term (30d): {medium_r.get('label','n/a')}  score={fmt_score(medium_r)}  trend={medium_r.get('trend','n/a')}  confidence={medium_r.get('confidence',0):.0%}
  Long-Term   (90d): {long_r.get('label','n/a')}   score={fmt_score(long_r)}  trend={long_r.get('trend','n/a')}  confidence={long_r.get('confidence',0):.0%}

STACK ALIGNMENT: {alignment}
MODEL ACTION: Tier {tier}/3 — 1=Defensive (minimum equity, 4% position cap), 2=Neutral (baseline, 8% cap), 3=Overweight equity (12% cap)
LONG-TERM CYCLE POSITION: {cycle_pos}

RECENT RESEARCH:
{notes_text}

Write 4-6 direct sentences covering:
1. What each horizon is saying and why they agree or diverge
2. The dominant risk/opportunity given the alignment
3. What the cycle position means for sizing
4. One specific catalyst that could shift the stack
5. Practical implication of max tier {tier}

No markdown. No hedging. Be specific."""

    resp = client.messages.create(
        model=AI_MODEL, max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text.strip()

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save_stack(db, short_r, medium_r, long_r, tier_info, alignment, cycle_pos, interpretation, prior_id):
    def s(v):
        if v is None: return None
        try:
            f = float(v)
            return None if f != f else f   # nan check
        except: return v

    dims_json = lambda r: json.dumps({
        k: round(float(v), 4) if v is not None else None
        for k, v in (r.get("dim_aggregates") or {}).items()
    })

    with db.cursor() as cur:
        cur.execute("UPDATE regime_stack SET is_current=FALSE WHERE is_current=TRUE")

    with db.cursor() as cur:
        cur.execute("""
            INSERT INTO regime_stack (
                short_term_score, short_term_label, short_term_confidence,
                short_term_trend, short_term_notes_used, short_term_sources, short_term_dims,
                medium_term_score, medium_term_label, medium_term_confidence,
                medium_term_trend, medium_term_notes_used, medium_term_sources, medium_term_dims,
                long_term_score, long_term_label, long_term_confidence,
                long_term_trend, long_term_cycle_position,
                long_term_notes_used, long_term_sources, long_term_dims,
                stack_alignment, max_tier_eligible, new_positions_allowed,
                reduce_existing_flag, short_vs_long_opposed,
                ai_interpretation, ai_model, is_current, prior_stack_id
            ) VALUES (
                %s,%s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s,%s,TRUE,%s
            ) RETURNING id
        """, (
            s(short_r.get("score")),  short_r.get("label"),  s(short_r.get("confidence")),
            short_r.get("trend"), short_r.get("notes_count",0),
            short_r.get("source_ids",[]), dims_json(short_r),

            s(medium_r.get("score")), medium_r.get("label"), s(medium_r.get("confidence")),
            medium_r.get("trend"), medium_r.get("notes_count",0),
            medium_r.get("source_ids",[]), dims_json(medium_r),

            s(long_r.get("score")),   long_r.get("label"),   s(long_r.get("confidence")),
            long_r.get("trend"), cycle_pos,
            long_r.get("notes_count",0), long_r.get("source_ids",[]), dims_json(long_r),

            alignment,
            tier_info["max_tier"], tier_info["new_positions"],
            tier_info["reduce"],   tier_info["opposed"],
            interpretation, AI_MODEL,
            prior_id,
        ))
        stack_id = cur.fetchone()["id"]

        for horizon, result in [("short",short_r),("medium",medium_r),("long",long_r)]:
            for sid, ss in (result.get("sources") or {}).items():
                cur.execute("""
                    INSERT INTO regime_stack_source_votes (
                        stack_id, source_id, horizon,
                        composite_score, score_macro_regime, score_micro_levels,
                        score_options_flow, score_timing,
                        decay_factor, notes_count, most_recent_note_at, label_vote
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    stack_id, sid, horizon,
                    s(ss.get("composite")), s(ss.get("macro_regime")),
                    s(ss.get("micro_levels")), s(ss.get("options_flow")),
                    s(ss.get("timing")), s(ss.get("decay_factor")),
                    ss.get("notes_count"), ss.get("most_recent"), ss.get("label_vote"),
                ))
    db.commit()

    # Legacy snapshot — write dims from medium-term so memo renders correctly
    primary = medium_r if medium_r.get("score") is not None else \
              long_r   if long_r.get("score")   is not None else short_r
    dims = primary.get("dim_aggregates") or {}

    with db.cursor() as cur:
        cur.execute("""
            INSERT INTO regime_snapshots (
                composite_score, regime, confidence, narrative, ai_model,
                sources_scored, notes_used_count, prior_regime, regime_changed,
                dim_macro_regime, dim_micro_levels, dim_options_flow, dim_timing,
                layer_global_liquidity, layer_macro_regime, layer_market_structure,
                layer_cycle_sentiment, layer_options_flow, layer_macro_to_micro
            ) SELECT %s,%s,%s,%s,%s,%s,%s,
                   (SELECT regime FROM regime_snapshots ORDER BY scored_at DESC LIMIT 1),
                   (SELECT COALESCE(
                       (SELECT regime FROM regime_snapshots ORDER BY scored_at DESC LIMIT 1) != %s,
                       FALSE)),
                   %s,%s,%s,%s,
                   %s,%s,%s,%s,%s,%s
        """, (
            s(primary.get("score")), primary.get("label"), s(primary.get("confidence")),
            interpretation, AI_MODEL,
            primary.get("source_ids",[]), primary.get("notes_count",0),
            primary.get("label"),
            # dims
            s(dims.get("macro_regime")), s(dims.get("micro_levels")),
            s(dims.get("options_flow")), s(dims.get("timing")),
            # layers — derive from dims using standard mapping
            s((dims.get("macro_regime") + dims.get("timing")) / 2
              if dims.get("macro_regime") is not None and dims.get("timing") is not None else None),
            s(dims.get("macro_regime")),
            s((dims.get("micro_levels") + (dims.get("macro_regime") or 0)) / 2
              if dims.get("micro_levels") is not None else None),
            s((dims.get("timing") + (dims.get("micro_levels") or 0)) / 2
              if dims.get("timing") is not None else None),
            s(dims.get("options_flow")),
            s((dims.get("macro_regime") + (dims.get("micro_levels") or 0) + (dims.get("timing") or 0)) / 3
              if dims.get("macro_regime") is not None else None),
        ))

        # Write per-source scores to legacy regime_source_scores table
        snap_id = None
        cur.execute("SELECT id FROM regime_snapshots ORDER BY scored_at DESC LIMIT 1")
        row = cur.fetchone()
        if row:
            snap_id = row["id"]

    if snap_id:
        with db.cursor() as cur:
            for sid, ss in (primary.get("sources") or {}).items():
                cur.execute("""
                    INSERT INTO regime_source_scores (
                        snapshot_id, source_id,
                        score_macro_regime, score_micro_levels,
                        score_options_flow, score_timing,
                        composite_score, decay_factor,
                        notes_count, most_recent_note_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING
                """, (
                    snap_id, sid,
                    s(ss.get("macro_regime")), s(ss.get("micro_levels")),
                    s(ss.get("options_flow")), s(ss.get("timing")),
                    s(ss.get("composite")), s(ss.get("decay_factor")),
                    ss.get("notes_count"), ss.get("most_recent"),
                ))
    db.commit()
    return stack_id

# ---------------------------------------------------------------------------
# Pretty print
# ---------------------------------------------------------------------------

def print_stack(short_r, medium_r, long_r, tier_info, alignment, cycle_pos, interpretation):
    def fmt(r, name):
        sc    = f"{float(r['score']):+.4f}" if r.get("score") is not None else "  n/a  "
        label = r.get("label") or "n/a"
        trend = TREND_ARROW.get(r.get("trend",""), "→")
        conf  = f"{r.get('confidence',0)*100:.0f}%"
        icon  = LABEL_ICON.get(label,"⚪")
        n     = r.get("notes_count",0)
        dims  = r.get("dim_aggregates") or {}
        d_str = "  ".join(
            f"{k[:5]}={float(v):+.2f}" for k,v in dims.items() if v is not None
        )
        srcs  = ", ".join(r.get("source_ids",[]))
        log.info("  %-12s %s %-9s %s %s  conf=%s  n=%d", name, icon, label, sc, trend, conf, n)
        if d_str: log.info("               dims: %s", d_str)
        if srcs:  log.info("               sources: %s", srcs)

    log.info("╔══════════════════════════════════════════════════════════════╗")
    log.info("║   REGIME STACK  ·  %s UTC  ║",
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))
    log.info("╠══════════════════════════════════════════════════════════════╣")
    fmt(short_r,  "SHORT  (7d)")
    fmt(medium_r, "MEDIUM (30d)")
    fmt(long_r,   "LONG   (90d)")
    # Action label — mirrors the tier_action() logic in generate_reports.py
    max_tier  = tier_info["max_tier"]
    new_pos   = tier_info["new_positions"]
    reduce    = tier_info["reduce"]
    ACTION_MAP = {
        3: ("OVERWEIGHT EQUITY", "Regime supportive — size up to full 12% positions"),
        2: ("NEUTRAL",           "Baseline weights — selective adds up to 8%"),
        1: ("DEFENSIVE",         "Minimum equity — positions capped at 4%, protect capital"),
    }
    if reduce:
        action, sublabel = "CUT RISK", "Reduce existing positions — deep risk-off"
    elif not new_pos:
        action, sublabel = "NO NEW POSITIONS", "Deep risk-off — hold & protect"
    else:
        action, sublabel = ACTION_MAP.get(max_tier, ("NEUTRAL", "Insufficient data"))

    log.info("╠══════════════════════════════════════════════════════════════╣")
    log.info("  Alignment:      %s", alignment)
    log.info("  Cycle Position: %s  (long-term liquidity cycle)", cycle_pos)
    log.info("  Stack Score:    %+.3f  (0.5·MT + 0.3·ST + 0.2·LT)", tier_info.get("stack_score", 0.0))
    log.info("  MODEL ACTION:   %s  (Tier %d/3)", action, max_tier)
    log.info("                  %s", sublabel)
    if tier_info["opposed"]:
        log.warning("  ⚠️  ST OPPOSED TO LT — review before acting")
    log.info("╠══════════════════════════════════════════════════════════════╣")
    log.info("  Interpretation:")
    for line in interpretation.replace(". ", ".\n").split("\n"):
        if line.strip():
            log.info("    %s", line.strip())
    log.info("╚══════════════════════════════════════════════════════════════╝")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    db     = get_db()
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    run_migration(db)

    now          = datetime.now(timezone.utc)
    max_lookback = now - timedelta(days=91)
    all_notes    = fetch_notes(db, max_lookback)
    log.info("Fetched %d scored notes (90-day window)", len(all_notes))

    # Inject image signals (last 7 days) into short-term only
    img_cutoff  = now - timedelta(days=7)
    image_notes = fetch_image_signals_as_notes(db, img_cutoff)

    if not all_notes and not image_notes:
        log.error("No scored notes found. Run vault_watcher + inbox_processor first.")
        return

    # Short-term gets text notes + image signals merged
    short_notes  = all_notes + image_notes
    # Medium and long-term use text notes only
    short_r  = score_horizon(short_notes, HORIZONS["short"],  now)
    medium_r = score_horizon(all_notes,   HORIZONS["medium"], now)
    long_r   = score_horizon(all_notes,   HORIZONS["long"],   now)

    prior = fetch_prior_stack(db)
    short_r["trend"]  = calc_trend(short_r.get("score"),
        float(prior["short_term_score"])  if prior and prior.get("short_term_score")  else None)
    medium_r["trend"] = calc_trend(medium_r.get("score"),
        float(prior["medium_term_score"]) if prior and prior.get("medium_term_score") else None)
    long_r["trend"]   = calc_trend(long_r.get("score"),
        float(prior["long_term_score"])   if prior and prior.get("long_term_score")   else None)

    cycle_pos  = determine_cycle_position(long_r)
    alignment  = compute_alignment(short_r.get("label"), medium_r.get("label"), long_r.get("label"))
    prior_tier = int(prior["max_tier_eligible"]) if prior and prior.get("max_tier_eligible") is not None else None
    tier_info  = compute_tier_eligibility(short_r, medium_r, long_r, prior_tier=prior_tier)

    log.info("Generating AI interpretation…")
    interpretation = generate_interpretation(
        client, short_r, medium_r, long_r,
        tier_info["max_tier"], alignment, cycle_pos, all_notes
    )

    prior_id  = str(prior["id"]) if prior and prior.get("id") else None
    stack_id  = save_stack(db, short_r, medium_r, long_r,
                           tier_info, alignment, cycle_pos, interpretation, prior_id)

    print_stack(short_r, medium_r, long_r, tier_info, alignment, cycle_pos, interpretation)
    log.info("Stack saved: %s", stack_id)
    db.close()

if __name__ == "__main__":
    main()
