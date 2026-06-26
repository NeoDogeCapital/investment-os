#!/usr/bin/env python3
"""
inbox_processor.py — Process raw clippings from the Obsidian Clippings folder.

For each .md file in Clippings/:
  1. Sends content to Claude to identify source, extract signals, write summary
  2. Rewrites the note frontmatter with all correct fields
  3. Moves the note to the correct Sources/<folder> based on source_id
  4. Ingests the processed note into Supabase via vault_watcher logic

Usage:
    python scripts/inbox_processor.py [--dry-run]
"""

import os
import re
import sys
import json
import shutil
import logging
import argparse
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras
import anthropic
import yaml

try:
    import pdfplumber
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False
    logging.getLogger(__name__).warning("pdfplumber not installed — PDF support disabled. Run: pip install pdfplumber")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ─── Config ──────────────────────────────────────────────────────────────────
DB_URL          = os.environ["DATABASE_URL"]
ANTHROPIC_KEY   = os.environ["ANTHROPIC_API_KEY"]
VAULT_PATH      = Path(os.environ["OBSIDIAN_VAULT_PATH"])
CLIPPINGS_DIR   = VAULT_PATH / "Clippings"
AI_MODEL        = "claude-sonnet-4-6"

SOURCES_CFG_PATH = Path(__file__).parent.parent / "config" / "sources.yaml"
with open(SOURCES_CFG_PATH) as f:
    _cfg = yaml.safe_load(f)

# Build lookup structures from sources.yaml
SOURCES = {s["id"]: s for s in _cfg["sources"]}

# Map source_id → vault Sources/ subfolder name
SOURCE_FOLDERS = {
    "hedgeye":          "Sources/Hedgeye",
    "42macro":          "Sources/42-Macro",
    "fftt":             "Sources/FFTT-Gromen",
    "crossborder":      "Sources/Howell-CrossBorder",
    "mike_green":       "Sources/Mike-Green",
    "milton_berg":      "Sources/Milton-Berg",
    "spotgamma":        "Sources/SpotGamma",
    "investech":        "Sources/Investech",
    "laduc":            "Sources/LaDuc",
    "dillian":          "Sources/Jarred-Dillian",
    "other_analyst":    "Sources/Other-Analyst",
    "macro_data":       "Sources/Macro-Data",
    "market_commentary":"Sources/Market-Commentary",
    "emerging_voice":   "Sources/Emerging-Voice",
}


# ─── Helpers ─────────────────────────────────────────────────────────────────

def get_db():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def parse_frontmatter(content: str) -> tuple[dict, str]:
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            try:
                fm = yaml.safe_load(content[3:end])
                return fm or {}, content[end + 3:].strip()
            except yaml.YAMLError:
                pass
    return {}, content


def strip_templater_blocks(content: str) -> str:
    """Remove any unrendered Templater <% %> blocks from raw clippings."""
    return re.sub(r'<%[-_]?.*?[-_]?%>', '', content, flags=re.DOTALL).strip()


def build_frontmatter(fields: dict) -> str:
    """Render a dict as a YAML frontmatter block."""
    lines = ["---"]
    for k, v in fields.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {item}")
        elif v is None or v == "":
            lines.append(f"{k}:")
        else:
            # Quote strings that contain special YAML characters
            if isinstance(v, str) and any(c in v for c in [':', '#', '[', ']', '{', '}']):
                lines.append(f'{k}: "{v}"')
            else:
                lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


# ─── Claude: classify + extract ──────────────────────────────────────────────

IDENTIFICATION_PROMPT = """You are an investment research analyst processing a raw clipped note.

KNOWN SOURCES and their source_ids:
{sources_list}

RAW NOTE CONTENT:
{content}

Your job:
1. Identify which source this note is from based on writing style, terminology, content, URL, or any explicit attribution.
2. Extract all structured signals and metadata.
3. Write a concise 3-5 sentence AI summary of the key thesis and signals.
4. Provide one actionable implication for a portfolio.

Respond with a JSON object only — no markdown fences, no explanation:
{{
  "source_id": "<one of the known source_ids, or 'unknown'>",
  "source_name": "<human-readable source name>",
  "confidence": "high" | "medium" | "low",
  "identification_rationale": "<1 sentence: why you identified this source>",
  "signal_direction": "bullish" | "bearish" | "neutral" | "mixed",
  "signal_strength": <float -1.0 to 1.0>,
  "score_macro_regime": <float -1.0 to 1.0 or null>,
  "score_micro_levels": <float -1.0 to 1.0 or null>,
  "score_options_flow": <float -1.0 to 1.0 or null>,
  "score_timing": <float -1.0 to 1.0 or null>,
  "regime_vote": "RISK_ON" | "NEUTRAL" | "RISK_OFF",
  "conviction": "high" | "medium" | "low",
  "asset_classes": ["<asset class>", ...],
  "tickers_mentioned": ["<TICKER>", ...],
  "layers_mentioned": ["<layer>", ...],
  "themes": ["<short theme>", ...],
  "invalidation_signals": ["<condition>", ...],
  "ai_summary": "<3-5 sentence summary of key thesis and signals>",
  "actionable_implication": "<1-2 sentence portfolio implication>",
  "published_date": "<YYYY-MM-DD or null if unknown>"
}}

Valid layers: global_liquidity, macro_regime, market_structure, cycle_sentiment, options_flow, macro_to_micro"""


def classify_and_extract(client: anthropic.Anthropic, content: str, filename: str) -> dict:
    sources_list = "\n".join(
        f"  - {s['id']}: {s['name']} (specialty: {', '.join(s['specialty'][:3])})"
        for s in _cfg["sources"]
    )

    # Truncate very long notes but keep enough for identification
    content_truncated = content[:6000]

    prompt = IDENTIFICATION_PROMPT.format(
        sources_list=sources_list,
        content=content_truncated,
    )

    response = client.messages.create(
        model=AI_MODEL,
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    # Strip markdown fences if Claude added them
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("Failed to parse Claude response for %s: %s\nRaw: %s", filename, e, raw[:300])
        return {}


# ─── Note rewriting ──────────────────────────────────────────────────────────

def rewrite_note(original_content: str, filename: str, extraction: dict) -> str:
    """Rebuild the note with correct frontmatter prepended, body preserved."""
    _, body = parse_frontmatter(original_content)
    body = strip_templater_blocks(body)

    source_id   = extraction.get("source_id", "unknown")
    source_name = extraction.get("source_name", SOURCES.get(source_id, {}).get("name", "Unknown"))
    today       = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    pub_date    = extraction.get("published_date") or today

    fm = {
        "title":                 f'"{filename}"',
        "source":                source_name,
        "source_id":             source_id,
        "note_type":             "research",
        "published_at":          pub_date,
        "processed_at":          today,
        "identification_conf":   extraction.get("confidence", "low"),
        "tags":                  [
                                     "research",
                                     f"source/{source_id}",
                                     f"regime/{extraction.get('regime_vote', 'NEUTRAL').lower()}",
                                 ],
        "signal_direction":      extraction.get("signal_direction", ""),
        "signal_strength":       extraction.get("signal_strength", ""),
        "regime_vote":           extraction.get("regime_vote", ""),
        "conviction":            extraction.get("conviction", ""),
        "asset_classes":         extraction.get("asset_classes", []),
        "tickers_mentioned":     extraction.get("tickers_mentioned", []),
        "layers_mentioned":      extraction.get("layers_mentioned", []),
        "themes":                extraction.get("themes", []),
        "invalidation_signals":  extraction.get("invalidation_signals", []),
    }

    header = build_frontmatter(fm)

    ai_block = f"""
## AI Analysis
_{extraction.get('identification_rationale', '')} (confidence: {extraction.get('confidence', 'low')})_

**Summary:** {extraction.get('ai_summary', '')}

**Actionable Implication:** {extraction.get('actionable_implication', '')}

---
"""
    return f"{header}\n{ai_block}\n{body}\n"


# ─── Supabase ingestion (mirrors vault_watcher logic) ────────────────────────

def ingest_to_db(db_conn, vault_relative_path: str, content: str, extraction: dict):
    source_id = extraction.get("source_id", "unknown")
    if source_id not in SOURCES:
        log.warning("source_id '%s' not in sources table — skipping DB ingest", source_id)
        return None

    pub_date = None
    raw_pub = extraction.get("published_date")
    if raw_pub:
        try:
            pub_date = datetime.strptime(raw_pub, "%Y-%m-%d")
        except ValueError:
            pass

    with db_conn.cursor() as cur:
        # Avoid duplicates on same file path
        cur.execute(
            "SELECT id FROM research_notes WHERE raw_file_path = %s LIMIT 1",
            (vault_relative_path,)
        )
        existing = cur.fetchone()
        if existing:
            log.info("  DB: already ingested at path %s — updating", vault_relative_path)
            cur.execute("""
                UPDATE research_notes SET
                    source_id           = %s,
                    signal_direction    = %s,
                    signal_strength     = %s,
                    score_macro_regime  = %s,
                    score_micro_levels  = %s,
                    score_options_flow  = %s,
                    score_timing        = %s,
                    layers_mentioned    = %s,
                    tickers_mentioned   = %s,
                    themes              = %s,
                    ai_summary          = %s,
                    ai_processed_at     = NOW(),
                    ai_model            = %s,
                    published_at        = %s,
                    content             = %s,
                    updated_at          = NOW()
                WHERE raw_file_path = %s
                RETURNING id
            """, (
                source_id,
                extraction.get("signal_direction"),
                extraction.get("signal_strength"),
                extraction.get("score_macro_regime"),
                extraction.get("score_micro_levels"),
                extraction.get("score_options_flow"),
                extraction.get("score_timing"),
                extraction.get("layers_mentioned", []),
                extraction.get("tickers_mentioned", []),
                extraction.get("themes", []),
                extraction.get("ai_summary"),
                AI_MODEL,
                pub_date,
                content,
                vault_relative_path,
            ))
        else:
            cur.execute("""
                INSERT INTO research_notes (
                    source_id, title, content, raw_file_path, note_type,
                    signal_direction, signal_strength,
                    score_macro_regime, score_micro_levels,
                    score_options_flow, score_timing,
                    layers_mentioned, tickers_mentioned, themes,
                    ai_summary, ai_processed_at, ai_model, published_at
                ) VALUES (%s,%s,%s,%s,'research',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s,%s)
                RETURNING id
            """, (
                source_id,
                vault_relative_path.split("/")[-1].replace(".md", ""),
                content,
                vault_relative_path,
                extraction.get("signal_direction"),
                extraction.get("signal_strength"),
                extraction.get("score_macro_regime"),
                extraction.get("score_micro_levels"),
                extraction.get("score_options_flow"),
                extraction.get("score_timing"),
                extraction.get("layers_mentioned", []),
                extraction.get("tickers_mentioned", []),
                extraction.get("themes", []),
                extraction.get("ai_summary"),
                AI_MODEL,
                pub_date,
            ))

        row = cur.fetchone()
    db_conn.commit()
    return str(row["id"]) if row else None


# ─── PDF extraction ───────────────────────────────────────────────────────────

def extract_pdf_text(pdf_path: Path) -> str:
    """Extract all text from a PDF using pdfplumber. Returns clean plain text."""
    if not PDF_SUPPORT:
        raise RuntimeError("pdfplumber not installed. Run: pip install pdfplumber")

    pages_text = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        log.info("  PDF: %d pages", len(pdf.pages))
        for i, page in enumerate(pdf.pages):
            text = page.extract_text()
            if text and text.strip():
                pages_text.append(f"[Page {i+1}]\n{text.strip()}")

    if not pages_text:
        raise ValueError("No text could be extracted from PDF (may be scanned/image-only)")

    full_text = "\n\n".join(pages_text)
    log.info("  PDF: extracted %d characters from %d pages",
             len(full_text), len(pages_text))
    return full_text


def pdf_to_md_clipping(pdf_path: Path) -> Path:
    """
    Extract PDF text and write it as a .md file alongside the PDF.
    Returns the path to the new .md file.
    """
    text = extract_pdf_text(pdf_path)
    md_path = pdf_path.with_suffix(".md")

    # Write as a plain markdown note — inbox_processor will handle the rest
    md_content = f"# {pdf_path.stem}\n\n_Source PDF: {pdf_path.name}_\n\n{text}"
    md_path.write_text(md_content, encoding="utf-8")
    log.info("  PDF → MD: %s", md_path.name)
    return md_path


# ─── Core processor ──────────────────────────────────────────────────────────

def process_file(
    note_path: Path,
    client: anthropic.Anthropic,
    db_conn,
    dry_run: bool = False,
) -> dict:
    """Process a single clipping. Returns a result summary dict."""
    filename = note_path.stem
    log.info("━━━ Processing: %s", note_path.name)

    content = note_path.read_text(encoding="utf-8")
    if not content.strip():
        log.warning("  Empty file — skipping")
        return {"file": note_path.name, "status": "skipped", "reason": "empty"}

    # ── Detect and analyze embedded chart images (![[image.png]] syntax) ─────────
    embedded_image_names = re.findall(
        r'!\[\[([^\]]+\.(?:png|jpg|jpeg|webp|gif))\]\]', content, re.IGNORECASE
    )
    if embedded_image_names and not dry_run:
        log.info("  Found %d embedded image(s) — running Vision analysis…",
                 len(embedded_image_names))
        for img_name in embedded_image_names:
            # Search for image relative to note folder, then vault root
            candidates = [
                note_path.parent / img_name,
                VAULT_PATH / img_name,
                VAULT_PATH / "Clippings" / img_name,
            ]
            img_path = next((c for c in candidates if c.exists()), None)
            if img_path:
                log.info("    → Analyzing: %s", img_name)
                try:
                    from image_analyzer import analyze_image, save_to_db, write_analysis_note
                    import sys as _sys
                    _sys.path.insert(0, str(Path(__file__).parent))
                    from image_analyzer import analyze_image as _ai, save_to_db as _sdb
                    extraction = _ai(client, img_path,
                                     note_context=content[:500],
                                     filename_hint=img_name)
                    if extraction:
                        img_db_id = _sdb(db_conn, img_path, note_path, extraction)
                        sig = extraction.get("technical_signal","?")
                        vote = extraction.get("regime_vote", 0)
                        log.info("    ✓ %s → %s (vote=%+.1f)  db=%s",
                                 img_name, sig, float(vote or 0),
                                 str(img_db_id)[:8] if img_db_id else "dup")
                except Exception as e:
                    log.warning("    Vision failed for %s: %s", img_name, e)
            else:
                log.warning("    Image not found on disk: %s", img_name)

    # ── Detect chart-page scrapes that have no image data ──────────────────────
    CHART_PLATFORMS = ["stockcharts.com", "tradingview.com/chart", "finviz.com/chart",
                       "sharpchart", "sharpcharts"]
    is_chart_scrape = any(p in content.lower() for p in CHART_PLATFORMS)
    has_embedded_image = bool(embedded_image_names or re.search(r'data:image/', content))
    if is_chart_scrape and not has_embedded_image:
        log.warning("  ⚠  CHART PAGE SCRAPE — no image captured")
        log.warning("     This file contains a chart URL but the chart image was not clipped.")
        log.warning("     TO ANALYZE: take a screenshot of the chart and save as a PNG in Clippings/")
        log.warning("     Example: Cmd+Shift+4 → save as '%s.png'", note_path.stem[:40])
        log.warning("     Then re-run inbox_processor.py — Claude Vision will analyze the image.")
        return {"file": note_path.name, "status": "skipped",
                "reason": "chart_scrape_no_image — screenshot the chart and re-clip as PNG"}

    # ── Step 1: Claude classification + extraction ──
    log.info("  → Sending to Claude for classification and extraction...")
    extraction = classify_and_extract(client, content, filename)

    if not extraction:
        log.error("  Claude extraction failed — leaving file in Clippings")
        return {"file": note_path.name, "status": "error", "reason": "extraction_failed"}

    source_id   = extraction.get("source_id", "unknown")
    conf        = extraction.get("confidence", "low")
    direction   = extraction.get("signal_direction", "?")
    strength    = extraction.get("signal_strength") or 0
    regime_vote = extraction.get("regime_vote", "?")

    log.info("  → Identified source:  %s (confidence: %s)", source_id, conf)
    log.info("  → Signal:             %s (%+.2f)", direction, float(strength))
    log.info("  → Regime vote:        %s", regime_vote)
    log.info("  → Themes:             %s", extraction.get("themes", []))
    log.info("  → Tickers:            %s", extraction.get("tickers_mentioned", []))
    log.info("  → AI Summary:\n      %s", extraction.get("ai_summary", "").replace("\n", "\n      "))
    log.info("  → Actionable:         %s", extraction.get("actionable_implication", ""))

    # ── Step 2: Rewrite note with correct frontmatter ──
    rewritten = rewrite_note(content, filename, extraction)

    # ── Step 3: Determine destination folder ──
    dest_folder_rel = SOURCE_FOLDERS.get(source_id)
    if not dest_folder_rel:
        dest_folder_rel = "Sources/Hedgeye"  # fallback — leave in place with warning
        log.warning("  Unknown source_id '%s' — no folder mapping. Leaving in Clippings.", source_id)
        if not dry_run:
            note_path.write_text(rewritten, encoding="utf-8")
        return {
            "file": note_path.name, "status": "rewritten_only",
            "source_id": source_id, "reason": "unknown_source",
        }

    dest_dir  = VAULT_PATH / dest_folder_rel

    # Cap filename length — macOS caps a single filename at 255 bytes. Long
    # social-media titles (e.g. full tweets) blow past this, so truncate the
    # stem, leaving headroom for a possible "_YYYYmmdd_HHMMSS" collision suffix.
    MAX_STEM = 180
    suffix = note_path.suffix
    stem   = note_path.stem
    if len(stem) > MAX_STEM:
        stem = stem[:MAX_STEM].rstrip()
    dest_path = dest_dir / f"{stem}{suffix}"

    # Handle filename collision
    if dest_path.exists() and dest_path != note_path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest_path = dest_dir / f"{stem}_{ts}{suffix}"
        log.info("  Collision resolved → %s", dest_path.name)

    # ── Step 4: Write rewritten note + move ──
    if dry_run:
        log.info("  [DRY RUN] Would write frontmatter and move to: %s", dest_folder_rel)
    else:
        dest_dir.mkdir(parents=True, exist_ok=True)
        note_path.write_text(rewritten, encoding="utf-8")
        shutil.move(str(note_path), str(dest_path))
        log.info("  → Moved to: %s", dest_folder_rel + "/" + dest_path.name)

        # If a PDF with the same stem exists alongside this note, move it too
        orig_pdf = note_path.parent / (note_path.stem + ".pdf")
        if orig_pdf.exists():
            pdf_dest = dest_dir / orig_pdf.name
            shutil.move(str(orig_pdf), str(pdf_dest))
            log.info("  → PDF archived: %s", dest_folder_rel + "/" + orig_pdf.name)

    # ── Step 5: Ingest into Supabase ──
    db_id = None
    if not dry_run:
        vault_rel = str(dest_path.relative_to(VAULT_PATH))
        db_id = ingest_to_db(db_conn, vault_rel, rewritten, extraction)
        if db_id:
            log.info("  → Ingested to Supabase: %s", db_id)
        else:
            log.warning("  → Supabase ingest skipped (source not in DB)")

    return {
        "file":       note_path.name,
        "status":     "processed",
        "source_id":  source_id,
        "confidence": conf,
        "dest":       str(dest_folder_rel),
        "db_id":      db_id,
        "signal":     f"{direction} ({float(strength):+.2f})",
        "regime":     regime_vote,
    }


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Process Obsidian Clippings inbox")
    parser.add_argument("--dry-run", action="store_true",
                        help="Classify and show what would happen — no files moved, no DB writes")
    args = parser.parse_args()

    if not CLIPPINGS_DIR.exists():
        CLIPPINGS_DIR.mkdir(parents=True)
        log.info("Created Clippings directory: %s", CLIPPINGS_DIR)

    # ── Process standalone image files first ──
    images = [f for f in CLIPPINGS_DIR.iterdir()
              if f.suffix.lower() in IMAGE_EXTS]
    if images:
        log.info("Found %d image file(s) in Clippings — running image analyzer…", len(images))
        try:
            import subprocess
            result = subprocess.run(
                [sys.executable,
                 str(Path(__file__).parent / "image_analyzer.py"),
                 "--folder", "Clippings"] +
                (["--dry-run"] if args.dry_run else []),
                capture_output=False,
            )
        except Exception as e:
            log.error("Image analyzer failed: %s", e)

    # ── Convert any PDFs to .md first ──
    pdfs = sorted(CLIPPINGS_DIR.glob("*.pdf")) + sorted(CLIPPINGS_DIR.glob("*.PDF"))
    if pdfs and not PDF_SUPPORT:
        log.warning("Found %d PDF(s) but pdfplumber not installed — skipping. Run: pip install pdfplumber", len(pdfs))
    elif pdfs:
        log.info("Found %d PDF(s) — extracting text…", len(pdfs))
        for pdf_path in pdfs:
            try:
                md_path = pdf_to_md_clipping(pdf_path)
                # Remove the original PDF after successful extraction
                if not args.dry_run:
                    pdf_path.unlink()
                    log.info("  Removed original PDF: %s", pdf_path.name)
                else:
                    log.info("  [DRY RUN] Would convert and remove: %s", pdf_path.name)
            except Exception as e:
                log.error("  Failed to extract PDF %s: %s", pdf_path.name, e)

    notes = sorted(CLIPPINGS_DIR.glob("*.md"))
    if not notes:
        log.info("No notes found in Clippings/ — nothing to process.")
        return

    log.info("Found %d note(s) in Clippings/", len(notes))
    if args.dry_run:
        log.info("─── DRY RUN MODE — no changes will be made ───")

    client  = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    db_conn = get_db() if not args.dry_run else None

    results = []
    errors  = []

    for note_path in notes:
        try:
            result = process_file(note_path, client, db_conn, dry_run=args.dry_run)
            results.append(result)
        except Exception as e:
            log.error("  Unhandled error processing %s: %s", note_path.name, e, exc_info=True)
            errors.append({"file": note_path.name, "error": str(e)})

    if db_conn:
        db_conn.close()

    # ── Summary ──
    processed     = [r for r in results if r.get("status") == "processed"]
    chart_scrapes = [r for r in results if "chart_scrape" in (r.get("reason") or "")]
    skipped       = [r for r in results if r.get("status") not in ("processed",)
                     and "chart_scrape" not in (r.get("reason") or "")]

    print("\n" + "═" * 60)
    print(f"  INBOX PROCESSOR COMPLETE")
    print("═" * 60)
    print(f"  Total found:    {len(notes)}")
    print(f"  Processed:      {len(processed)}")
    if chart_scrapes:
        print(f"  Need screenshot:{len(chart_scrapes)}  ← screenshot chart → save as PNG → re-run")
    print(f"  Skipped/errors: {len(skipped) + len(errors)}")

    if processed:
        print("\n  ── Processed ──")
        for r in processed:
            db_tag = f"  db={r['db_id'][:8]}…" if r.get("db_id") else "  [no db]"
            print(f"  {r['file']}")
            print(f"    source={r['source_id']} ({r['confidence']}) | {r['signal']} | {r['regime']}")
            print(f"    → {r['dest']}{db_tag}")

    if chart_scrapes:
        print("\n  ── Need Screenshot (chart pages with no image) ──")
        print("  These files contain chart URLs but the chart image wasn't captured.")
        print("  HOW TO FIX: Open each chart → Cmd+Shift+4 → screenshot → save PNG to Clippings/")
        for r in chart_scrapes:
            print(f"  📸 {r['file']}")

    if errors:
        print("\n  ── Errors ──")
        for e in errors:
            print(f"  {e['file']}: {e['error']}")

    print("═" * 60)


if __name__ == "__main__":
    main()
