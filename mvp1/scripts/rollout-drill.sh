#!/bin/bash
# Roundhouse MVP2 rollout drill — live boltzmann gemma4 checklist.
#
# This is an operator-driven script: you read prompts, type commands, click the UI.
# The script performs ZERO actuation itself — only observations via systemctl/API.
#
# Recommended: run this in a tmux or screen session with a second window for the UI browser.

set -euo pipefail

UNIT_DIR="${1:-$HOME/.config/systemd/user}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

echo "=========================================="
echo "Roundhouse MVP2 Rollout Drill"
echo "=========================================="
echo
echo "Unit directory: $UNIT_DIR"
echo "Repo: $REPO"
echo

# Check that the unit directory exists and has both units
if [[ ! -d "$UNIT_DIR" ]]; then
    echo "Error: unit directory $UNIT_DIR not found" >&2
    exit 1
fi

if [[ ! -f "$UNIT_DIR/qwen3.6-coding.service" ]]; then
    echo "Error: qwen3.6-coding.service not found in $UNIT_DIR" >&2
    exit 1
fi

if [[ ! -f "$UNIT_DIR/llama-server-gemma4.service" ]]; then
    echo "Error: llama-server-gemma4.service not found in $UNIT_DIR" >&2
    exit 1
fi

echo "Phase 1: Capture pre-state"
echo "=========================================="
echo

pre_qwen_active=$(systemctl --user is-active qwen3.6-coding.service || echo "unknown")
pre_gemma_active=$(systemctl --user is-active llama-server-gemma4.service || echo "unknown")

echo "Pre-state:"
echo "  qwen3.6-coding: $pre_qwen_active"
echo "  llama-server-gemma4: $pre_gemma_active"
echo

# Check if git repo is initialized
if [[ ! -d "$UNIT_DIR/.git" ]]; then
    echo "Phase 2: Initialize git repository"
    echo "=========================================="
    echo
    echo "The unit directory is not a git repository. Initialize it as the operator:"
    echo
    echo "  cd $UNIT_DIR"
    echo "  git init"
    echo "  printf '%s\\n' '*.bak*' '*.roundhouse-tmp' > .gitignore"
    echo "  git add .gitignore qwen3.6-coding.service llama-server-gemma4.service"
    echo "  git commit -m 'roundhouse baseline: managed units'"
    echo
    read -p "Press Enter when git init is complete: " _
    echo
fi

echo "Phase 3: Start Roundhouse with --actuate"
echo "=========================================="
echo
echo "In another terminal, start Roundhouse on port 8091:"
echo
echo "  python3 $REPO/mvp1/roundhouse.py --serve --actuate --port 8091 --unit-dir $UNIT_DIR"
echo
echo "Then find your token:"
echo "  cat ~/.config/roundhouse/token"
echo
read -p "Press Enter when Roundhouse is running and you have the token: " _
echo

echo "Phase 4: Open the UI and perform the rollout"
echo "=========================================="
echo
echo "1. Open http://localhost:8091/ in your browser"
echo "2. You should see [ACTUATE] badge in the header"
echo "3. Paste your token into the token field"
echo "4. Click on llama-server-gemma4 unit row to open detail pane"
echo "5. Click 'edit' button"
echo "6. In the edit form, change ctx to 32768 (or another value)"
echo "7. Click 'preview' button"
echo "8. Review the diff and preflight checks"
echo "9. Click 'apply' button"
echo "10. Watch the rollout stepper: preflight → applying → reloading → starting → watching → done"
echo "11. Once READY, click on qwen3.6-coding to verify it's still READY"
echo
read -p "Press Enter when the rollout is complete and llama-server-gemma4 is READY: " _
echo

echo "Phase 5: Perform a rollback"
echo "=========================================="
echo
echo "1. If a failure occurred during the rollout (or you want to test rollback):"
echo "   - You should see a 'roll back to previous config' button in the stepper"
echo "   - Click it"
echo "2. Watch the stepper: rolling_back → rolled_back"
echo "3. Verify llama-server-gemma4 returns to its previous state"
echo "4. Verify qwen3.6-coding is still READY"
echo
read -p "Press Enter when rollout testing is complete (skip if no failure occurred): " _ || true
echo

echo "Phase 6: Post-state verification"
echo "=========================================="
echo

# Verify final state
post_qwen_active=$(systemctl --user is-active qwen3.6-coding.service || echo "unknown")
post_gemma_active=$(systemctl --user is-active llama-server-gemma4.service || echo "unknown")

echo "Post-state:"
echo "  qwen3.6-coding: $post_qwen_active"
echo "  llama-server-gemma4: $post_gemma_active"
echo

# Check git log
echo "Git history (last 3 commits):"
git -C "$UNIT_DIR" log --oneline -3 || echo "(no git history)"
echo

if [[ "$pre_qwen_active" == "$post_qwen_active" ]]; then
    echo "✓ qwen3.6-coding state preserved"
else
    echo "✗ qwen3.6-coding state changed (pre: $pre_qwen_active, post: $post_qwen_active)"
fi

# Verify qwen3.6-coding is READY via API
if curl -s http://localhost:8091/api/units/qwen3.6-coding.service | grep -q '"rung":"READY"'; then
    echo "✓ qwen3.6-coding is READY via API"
else
    echo "✗ qwen3.6-coding is not READY via API"
fi

echo
echo "=========================================="
echo "Rollout drill complete!"
echo "=========================================="
