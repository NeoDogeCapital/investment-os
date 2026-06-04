#!/usr/bin/env python3
"""
vault_watcher.py — Watch the Obsidian vault for new/modified research notes
and ingest them into the database with AI-extracted scores.
"""

import os
import re
import time
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras
import anthropic
import yaml
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Config ──────────────────────────────────────────────────────────────────
DB_URL = os.environ["DATABASE_URL"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
VAULT_PATH = Path(os.environ["OBSIDIAN_VAULT_PATH"])
AI_MODEL = "claude-sonnet-4-6"

SOURCES_CONFIG = Path(__file__).parent.parent / "config" / "sources.yaml"

# Load source IDs from config
with open(SOURCES_CONFIG) as f:
    _cfg = yaml.safe_load(f)
KNOWN_SOURCE_IDS = {s["id"] for s in _cfg["sources"]}


def get_db():
    return psycopg2.connect(DB_URL)


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Extract YAML frontmatter and body from a markdown file."""
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            try:
                fm = yaml.safe_load(content[3:end])
                body = content[end + 3:].strip()
                return fm or {}, body
            except yaml.YAMLError:
                pass
    return {}, content


def detect_source_from_frontmatter(fm: dict, file_path: Path) -> Optional[str]:
    """Try to identify source_id from frontmatter or file path."""
    # Direct field
    source_id = fm.get("source_id") or fm.get("source")
    if source_id and source_id in KNOWN_SOURCE_IDS:
        return source_id

    # Try matching source name or short name from filename/tags
    name_lower = file_path.stem.lower()
    tags = fm.get("tags", []) or []
    search_text = name_lower + " " + " ".join(str(t).lower() for t in tags)

    name_map = {
        "hedgeye": "hedgeye",
        "42macro": "42macro",
        "42 macro": "42macro",
        "gromen": "fftt",
        "fftt": "fftt",
        "howell": "crossborder",
        "crossborder": "crossborder",
        "green": "mike_green",
        "simplify": "mike_green",
        "berg": "milton_berg",
        "investech": "investech",
        "spotgamma": "spotgamma",
        "spot gamma": "spotgamma",
        "laduc": "laduc",
        "dillian": "dillian",
    }
    for keyword, sid in name_map.items():
        if keyword in search_text:
            return sid
    return None


def ai_score_note(client: anthropic.Anthropic, source_id: str, title: str, body: str) -> dict:
    """Use Claude to extract dimensional scores and metadata from a research note."""
    prompt = f"""You are analyzing an investment research note from source '{source_id}'.

Title: {title}

Content:
{body[:4000]}

Extract the following and respond in JSON only (no markdown, no explanation):
{{
  "signal_direction": "bullish" | "bearish" | "neutral" | "mixed",
  "signal_strength": <float -1.0 to 1.0>,
  "score_macro_regime": <float -1.0 to 1.0, null if not applicable>,
  "score_micro_levels": <float -1.0 to 1.0, null if not applicable>,
  "score_options_flow": <float -1.0 to 1.0, null if not applicable>,
  "score_timing": <float -1.0 to 1.0, null if not applicable>,
  "layers_mentioned": [list of: "global_liquidity","macro_regime","market_structure","cycle_sentiment","options_flow","macro_to_micro"],
  "tickers_mentioned": [list of ticker symbols],
  "themes": [list of 1-5 word themes],
  "ai_summary": "<2-3 sentence summary of key thesis/signal>"
}}

Scoring guide:
- +1.0 = strongly bullish / risk-on
- -1.0 = strongly bearish / risk-off
- 0.0 = neutral
- null = dimension not addressed in this note
"""
    response = client.messages.create(
        model=AI_MODEL,
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    import json
    try:
        return json.loads(response.content[0].text)
    except (json.JSONDecodeError, IndexError, KeyError):
        log.warning("Failed to parse AI response for %s", title)
        return {}


def ingest_note(db_conn, client: anthropic.Anthropic, file_path: Path):
    """Ingest a single markdown note into the database."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as e:
        log.error("Cannot read %s: %s", file_path, e)
        return

    fm, body = parse_frontmatter(content)
    source_id = detect_source_from_frontmatter(fm, file_path)

    if not source_id:
        log.debug("Skipping %s — could not identify source", file_path.name)
        return

    title = fm.get("title") or file_path.stem
    note_type = fm.get("note_type", "research")
    if note_type not in ("research", "tweet", "summary", "alert"):
        note_type = "research"

    # Parse published_at
    published_at = None
    pub_raw = fm.get("published_at") or fm.get("date")
    if pub_raw:
        try:
            if isinstance(pub_raw, datetime):
                published_at = pub_raw
            else:
                published_at = datetime.fromisoformat(str(pub_raw))
        except ValueError:
            pass

    # Check if already ingested (by file path + hash)
    fhash = file_hash(file_path)
    rel_path = str(file_path.relative_to(VAULT_PATH))

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM research_notes WHERE raw_file_path = %s AND ai_summary IS NOT NULL LIMIT 1",
            (rel_path,)
        )
        existing = cur.fetchone()
        if existing:
            log.debug("Already ingested: %s", rel_path)
            return

    log.info("Ingesting: %s [source=%s]", file_path.name, source_id)
    scores = ai_score_note(client, source_id, title, body)

    with db_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO research_notes (
                source_id, title, content, raw_file_path, note_type,
                signal_direction, signal_strength,
                score_macro_regime, score_micro_levels, score_options_flow, score_timing,
                layers_mentioned, tickers_mentioned, themes,
                ai_summary, ai_processed_at, ai_model, published_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, NOW(), %s, %s
            )
            ON CONFLICT DO NOTHING
        """, (
            source_id, title, content, rel_path, note_type,
            scores.get("signal_direction"), scores.get("signal_strength"),
            scores.get("score_macro_regime"), scores.get("score_micro_levels"),
            scores.get("score_options_flow"), scores.get("score_timing"),
            scores.get("layers_mentioned", []),
            scores.get("tickers_mentioned", []),
            scores.get("themes", []),
            scores.get("ai_summary"), AI_MODEL, published_at,
        ))
    db_conn.commit()
    log.info("Ingested: %s (signal=%s %.2f)", title,
             scores.get("signal_direction", "?"), scores.get("signal_strength") or 0)


class VaultHandler(FileSystemEventHandler):
    def __init__(self, db_conn, client):
        self.db_conn = db_conn
        self.client = client

    def _handle(self, path_str: str):
        path = Path(path_str)
        if path.suffix.lower() != ".md":
            return
        ingest_note(self.db_conn, self.client, path)

    def on_created(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._handle(event.src_path)


def backfill(db_conn, client):
    """Ingest all existing .md files in vault on startup."""
    log.info("Backfilling existing vault notes from %s", VAULT_PATH)
    count = 0
    for md_file in VAULT_PATH.rglob("*.md"):
        ingest_note(db_conn, client, md_file)
        count += 1
    log.info("Backfill complete: %d files scanned", count)


def main():
    db_conn = get_db()
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    if not VAULT_PATH.exists():
        log.error("Vault path does not exist: %s", VAULT_PATH)
        return

    backfill(db_conn, client)

    handler = VaultHandler(db_conn, client)
    observer = Observer()
    observer.schedule(handler, str(VAULT_PATH), recursive=True)
    observer.start()
    log.info("Watching vault: %s", VAULT_PATH)

    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
    db_conn.close()


if __name__ == "__main__":
    main()
