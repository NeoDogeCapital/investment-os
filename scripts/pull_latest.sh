#!/usr/bin/env bash
# pull_latest.sh — Pull latest code and update dependencies
# Usage: bash scripts/pull_latest.sh

set -euo pipefail
cd "$(dirname "$0")/.."

echo "→ Pulling latest from GitHub…"
git pull origin main

echo "→ Updating dependencies…"
pip install -r requirements.txt --quiet

echo "✓ Up to date — $(git log -1 --format='%h %s')"
