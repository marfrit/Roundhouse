#!/bin/bash
# MVP3 switch drill — operator phone UI flow (zero actuation by script).
# Prerequisites:
#   • Roundhouse running on :8090 with --actuate (container or boltzmann)
#   • UNIT_A READY (live default: qwen3.6-coding on :8085)
#   • UNIT_B startable (live default: llama-server-gemma4 on :8093)
#   • Token in ~/.config/roundhouse/token or passed as ROUNDHOUSE_TOKEN
#
# In the acceptance CONTAINER the two units are named differently; override them:
#   UNIT_B=llama-server-fake-b.service mvp1/scripts/switch-drill.sh
#
# What this script does:
#   1. Captures fleet state BEFORE
#   2. Prompts to navigate to Roundhouse on a phone (or desktop)
#   3. Walks through the UI switch steps
#   4. Verifies fleet state AFTER
#
# What the operator does (via UI):
#   1. Open Roundhouse on the phone
#   2. Tap qwen3.6-coding (ACTIVE)
#   3. (optional: observe detail pane)
#   4. Close detail, tap llama-server-fake-b (OFF)
#   5. Tap [switch to this]
#   6. Preview: tick qwen3.6-coding to stop
#   7. Watch fit check pass
#   8. Tap [switch]
#   9. Watch STOPPING → STARTING → WATCHING → DONE
#   10. Verify $UNIT_A is OFF, $UNIT_B is READY
#   11. Repeat: switch back to $UNIT_A

set -euo pipefail

echo "=== Roundhouse MVP3 Switch Drill (UI-driven, zero script actuation) ==="
echo

# Configuration
ROUNDHOUSE_HOST="${ROUNDHOUSE_HOST:-127.0.0.1}"
ROUNDHOUSE_PORT="${ROUNDHOUSE_PORT:-8090}"
ROUNDHOUSE_URL="http://${ROUNDHOUSE_HOST}:${ROUNDHOUSE_PORT}"
TOKEN="${ROUNDHOUSE_TOKEN:-}"
# The live drill of MVP3.md is qwen3.6-coding -> llama-server-gemma4 (:8093) -> back.
UNIT_A="${UNIT_A:-qwen3.6-coding.service}"
UNIT_B="${UNIT_B:-llama-server-gemma4.service}"

# Try to read token from file if not set
if [ -z "$TOKEN" ] && [ -f ~/.config/roundhouse/token ]; then
    TOKEN=$(cat ~/.config/roundhouse/token)
fi

echo "Roundhouse URL: $ROUNDHOUSE_URL"
echo "Token: ${TOKEN:-(none; read-only mode)}"
echo

# Pre-state capture
echo "Step 1: Capture pre-state"
echo "---"
PRE_STATE=$(curl -s --fail "$ROUNDHOUSE_URL/api/units" | UNIT_A="$UNIT_A" UNIT_B="$UNIT_B" python3 -c "
import json, os, sys
# /api/units answers the whole snapshot OBJECT; iterating it walked the top-level KEYS
# (strings) and every u['unit'] raised TypeError, so this section never once printed state.
snap = json.load(sys.stdin)
want = {os.environ['UNIT_A'], os.environ['UNIT_B']}
for u in snap['units']:
    if u['unit'] in want:
        print(f\"  {u['unit']}: {u.get('rung', '?')} (:{u.get('port', '?')})\")
")
echo "$PRE_STATE"
echo

# Prompt for phone access
echo "Step 2: Open Roundhouse on your phone (or desktop)"
echo "---"
echo "URL: $ROUNDHOUSE_URL"
if [ -n "$TOKEN" ]; then
    echo "Token: $TOKEN (or paste from ~/.config/roundhouse/token)"
fi
echo
printf "Once open and logged in, press enter to continue... "; read _
echo

# Switch sequence
echo "Step 3: Switch $UNIT_A → $UNIT_B"
echo "---"
echo "  a) Tap qwen3.6-coding (ACTIVE section, top)"
echo "  b) Verify detail pane shows: qwen3.6-coding, READY, :8085, peak memory, etc."
echo "  c) Close detail pane (click 'Close' button)"
echo "  d) Scroll down, tap $UNIT_B (OFF section, turntable)"
echo "  e) You should see detail pane for $UNIT_B"
echo "  f) Tap [switch to this] button"
echo

printf "After tapping [switch to this], press enter... "; read _
echo

# Preview phase
echo "Step 4: Review switch preview"
echo "---"
echo "  a) Modal shows:"
echo "     • Target: $UNIT_B · estimate (GiB)"
echo "     • Stop Candidates: $UNIT_A (checkbox, READY, residency)"
echo "     • Memory Check: estimate vs available + freed budget"
echo "     • Port Check: :8093 status"
echo "  b) Tick the checkbox for $UNIT_A"
echo "  c) Verify [switch] button becomes enabled (estimate + headroom ≤ budget)"
echo "  d) Tap [switch]"
echo

printf "After tapping [switch], press enter... "; read _
echo

# Execution phase
echo "Step 5: Watch the switch execute"
echo "---"
echo "  Stepper should progress:"
echo "    preflight ● → stopping (1/1) → starting → watching → done ✓"
echo "  Detail line updates as it goes"
echo "  Watch for SSE events showing:"
echo "    • 'stopping $UNIT_A (1/1)'"
echo "    • 'starting $UNIT_B'"
echo "    • elapsed timer rising in LOADING phase"
echo "    • 'switched: $UNIT_B ready in Xs'"
echo

printf "Once done (stepper shows done ✓), press enter... "; read _
echo

# Reverse switch (optional)
echo "Step 6: Switch back to $UNIT_A (optional)"
echo "---"
echo "  Repeat steps 3–5, but:"
echo "  a) Close detail, tap $UNIT_A (now in OFF)"
echo "  b) Tap [switch to this]"
echo "  c) Tick $UNIT_B in the preview"
echo "  d) Tap [switch]"
echo "  e) Watch it stop $UNIT_B and start $UNIT_A"
echo

printf "Do you want to perform the reverse switch? (y/n): "; read REVERSE
if [ "$REVERSE" = "y" ] || [ "$REVERSE" = "Y" ]; then
    printf "After switch back completes, press enter... "; read _
fi
echo

# Post-state verification
echo "Step 7: Verify post-state"
echo "---"
POST_STATE=$(curl -s --fail "$ROUNDHOUSE_URL/api/units" | UNIT_A="$UNIT_A" UNIT_B="$UNIT_B" python3 -c "
import json, os, sys
# /api/units answers the whole snapshot OBJECT; iterating it walked the top-level KEYS
# (strings) and every u['unit'] raised TypeError, so this section never once printed state.
snap = json.load(sys.stdin)
want = {os.environ['UNIT_A'], os.environ['UNIT_B']}
for u in snap['units']:
    if u['unit'] in want:
        print(f\"  {u['unit']}: {u.get('rung', '?')} (:{u.get('port', '?')})\")
")
echo "$POST_STATE"
echo

echo "=== Drill Complete ==="
echo "Pre-state:"
echo "$PRE_STATE"
echo
echo "Post-state:"
echo "$POST_STATE"
echo
if [ "$POST_STATE" = "$PRE_STATE" ]; then
    echo "✓ fleet is back to its pre-drill state"
else
    echo "! fleet does NOT match its pre-drill state — check the rungs above before you walk away"
fi
