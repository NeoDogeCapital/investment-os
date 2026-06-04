#!/usr/bin/env bash
# new_device_setup.sh — One-command IWP Investment OS setup on a new machine
# Usage: bash scripts/new_device_setup.sh
# Or remotely: curl -s https://raw.githubusercontent.com/NeoDogeCapital/investment-os/main/scripts/new_device_setup.sh | bash

set -euo pipefail

REPO_URL="https://github.com/NeoDogeCapital/investment-os.git"
INSTALL_DIR="$HOME/Documents/investment-os"
VENV_DIR="$INSTALL_DIR/.venv"

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║         IWP Models — Macro Investment OS Setup           ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# ── 1. Check prerequisites ────────────────────────────────────
echo "→ Checking prerequisites…"
command -v python3 >/dev/null || { echo "✗ python3 not found. Install from python.org"; exit 1; }
command -v git     >/dev/null || { echo "✗ git not found. Install Xcode Command Line Tools: xcode-select --install"; exit 1; }
echo "  ✓ python3 $(python3 --version 2>&1 | cut -d' ' -f2)"
echo "  ✓ git $(git --version | cut -d' ' -f3)"

# ── 2. Clone or update repo ───────────────────────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "→ Repo already exists — pulling latest…"
    cd "$INSTALL_DIR" && git pull origin main
else
    echo "→ Cloning repository…"
    git clone "$REPO_URL" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"
echo "  ✓ Repository ready at $INSTALL_DIR"

# ── 3. Python virtual environment ────────────────────────────
echo "→ Creating Python virtual environment…"
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
echo "  ✓ venv created"

# ── 4. Install dependencies ───────────────────────────────────
echo "→ Installing Python dependencies…"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
echo "  ✓ Dependencies installed"

# ── 5. Create .env from prompts ───────────────────────────────
if [ ! -f "$INSTALL_DIR/.env" ]; then
    echo ""
    echo "→ Setting up credentials (.env)…"
    echo "  (Press Enter to skip any field — you can edit .env later)"
    echo ""

    read -p "  DATABASE_URL (Supabase connection string): " db_url
    read -p "  ANTHROPIC_API_KEY (sk-ant-...): " anthropic_key
    read -p "  OBSIDIAN_VAULT_PATH (e.g. /Users/you/Documents/Research Vault): " vault_path

    cat > "$INSTALL_DIR/.env" << ENVEOF
DATABASE_URL=${db_url}
ANTHROPIC_API_KEY=${anthropic_key}
OBSIDIAN_VAULT_PATH="${vault_path}"
ENVEOF
    echo "  ✓ .env created"
else
    echo "  ✓ .env already exists — skipping"
fi

# ── 6. Add iwp alias to shell ─────────────────────────────────
echo "→ Adding iwp alias to shell…"
SHELL_RC="$HOME/.zshrc"
[ -f "$HOME/.bashrc" ] && [ ! -f "$HOME/.zshrc" ] && SHELL_RC="$HOME/.bashrc"

ALIAS_LINE="alias iwp='cd $INSTALL_DIR && source .venv/bin/activate && export \$(cat .env | grep -v \"^#\" | xargs) && echo \"✓ IWP Models loaded — type claude to start\"'"

if grep -q "alias iwp=" "$SHELL_RC" 2>/dev/null; then
    echo "  ✓ iwp alias already present"
else
    echo "" >> "$SHELL_RC"
    echo "# IWP Models Investment OS" >> "$SHELL_RC"
    echo "$ALIAS_LINE" >> "$SHELL_RC"
    echo "  ✓ iwp alias added to $SHELL_RC"
fi

# ── 7. Run connection test ────────────────────────────────────
echo ""
echo "→ Running connection test…"
source "$INSTALL_DIR/.env" 2>/dev/null || true
python3 scripts/test_connection.py || true

# ── 8. Done ───────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  ✅  Setup complete!                                     ║"
echo "║                                                          ║"
echo "║  To start:                                               ║"
echo "║    source ~/.zshrc  (reload shell)                       ║"
echo "║    iwp              (load the environment)               ║"
echo "║    python scripts/regime_scanner.py                      ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
