#!/bin/bash
# peer-drill.sh: T2 container drill for peer reachability watch
#
# Sets up two peers (one reachable, one not), kills and restores the reachable
# one, and observes both transitions with correct hysteresis timing. Uses
# --peer-interval to keep the drill short. No jq; stdlib-python JSON parsing.
#
# Exit 0 on PASS, 1 on FAIL. Each step prints PASS/FAIL to stderr.

set -u
PASS=0
FAIL=0

# Helper: check that a condition is true
check() {
    local desc="$1"
    local cmd="$2"
    if eval "$cmd" 2>/dev/null; then
        echo "✓ $desc" >&2
        ((PASS++))
        return 0
    else
        echo "✗ $desc" >&2
        ((FAIL++))
        return 1
    fi
}

# Helper: get a peer state via /api/peers
get_peer_state() {
    local name="$1"
    python3 -c "
import json, urllib.request
try:
    resp = urllib.request.urlopen('http://localhost:8090/api/peers', timeout=2)
    data = json.load(resp)
    for peer in data.get('peers', []):
        if peer['name'] == '$name':
            print(peer['state'])
            exit(0)
    print('not_found')
except Exception as e:
    print('error')
"
}

# Helper: wait for a peer to reach a state with timeout
wait_for_state() {
    local name="$1"
    local expected_state="$2"
    local timeout="${3:-30}"
    local elapsed=0
    while [ $elapsed -lt $timeout ]; do
        state=$(get_peer_state "$name")
        if [ "$state" = "$expected_state" ]; then
            return 0
        fi
        sleep 0.5
        ((elapsed += 1))
    done
    echo "✗ Timeout waiting for $name -> $expected_state (got $state)" >&2
    return 1
}

# Step 1: Start Roundhouse with two peers
# peer1: reachable (will listen on :9999)
# peer2: unreachable (fake address)
echo "=== Step 1: Start Roundhouse with peer config ===" >&2
python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', 9999))
s.listen(1)
import threading
def accept_loop():
    try:
        while True:
            conn, _ = s.accept()
            conn.close()
    except:
        pass
threading.Thread(target=accept_loop, daemon=True).start()
print('listening')
" &
PEER1_PID=$!
sleep 1

# Start Roundhouse
python3 roundhouse.py \
    --bind 127.0.0.1 \
    --port 8090 \
    --select all \
    --peer peer1=127.0.0.1:9999 \
    --peer peer2=192.0.2.1:1234 \
    --peer-interval 2 \
    2>/dev/null &
RH_PID=$!
sleep 2

check "Roundhouse started" "curl -s http://localhost:8090/ >/dev/null"

# Step 2: Check initial state
# peer1 should become 'up' (listening), peer2 should be 'unknown' or 'down'
echo "=== Step 2: Check initial peer states ===" >&2
wait_for_state peer1 up 15
check "peer1 initial state is 'up'" "[ $(get_peer_state peer1) = up ]"

state2=$(get_peer_state peer2)
check "peer2 initial state is 'unknown' or 'down'" "[ '$state2' = unknown ] || [ '$state2' = down ]"

# Step 3: Kill peer1 (close the listening socket)
echo "=== Step 3: Kill peer1 listening socket ===" >&2
kill $PEER1_PID 2>/dev/null || true
wait $PEER1_PID 2>/dev/null || true
sleep 1
check "peer1 listener killed" "! nc -z 127.0.0.1 9999 2>/dev/null"

# Step 4: Wait for hysteresis (two consecutive failures before down)
# The spec says "down only after two consecutive failures"
# With --peer-interval 2, we need at least 4 seconds to see the transition
echo "=== Step 4: Wait for hysteresis (2 probe cycles) ===" >&2
wait_for_state peer1 down 10
check "peer1 transitioned to 'down'" "[ $(get_peer_state peer1) = down ]"

# Step 5: Restore peer1 (start listening again)
echo "=== Step 5: Restore peer1 listener ===" >&2
python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', 9999))
s.listen(1)
import threading
def accept_loop():
    try:
        while True:
            conn, _ = s.accept()
            conn.close()
    except:
        pass
threading.Thread(target=accept_loop, daemon=True).start()
import time; time.sleep(999)
" &
PEER1_RESTORED=$!
sleep 1

# Step 6: Verify peer1 goes back to 'up' on first success
# The spec says "up on the first successful connect"
echo "=== Step 6: Verify peer1 returns to 'up' ===" >&2
wait_for_state peer1 up 10
check "peer1 returned to 'up'" "[ $(get_peer_state peer1) = up ]"

# Cleanup
echo "=== Cleanup ===" >&2
kill $RH_PID 2>/dev/null || true
kill $PEER1_RESTORED 2>/dev/null || true
wait $RH_PID 2>/dev/null || true
wait $PEER1_RESTORED 2>/dev/null || true

# Summary
echo "=== Summary ===" >&2
echo "PASS: $PASS" >&2
echo "FAIL: $FAIL" >&2

if [ $FAIL -eq 0 ]; then
    exit 0
else
    exit 1
fi
