#!/bin/bash
# peer-drill.sh — MVP7 container drill: the listen list and the peer watch.
#
# Everything here is automated and every timing claim is asserted, not eyeballed:
# the hysteresis legs assert the state AND the consecutive_failures counter at each
# round boundary, so "down after two failures" is proven rather than observed once.
#
#   ./peer-drill.sh              container legs (read-only instance; no actuation)
#   RH_UNIT_DIR=<disposable> ./peer-drill.sh --slot
#                                also the shared-operation-slot leg. This one ARMS
#                                actuation and STOPS whichever unit the preview
#                                suggests, so it REFUSES to run unless RH_UNIT_DIR
#                                is set explicitly — never against the default dir,
#                                which on a live host holds production models.
#   ./peer-drill.sh --live       print the live-boltzmann operator checklist and exit
#
# Env: RH_PORT (default 8190 — deliberately NOT 8090, so a running roundhouse.service
# is never disturbed), RH_UNIT_DIR (default ~/.config/systemd/user).
#
# Exit 0 on PASS, 1 on any FAIL. stdlib python3 only; no jq, no nc.

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MVP1="$(dirname "$HERE")"
RH="$MVP1/roundhouse.py"
RH_PORT="${RH_PORT:-8190}"
# Remember whether the operator chose the unit dir BEFORE defaulting it — the
# --slot guard below is meaningless once the default has been substituted.
RH_UNIT_DIR_EXPLICIT="${RH_UNIT_DIR+yes}"
RH_UNIT_DIR="${RH_UNIT_DIR:-$HOME/.config/systemd/user}"
INTERVAL=5
PFAKE=9401          # unmanaged port: a legitimate local fake peer
PGONE=9402          # never bound: the peer that is simply not there
LOG_DIR="$(mktemp -d /tmp/peer-drill.XXXXXX)"

PASS=0
FAIL=0

pass() { echo "  PASS  $*"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL  $*"; FAIL=$((FAIL + 1)); }
step() { echo; echo "=== $* ==="; }
note() { echo "        $*"; }

assert() {  # assert "<description>" <shell condition...>
    local desc="$1"; shift
    if "$@"; then pass "$desc"; else fail "$desc"; fi
}

# ---------------------------------------------------------------- live checklist
if [ "${1:-}" = "--live" ]; then
    cat <<'LIVE'
=== MVP7 live leg — boltzmann, read-only, sensing only ===

The peer watch actuates nothing, so the live row needs no arming and no token:

  cd ~/roundhouse-live/mvp1
  python3 roundhouse.py --serve --port 8099 --no-db \
      --peer ampere=ampere.fritz.box:8099 \
      --peer dirac=dirac.fritz.box:22

Port 8099 for the instance itself: it is unclaimed by the fleet, so nothing collides.
Peer ports: ampere:8099 is the port a peer Roundhouse would answer on; dirac:22 is
sshd, which is up whenever the host is up and is therefore the honest "is dirac here"
question — a peer port is not required to be a Roundhouse.

Watch for a few minutes and check:
  * curl -s http://127.0.0.1:8099/api/peers   -> states move unknown -> up/down
  * an absent host settles `down` only after TWO probes (one 60 s cadence apart)
  * `getent hosts ampere.fritz.box` still ANSWERS while the probe says down — that is
    the whole reason the probe is a TCP connect and not a name lookup
  * the API stays responsive while a probe against an unreachable host is in flight
    (the 2 s connect must not be holding watcher_lock)
  * the fleet is untouched: systemctl --user show -p MainPID -p ActiveEnterTimestamp
    llama-embed.service llama-task.service qwen3.6-coding.service before and after
LIVE
    exit 0
fi

RUN_SLOT=0
if [ "${1:-}" = "--slot" ]; then
    # MVP7 review M3: this leg ARMS an instance and STOPS whatever unit the
    # preview suggests. Against the default unit dir on a live host that means
    # production models — the header used to promise "a fake unit", which is
    # true only inside the throwaway container. Refuse unless the operator has
    # deliberately pointed the drill somewhere disposable.
    if [ -z "$RH_UNIT_DIR_EXPLICIT" ]; then
        echo "refusing --slot: it arms actuation and stops the unit the preview suggests." >&2
        echo "  Set RH_UNIT_DIR explicitly to a disposable unit dir (the container's, or a" >&2
        echo "  copy) so this can never stop a production model:" >&2
        echo "    RH_UNIT_DIR=/home/roundhouse/.config/systemd/user $0 --slot" >&2
        exit 2
    fi
    RUN_SLOT=1
fi

# ---------------------------------------------------------------- helpers
PIDS_TO_KILL=()
cleanup() {
    for pid in "${PIDS_TO_KILL[@]:-}"; do
        [ -n "$pid" ] && kill "$pid" 2>/dev/null
    done
    wait 2>/dev/null
}
trap cleanup EXIT

# A fake peer: a real listening socket on an unmanaged port, nothing more.
start_fake_peer() {
    python3 -c "
import socket, time
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', $PFAKE))
s.listen(8)
while True:
    try:
        conn, _ = s.accept()
        conn.close()
    except Exception:
        break
" &
    FAKE_PID=$!
    PIDS_TO_KILL+=("$FAKE_PID")
    # Do not proceed until the socket is actually accepting.
    python3 -c "
import socket, sys, time
for _ in range(50):
    try:
        socket.create_connection(('127.0.0.1', $PFAKE), timeout=0.5).close()
        sys.exit(0)
    except OSError:
        time.sleep(0.1)
sys.exit(1)
" || { fail "fake peer did not come up on :$PFAKE"; exit 1; }
}

stop_fake_peer() {
    kill "$FAKE_PID" 2>/dev/null
    wait "$FAKE_PID" 2>/dev/null
    python3 -c "
import socket, sys, time
for _ in range(50):
    try:
        socket.create_connection(('127.0.0.1', $PFAKE), timeout=0.5).close()
        time.sleep(0.1)
    except OSError:
        sys.exit(0)
sys.exit(1)
" || { fail "fake peer socket still accepting after kill"; exit 1; }
}

peers_json() {  # peers_json <host> -> the raw /api/peers body
    python3 -c "
import json, sys, urllib.request
try:
    with urllib.request.urlopen('http://$1:$RH_PORT/api/peers', timeout=3) as r:
        sys.stdout.write(r.read().decode())
except Exception as e:
    sys.stdout.write(json.dumps({'error': str(e)}))
"
}

# Wait until <name> reaches <state> with consecutive_failures <cf> (or any cf if '-'),
# printing the body whenever it changes. Fails if the deadline passes.
wait_peer() {  # wait_peer <name> <state> <cf|-> <timeout-seconds> <why>
    local name="$1" want_state="$2" want_cf="$3" timeout="$4" why="$5"
    local out
    out=$(python3 - "$name" "$want_state" "$want_cf" "$timeout" <<PYEOF
import json, sys, time, urllib.request
name, want_state, want_cf, timeout = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
deadline, last = time.time() + timeout, None
while time.time() < deadline:
    try:
        with urllib.request.urlopen('http://127.0.0.1:$RH_PORT/api/peers', timeout=3) as r:
            body = json.load(r)
    except Exception as e:
        time.sleep(0.25); continue
    row = next((p for p in body['peers'] if p['name'] == name), None)
    snap = json.dumps(row, sort_keys=True)
    if snap != last:
        print('        ' + snap)
        last = snap
    if row and row['state'] == want_state and (want_cf == '-' or row['consecutive_failures'] == int(want_cf)):
        sys.exit(0)
    time.sleep(0.25)
print('        TIMEOUT waiting for %s -> %s (cf=%s)' % (name, want_state, want_cf))
sys.exit(1)
PYEOF
)
    local rc=$?
    echo "$out"
    if [ $rc -eq 0 ]; then pass "$why"; else fail "$why"; fi
    return $rc
}

# Assert a peer STAYS in a state for the whole window (the hysteresis proof: `up` must
# survive the first failure, so we watch a full probe interval without it flipping).
hold_peer() {  # hold_peer <name> <state> <seconds> <why>
    local name="$1" want="$2" secs="$3" why="$4"
    if python3 - "$name" "$want" "$secs" <<PYEOF
import json, sys, time, urllib.request
name, want, secs = sys.argv[1], sys.argv[2], float(sys.argv[3])
deadline = time.time() + secs
while time.time() < deadline:
    with urllib.request.urlopen('http://127.0.0.1:$RH_PORT/api/peers', timeout=3) as r:
        row = next(p for p in json.load(r)['peers'] if p['name'] == name)
    if row['state'] != want:
        print('        BROKE EARLY: %s' % json.dumps(row, sort_keys=True))
        sys.exit(1)
    time.sleep(0.25)
print('        held: %s' % json.dumps(row, sort_keys=True))
sys.exit(0)
PYEOF
    then pass "$why"; else fail "$why"; fi
}

wait_http() {  # wait_http <host> <path> <timeout>
    python3 -c "
import sys, time, urllib.request
deadline = time.time() + float('$3')
while time.time() < deadline:
    try:
        urllib.request.urlopen('http://$1:$RH_PORT$2', timeout=2).read()
        sys.exit(0)
    except Exception:
        time.sleep(0.2)
sys.exit(1)
"
}

# ---------------------------------------------------------------- environment
step "Environment"
SECOND_ADDR=$(python3 -c "
import socket
try:
    s = socket.socket(socket.AF_INET6); s.bind(('::1', 0)); s.close(); print('::1')
except OSError:
    import subprocess
    print(subprocess.check_output(['hostname', '-I']).split()[0].decode())
")
note "roundhouse:   $RH"
note "unit dir:     $RH_UNIT_DIR"
note "instance:     127.0.0.1 + $SECOND_ADDR on :$RH_PORT (cadence ${INTERVAL}s)"
note "fake peer:    127.0.0.1:$PFAKE   absent peer: 127.0.0.1:$PGONE"
note "logs:         $LOG_DIR"

BIND_ARG="127.0.0.1,$SECOND_ADDR"
if [ "$SECOND_ADDR" = "::1" ]; then DOOR_B="[::1]"; else DOOR_B="$SECOND_ADDR"; fi

# ---------------------------------------------------------------- 1. both doors
step "1. Two peers, two doors: start the instance"
start_fake_peer
note "fake peer listening on 127.0.0.1:$PFAKE (pid $FAKE_PID)"

python3 "$RH" --serve --unit-dir "$RH_UNIT_DIR" --no-db \
    --port "$RH_PORT" --bind "$BIND_ARG" \
    --peer good=127.0.0.1:$PFAKE \
    --peer gone=127.0.0.1:$PGONE \
    --peer-interval "$INTERVAL" \
    > "$LOG_DIR/rh.out" 2> "$LOG_DIR/rh.err" &
RH_PID=$!
PIDS_TO_KILL+=("$RH_PID")

if wait_http 127.0.0.1 /api/units 20; then pass "instance answers on door A (127.0.0.1)"
else fail "instance never came up"; cat "$LOG_DIR/rh.err"; exit 1; fi

if wait_http "$DOOR_B" /api/units 10; then pass "instance answers on door B ($DOOR_B)"
else fail "second listener does not answer"; fi

assert "both listening lines printed, none before the binds succeeded" \
    test "$(grep -c 'listening on http' "$LOG_DIR/rh.out")" = 2
note "$(tr '\n' '|' < "$LOG_DIR/rh.out")"

# Shared state: the same watcher behind both doors.
if python3 -c "
import json, sys, urllib.request
def units(host):
    with urllib.request.urlopen('http://%s:$RH_PORT/api/units' % host, timeout=3) as r:
        d = json.load(r)
    return d['host'], sorted(u['unit'] for u in d['units']), [p['name'] for p in d['peers']]
a, b = units('127.0.0.1'), units('$DOOR_B')
print('        door A: host=%s units=%d peers=%s' % (a[0], len(a[1]), a[2]))
print('        door B: host=%s units=%d peers=%s' % (b[0], len(b[1]), b[2]))
sys.exit(0 if a == b else 1)
"; then pass "both doors serve one shared state (same host, units, peers)"
else fail "the two listeners disagree — state is not shared"; fi

# ---------------------------------------------------------------- 2. SSE capture
step "2. Start the SSE capture before the flap"
python3 -c "
import json, sys, urllib.request
ev = None
with urllib.request.urlopen('http://127.0.0.1:$RH_PORT/api/events', timeout=None) as r:
    for raw in r:
        line = raw.decode().strip()
        if line.startswith('event: '):
            ev = line[7:]
        elif line.startswith('data: ') and ev == 'snapshot':
            d = json.loads(line[6:])
            print('SNAPSHOT peers=%s' % json.dumps(
                [(p['name'], p['state']) for p in d.get('peers', [])]), flush=True)
        elif line.startswith('data: ') and ev == 'peer':
            d = json.loads(line[6:])
            print('%s %s->%s cf=%s' % (d['name'], d['prev_state'], d['state'], d['consecutive_failures']), flush=True)
" > "$LOG_DIR/sse.log" 2>/dev/null &
SSE_PID=$!
PIDS_TO_KILL+=("$SSE_PID")
sleep 0.5
note "SSE reader pid $SSE_PID -> $LOG_DIR/sse.log"

# ---------------------------------------------------------------- 3. first states
step "3. First round: one peer reachable, one not"
wait_peer good up 0 "$((INTERVAL * 3))" "reachable peer is 'up' on the FIRST success"
wait_peer gone down - "$((INTERVAL * 4))" "absent peer reaches 'down' (never before its 2nd failure)"
note "full /api/peers body:"
peers_json 127.0.0.1 | python3 -m json.tool | sed 's/^/        /'

# ---------------------------------------------------------------- 4. hysteresis
step "4. Kill the reachable listener: down only on the SECOND failure"
stop_fake_peer
note "fake peer killed at $(date +%T)"

wait_peer good up 1 "$((INTERVAL * 2))" "after ONE failure the peer is STILL 'up' (cf=1)"
hold_peer good up 2 "and it stays 'up' while cf=1 (a dropped packet is not an outage)"
wait_peer good down 2 "$((INTERVAL * 2))" "on the SECOND consecutive failure it goes 'down' (cf=2)"
note "full /api/peers body:"
peers_json 127.0.0.1 | python3 -m json.tool | sed 's/^/        /'

# ---------------------------------------------------------------- 5. restore
step "5. Restore the listener: up on the FIRST success"
start_fake_peer
note "fake peer restarted (pid $FAKE_PID)"
wait_peer good up 0 "$((INTERVAL * 2))" "back to 'up' on the FIRST success, no second-chance delay"
note "full /api/peers body:"
peers_json 127.0.0.1 | python3 -m json.tool | sed 's/^/        /'

# ---------------------------------------------------------------- 6. SSE ledger
step "6. The SSE ledger: exactly the transitions earned, and no others"
sleep "$INTERVAL"
kill "$SSE_PID" 2>/dev/null
sleep 0.3
note "captured events:"
sed 's/^/        /' "$LOG_DIR/sse.log"

assert "the stream opened with a snapshot carrying current peer state (no replay needed)" \
    grep -q '^SNAPSHOT peers=' "$LOG_DIR/sse.log"

# The reader is a mid-life client: `good unknown->up` fires in the round that runs at
# startup+0, before any HTTP client can have subscribed. That is the designed
# behavior — the initial snapshot carries the state, transitions are deltas on top —
# so the head event is optional and everything after it is not.
ACTUAL=$(grep -v '^SNAPSHOT ' "$LOG_DIR/sse.log")
HEAD='good unknown->up cf=0'
TAIL=$'gone unknown->down cf=2\ngood up->down cf=2\ngood down->up cf=0'
if [ "$ACTUAL" = "$TAIL" ]; then
    pass "exactly the 3 transitions earned after subscription, in order, and none between"
    note "the unknown->up transition predates the subscriber (startup+0 round) and is"
    note "carried by the initial snapshot instead — that is the transition-only contract"
elif [ "$ACTUAL" = "$HEAD"$'\n'"$TAIL" ]; then
    pass "exactly the 4 transitions earned, in order, and none while a state was unchanged"
else
    fail "SSE ledger mismatch"
    note "expected: [$HEAD |] $(echo "$TAIL" | tr '\n' '|')"
    note "actual:   $(echo "$ACTUAL" | tr '\n' '|')"
fi

# ---------------------------------------------------------------- 7. the envelope
step "7. The frozen envelope and its agreement with the snapshot"
if python3 -c "
import json, sys, urllib.request
with urllib.request.urlopen('http://127.0.0.1:$RH_PORT/api/peers', timeout=3) as r:
    peers = json.load(r)
with urllib.request.urlopen('http://127.0.0.1:$RH_PORT/api/units', timeout=3) as r:
    snap = json.load(r)
means = 'reachable, not healthy: a TCP connect proves something is listening on that port and nothing more'
ok = True
if set(peers) != {'peers', 'probe', 'means'}:
    print('        envelope keys: %s' % sorted(peers)); ok = False
if peers['means'] != means:
    print('        means string drifted: %r' % peers['means']); ok = False
if peers['probe'] != {'method': 'tcp-connect', 'timeout_seconds': 2.0, 'cadence_seconds': $INTERVAL}:
    print('        probe block: %s' % peers['probe']); ok = False
if [dict(p) for p in snap['peers']] != peers['peers']:
    print('        snapshot rows differ from /api/peers rows'); ok = False
print('        probe: %s' % json.dumps(peers['probe']))
print('        means: %s' % peers['means'])
sys.exit(0 if ok else 1)
"; then pass "/api/peers envelope frozen; snapshot 'peers' agrees field-for-field"
else fail "envelope or snapshot disagreement"; fi

assert "POST /api/peers is 405 (read route)" test "$(python3 -c "
import urllib.request, urllib.error
req = urllib.request.Request('http://127.0.0.1:$RH_PORT/api/peers', method='POST', data=b'{}')
try:
    urllib.request.urlopen(req, timeout=3); print(200)
except urllib.error.HTTPError as e:
    print(e.code)
")" = 405

assert "GET /api/peers needs no token (read routes are unauthenticated)" test "$(python3 -c "
import urllib.request
req = urllib.request.Request('http://127.0.0.1:$RH_PORT/api/peers')
print(urllib.request.urlopen(req, timeout=3).status)
")" = 200

# ---------------------------------------------------------------- 8. MCP leg
step "8. MCP tool #17 through the real stdio subprocess"
MCP_OUT=$(python3 - <<PYEOF
import json, subprocess, sys
proc = subprocess.Popen([sys.executable, '$MVP1/roundhouse_mcp.py',
                         '--url', 'http://127.0.0.1:$RH_PORT'],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
def send(msg):
    proc.stdin.write(json.dumps(msg) + '\n'); proc.stdin.flush()
    return json.loads(proc.stdout.readline())
send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
      'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                 'clientInfo': {'name': 'peer-drill', 'version': '1'}}})
tools = send({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}})
call = send({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
             'params': {'name': 'peer_status', 'arguments': {}}})
proc.stdin.close(); proc.terminate()
result = json.loads(call['result']['content'][0]['text'])
print(len(tools['result']['tools']))
print(json.dumps(result, sort_keys=True))
PYEOF
)
MCP_COUNT=$(echo "$MCP_OUT" | head -1)
MCP_BODY=$(echo "$MCP_OUT" | tail -1)
assert "MCP catalog is 17 tools" test "$MCP_COUNT" = 17
note "peer_status -> $MCP_BODY"
if echo "$MCP_BODY" | python3 -c "
import json, sys
d = json.load(sys.stdin)
sys.exit(0 if d['http_status'] == 200 and [p['name'] for p in d['peers']] == ['gone', 'good']
         and 'not healthy' in d['means'] else 1)
"; then pass "peer_status passthrough carries the whole envelope + http_status"
else fail "peer_status passthrough mismatch"; fi

step "9. Stop the instance"
kill "$RH_PID" 2>/dev/null
wait "$RH_PID" 2>/dev/null
assert "no listener left behind on door A" test "$(python3 -c "
import socket
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(('127.0.0.1', $RH_PORT)); print('free')
except OSError:
    print('held')
finally:
    s.close()
")" = free

# ---------------------------------------------------------------- 10. D2 refusal
step "10. D2: a peer that names THIS host on a managed unit's port is refused"
D2_OUT=$(python3 "$RH" --serve --unit-dir "$RH_UNIT_DIR" --no-db --port "$RH_PORT" \
    --bind 127.0.0.1 --peer self=127.0.0.1:8085 2>"$LOG_DIR/d2.err")
D2_RC=$?
note "stderr: $(cat "$LOG_DIR/d2.err")"
assert "startup refused (exit 1)" test "$D2_RC" = 1
assert "the error names the collision and the unit that owns the port" \
    grep -q "peer 'self' targets 127.0.0.1:8085" "$LOG_DIR/d2.err"
assert "no 'listening' line was printed before the refusal" \
    test -z "$(echo "$D2_OUT" | grep -i listening)"

step "11. The same port on ANOTHER host is legal (the rule is host-AND-port)"
python3 "$RH" --serve --unit-dir "$RH_UNIT_DIR" --no-db --port "$RH_PORT" \
    --bind 127.0.0.1 --peer ampere=ampere.fritz.box:8085 --peer-interval 3600 \
    > "$LOG_DIR/legit.out" 2> "$LOG_DIR/legit.err" &
LEGIT_PID=$!
PIDS_TO_KILL+=("$LEGIT_PID")
if wait_http 127.0.0.1 /api/peers 20; then
    pass "ampere=ampere.fritz.box:8085 starts — qwen3.6-coding serves :8085 on ampere too"
    note "$(peers_json 127.0.0.1)"
else
    fail "a legitimate same-port peer was refused"
    note "$(cat "$LOG_DIR/legit.err")"
fi
kill "$LEGIT_PID" 2>/dev/null; wait "$LEGIT_PID" 2>/dev/null

# ---------------------------------------------------------------- 12. bind failure
step "12. Bind failure is all-or-nothing and does not lie first"
python3 -c "
import socket, time
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', $RH_PORT)); s.listen(1)
time.sleep(60)
" &
BLOCKER_PID=$!
PIDS_TO_KILL+=("$BLOCKER_PID")
sleep 1

BIND_OUT=$(python3 "$RH" --serve --unit-dir "$RH_UNIT_DIR" --no-db --port "$RH_PORT" \
    --bind "$SECOND_ADDR,127.0.0.1" 2>"$LOG_DIR/bind.err")
BIND_RC=$?
note "stderr: $(cat "$LOG_DIR/bind.err")"
assert "startup refused (exit 1)" test "$BIND_RC" = 1
assert "the failing address is named with its errno" \
    grep -q "cannot bind 127.0.0.1:$RH_PORT: \[Errno" "$LOG_DIR/bind.err"
assert "no 'listening' line for the address that DID bind" \
    test -z "$(echo "$BIND_OUT" | grep -i listening)"
assert "nothing left listening on the address that DID bind" test "$(python3 -c "
import socket
fam = socket.AF_INET6 if '$SECOND_ADDR' == '::1' else socket.AF_INET
s = socket.socket(fam); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
if fam == socket.AF_INET6:
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
try:
    s.bind(('$SECOND_ADDR', $RH_PORT)); print('free')
except OSError:
    print('held')
finally:
    s.close()
")" = free
kill "$BLOCKER_PID" 2>/dev/null; wait "$BLOCKER_PID" 2>/dev/null

# ---------------------------------------------------------------- 13. slot leg
if [ "$RUN_SLOT" = "1" ]; then
    step "13. Shared operation slot across listeners (ARMED — actuates a fake unit)"
    # A COPY of the measurement db, not --no-db and not the live file: switch
    # preflight fits against measured peaks, so a memory-less instance refuses
    # every target with preflight_failed and the leg would silently skip.
    SLOT_DB="$LOG_DIR/slot.sqlite"
    LIVE_DB="${XDG_STATE_HOME:-$HOME/.local/state}/roundhouse/roundhouse.sqlite"
    [ -f "$LIVE_DB" ] && cp "$LIVE_DB" "$SLOT_DB"
    python3 "$RH" --serve --unit-dir "$RH_UNIT_DIR" --db "$SLOT_DB" --actuate \
        --port "$RH_PORT" --bind "$BIND_ARG" \
        > "$LOG_DIR/armed.out" 2> "$LOG_DIR/armed.err" &
    ARMED_PID=$!
    PIDS_TO_KILL+=("$ARMED_PID")
    if wait_http 127.0.0.1 /api/units 20; then
        TOKEN=$(cat "$HOME/.config/roundhouse/token" 2>/dev/null)
        SLOT_RESULT=$(python3 - "$TOKEN" "$DOOR_B" <<PYEOF
import json, sys, time, urllib.request, urllib.error
token, door_b = sys.argv[1], sys.argv[2]
A, B = 'http://127.0.0.1:$RH_PORT', 'http://%s:$RH_PORT' % sys.argv[2]

def call(base, path, body=None, method='GET'):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header('Content-Type', 'application/json')
    if token:
        req.add_header('Authorization', 'Bearer ' + token)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b'{}')

# Pick an OFF unit to switch to: the least invasive operation the slot understands.
_s, snap = call(A, '/api/units')
target = next((u['unit'] for u in snap['units']
               if u['rung'] in ('OFF', 'STANDBY') and not u['retired']), None)
if target is None:
    print('SKIP no OFF unit to switch to'); sys.exit(0)

status, prev = call(A, '/api/switch/preview', {'target': target, 'stops': []}, 'POST')
if status != 200 or 'confirm' not in prev:
    print('SKIP preview refused: %s %s' % (status, json.dumps(prev)[:200])); sys.exit(0)
print('preview ok: target=%s suggested_stops=%s' % (target, prev.get('suggested_stops')))

status, started = call(A, '/api/switch',
                       {'target': target, 'stops': prev.get('suggested_stops', []),
                        'confirm': prev['confirm']}, 'POST')
print('door A switch -> %s %s' % (status, json.dumps(started)[:160]))
if status != 202:
    print('SKIP switch not accepted'); sys.exit(0)

# The slot is now claimed. The SECOND door must refuse with 409, and must be able
# to SEE the operation that was started through the first one.
status_b, refused = call(B, '/api/switch',
                         {'target': target, 'stops': [], 'confirm': 'whatever'}, 'POST')
print('door B switch -> %s %s' % (status_b, json.dumps(refused)[:160]))
rid = started.get('rollout_id')
status_v, visible = call(B, '/api/rollouts/' + rid)
print('door B sees operation %s -> %s %s' % (rid, status_v, visible.get('kind')))

ok = (status_b == 409 and refused.get('error') == 'operation_in_progress'
      and status_v == 200 and visible.get('rollout_id') == rid)

for _ in range(60):
    _s, rec = call(A, '/api/rollouts/' + rid)
    if rec.get('phase') in ('done', 'failed', 'rolled_back'):
        break
    time.sleep(1)
print('operation finished in phase %s' % rec.get('phase'))
print('RESULT ' + ('PASS' if ok else 'FAIL'))
print('TARGET ' + target)
PYEOF
)
        echo "$SLOT_RESULT" | sed 's/^/        /'
        if echo "$SLOT_RESULT" | grep -q '^RESULT PASS'; then
            pass "the operation slot is shared: door B answers 409 operation_in_progress and sees the operation"
        elif echo "$SLOT_RESULT" | grep -q '^SKIP'; then
            note "slot leg skipped (see above)"
        else
            fail "shared-slot proof failed"
        fi
        SWITCHED=$(echo "$SLOT_RESULT" | sed -n 's/^TARGET //p')
        if [ -n "$SWITCHED" ]; then
            note "leaving the fleet as found: stopping $SWITCHED"
            systemctl --user stop "$SWITCHED" 2>/dev/null || true
        fi
    else
        fail "armed instance never came up"
    fi
    kill "$ARMED_PID" 2>/dev/null; wait "$ARMED_PID" 2>/dev/null
fi

# ---------------------------------------------------------------- summary
step "Summary"
echo "  PASS: $PASS"
echo "  FAIL: $FAIL"
echo "  logs: $LOG_DIR"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
