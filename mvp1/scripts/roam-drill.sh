#!/bin/bash
# roam-drill.sh — MVP9 container drill: roaming, both sides, with a REAL address.
#
# The milestone's claim is that an address appearing and disappearing is a thing
# Roundhouse notices. Mocks cannot prove that, so this drill adds and removes an
# address on the running kernel (`ip addr add|del 192.0.2.9/32 dev lo`) and watches
# two instances react:
#
#   instance B — the roamer:    --bind 127.0.0.1 --bind-optional 192.0.2.9
#   instance A — the federator: --fleet-peer bee=http://192.0.2.9:PB,http://127.0.0.1:PB
#
# The scratch address is TEST-NET-1 (RFC 5737) and NOT a loopback alias: all of
# 127.0.0.0/8 binds with no configuration at all, so a 127.x address can never be
# absent and could never prove appearance. 192.0.2.9 is unassigned until this script
# assigns it, which is the whole point.
#
#   ./roam-drill.sh          all container legs (needs root for `ip addr`)
#   ./roam-drill.sh --live   print the live ampere/boltzmann checklist and exit
#
# Env: RH_PORT_A / RH_PORT_B (defaults 8391/8392 — deliberately not 8090/8099, so a
# running roundhouse.service is never disturbed).
#
# Nothing here actuates a unit: both instances run unarmed (no --actuate, no token).
# Exit 0 on PASS, 1 on any FAIL, 2 if not root. stdlib python3 only; no jq, no nc.

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MVP1="$(dirname "$HERE")"
REPO="$(dirname "$MVP1")"
RH="$MVP1/roundhouse.py"
FIXTURES="$REPO/docs/fixtures"

SCRATCH_ADDR="192.0.2.9"
PA="${RH_PORT_A:-8391}"          # instance A — the federator
PB="${RH_PORT_B:-8392}"          # instance B — the roamer
INTERVAL=5                       # --peer-interval on A
RETRY=2                          # --bind-retry on B

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
=== MVP9 live leg — ampere (the laptop) and boltzmann (the fixed host) ===

Read-only on both sides; nothing is armed and no packaged unit is restarted. Run a
THROWAWAY instance beside the enabled one on each host.

ampere, at home (the sofa — wlP2p33s0 up, no VPN interface):

  cd ~/roundhouse-live/mvp1
  python3 roundhouse.py --serve --no-db --port 8098 \
      --bind 127.0.0.1 \
      --bind-optional 192.168.88.168 --bind-optional ampere.vpn \
      --bind-retry 30

  curl -s http://127.0.0.1:8098/api/units | python3 -m json.tool | grep -A8 listeners

Check:
  * it STARTS — with ampere.vpn absent. That is the milestone: the same ExecStart
    boots on the sofa and at the desk.
  * listeners: 127.0.0.1 mandatory/bound, 192.168.88.168 optional/bound,
    ampere.vpn optional/absent with resolved []
  * http://192.168.88.168:8098/api/units answers from another host on the LAN

boltzmann, federating ampere as ONE peer with TWO candidates:

  cd ~/roundhouse-live/mvp1
  python3 roundhouse.py --serve --no-db --port 8097 --bind 127.0.0.1 \
      --fleet-peer ampere=http://ampere.fritz.box:8098,http://[fd96:cafe:cafe::1000]:8098 \
      --peer-interval 20

  curl -s http://127.0.0.1:8097/api/fleet
  curl -s http://127.0.0.1:8097/api/routing-config/fleet

Check:
  * ampere is `up` with endpoint_in_use = http://ampere.fritz.box:8098 (candidate 1)
  * /api/fleet carries both hosts' units, each tagged with its source
  * /api/routing-config/fleet merges both hosts with no conflicts (host namespacing)
  * stop ampere's instance: BOTH candidates are tried each round, and the peer goes
    down after two whole-walk failures — that is the away-from-home simulation
  * restart it: up again on the next round, via candidate 1

At the desk (the other half, operator-run later): ampere.vpn is bound and
192.168.88.168 is absent — the listeners block is the mirror image — and boltzmann
reaches ampere through candidate 2 with the peer never leaving `up` if the move
happens between rounds.

Afterwards: kill both throwaway instances, and confirm the enabled roundhouse.service
on each host is still serving and the llama-server PIDs are untouched.
LIVE
    exit 0
fi

# ---------------------------------------------------------------- root gate
if [ "$(id -u)" != 0 ]; then
    echo "roam-drill needs root for ip addr add/del"
    exit 2
fi

if ! command -v ip >/dev/null 2>&1; then
    echo "roam-drill needs iproute2 (ip addr add/del)"
    exit 2
fi

# ---------------------------------------------------------------- cleanup
SCRATCH="$(mktemp -d /tmp/roam-drill.XXXXXX)"
LOG_DIR="$SCRATCH/logs"; mkdir -p "$LOG_DIR"
HOME_A="$SCRATCH/home"; mkdir -p "$HOME_A"
DIR_A="$SCRATCH/units-a"; mkdir -p "$DIR_A"
DIR_B="$SCRATCH/units-b"; mkdir -p "$DIR_B"

PIDS_TO_KILL=()
cleanup() {
    for pid in "${PIDS_TO_KILL[@]:-}"; do
        [ -n "$pid" ] && kill "$pid" 2>/dev/null
    done
    ip addr del "$SCRATCH_ADDR/32" dev lo 2>/dev/null || true
    wait 2>/dev/null
}
trap cleanup EXIT

addr_add() { ip addr add "$SCRATCH_ADDR/32" dev lo 2>/dev/null || true; }
addr_del() { ip addr del "$SCRATCH_ADDR/32" dev lo 2>/dev/null || true; }

# ---------------------------------------------------------------- helpers
get() {  # get <host> <port> <path>  -> body on stdout (empty on error)
    python3 -c "
import sys, urllib.request
host = '$1'
host = '[%s]' % host if ':' in host else host
try:
    with urllib.request.urlopen('http://%s:$2$3' % host, timeout=5) as r:
        sys.stdout.write(r.read().decode())
except Exception as e:
    print('', end='')
    print('        fetch failed: %s' % e, file=sys.stderr)
"
}

wait_http() {  # wait_http <host> <port> <path> <timeout>
    python3 -c "
import sys, time, urllib.request
deadline = time.time() + float('$4')
while time.time() < deadline:
    try:
        urllib.request.urlopen('http://$1:$2$3', timeout=2).read()
        sys.exit(0)
    except Exception:
        time.sleep(0.2)
sys.exit(1)
"
}

# True when NOTHING answers on host:port — the disappearance half of the story.
refuses() {  # refuses <host> <port>
    python3 -c "
import socket, sys
s = socket.socket(); s.settimeout(1.5)
try:
    s.connect(('$1', $2)); s.close(); sys.exit(1)
except OSError:
    sys.exit(0)
"
}

# Wait until something accepts on host:port — a TCP connect only, because the scratch
# listener in leg 3 accepts and never speaks HTTP.
wait_listening() {  # wait_listening <host> <port> <timeout>
    local deadline=$((SECONDS + $3))
    while [ "$SECONDS" -lt "$deadline" ]; do
        if ! refuses "$1" "$2"; then return 0; fi
        sleep 0.5
    done
    return 1
}

# Wait until nothing answers on host:port. A graceful shutdown takes a moment, and
# whoever wants that address next would otherwise lose an intermittent race to it.
wait_free() {  # wait_free <host> <port> <timeout>
    local deadline=$((SECONDS + $3))
    while [ "$SECONDS" -lt "$deadline" ]; do
        if refuses "$1" "$2"; then return 0; fi
        sleep 0.5
    done
    return 1
}

require_free_port() {  # require_free_port <port>
    python3 -c "
import socket, sys
s = socket.socket()
try:
    s.connect(('127.0.0.1', $1))
    print('        port $1 is already in use — refusing to test a stranger', file=sys.stderr)
    sys.exit(1)
except OSError:
    sys.exit(0)
finally:
    s.close()
"
}

# Poll the snapshot until listener row <addr> reaches <state>, printing the row every
# time it changes: that printout IS the transition timeline this drill exists to show.
wait_listener() {  # wait_listener <port> <addr> <state> <timeout> <why>
    local port="$1" addr="$2" want="$3" timeout="$4" why="$5" out rc
    out=$(python3 - "$port" "$addr" "$want" "$timeout" <<'PYEOF'
import json, sys, time, urllib.request
port, addr, want, timeout = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
deadline, last, t0 = time.time() + timeout, None, time.time()
while time.time() < deadline:
    try:
        with urllib.request.urlopen('http://127.0.0.1:%s/api/units' % port, timeout=3) as r:
            doc = json.load(r)
    except Exception:
        time.sleep(0.25); continue
    row = next((l for l in doc.get('listeners', []) if l['addr'] == addr), None)
    snap = json.dumps(row, sort_keys=True)
    if snap != last:
        print('        +%5.1fs  %s' % (time.time() - t0, snap))
        last = snap
    if row and row['state'] == want:
        sys.exit(0)
    time.sleep(0.25)
print('        TIMEOUT waiting for listener %s -> %s' % (addr, want))
sys.exit(1)
PYEOF
)
    rc=$?
    echo "$out"
    if [ $rc -eq 0 ]; then pass "$why"; else fail "$why"; fi
    return $rc
}

# Poll /api/peers until <name>'s endpoint_in_use is <url>, printing every change and
# refusing to pass if the peer's own state left `up` on the way (the no-flap rule).
wait_endpoint() {  # wait_endpoint <port> <name> <url> <timeout> <why>
    local port="$1" name="$2" url="$3" timeout="$4" why="$5" out rc
    out=$(python3 - "$port" "$name" "$url" "$timeout" <<'PYEOF'
import json, sys, time, urllib.request
port, name, want, timeout = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
deadline, last, t0 = time.time() + timeout, None, time.time()
states = []
while time.time() < deadline:
    try:
        with urllib.request.urlopen('http://127.0.0.1:%s/api/peers' % port, timeout=3) as r:
            doc = json.load(r)
    except Exception:
        time.sleep(0.25); continue
    row = next((p for p in doc['peers'] if p['name'] == name), None)
    if row is not None and (not states or states[-1] != row['state']):
        states.append(row['state'])
    keep = {k: row.get(k) for k in ('state', 'endpoint_in_use', 'endpoint_changed_at',
                                    'consecutive_failures', 'host')} if row else None
    snap = json.dumps(keep, sort_keys=True)
    if snap != last:
        print('        +%5.1fs  %s' % (time.time() - t0, snap))
        last = snap
    if row and row['endpoint_in_use'] == want:
        print('        states seen: %s' % ' -> '.join(states))
        sys.exit(0 if states[-1] == 'up' else 1)
    time.sleep(0.25)
print('        TIMEOUT waiting for %s endpoint -> %s (states %s)' % (name, want, states))
sys.exit(1)
PYEOF
)
    rc=$?
    echo "$out"
    if [ $rc -eq 0 ]; then pass "$why"; else fail "$why"; fi
    return $rc
}

# A fixture unit, so each instance has something to federate.
make_unit() {  # make_unit <dir> <fixture> <unit-name> <alias> <port>
    python3 - "$1" "$FIXTURES/$2" "$3" "$4" "$5" <<'PYEOF'
import re, sys, pathlib
out_dir, fixture, name, alias, port = sys.argv[1:6]
raw = pathlib.Path(fixture).read_text()
raw = re.sub(r'--alias\s+\S+', '--alias ' + alias, raw, count=1)
raw = re.sub(r'--port\s+\d+', '--port ' + port, raw, count=1)
if '# roundhouse: on-demand' not in raw:
    raw = raw.replace('[Unit]', '# roundhouse: on-demand\n[Unit]', 1)
pathlib.Path(out_dir, name).write_text(raw)
PYEOF
}

# Sets RH_PID. NOT a command substitution: $(...) runs in a subshell and the
# PIDS_TO_KILL append would be lost, leaving instances behind for the next run.
start_instance() {  # start_instance <port> <unit-dir> <log> [extra args...]
    local port="$1" dir="$2" log="$3"; shift 3
    HOME="$HOME_A" python3 "$RH" --serve --unit-dir "$dir" --no-db \
        --port "$port" "$@" \
        > "$LOG_DIR/$log.out" 2> "$LOG_DIR/$log.err" &
    RH_PID=$!
    PIDS_TO_KILL+=("$RH_PID")
}

# Capture the SSE stream in the background; every event lands as one line so the
# assertions can count them. Sets SSE_PID.
sse_capture() {  # sse_capture <port> <outfile>
    python3 - "$1" "$2" <<'PYEOF' &
import json, sys, time, urllib.request
port, out = sys.argv[1], sys.argv[2]
sink = open(out, 'w', buffering=1)
try:
    stream = urllib.request.urlopen('http://127.0.0.1:%s/api/events' % port, timeout=300)
except Exception as e:
    sink.write('OPEN-FAILED\t%s\n' % e); sys.exit(1)
name = None
for raw in stream:
    line = raw.decode('utf-8', 'replace').rstrip('\n')
    if line.startswith('event: '):
        name = line[7:]
    elif line.startswith('data: ') and name:
        sink.write('%.3f\t%s\t%s\n' % (time.time(), name, line[6:]))
        name = None
PYEOF
    SSE_PID=$!
    PIDS_TO_KILL+=("$SSE_PID")
}

# ---------------------------------------------------------------- environment
step "Environment"
note "roundhouse:  $RH"
note "instance A:  127.0.0.1:$PA  units $DIR_A   (the federator)"
note "instance B:  127.0.0.1:$PB  units $DIR_B   (the roamer)"
note "scratch addr: $SCRATCH_ADDR/32 on lo  (TEST-NET-1, unassigned until we assign it)"
note "scratch dir: $SCRATCH"

make_unit "$DIR_A" llama-server-gemma4.service alpha-one.service alpha-one 8181
make_unit "$DIR_B" llama-server-gemma4.service bravo-one.service bravo-one 8182

assert "port $PA is free" require_free_port "$PA"
assert "port $PB is free" require_free_port "$PB"

addr_del   # start from a known world: the address is NOT here
assert "$SCRATCH_ADDR is absent to begin with" refuses "$SCRATCH_ADDR" "$PB"

# ---------------------------------------------------------------- 1. refusals
step "1. Configuration refusals (parse level, before anything binds)"

wildcard_out="$(HOME="$HOME_A" python3 "$RH" --serve --unit-dir "$DIR_B" --no-db \
    --port "$PB" --bind 127.0.0.1 --bind-optional 0.0.0.0 2>&1)"
wildcard_rc=$?
note "$wildcard_out"
assert "a wildcard optional bind exits non-zero" test "$wildcard_rc" -ne 0
assert "and says a wildcard cannot be absent" \
    grep -q "a wildcard cannot be absent" <<<"$wildcard_out"

both_out="$(HOME="$HOME_A" python3 "$RH" --serve --unit-dir "$DIR_B" --no-db \
    --port "$PB" --bind 127.0.0.1 --bind-optional 127.0.0.1 2>&1)"
both_rc=$?
note "$both_out"
assert "an address in both lists exits non-zero" test "$both_rc" -ne 0
assert "and says mandatory or optional, not both" \
    grep -q "mandatory or optional, not both" <<<"$both_out"

# ---------------------------------------------------------------- 2. appear/disappear
step "2. Appearance and disappearance, with a real address"

start_instance "$PB" "$DIR_B" "b-roamer" --bind 127.0.0.1 \
    --bind-optional "$SCRATCH_ADDR" --bind-retry "$RETRY"
B_PID="$RH_PID"
assert "instance B is serving on loopback" wait_http 127.0.0.1 "$PB" /api/units 20

SSE_B="$LOG_DIR/sse-b.txt"
sse_capture "$PB" "$SSE_B"
sleep 1

wait_listener "$PB" "$SCRATCH_ADDR" absent 10 "the optional row reads absent while the address is not here"
assert "nothing answers on $SCRATCH_ADDR:$PB" refuses "$SCRATCH_ADDR" "$PB"

note "ip addr add $SCRATCH_ADDR/32 dev lo"
addr_add
wait_listener "$PB" "$SCRATCH_ADDR" bound $((RETRY * 4 + 6)) "the address appears -> the row goes bound"
assert "and $SCRATCH_ADDR:$PB now serves /api/units" wait_http "$SCRATCH_ADDR" "$PB" /api/units 10

note "ip addr del $SCRATCH_ADDR/32 dev lo"
addr_del
wait_listener "$PB" "$SCRATCH_ADDR" absent $((RETRY * 4 + 6)) "the address goes -> the listener is closed"
assert "nothing answers on $SCRATCH_ADDR:$PB any more" refuses "$SCRATCH_ADDR" "$PB"
assert "and loopback keeps serving throughout" wait_http 127.0.0.1 "$PB" /api/units 5

note "ip addr add $SCRATCH_ADDR/32 dev lo   (again)"
addr_add
wait_listener "$PB" "$SCRATCH_ADDR" bound $((RETRY * 4 + 6)) "the address returns -> the row goes bound again"
assert "and $SCRATCH_ADDR:$PB serves again" wait_http "$SCRATCH_ADDR" "$PB" /api/units 10

# The SSE ledger: exactly the three earned transitions for this address, and nothing
# published while the world was flat. Announce lines are the operator's copy of the
# same story, so they are printed too.
sleep $((RETRY + 2))
python3 - "$SSE_B" "$SCRATCH_ADDR" <<'PYEOF'
import json, sys
path, addr = sys.argv[1], sys.argv[2]
rows = []
for line in open(path):
    parts = line.rstrip('\n').split('\t')
    if len(parts) == 3 and parts[1] == 'listener':
        data = json.loads(parts[2])
        if data['addr'] == addr:
            rows.append((float(parts[0]), data['prev_state'], data['state']))
t0 = rows[0][0] if rows else 0
for ts, prev, now in rows:
    print('        +%5.1fs  listener %s: %s -> %s' % (ts - t0, addr, prev, now))
seq = [(p, s) for _ts, p, s in rows]
want = [('absent', 'bound'), ('bound', 'absent'), ('absent', 'bound')]
sys.exit(0 if seq == want else 1)
PYEOF
if [ $? -eq 0 ]; then
    pass "the listener SSE ledger is exactly absent->bound, bound->absent, absent->bound"
else
    fail "the listener SSE ledger is exactly absent->bound, bound->absent, absent->bound"
fi
note "announce lines from instance B:"
grep -E "optional bind (bound|released)" "$LOG_DIR/b-roamer.out" | sed 's/^/        /'

# ---------------------------------------------------------------- 3. error and continue
step "3. An occupied optional address is an error, not a death"

kill "$B_PID" 2>/dev/null; wait "$B_PID" 2>/dev/null
kill "$SSE_PID" 2>/dev/null
assert "instance B has released $SCRATCH_ADDR:$PB" wait_free "$SCRATCH_ADDR" "$PB" 15

# $SCRATCH_ADDR is present; take its port away before B can have it.
python3 - "$SCRATCH_ADDR" "$PB" <<'PYEOF' &
import socket, sys, time
addr, port = sys.argv[1], int(sys.argv[2])
deadline = time.time() + 15
while True:                      # the departing instance may still hold it
    s = socket.socket()
    # SO_REUSEADDR because the drill just fetched from this address:port and the
    # TIME_WAIT sockets would otherwise refuse us — every real server sets it, which
    # is precisely why Roundhouse still collides with us and reports EADDRINUSE.
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((addr, port)); s.listen(4); break
    except OSError:
        s.close()
        if time.time() > deadline:
            raise
        time.sleep(0.5)
time.sleep(3600)
PYEOF
BLOCKER_PID=$!
PIDS_TO_KILL+=("$BLOCKER_PID")
assert "the scratch listener owns $SCRATCH_ADDR:$PB" wait_listening "$SCRATCH_ADDR" "$PB" 15

start_instance "$PB" "$DIR_B" "b-occupied" --bind 127.0.0.1 \
    --bind-optional "$SCRATCH_ADDR" --bind-retry "$RETRY"
B_PID="$RH_PID"
assert "instance B still starts with its optional port occupied" \
    wait_http 127.0.0.1 "$PB" /api/units 20
wait_listener "$PB" "$SCRATCH_ADDR" error $((RETRY * 4 + 6)) \
    "the row reads error (EADDRINUSE), and the process is still serving"
assert "the error row names an errno" \
    grep -q '"last_error": "\[Errno' <(get 127.0.0.1 "$PB" /api/units | python3 -m json.tool)

kill "$BLOCKER_PID" 2>/dev/null; wait "$BLOCKER_PID" 2>/dev/null
wait_listener "$PB" "$SCRATCH_ADDR" bound $((RETRY * 4 + 6)) \
    "the port is freed -> the next cycle binds it"

# ---------------------------------------------------------------- 4. the roaming leg
step "4. Two candidates, one peer: the endpoint moves and the peer does not"

start_instance "$PA" "$DIR_A" "a-federator" --bind 127.0.0.1 \
    --fleet-peer "bee=http://$SCRATCH_ADDR:$PB,http://127.0.0.1:$PB" \
    --peer-interval "$INTERVAL"
A_PID="$RH_PID"
assert "instance A is serving" wait_http 127.0.0.1 "$PA" /api/units 20

SSE_A="$LOG_DIR/sse-a.txt"
sse_capture "$PA" "$SSE_A"
sleep 1

wait_endpoint "$PA" bee "http://$SCRATCH_ADDR:$PB" $((INTERVAL * 3 + 10)) \
    "candidate 1 answers while the address is here"

note "ip addr del $SCRATCH_ADDR/32 dev lo   (the laptop leaves the sofa)"
addr_del
wait_endpoint "$PA" bee "http://127.0.0.1:$PB" $((INTERVAL * 4 + 15)) \
    "the walk falls through to candidate 2 and the peer never leaves up"

note "ip addr add $SCRATCH_ADDR/32 dev lo   (and comes back)"
addr_add
wait_endpoint "$PA" bee "http://$SCRATCH_ADDR:$PB" $((INTERVAL * 4 + 15)) \
    "the re-walk from the top picks candidate 1 up again"

assert "the fleet view is still fresh through the new endpoint" \
    grep -q '"fed_state": "fresh"' <(get 127.0.0.1 "$PA" /api/fleet | python3 -m json.tool)

# Every peer event across the whole leg. An endpoint move is prev_state == state ==
# 'up'; the peer's first sighting (unknown -> up) is allowed once; anything else is a
# flap — the thing this milestone promises an address change cannot cause. The stream
# is joined after instance A's first round, so the initial event may not be in it.
python3 - "$SSE_A" <<'PYEOF'
import json, sys
rows = []
for line in open(sys.argv[1]):
    parts = line.rstrip('\n').split('\t')
    if len(parts) == 3 and parts[1] == 'peer':
        rows.append((float(parts[0]), json.loads(parts[2])))
t0 = rows[0][0] if rows else 0
moves = flaps = 0
for ts, d in rows:
    prev, now = d['prev_state'], d['state']
    kind = ('move' if prev == 'up' and now == 'up'
            else 'first sighting' if prev == 'unknown' and now == 'up' else 'FLAP')
    moves += kind == 'move'
    flaps += kind == 'FLAP'
    print('        +%5.1fs  peer %s: %s -> %s  via %s   [%s]' %
          (ts - t0, d['name'], prev, now, d.get('endpoint_in_use'), kind))
print('        endpoint moves: %d, flaps: %d' % (moves, flaps))
sys.exit(0 if flaps == 0 and moves >= 2 else 1)
PYEOF
if [ $? -eq 0 ]; then
    pass "the peer ledger is endpoint moves only — zero up/down flaps"
else
    fail "the peer ledger is endpoint moves only — zero up/down flaps"
fi

# ---------------------------------------------------------------- summary
step "Summary"
echo "  PASS: $PASS"
echo "  FAIL: $FAIL"
if [ "$FAIL" -gt 0 ]; then exit 1; fi
exit 0
