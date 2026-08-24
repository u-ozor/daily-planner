#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLANNER="$SCRIPT_DIR/planner.py"
SYSTEM_PYTHON="$(which python3)"
DATA_DIR="$HOME/Library/Application Support/DailyPlanner"
VENV_DIR="$DATA_DIR/venv"
VENV_PYTHON="$VENV_DIR/bin/python3"
ZSHRC="$HOME/.zshrc"

echo ""
echo "  Daily Planner — Install"
echo "  ─────────────────────────────────────"

# 1. Create data directory
mkdir -p "$DATA_DIR"
echo "  [✓] Data dir: $DATA_DIR"

# 2. Migrate tasks.db if found in the script directory (old location before ~/Library move)
OLD_DB="$SCRIPT_DIR/tasks.db"
NEW_DB="$DATA_DIR/tasks.db"
if [ -f "$OLD_DB" ] && [ ! -f "$NEW_DB" ]; then
    cp "$OLD_DB" "$DATA_DIR/tasks_old_backup.db"
    echo "  [!] Old tasks.db found next to script — backed up to: $DATA_DIR/tasks_old_backup.db"
fi

# 3. Create venv and install pywebview into it
echo "  Creating virtual environment..."
"$SYSTEM_PYTHON" -m venv "$VENV_DIR"
echo "  [✓] venv at: $VENV_DIR"

echo "  Installing pywebview..."
"$VENV_PYTHON" -m pip install pywebview --quiet
echo "  [✓] pywebview installed"

# 4. Write and load launchd agents (uses venv Python so plists get the right interpreter)
echo "  Installing launchd agents..."
"$VENV_PYTHON" "$PLANNER" install
echo "  [✓] launchd agents installed"

# 5. Add alias to ~/.zshrc (remove old weekly_planner block first)
if grep -q "weekly_planner" "$ZSHRC" 2>/dev/null; then
    echo "  Removing old weekly_planner block from .zshrc..."
    "$VENV_PYTHON" - "$ZSHRC" <<'PYEOF'
import sys, re
path = sys.argv[1]
text = open(path, encoding='utf-8').read()
cleaned = re.sub(
    r'\n?# weekly_planner[^\n]*\n.*?# end weekly_planner[^\n]*\n?',
    '\n', text, flags=re.DOTALL
)
open(path, 'w', encoding='utf-8').write(cleaned)
PYEOF
    echo "  [✓] Old hook removed"
fi

# Escape paths for use in alias (handles spaces)
ESCAPED_VENV_PYTHON="${VENV_PYTHON// /\\ }"
ESCAPED_PLANNER="${PLANNER// /\\ }"
ALIAS_LINE="alias planner='${ESCAPED_VENV_PYTHON} ${ESCAPED_PLANNER}'"

if grep -q "alias planner=" "$ZSHRC" 2>/dev/null; then
    # Update existing alias in place
    "$VENV_PYTHON" - "$ZSHRC" "$ALIAS_LINE" <<'PYEOF'
import sys, re
path, new_line = sys.argv[1], sys.argv[2]
text = open(path, encoding='utf-8').read()
updated = re.sub(r'alias planner=.*', new_line, text)
open(path, 'w', encoding='utf-8').write(updated)
PYEOF
    echo "  [✓] Updated alias in .zshrc"
else
    printf '\n# daily-planner\n%s\n# end daily-planner\n' "$ALIAS_LINE" >> "$ZSHRC"
    echo "  [✓] Added alias to .zshrc"
fi

echo ""
echo "  Done! Reload your shell to use the planner:"
echo ""
echo "    source ~/.zshrc"
echo ""
echo "  Then:"
echo "    planner morning   → open morning planning window"
echo "    planner evening   → open evening review window"
echo "    planner status    → print today's tasks in terminal"
echo ""
