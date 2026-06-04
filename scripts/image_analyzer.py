#!/usr/bin/env python3
"""
image_analyzer.py — Claude vision analysis of chart images and screenshots.

Usage:
    python scripts/image_analyzer.py --folder Clippings
    python scripts/image_analyzer.py --file path/to/chart.png
    python scripts/image_analyzer.py --folder Clippings --dry-run
"""

import os
import re
import sys
import json
import base64
import logging
import argparse
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras
import anthropic
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).parent.parent

def load_env():
    for line in (PROJECT_DIR / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))
load_env()

DB_URL       = os.environ["DATABASE_URL"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
VAULT_PATH   = Path(os.environ["OBSIDIAN_VAULT_PATH"])
AI_MODEL     = "claude-sonnet-4-6"

IMAGE_EXTS   = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
MEDIA_TYPES  = {".png": "image/png", ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}

# Source name hints from filename keywords
FILENAME_SOURCE_HINTS = {
    "laduc":       "laduc",      "ladu":        "laduc",
    "spotgamma":   "spotgamma",  "spgamma":     "spotgamma",
    "hedgeye":     "hedgeye",    "he_":         "hedgeye",
    "42macro":     "42macro",    "42m":         "42macro",
    "gromen":      "fftt",       "fftt":        "fftt",     "luke":    "fftt",
    "howell":      "crossborder","crossborder":  "crossborder",
    "green":       "mike_green", "simplify":    "mike_green",
    "berg":        "milton_berg","milton":      "milton_berg",
    "investech":   "investech",
    "dillian":     "dillian",    "jarred":      "dillian",
}

CHART_ANALYSIS_PROMPT = """You are a professional financial analyst for an investment firm analyzing a chart image or research screenshot.

Analyze this image carefully and extract all available information.
{context}

Return a JSON object only — no markdown fences, no explanation:
{{
  "chart_type": "price_chart" | "indicator" | "breadth" | "tweet" | "options_flow" | "macro_data" | "other",
  "ticker": "primary ticker symbol visible or null",
  "timeframe": "intraday" | "daily" | "weekly" | "monthly" | "unknown",
  "source": "best guess at analyst/platform source or null",
  "source_id": "hedgeye|42macro|fftt|crossborder|mike_green|milton_berg|spotgamma|laduc|dillian|investech|market_commentary|macro_data|other_analyst|null",
  "key_levels": ["list of specific price levels, % levels, or index values visible"],
  "indicators_shown": ["list of indicators, overlays, or studies visible"],
  "technical_signal": "BULLISH" | "NEUTRAL" | "BEARISH",
  "signal_strength": "high" | "medium" | "low",
  "key_observation": "2-3 sentences describing the most important thing this chart shows — be specific about levels, trends, patterns, and what they mean",
  "regime_implication": "1-2 sentences: what does this chart signal for the macro regime (RISK_ON / NEUTRAL / RISK_OFF)?",
  "action_implication": "1-2 sentences: what should a portfolio manager do with this information?",
  "regime_vote": -1 or -0.5 or 0 or 0.5 or 1,
  "asset_classes": ["equities","fixed_income","commodities","crypto","fx","rates"],
  "regime_tags": ["trend-following","mean-reversion","breadth","sentiment","momentum","volatility","macro"]
}}

regime_vote scale: -1 = strongly bearish/risk-off, 0 = neutral, +1 = strongly bullish/risk-on"""


def get_db():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def run_migration(db):
    sql = (PROJECT_DIR / "database/migrations/010_image_signals.sql").read_text()
    try:
        with db.cursor() as cur: cur.execute(sql)
        db.commit(); log.info("Migration 010 OK")
    except Exception as e:
        db.rollback()
        if "already exists" in str(e): log.info("image_signals table already exists")
        else: raise


def encode_image(path: Path) -> tuple[str, str]:
    """Return (base64_data, media_type)."""
    ext = path.suffix.lower()
    media_type = MEDIA_TYPES.get(ext, "image/png")
    data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
    return data, media_type


def guess_source_from_filename(filename: str) -> Optional[str]:
    name_lower = filename.lower()
    for keyword, source_id in FILENAME_SOURCE_HINTS.items():
        if keyword in name_lower:
            return source_id
    return None


def guess_ticker_from_filename(filename: str) -> Optional[str]:
    """Try to extract a ticker from filename like 'spy-daily-chart.png' → 'SPY'."""
    stem = Path(filename).stem.upper()
    # Common patterns: ticker-date, ticker_chart, TICKER_something
    parts = re.split(r"[-_\s]", stem)
    common_tickers = {"SPX","SPY","QQQ","IWM","TLT","GLD","VIX","NDX",
                      "NVDA","AAPL","MSFT","META","GOOGL","AMZN","TSLA",
                      "BTC","ETH","DXY","TNX","USO","GDX","XLF","XLE"}
    for part in parts:
        if part in common_tickers:
            return part
        if 2 <= len(part) <= 5 and part.isalpha():
            return part
    return None


def analyze_image(client: anthropic.Anthropic, image_path: Path,
                  note_context: str = "", filename_hint: str = "") -> dict:
    """Send image to Claude vision and return structured extraction."""
    img_b64, media_type = encode_image(image_path)

    context_parts = []
    if filename_hint:
        context_parts.append(f"Image filename: {filename_hint}")
    src_hint = guess_source_from_filename(image_path.name)
    if src_hint:
        context_parts.append(f"Likely source based on filename: {src_hint}")
    ticker_hint = guess_ticker_from_filename(image_path.name)
    if ticker_hint:
        context_parts.append(f"Possible ticker from filename: {ticker_hint}")
    if note_context:
        context_parts.append(f"Note context: {note_context[:500]}")

    context_str = ("\n\nContext:\n" + "\n".join(context_parts)) if context_parts else ""
    prompt = CHART_ANALYSIS_PROMPT.format(context=context_str)

    response = client.messages.create(
        model=AI_MODEL,
        max_tokens=1000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": img_b64,
                }},
                {"type": "text", "text": prompt},
            ],
        }],
    )

    raw = response.content[0].text.strip()
    raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("Failed to parse vision response: %s\nRaw: %s", e, raw[:300])
        return {}


def save_to_db(db, image_path: Path, note_path: Optional[Path],
               extraction: dict) -> Optional[str]:
    """Persist image signal to Supabase."""
    source_id = extraction.get("source_id")
    # Validate source_id against known sources
    with db.cursor() as cur:
        cur.execute("SELECT id FROM sources WHERE id = %s", (source_id,))
        if not cur.fetchone():
            source_id = "other_analyst"

    rel_img  = str(image_path.relative_to(VAULT_PATH)) if image_path.is_relative_to(VAULT_PATH) else str(image_path)
    rel_note = str(note_path.relative_to(VAULT_PATH)) if note_path and note_path.is_relative_to(VAULT_PATH) else (str(note_path) if note_path else None)

    # Regime vote — apply 70% discount vs text signal
    raw_vote = extraction.get("regime_vote", 0)
    try:
        regime_vote = float(raw_vote) * 0.7
    except (TypeError, ValueError):
        regime_vote = 0.0

    with db.cursor() as cur:
        cur.execute("""
            INSERT INTO image_signals (
                source_id, image_path, note_path, chart_type, ticker,
                timeframe, technical_signal, signal_strength,
                key_levels, indicators_shown, key_observation,
                regime_implication, action_implication,
                regime_vote, asset_classes, regime_tags,
                raw_extraction, ai_model
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT DO NOTHING
            RETURNING id
        """, (
            source_id, rel_img, rel_note,
            extraction.get("chart_type"), extraction.get("ticker"),
            extraction.get("timeframe"), extraction.get("technical_signal"),
            extraction.get("signal_strength"),
            json.dumps(extraction.get("key_levels", [])),
            json.dumps(extraction.get("indicators_shown", [])),
            extraction.get("key_observation"),
            extraction.get("regime_implication"),
            extraction.get("action_implication"),
            round(regime_vote, 4),
            extraction.get("asset_classes", []),
            extraction.get("regime_tags", []),
            json.dumps(extraction),
            AI_MODEL,
        ))
        row = db.cursor().fetchone() if False else cur.fetchone()
    db.commit()
    return str(row["id"]) if row else None


def write_analysis_note(image_path: Path, extraction: dict,
                        existing_note: Optional[Path] = None) -> Path:
    """Write or update an .md note with the chart analysis."""
    today = date.today().isoformat()
    ticker  = extraction.get("ticker") or "unknown"
    signal  = extraction.get("technical_signal", "NEUTRAL")
    source  = extraction.get("source", "unknown")
    src_id  = extraction.get("source_id", "other_analyst")
    vote    = extraction.get("regime_vote", 0)

    key_levels = extraction.get("key_levels", [])
    indicators = extraction.get("indicators_shown", [])
    levels_list = "\n".join(f"- {lv}" for lv in key_levels) or "- None identified"
    ind_list    = ", ".join(indicators) if indicators else "none identified"

    # Obsidian embed syntax — use relative path from vault root
    try:
        rel_img = str(image_path.relative_to(VAULT_PATH))
    except ValueError:
        rel_img = image_path.name
    obsidian_embed = f"![[{image_path.name}]]"

    content = f"""---
title: "Chart: {ticker} ({signal})"
source: "{source}"
source_id: "{src_id}"
type: chart_image
ticker: "{ticker}"
timeframe: "{extraction.get('timeframe', 'unknown')}"
technical_signal: "{signal}"
signal_strength: "{extraction.get('signal_strength', 'medium')}"
regime_vote: {vote}
chart_type: "{extraction.get('chart_type', 'other')}"
image_path: "{rel_img}"
date: {today}
tags:
  - chart-analysis
  - source/{src_id}
  - signal/{signal.lower()}
---

## Chart Analysis

{extraction.get('key_observation', '_No observation extracted._')}

## Key Levels

{levels_list}

## Indicators / Studies

{ind_list}

## Regime Implication

{extraction.get('regime_implication', '_None extracted._')}

## Action Implication

{extraction.get('action_implication', '_None extracted._')}

---

{obsidian_embed}
"""

    # Determine output path
    if existing_note:
        out_path = existing_note
    else:
        # Write alongside the image, or in the same folder
        out_path = image_path.with_suffix(".md")
        if out_path.exists():
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = image_path.parent / f"{image_path.stem}_analysis_{ts}.md"

    out_path.write_text(content, encoding="utf-8")
    log.info("  → Analysis note: %s", out_path.name)
    return out_path


def process_image(image_path: Path, client: anthropic.Anthropic,
                  db, note_context: str = "",
                  associated_note: Optional[Path] = None,
                  dry_run: bool = False) -> dict:
    """Full pipeline for one image: analyze → save → write note."""
    log.info("━━━ Analyzing image: %s (%s)",
             image_path.name, f"{image_path.stat().st_size / 1024:.0f} KB")

    extraction = analyze_image(client, image_path,
                               note_context=note_context,
                               filename_hint=image_path.name)
    if not extraction:
        return {"file": image_path.name, "status": "error", "reason": "vision_failed"}

    ticker  = extraction.get("ticker") or "?"
    signal  = extraction.get("technical_signal", "?")
    vote    = extraction.get("regime_vote", 0)
    src_id  = extraction.get("source_id", "?")
    chart_t = extraction.get("chart_type", "?")

    log.info("  Source:    %s", src_id)
    log.info("  Ticker:    %s  |  Timeframe: %s  |  Chart: %s",
             ticker, extraction.get("timeframe","?"), chart_t)
    log.info("  Signal:    %s (%s strength)  |  Vote: %+.1f",
             signal, extraction.get("signal_strength","?"), float(vote or 0))
    log.info("  Levels:    %s", extraction.get("key_levels", []))
    log.info("  Indicators:%s", extraction.get("indicators_shown", []))
    log.info("  Observation: %s", (extraction.get("key_observation","") or "")[:200])
    log.info("  Regime:    %s", extraction.get("regime_implication","")[:120])
    log.info("  Action:    %s", extraction.get("action_implication","")[:120])

    db_id = None
    note_path = None
    if not dry_run:
        db_id = save_to_db(db, image_path, associated_note, extraction)
        note_path = write_analysis_note(image_path, extraction, associated_note)
        log.info("  → DB id: %s", db_id or "duplicate/skipped")

    return {
        "file":       image_path.name,
        "status":     "processed",
        "ticker":     ticker,
        "signal":     signal,
        "vote":       vote,
        "source_id":  src_id,
        "db_id":      db_id,
        "note_path":  str(note_path) if note_path else None,
        "extraction": extraction,
    }


def find_images_in_note(note_path: Path) -> list[Path]:
    """Find all image files embedded in an Obsidian note (![[image.png]])."""
    content = note_path.read_text(encoding="utf-8", errors="ignore")
    embedded = re.findall(r'!\[\[([^\]]+\.(?:png|jpg|jpeg|webp|gif))\]\]', content, re.IGNORECASE)
    found = []
    for name in embedded:
        # Search relative to note, then vault root
        candidates = [
            note_path.parent / name,
            VAULT_PATH / name,
            VAULT_PATH / "Clippings" / name,
        ]
        for c in candidates:
            if c.exists():
                found.append(c)
                break
        else:
            log.debug("Embedded image not found on disk: %s", name)
    return found


def find_standalone_images(folder: Path) -> list[Path]:
    """Find image files in a folder that don't have an associated .md."""
    images = []
    for ext in IMAGE_EXTS:
        images.extend(folder.glob(f"*{ext}"))
        images.extend(folder.glob(f"*{ext.upper()}"))
    return sorted(set(images))


def main():
    parser = argparse.ArgumentParser(description="Analyze chart images with Claude vision")
    parser.add_argument("--folder", type=str, default=None,
                        help="Vault subfolder to scan (e.g. Clippings)")
    parser.add_argument("--file",   type=str, default=None,
                        help="Single image file to analyze (absolute or vault-relative path)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db     = get_db()
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    run_migration(db)

    images_to_process = []

    if args.file:
        p = Path(args.file)
        if not p.is_absolute():
            p = VAULT_PATH / args.file
        if not p.exists():
            log.error("File not found: %s", p)
            sys.exit(1)
        images_to_process = [(p, None, "")]

    elif args.folder:
        folder = VAULT_PATH / args.folder
        if not folder.exists():
            log.error("Folder not found: %s", folder)
            sys.exit(1)

        # Standalone images
        for img in find_standalone_images(folder):
            images_to_process.append((img, None, ""))

        # Images embedded in notes
        for md in folder.glob("*.md"):
            embedded = find_images_in_note(md)
            ctx = md.read_text(encoding="utf-8", errors="ignore")[:1000]
            for img in embedded:
                images_to_process.append((img, md, ctx))

    else:
        # Default: scan Clippings
        folder = VAULT_PATH / "Clippings"
        for img in find_standalone_images(folder):
            images_to_process.append((img, None, ""))
        for md in folder.glob("*.md"):
            for img in find_images_in_note(md):
                images_to_process.append((img, md, md.read_text(encoding="utf-8", errors="ignore")[:1000]))

    if not images_to_process:
        log.info("No images found to process.")
        db.close()
        return

    log.info("Found %d image(s) to analyze", len(images_to_process))
    if args.dry_run:
        log.info("─── DRY RUN — no writes ───")

    results = []
    for img_path, note_path, ctx in images_to_process:
        try:
            r = process_image(img_path, client, db,
                              note_context=ctx,
                              associated_note=note_path,
                              dry_run=args.dry_run)
            results.append(r)
        except Exception as e:
            log.error("Error on %s: %s", img_path.name, e, exc_info=True)
            results.append({"file": img_path.name, "status": "error", "reason": str(e)})

    db.close()

    # Summary
    print("\n" + "═" * 65)
    print(f"  IMAGE ANALYSIS COMPLETE — {len(results)} processed")
    print("═" * 65)
    for r in results:
        if r["status"] == "processed":
            print(f"  {r['file'][:40]}")
            print(f"    {r.get('source_id','?')} | {r.get('ticker','?')} | "
                  f"{r.get('signal','?')} | vote={float(r.get('vote',0)):+.1f}")
        else:
            print(f"  ✗ {r['file']}: {r.get('reason','error')}")
    print("═" * 65)


if __name__ == "__main__":
    main()
