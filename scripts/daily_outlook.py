#!/usr/bin/env python3
"""
daily_outlook.py — The converged daily house view for the Research tab.

Synthesizes, via one structured AI call:
  - the current regime stack (scores, dims, tier, interpretation)
  - the last 7 days of scored research notes (all sources)
  - current macro/sector technicals (trend + timing from fund_technicals)
into a JSON payload:
  { regime: {call, short, medium, long},          # RISK_ON / NEUTRAL / RISK_OFF per horizon
    drivers: [ {name, direction, note} ],          # what's scoring/driving (liquidity, quads, gamma...)
    macro_outlook: str,                            # 3-4 sentence plain synthesis
    key_risks: [ {risk, trigger} ],                # max 4, with watchable triggers
    asset_outlook: [ {asset, stance, note} ] }     # bullish/neutral/bearish per asset class
Stored in daily_outlook; the site renders it on the Research tab.
Runs in the post-close chain after the scanner.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_market_date import us_market_date  # noqa: E402

import anthropic  # noqa: E402
import psycopg2  # noqa: E402

AI_MODEL = "claude-sonnet-4-6"
ASSETS = ["US equities (broad)", "Growth stocks", "Value stocks", "International (ex-US)",
          "Emerging markets", "Long-duration bonds", "Short-duration / cash",
          "Gold / real assets", "Crypto", "Commodities / energy"]


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    cur.execute("""SELECT short_term_score, short_term_label, short_term_trend,
                          medium_term_score, medium_term_label, medium_term_trend,
                          long_term_score, long_term_label, long_term_trend,
                          stack_alignment, max_tier_eligible, long_term_cycle_position,
                          short_term_dims, medium_term_dims, ai_interpretation
                   FROM regime_stack WHERE is_current = TRUE""")
    r = cur.fetchone()

    cur.execute("""SELECT s.short_name, rn.title, rn.signal_direction, rn.signal_strength,
                          LEFT(rn.ai_summary, 300)
                   FROM research_notes rn LEFT JOIN sources s ON s.id = rn.source_id
                   WHERE rn.ingested_at >= CURRENT_DATE - INTERVAL '7 days'
                   ORDER BY ABS(rn.signal_strength) DESC NULLS LAST LIMIT 30""")
    notes = cur.fetchall()

    cur.execute("""SELECT ticker, trend, timing, rel_strength_63d FROM fund_technicals
                   WHERE score_date = (SELECT MAX(score_date) FROM fund_technicals)
                     AND ticker IN ('SPY','QQQ','IWM','GLD','USO','TLT','HYG','EEM',
                                    'XLK','XLE','XLF','UUP','SMH','MTUM','VLUE')""")
    tech = cur.fetchall()

    notes_txt = "\n".join(f"- [{n[0]}] {n[1][:70]} | {n[2]} ({n[3]}): {n[4]}" for n in notes)
    tech_txt = "\n".join(f"- {t[0]}: trend={t[1]} timing={t[2]} rs63={t[3]}" for t in tech)

    prompt = f"""You are the IWP macro model's daily synthesis engine. Produce today's house view
as STRICT JSON (no markdown, no commentary) with exactly these keys:

regime: {{call: "RISK_ON"|"NEUTRAL"|"RISK_OFF", short: {{label, score, trend}},
         medium: {{label, score, trend}}, long: {{label, score, trend}},
         tier: int, cycle: str}}
drivers: array of 3-5 {{name, direction: "supportive"|"headwind"|"mixed", note}} —
  what is actually scoring/driving markets now (liquidity, quad/growth-inflation,
  options positioning/gamma, credit, policy...)
macro_outlook: 3-4 sentence plain-English synthesis
key_risks: array of 3-4 {{risk, trigger}} where trigger is a WATCHABLE level/event
asset_outlook: array covering ALL of {json.dumps(ASSETS)} as
  {{asset, stance: "bullish"|"neutral"|"bearish", note: one sentence}}

Ground every field in the inputs. Stances must be consistent with the regime
tier and the weighted evidence, not more aggressive than the stack supports.

REGIME STACK: short {r[0]} {r[1]} {r[2]} | medium {r[3]} {r[4]} {r[5]} |
long {r[6]} {r[7]} {r[8]} | alignment {r[9]} | tier {r[10]}/3 | cycle {r[11]}
DIMS short: {r[12]} | medium: {r[13]}
STACK INTERPRETATION: {r[14][:1200]}

LAST 7 DAYS OF RESEARCH ({len(notes)} strongest notes):
{notes_txt}

CURRENT TECHNICALS:
{tech_txt}"""

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(model=AI_MODEL, max_tokens=2500,
                                  messages=[{"role": "user", "content": prompt}])
    txt = resp.content[0].text.strip()
    if txt.startswith("```"):
        txt = txt.split("```")[1].lstrip("json\n")
    payload = json.loads(txt)

    cur.execute("""INSERT INTO daily_outlook (outlook_date, payload) VALUES (%s, %s)
                   ON CONFLICT (outlook_date) DO UPDATE SET payload = EXCLUDED.payload,
                   created_at = NOW()""", (us_market_date(), json.dumps(payload)))
    conn.commit()
    print(f"daily outlook stored for {us_market_date()}: call={payload['regime']['call']}, "
          f"{len(payload['asset_outlook'])} asset stances")
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
