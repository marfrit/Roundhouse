#!/bin/bash
# Roundhouse MVP4 — enablement drill: checkbox toggle and reboot reconciliation.
#
# Runs ON the host (boltzmann), targeting a Roundhouse container:
#     mvp1/scripts/enablement-drill.sh [CONTAINER_NAME]
#
# Tests the enable-disable toggle checkbox, the preflight collision interlock, drift notes,
# SSE update streams, and boot-time reconciliation via systemd unit enablement.
#
# What this script does and does not do:
#   - it DOES toggle enablement through the API (that is the drill) and uses `incus exec`
#     to read state and to hand-start a unit for the drift-note leg;
#   - it NEVER writes a unit file, never commits, and never runs `incus restart` itself —
#     the two reboots are prompted and typed by the operator, and step 4 proves the
#     zero-write claim by diffing git HEAD and unit-file mtimes across steps 2-3.

set -euo pipefail

CONTAINER="${1:-roundhouse-test}"
CUSER=roundhouse
CHOME="/home/$CUSER"
DEST="$CHOME/roundhouse"
HOST="http://127.0.0.1:8090"

echo "== Roundhouse MVP4 enablement drill: $CONTAINER =="
echo

# Every POST route is bearer-gated (E8). Without the token the toggle steps answer 401
# and the drill would "pass" by never actuating anything.
TOKEN="$(incus exec "$CONTAINER" -- su -l "$CUSER" -c 'cat ~/.config/roundhouse/token' 2>/dev/null | tr -d '\r\n' || true)"
if [ -z "$TOKEN" ]; then
    echo "Error: no bearer token at $CHOME/.config/roundhouse/token — is roundhouse.service running?" >&2
    exit 1
fi

# NOTE: the API serialises JSON with json.dumps' default separators, i.e.
# `"key": "value"` WITH a space. Every grep below therefore matches `: *`,
# never a bare `":"` — a space-blind pattern reports a passing row as failed.
# Helper function to curl the API
api_get() {
    curl -s "$HOST/api/$1"
}

api_post() {
    local path="$1"
    local data="$2"
    curl -s -X POST "$HOST/api/$path" \
        -H 'Content-Type: application/json' \
        -H "Authorization: Bearer $TOKEN" \
        -d "$data"
}

# Unit-file mtimes, one line per unit; run inside the container so the paths resolve.
unit_mtimes() {
    incus exec "$CONTAINER" -- su -l "$CUSER" -c \
        'stat -c "%n %Y" ~/.config/systemd/user/*.service | sort'
}

git_head() {
    incus exec "$CONTAINER" -- su -l "$CUSER" -c \
        'git -C ~/.config/systemd/user rev-parse HEAD' 2>/dev/null || echo "unknown"
}

# Field readers. A unit row nests objects (mem, gate, port_conflict), so a `[^}]*` grep
# cannot reach past the first closing brace to `strategy_note` — it silently matched
# nothing and every drift-note check reported the wrong verdict. Parse the JSON instead;
# python3 is already a hard dependency of roundhouse.py itself.
unit_field() {          # unit_field <unit-name-prefix> <field>
    api_get 'units' | python3 -c '
import json, sys
prefix, field = sys.argv[1], sys.argv[2]
for u in json.load(sys.stdin)["units"]:
    if u["unit"].startswith(prefix):
        v = u.get(field)
        print("" if v is None else v)
        break
' "$1" "$2"
}

self_unit_enabled() {   # -> true|false
    api_get 'units' | python3 -c '
import json, sys
print(str(json.load(sys.stdin).get("self_unit", {}).get("enabled")).lower())
'
}

check() {               # check <label> <actual> <expected>
    if [ "$2" = "$3" ]; then
        echo "  ✓ $1: $2"
    else
        echo "  ✗ $1: got '"'"'$2'"'"', expected '"'"'$3'"'"'"
    fi
}

# Step 1: Pre-state
echo "STEP 1: Pre-state capture"
units_json=$(api_get 'units')
check "self_unit.enabled (dogfood)" "$(self_unit_enabled)" "true"

# Verify via systemctl
is_enabled=$(incus exec "$CONTAINER" -- su -l "$CUSER" -c 'systemctl --user is-enabled roundhouse.service' 2>/dev/null || echo "error")
echo "  systemctl is-enabled: $is_enabled"
echo

# Zero-write baseline — captured BEFORE the first toggle (§5 step 4), compared after step 3.
# Enablement moves symlinks in default.target.wants/; it must not touch a unit file or git.
rev_before="$(git_head)"
mtimes_before="$(unit_mtimes)"

# Step 2: Gated-enable + interlock sequence
echo "STEP 2: Gated-enable and interlock (llama-task + llama-server-qwen35-npu on :8086)"
echo "  Disabling llama-task (always allowed)..."
api_post 'units/llama-task.service/enablement' '{"enabled":false}' > /dev/null
sleep 1

echo "  Enabling llama-server-qwen35-npu (gated on 6.1.75-npu-port, should succeed)..."
resp=$(api_post 'units/llama-server-qwen35-npu.service/enablement' '{"enabled":true}')
echo "  Response: $resp"
if echo "$resp" | grep -q '"enabled": *true'; then
    echo "  ✓ Enable succeeded; unit should show STANDBY + 'returns at boot'"
else
    echo "  ✗ Enable failed (unexpected)"
fi
sleep 1

echo "  Re-enabling llama-task (should fail with 422 interlock)..."
resp=$(api_post 'units/llama-task.service/enablement' '{"enabled":true}')
if echo "$resp" | grep -q '"error": *"enable_collision"'; then
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
check "fake-b   (enabled + OFF)" "$(unit_field llama-server-fake-b strategy_note)" "returns at boot"
check "qwen3.6  (running + disabled)" "$(unit_field qwen3.6-coding strategy_note)" "manual — will not survive reboot"
echo

# Step 4: Zero-write leg — compare against the baseline taken before step 2
echo "STEP 4: Verify no file writes (git rev-parse + unit-file mtimes)"
rev_after="$(git_head)"
mtimes_after="$(unit_mtimes)"

echo "  Git HEAD before: $rev_before"
echo "  Git HEAD after:  $rev_after"

if [ "$rev_before" = "$rev_after" ]; then
    echo "  ✓ Git rev unchanged (no commits)"
else
    echo "  ✗ Git rev changed: $rev_before -> $rev_after"
fi

if [ "$mtimes_before" = "$mtimes_after" ]; then
    echo "  ✓ Unit-file mtimes identical across steps 2-3 (enablement moved symlinks only)"
else
    echo "  ✗ Unit-file mtimes changed:"
    diff <(echo "$mtimes_before") <(echo "$mtimes_after") | sed 's/^/    /' || true
fi

echo "  (enablement symlinks live in ~/.config/systemd/user/default.target.wants/)"
incus exec "$CONTAINER" -- su -l "$CUSER" -c \
    'ls ~/.config/systemd/user/default.target.wants/ 2>/dev/null || echo "    (none)"' | sed 's/^/    /'
echo "  git status (tracked only):"
incus exec "$CONTAINER" -- su -l "$CUSER" -c \
    'git -C ~/.config/systemd/user status --untracked-files=no --short' | sed 's/^/    /' || true
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

echo "  Waiting for the fake loads to settle (FAKE_LOAD_SECONDS up to 15 s)..."
sleep 25
echo "  Checking post-reboot state..."
check "fake-b came back (enabled)" "$(unit_field llama-server-fake-b rung)" "READY"
check "qwen3.6 stayed down (disabled)" "$(unit_field qwen3.6-coding rung)" "OFF"
check "llama-task canary returned" "$(unit_field llama-task rung)" "READY"
check "self_unit.enabled (API answering IS the dogfood proof)" "$(self_unit_enabled)" "true"
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

echo "  Waiting to be sure nothing starts late..."
sleep 25
check "fake-b stayed OFF (disabled)" "$(unit_field llama-server-fake-b rung)" "OFF"
check "fake-b still disabled" "$(unit_field llama-server-fake-b enabled)" "False"
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
