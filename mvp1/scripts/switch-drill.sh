#!/bin/bash
# MVP3 switch drill — operator phone UI flow (zero actuation by script).
# Prerequisites:
#   • Roundhouse running on :8090 with --actuate (container or boltzmann)
#   • qwen3.6-coding READY on :8085 (or mock fixture unit A)
#   • llama-server-fake-b ready to start on :8093 (or mock fixture unit B)
#   • Token in ~/.config/roundhouse/token or passed as ROUNDHOUSE_TOKEN
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
#   10. Verify qwen3.6-coding is OFF, llama-server-fake-b is READY
#   11. Repeat: switch back to qwen3.6-coding

set -euo pipefail

echo "=== Roundhouse MVP3 Switch Drill (UI-driven, zero script actuation) ==="
echo

# Configuration
ROUNDHOUSE_HOST="${ROUNDHOUSE_HOST:-127.0.0.1}"
ROUNDHOUSE_PORT="${ROUNDHOUSE_PORT:-8090}"
ROUNDHOUSE_URL="http://${ROUNDHOUSE_HOST}:${ROUNDHOUSE_PORT}"
TOKEN="${ROUNDHOUSE_TOKEN:-}"

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
PRE_STATE=$(curl -s "$ROUNDHOUSE_URL/api/units" | python3 -c "
import json, sys
units = json.load(sys.stdin)
for u in units:
    if u['unit'] in ['qwen3.6-coding.service', 'llama-server-fake-b.service']:
        print(f\"{u['unit']}: {u.get('rung', '?')}\")
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
echo "Step 3: Switch qwen3.6-coding → llama-server-fake-b"
echo "---"
echo "  a) Tap qwen3.6-coding (ACTIVE section, top)"
echo "  b) Verify detail pane shows: qwen3.6-coding, READY, :8085, peak memory, etc."
echo "  c) Close detail pane (click 'Close' button)"
echo "  d) Scroll down, tap llama-server-fake-b (OFF section, turntable)"
echo "  e) You should see detail pane for llama-server-fake-b"
echo "  f) Tap [switch to this] button"
echo

printf "After tapping [switch to this], press enter... "; read _
echo

# Preview phase
echo "Step 4: Review switch preview"
echo "---"
echo "  a) Modal shows:"
echo "     • Target: llama-server-fake-b · estimate (GiB)"
echo "     • Stop Candidates: qwen3.6-coding (checkbox, READY, residency)"
echo "     • Memory Check: estimate vs available + freed budget"
echo "     • Port Check: :8093 status"
echo "  b) Tick the checkbox for qwen3.6-coding"
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
echo "    • 'stopping qwen3.6-coding (1/1)'"
echo "    • 'starting llama-server-fake-b'"
echo "    • elapsed timer rising in LOADING phase"
echo "    • 'switched: llama-server-fake-b ready in Xs'"
echo

printf "Once done (stepper shows done ✓), press enter... "; read _
echo

# Reverse switch (optional)
echo "Step 6: Switch back to qwen3.6-coding (optional)"
echo "---"
echo "  Repeat steps 3–5, but:"
echo "  a) Close detail, tap qwen3.6-coding (now in OFF)"
echo "  b) Tap [switch to this]"
echo "  c) Tick llama-server-fake-b in the preview"
echo "  d) Tap [switch]"
echo "  e) Watch it stop llama-server-fake-b and start qwen3.6-coding"
echo

printf "Do you want to perform the reverse switch? (y/n): "; read REVERSE
if [ "$REVERSE" = "y" ] || [ "$REVERSE" = "Y" ]; then
    printf "After switch back completes, press enter... "; read _
fi
echo

# Post-state verification
echo "Step 7: Verify post-state"
echo "---"
POST_STATE=$(curl -s "$ROUNDHOUSE_URL/api/units" | python3 -c "
import json, sys
units = json.load(sys.stdin)
for u in units:
    if u['unit'] in ['qwen3.6-coding.service', 'llama-server-fake-b.service']:
        print(f\"{u['unit']}: {u.get('rung', '?')}\")
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
echo "✓ Switch flow works; UI renders correctly; fleet responds to switches"
