#!/usr/bin/env bash
# publish_dashboards.sh — Generate fresh reports and publish to GitHub Pages
# Usage: bash scripts/publish_dashboards.sh [--no-generate]

set -euo pipefail
cd "$(dirname "$0")/.."

NO_GENERATE=false
[[ "${1:-}" == "--no-generate" ]] && NO_GENERATE=true

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║          IWP Models — Publishing Dashboards              ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# ── Load environment ──────────────────────────────────────────
if [ -f .env ]; then
  export $(grep -v "^#" .env | grep -v "^$" | \
    sed 's/OBSIDIAN_VAULT_PATH=.*/OBSIDIAN_VAULT_PATH="\/Users\/nikorrw\/Documents\/Research Vault"/' | \
    xargs) 2>/dev/null || true
  export OBSIDIAN_VAULT_PATH="/Users/nikorrw/Documents/Research Vault"
fi

# ── Optionally regenerate reports ────────────────────────────
if [ "$NO_GENERATE" = false ]; then
  echo "→ Generating fresh reports..."
  python3 scripts/generate_reports.py --no-ai 2>&1 | grep -E "✅|⚠️|Error" || true
  echo "  ✓ Reports generated"
else
  echo "  Skipping report generation (--no-generate)"
fi

# ── Copy to docs/ with stable names ──────────────────────────
echo "→ Copying reports to docs/..."
mkdir -p docs

[ -f reports/portfolio-dashboard.html ]   && cp reports/portfolio-dashboard.html   docs/portfolio-dashboard.html
[ -f reports/models-overview.html ]       && cp reports/models-overview.html       docs/models-overview.html
[ -f reports/model-liquid-core.html ]     && cp reports/model-liquid-core.html     docs/model-liquid-core.html
[ -f reports/model-conservative-core.html ] && cp reports/model-conservative-core.html docs/model-conservative-core.html
[ -f reports/model-income-real-return.html ] && cp reports/model-income-real-return.html docs/model-income-real-return.html
[ -f reports/model-flex-irr.html ]        && cp reports/model-flex-irr.html        docs/model-flex-irr.html
[ -f reports/model-balanced-core.html ]   && cp reports/model-balanced-core.html   docs/model-balanced-core.html
[ -f reports/model-tax-aware-balanced.html ] && cp reports/model-tax-aware-balanced.html docs/model-tax-aware-balanced.html
[ -f reports/model-diversified-growth.html ] && cp reports/model-diversified-growth.html docs/model-diversified-growth.html
[ -f reports/analytics-diversified-growth.html ] && cp reports/analytics-diversified-growth.html docs/analytics-report.html

# Latest regime memo — find most recent
LATEST_MEMO=$(ls -t reports/regime-memo-*.html 2>/dev/null | head -1)
if [ -n "$LATEST_MEMO" ]; then
  cp "$LATEST_MEMO" docs/regime-memo.html
  echo "  ✓ Regime memo: $(basename $LATEST_MEMO)"
fi

echo "  ✓ All reports copied to docs/"

# ── Commit and push ───────────────────────────────────────────
echo "→ Committing to GitHub..."
git add docs/
CHANGED=$(git diff --cached --name-only | wc -l | tr -d ' ')
if [ "$CHANGED" -gt 0 ]; then
  TIMESTAMP=$(date "+%Y-%m-%d %H:%M")
  git commit -m "Dashboard update — $TIMESTAMP

Auto-published by publish_dashboards.sh"
  git push origin main
  echo "  ✓ Pushed $CHANGED file(s) to GitHub"
else
  echo "  ✓ No changes to publish (reports unchanged)"
fi

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  ✅  Dashboards published!                               ║"
echo "║                                                          ║"
echo "║  Scott's URL:                                            ║"
echo "║  https://neodogecapital.github.io/investment-os          ║"
echo "║                                                          ║"
echo "║  Note: GitHub Pages can take 1-2 min to update          ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
