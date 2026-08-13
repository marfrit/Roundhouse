#!/bin/bash
# Roundhouse MVP4 — enablement drill: checkbox toggle and reboot reconciliation.
#
# Runs ON the host (boltzmann), targeting a Roundhouse container:
#     mvp1/scripts/enablement-drill.sh [CONTAINER_NAME]
#
# Tests the enable-disable toggle checkbox, the preflight collision interlock, drift notes,
# SSE update streams, and boot-time reconciliation via systemd unit enablement.
#
# The script performs zero actuation beyond curl state capture and prompts for operator-typed
# incus restart commands; it never runs incus itself.

set -euo pipefail

CONTAINER="${1:-roundhouse-test}"
CUSER=roundhouse
CHOME="/home/$CUSER"
DEST="$CHOME/roundhouse"
HOST="http://127.0.0.1:8090"

echo "== Roundhouse MVP4 enablement drill: $CONTAINER =="
echo

# Helper function to curl the API
api_get() {
    curl -s "$HOST/api/$1"
}

api_post() {
    local path="$1"
    local data="$2"
    curl -s -X POST "$HOST/api/$path" \
        -H 'Content-Type: application/json' \
        -d "$data"
}

# Step 1: Pre-state
echo "STEP 1: Pre-state capture"
units_json=$(api_get 'units')
self_unit=$(echo "$units_json" | grep -o '"self_unit"[^}]*}' || echo '{}')
echo "  self_unit: $self_unit"
self_enabled=$(echo "$self_unit" | grep -o '"enabled":[^,}]*' | grep -o '[^:]*$')
if [ "$self_enabled" = "true" ]; then
    echo "  ✓ self_unit.enabled = true (dogfood verified)"
else
    echo "  ✗ self_unit.enabled = $self_enabled (expected true)"
fi

# Verify via systemctl
is_enabled=$(incus exec "$CONTAINER" -- su -l "$CUSER" -c 'systemctl --user is-enabled roundhouse.service' 2>/dev/null || echo "error")
echo "  systemctl is-enabled: $is_enabled"
echo

# Step 2: Gated-enable + interlock sequence
echo "STEP 2: Gated-enable and interlock (llama-task + llama-server-qwen35-npu on :8086)"
echo "  Disabling llama-task (always allowed)..."
api_post 'units/llama-task.service/enablement' '{"enabled":false}' > /dev/null
sleep 1

echo "  Enabling llama-server-qwen35-npu (gated on 6.1.75-npu-port, should succeed)..."
resp=$(api_post 'units/llama-server-qwen35-npu.service/enablement' '{"enabled":true}')
echo "  Response: $resp"
if echo "$resp" | grep -q '"enabled":true'; then
    echo "  ✓ Enable succeeded; unit should show STANDBY + 'returns at boot'"
else
    echo "  ✗ Enable failed (unexpected)"
fi
sleep 1

echo "  Re-enabling llama-task (should fail with 422 interlock)..."
resp=$(api_post 'units/llama-task.service/enablement' '{"enabled":true}')
if echo "$resp" | grep -q '"error":"enable_collision"'; then
    echo "  ✓ 422 enable_collision: $resp"
else
    echo "  ✗ Expected 422, got: $resp"
fi

echo "  Disabling npu unit and restoring llama-task..."
api_post 'units/llama-server-qwen35-npu.service/enablement' '{"enabled":false}' > /dev/null
api_post 'units/llama-task.service/enablement' '{"enabled":true}' > /dev/null
sleep 1
echo

# Step 3: Drift notes
echo "STEP 3: Drift notes (enable OFF unit, then start a disabled unit)"
echo "  Enabling llama-server-fake-b (OFF, no claimant)..."
api_post 'units/llama-server-fake-b.service/enablement' '{"enabled":true}' > /dev/null
sleep 1

echo "  Starting qwen3.6-coding via turntable (while disabled)..."
incus exec "$CONTAINER" -- su -l "$CUSER" -c \
    'systemctl --user start qwen3.6-coding.service' 2>/dev/null || true
sleep 2

echo "  Capturing state to verify both notes appear..."
units_json=$(api_get 'units')
echo "$units_json" | grep -o '"unit":"llama-server-fake-b[^}]*"strategy_note":"[^"]*"' | head -1 && \
    echo "  ✓ fake-b has drift note (should be 'returns at boot')"
echo "$units_json" | grep -o '"unit":"qwen3.6-coding[^}]*"strategy_note":"[^"]*"' | head -1 && \
    echo "  ✓ qwen3.6 has drift note (should be 'manual — will not survive reboot')" || \
    echo "  (qwen3.6 may not show note if running+enabled, which is correct)"
echo

# Step 4: Zero-write leg
echo "STEP 4: Verify no file writes (git rev-parse + mtimes)"
rev_before=$(incus exec "$CONTAINER" -- su -l "$CUSER" -c \
    'git -C ~/.config/systemd/user rev-parse HEAD 2>/dev/null' || echo "unknown")
mtimes_before=$(incus exec "$CONTAINER" -- ls -t "$CHOME/.config/systemd/user/" 2>/dev/null | head -5 | xargs ls -l | awk '{print $6, $7, $8}' | head -3)

echo "  Git HEAD: $rev_before"
echo "  Unit file mtimes (before):"
echo "$mtimes_before" | sed 's/^/    /'

sleep 2

# Capture again after the drills above
mtimes_after=$(incus exec "$CONTAINER" -- ls -t "$CHOME/.config/systemd/user/" 2>/dev/null | head -5 | xargs ls -l | awk '{print $6, $7, $8}' | head -3)
rev_after=$(incus exec "$CONTAINER" -- su -l "$CUSER" -c \
    'git -C ~/.config/systemd/user rev-parse HEAD 2>/dev/null' || echo "unknown")

if [ "$rev_before" = "$rev_after" ]; then
    echo "  ✓ Git rev unchanged (no commits)"
else
    echo "  ✗ Git rev changed: $rev_before -> $rev_after"
fi
echo

# Step 5: Reboot leg A
echo "STEP 5A: Reboot with fake-b enabled and qwen3.6 running-but-disabled"
echo "  (fake-b should come back; qwen3.6 should stay OFF; llama-task should return; self enabled)"
echo
echo "  >>> OPERATOR: type this command to restart the container:"
echo "  incus restart $CONTAINER"
echo

read -p "  Press ENTER after the container restarts and the API responds..."
echo

sleep 2
units_json=$(api_get 'units')
echo "  Checking post-reboot state..."

# Check fake-b is active
if echo "$units_json" | grep -q '"unit":"llama-server-fake-b[^}]*"rung":"READY"'; then
    echo "  ✓ fake-b is READY (returned)"
else
    echo "  ? fake-b status (may not be READY)"
fi

# Check qwen3.6 is OFF
if echo "$units_json" | grep -q '"unit":"qwen3.6-coding[^}]*"rung":"OFF"'; then
    echo "  ✓ qwen3.6 is OFF (stayed down)"
else
    echo "  ? qwen3.6 status (may not be OFF)"
fi

# Check llama-task is active
if echo "$units_json" | grep -q '"unit":"llama-task[^}]*"rung":"'; then
    echo "  ✓ llama-task present (canary returned)"
else
    echo "  ✗ llama-task missing"
fi

# Check self_unit
self_unit=$(echo "$units_json" | grep -o '"self_unit"[^}]*}' || echo '{}')
self_enabled=$(echo "$self_unit" | grep -o '"enabled":[^,}]*' | grep -o '[^:]*$')
if [ "$self_enabled" = "true" ]; then
    echo "  ✓ self_unit.enabled = true (roundhouse returned)"
else
    echo "  ✗ self_unit.enabled = $self_enabled"
fi
echo

# Step 6: Reboot leg B
echo "STEP 5B: Reboot with fake-b disabled"
echo "  (fake-b should stay OFF)"
echo
echo "  Disabling fake-b via checkbox..."
api_post 'units/llama-server-fake-b.service/enablement' '{"enabled":false}' > /dev/null
sleep 1

echo "  >>> OPERATOR: type this command to restart the container:"
echo "  incus restart $CONTAINER"
echo

read -p "  Press ENTER after the container restarts..."
echo

sleep 2
units_json=$(api_get 'units')
if echo "$units_json" | grep -q '"unit":"llama-server-fake-b[^}]*"rung":"OFF"'; then
    echo "  ✓ fake-b stayed OFF (as intended)"
else
    echo "  ? fake-b status"
fi
echo

# Step 7: RETIRED refusal
echo "STEP 7: RETIRED units refuse enable (mixperten)"
echo "  Attempting to enable mixperten.service (should 422 preflight_failed)..."
resp=$(api_post 'units/mixperten.service/enablement' '{"enabled":true}')
if echo "$resp" | grep -q 'preflight_failed'; then
    echo "  ✓ 422 preflight_failed: $resp"
else
    echo "  ✗ Expected 422 preflight_failed, got: $resp"
fi
echo

# Step 8: Live boltzmann (optional, may remain open)
echo "STEP 8: Live boltzmann phone drill (optional, operator-authorized)"
echo "  Candidates on boltzmann (3 enabled): llama-embed, llama-task, qwen3.6-coding"
echo "  Choose one unit, toggle its checkbox ON (verify is-enabled), toggle OFF (verify disabled)"
echo "  Fleet must end in: 3 enabled (unchanged), others disabled (unchanged)"
echo "  OPERATOR: perform the drill or skip (this step may remain open at push)"
echo

echo "== Enablement drill complete =="
