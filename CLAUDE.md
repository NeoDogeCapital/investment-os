# Investment OS — Project Context

GITHUB:     https://github.com/NeoDogeCapital/investment-os
MODEL:      IWP Models — Macro Investment OS
DATABASE:   Supabase (credentials in .env)
TO PULL LATEST: git pull origin main
PROJECT DIR: ~/investment-os

## What This Is
A personal investment research and decision management system that aggregates research from 10 macro/market sources, scores market regime, gates trade entries via hard rules, and journals decisions.

## Project Structure
```
investment-os/
├── config/sources.yaml          # Source definitions, weights, metadata
├── database/migrations/         # PostgreSQL schema (run in order 001–007)
├── scripts/
│   ├── vault_watcher.py         # Watches Obsidian vault for new research notes
│   ├── regime_scanner.py        # Scores regime across analytical layers
│   ├── decision_gate.py         # Validates trades against hard/soft rules
│   └── trigger_monitor.py       # Monitors open positions for invalidation
├── obsidian-templates/
│   ├── research-note.md         # Template for source research
│   └── tweet-note.md            # Template for tweet/social captures
├── requirements.txt
└── .env                         # DATABASE_URL, ANTHROPIC_API_KEY, OBSIDIAN_VAULT_PATH
```

## Sources (10)
| Source | Specialty |
|--------|-----------|
| Hedgeye | Risk range process, macro regime |
| 42 Macro | Quantitative macro, regime scoring |
| FFTT / Luke Gromen | Global liquidity, dollar cycle |
| Michael Howell / CrossBorder Capital | Global liquidity flows |
| Mike Green / Simplify | Passive flows, market structure |
| Milton Berg | Cycle analysis, sentiment extremes |
| Investech | Technical/fundamental hybrid |
| SpotGamma | Options flow, GEX, dealer positioning |
| Samantha LaDuc | Market structure, tape reading |
| Jarred Dillian | Sentiment, contrarian signals |

## Model Rules (HARDCODED — NEVER BYPASS)
- Max allocation per position: **12%**
- Min initiation size: **2%**
- Allocation ladder: **2% → 4% → 6% → 8% → 10% → 12%** (sequential, no skipping)
- Every trade requires a **locked thesis** before entry
- Every trade requires **documented invalidation conditions**
- Hard rules cannot be bypassed under any circumstances
- Soft rules require written documentation to override

## Regime System
- **RISK_ON**: composite score > +0.15
- **NEUTRAL**: score between -0.15 and +0.15
- **RISK_OFF**: composite score < -0.15
  (recalibrated 2026-08-10 from ±0.40, which never fired in 59 scans)

**Tier system (2026-08-10 redesign — 3 tiers, score-driven):**
- Stack score = 0.5·medium + 0.3·short + 0.2·long
- **Tier 1 Defensive** (score < -0.10): minimum equity, gate caps positions at 4%
- **Tier 2 Neutral** (-0.10 to +0.10): baseline weights, cap 8%
- **Tier 3 Overweight** (score > +0.10): add equity, cap 12%
- Hysteresis: enter outer tiers at ±0.12 (±0.08 if short-term trend agrees), exit back inside ±0.05
- Confidence = source agreement × freshness (not coverage-based)
- Scoring dimensions: `macro_regime`, `micro_levels`, `options_flow`, `timing`
- Recency decay applied to research older than configured staleness window
- Analytical layers: `global_liquidity`, `macro_regime`, `market_structure`, `cycle_sentiment`, `options_flow`, `macro_to_micro`

## Database
PostgreSQL via Supabase. Connect using `DATABASE_URL` from `.env`.
Migrations in `database/migrations/` — run in order 001–011.

### Row Level Security
All tables have RLS enabled with no policies. The Supabase REST API is intentionally
locked down (deny-by-default). All access goes through the direct Postgres connection
(`DATABASE_URL` uses the `postgres` role which bypasses RLS). Any new table created
in a migration must include:
```sql
ALTER TABLE public.<name> ENABLE ROW LEVEL SECURITY;
```
Never add permissive policies unless explicitly required and reviewed.

## Key Scripts
- **vault_watcher.py**: Watches `OBSIDIAN_VAULT_PATH` for new/modified `.md` files and ingests research into the DB.
- **regime_scanner.py**: Pulls recent research, scores each source/dimension, computes composite regime.
- **decision_gate.py**: Given a proposed trade, validates all hard rules and flags soft rule violations.
- **trigger_monitor.py**: Polls open positions and alerts when invalidation conditions are met.

## Claude Model
Use `claude-sonnet-4-6` for all Claude API calls in this project.

## Running
```bash
pip install -r requirements.txt
# Apply migrations (already run against Supabase)
python scripts/vault_watcher.py      # daemon
python scripts/regime_scanner.py     # on-demand or scheduled
python scripts/decision_gate.py      # called with trade params
python scripts/trigger_monitor.py    # daemon or scheduled
```
