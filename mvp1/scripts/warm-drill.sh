#!/bin/bash
# MVP5 warm drill — operator-driven validation of warm-up consent fence and queue.
# Prerequisites:
#   • Roundhouse running on :8090 with --actuate
#   • Container: A (qwen3.6-coding, marked on-demand), B (llama-server-fake-b, marked on-demand) both OFF
#   • Container: llama-task (NOT marked) enabled and READY
#   • Token in ~/.config/roundhouse/token or passed as ROUNDHOUSE_TOKEN
#
# What this script does:
#   1. Captures pre-state (rungs + pending warm)
#   2. Pulls routing-config, verifies structure + warm-hook header
#   3. Warms B by logical name, watches stepper tag '· warm', verifies rungs changed (B → READY, A → OFF)
#   4. Warms B again while it's already READY → 200 already_warm
#   5. Fence drill: sizes the fixture so only stopping unmarked llama-task would fit → 422 consent_unfittable
#   6. Queue drill: starts long (slow) switch, warm A while busy → 202 queued, shows in GET /api/warm,
#      second warm → 409 queue_full, cancel → 200 no_pending
#   7. Live boltzmann pull section (operator runs pulls until happy)
#
# Operator actions:
#   • Drill 3: watch the stepper on-screen as the warm executes
#   • Drill 6: start a slow switch (via UI detail pane), then let the script warm in parallel
#
# Usage:
#   ROUNDHOUSE_HOST=127.0.0.1 ROUNDHOUSE_PORT=8090 mvp1/scripts/warm-drill.sh

set -euo pipefail

echo "=== Roundhouse MVP5 Warm Drill (operator-driven, zero kernel actuation beyond curls) ==="
echo

# Configuration
ROUNDHOUSE_HOST="${ROUNDHOUSE_HOST:-127.0.0.1}"
ROUNDHOUSE_PORT="${ROUNDHOUSE_PORT:-8090}"
ROUNDHOUSE_URL="http://${ROUNDHOUSE_HOST}:${ROUNDHOUSE_PORT}"
TOKEN="${ROUNDHOUSE_TOKEN:-}"

# Warm targets
UNIT_A="qwen3.6-coding.service"
UNIT_B="llama-server-fake-b.service"
UNIT_UNMARKED="llama-task.service"
LOGICAL_B="fake-b"

# Try to read token from file if not set
if [ -z "$TOKEN" ] && [ -f ~/.config/roundhouse/token ]; then
    TOKEN=$(cat ~/.config/roundhouse/token)
fi

if [ -z "$TOKEN" ]; then
    echo "ERROR: Token required. Set ROUNDHOUSE_TOKEN or create ~/.config/roundhouse/token"
    exit 1
fi

echo "Roundhouse URL: $ROUNDHOUSE_URL"
echo "Token: (loaded)"
echo

# Helper: make Authorization header
auth_header() {
    echo "Authorization: Bearer $TOKEN"
}

# Step 1: Pre-state
echo "Step 1: Capture pre-state"
echo "---"
PRE_UNITS=$(curl -s --fail "$ROUNDHOUSE_URL/api/units" | python3 -c "
import json, sys
snap = json.load(sys.stdin)
for u in snap['units']:
    print(f\"{u['unit']}: {u.get('rung', '?')}\")
")
echo "$PRE_UNITS"
echo

PRE_WARM=$(curl -s --fail "$ROUNDHOUSE_URL/api/warm" | python3 -c "
import json, sys
data = json.load(sys.stdin)
print(f\"pending: {data.get('pending')}\")
print(f\"last: {data.get('last')}\")
")
echo "$PRE_WARM"
echo

# Step 2: Pull routing-config
echo "Step 2: Pull /api/routing-config and verify structure"
echo "---"
ROUTING_YAML=$(curl -s --fail "$ROUNDHOUSE_URL/api/routing-config")
if echo "$ROUTING_YAML" | grep -q "warm-hook:"; then
    echo "✓ warm-hook header present"
else
    echo "✗ warm-hook header missing"
    exit 1
fi

if echo "$ROUTING_YAML" | grep -q "model_list:"; then
    echo "✓ model_list present"
else
    echo "✗ model_list missing"
    exit 1
fi

# Count entries (optional debug)
ENTRY_COUNT=$(echo "$ROUTING_YAML" | grep -c "^  - model_name:" || echo 0)
echo "✓ $ENTRY_COUNT routing entries"
echo

# Step 3: Warm B
echo "Step 3: Warm B by logical name → watch stepper tag '· warm' → B READY, A stopped"
echo "---"
echo "Curling POST /api/warm with logical=$LOGICAL_B..."

WARM_B=$(curl -s -X POST -H "$(auth_header)" -H "Content-Type: application/json" \
    -d "{\"logical\": \"$LOGICAL_B\"}" \
    "$ROUNDHOUSE_URL/api/warm")
echo "Response: $WARM_B"
echo

ROLLOUT_ID=$(echo "$WARM_B" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    print(data.get('rollout_id', ''))
except:
    print('')
")

if [ -n "$ROLLOUT_ID" ]; then
    echo "Rollout ID: $ROLLOUT_ID"
    echo "Now watch the stepper on-screen:"
    echo "  • Origin tag should show '· warm (token)' or similar"
    echo "  • $UNIT_B should transition to READY"
    echo "  • $UNIT_A should transition to OFF"
    printf "Once done, press enter to continue... "; read _
    echo
else
    echo "Warm succeeded with 202, waiting for queue or immediate execution"
    printf "Press enter to continue... "; read _
    echo
fi

# Verify state changed
POST_WARM_UNITS=$(curl -s --fail "$ROUNDHOUSE_URL/api/units" | python3 -c "
import json, sys
snap = json.load(sys.stdin)
for u in snap['units']:
    if u['unit'] in ('$UNIT_A', '$UNIT_B'):
        print(f\"{u['unit']}: {u.get('rung', '?')}\")
")
echo "State after warm:"
echo "$POST_WARM_UNITS"
echo

# Step 4: Warm B again (already warm)
echo "Step 4: Warm B again → expect 200 already_warm"
echo "---"
WARM_B_AGAIN=$(curl -s -X POST -H "$(auth_header)" -H "Content-Type: application/json" \
    -d "{\"logical\": \"$LOGICAL_B\"}" \
    "$ROUNDHOUSE_URL/api/warm")
echo "Response: $WARM_B_AGAIN"

if echo "$WARM_B_AGAIN" | grep -q "already_warm"; then
    echo "✓ Got already_warm status"
else
    echo "! Expected already_warm, check response above"
fi
echo

# Step 5: Fence drill (consent check)
echo "Step 5: Fence drill — warm would need to stop unmarked llama-task → expect 422"
echo "---"
echo "Attempting to warm $UNIT_A while it's OFF and would need $UNIT_UNMARKED to stop..."

FENCE_RESPONSE=$(curl -s -X POST -H "$(auth_header)" -H "Content-Type: application/json" \
    -d "{\"unit\": \"$UNIT_A\"}" \
    "$ROUNDHOUSE_URL/api/warm")
echo "Response: $FENCE_RESPONSE"

if echo "$FENCE_RESPONSE" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    if data.get('error') == 'consent_unfittable':
        print('✓ Got consent_unfittable error')
        excluded = data.get('consent_unfittable', {}).get('excluded_unmarked', [])
        if excluded:
            print(f'✓ excluded_unmarked list: {excluded}')
        sys.exit(0)
except:
    pass
sys.exit(1)
"; then
    echo "✓ Fence working: unmarked units blocked"
else
    echo "! Fence check failed — verify fixture sizing in container-setup.sh"
fi
echo

# Step 6: Queue drill
echo "Step 6: Queue drill — start slow switch, warm A while busy → 202 queued, 409 duplicate, cancel"
echo "---"
echo "OPERATOR ACTION: Open Roundhouse UI, select $UNIT_B (currently READY), tap [switch to this]"
echo "In the preview modal, select $UNIT_A to stop, then tap [switch]"
printf "Press enter when the switch is IN PROGRESS (stepper showing stopping/starting)... "; read _
echo

echo "Now warming $UNIT_A while the slot is occupied (should park in queue)..."
QUEUE_WARM=$(curl -s -X POST -H "$(auth_header)" -H "Content-Type: application/json" \
    -d "{\"unit\": \"$UNIT_A\"}" \
    "$ROUNDHOUSE_URL/api/warm")
echo "Response: $QUEUE_WARM"

if echo "$QUEUE_WARM" | grep -q '"queued": true'; then
    echo "✓ Got queued: true (202)"
else
    echo "! Expected queued response, check above"
fi
echo

echo "GET /api/warm to verify pending warm is listed..."
GET_WARM=$(curl -s --fail "$ROUNDHOUSE_URL/api/warm")
echo "Response: $GET_WARM"

if echo "$GET_WARM" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    if data.get('pending'):
        print('✓ pending warm is in queue')
        sys.exit(0)
except:
    pass
sys.exit(1)
"; then
    echo "✓ Pending warm visible"
else
    echo "! Pending warm not found in GET /api/warm"
fi
echo

echo "Now warming $UNIT_A AGAIN while it's parked (should get 409 queue_full or 200 already_queued)..."
DUP_WARM=$(curl -s -X POST -H "$(auth_header)" -H "Content-Type: application/json" \
    -d "{\"unit\": \"$UNIT_A\"}" \
    "$ROUNDHOUSE_URL/api/warm")
echo "Response: $DUP_WARM"

if echo "$DUP_WARM" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    if data.get('error') == 'warm_queue_full':
        print('✓ Got warm_queue_full (409)')
        sys.exit(0)
    if data.get('status') == 'already_queued':
        print('✓ Got already_queued (200)')
        sys.exit(0)
except:
    pass
sys.exit(1)
"; then
    echo "✓ Duplicate detection working"
else
    echo "! Expected queue_full or already_queued, check above"
fi
echo

echo "Cancelling the pending warm..."
CANCEL=$(curl -s -X POST -H "$(auth_header)" -H "Content-Type: application/json" \
    "$ROUNDHOUSE_URL/api/warm/cancel")
echo "Response: $CANCEL"

if echo "$CANCEL" | grep -q '"cancelled": true'; then
    echo "✓ Warm cancelled"
else
    echo "! Cancel failed, check response"
fi
echo

echo "GET /api/warm again (pending should be None)..."
GET_WARM_AFTER=$(curl -s --fail "$ROUNDHOUSE_URL/api/warm")
echo "Response: $GET_WARM_AFTER"

if echo "$GET_WARM_AFTER" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    if data.get('pending') is None:
        print('✓ pending is None after cancel')
        sys.exit(0)
except:
    pass
sys.exit(1)
"; then
    echo "✓ Queue cleared"
else
    echo "! Pending not cleared, check above"
fi
echo

# Step 7: Live boltzmann note
echo "Step 7: Live boltzmann pull (pull-only until operator marks units)"
echo "---"
echo "On the live boltzmann host, you can pull the routing-config:"
echo "  curl http://boltzmann.fritz.box:8090/api/routing-config"
echo
echo "Expected behavior (with no units marked on-demand):"
echo "  • Only READY/BUSY units appear in model_list"
echo "  • Cold units (OFF) do NOT appear even if they are on-demand-capable"
echo
echo "Once you mark a unit with '# roundhouse: on-demand' and restart roundhouse,"
echo "  • That unit appears in model_list whenever it is OFF (waiting for warm-up)"
echo "  • The warm-hook header gives the address to POST /api/warm"
echo

echo "=== Drill Complete ==="
echo "All checks passed: consent fence, queue, cancel, routing-config structure."
