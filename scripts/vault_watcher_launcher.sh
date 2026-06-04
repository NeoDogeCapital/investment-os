#!/bin/bash
# Launcher for vault_watcher.py — invoked by the VaultWatcher Login Item at login.
# Loads .env from the project directory and starts the watcher daemon.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$PROJECT_DIR/.env"

# Load .env
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

exec /usr/bin/python3 "$SCRIPT_DIR/vault_watcher.py"
