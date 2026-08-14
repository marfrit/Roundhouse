#!/bin/bash
# fleet-drill.sh — MVP8 container drill: federation across two real instances.
#
# Two Roundhouse instances on this host, one federating the other, plus three
# purpose-built peers that no Roundhouse could be: a version-skew peer (an entry
# carrying keys this version does not know), a self-signed HTTPS peer (the TLS row),
# and an accept-only listener (the hang row). Every assertion is mechanical.
#
#   ./fleet-drill.sh             all container legs
#   ./fleet-drill.sh --live      print the live-boltzmann operator checklist and exit
#
# Env: RH_PORT_A / RH_PORT_B (defaults 8291/8292 — deliberately not 8090/8099, so a
# running roundhouse.service is never disturbed).
#
# Both instances share this host's nodename, so `model_name` is `<host>-<alias>` on
# BOTH — that is deliberate (MVP8-SPEC recon 7): the disjoint aliases prove the clean
# merge and the ONE shared alias produces the conflict leg for free. Do not "fix" it
# by faking hostnames.
#
# Nothing here actuates a unit. The hazard leg arms instance A only so that the
# refusal it proves is a 404 (unarmed, every action route answers 403 first and the
# hazard question is never reached) — and it only ever POSTs a PEER's unit name, whose
# whole point is that it is refused before anything is touched. HOME is redirected to
# a scratch dir, so the token file lands there and not in the operator's home.
#
# Exit 0 on PASS, 1 on any FAIL. stdlib python3 only; no jq, no nc.

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MVP1="$(dirname "$HERE")"
REPO="$(dirname "$MVP1")"
RH="$MVP1/roundhouse.py"
MCP="$MVP1/roundhouse_mcp.py"
FIXTURES="$REPO/docs/fixtures"

PA="${RH_PORT_A:-8291}"          # instance A — the aggregator
PB="${RH_PORT_B:-8292}"          # instance B — the federated peer
PC="${RH_PORT_C:-8293}"          # instance C — the TLS and hang legs
PSKEW=9411                       # static peer serving a version-skewed fragment
PTLS=9412                        # self-signed HTTPS listener
PHANG=9413                       # accepts and never answers
INTERVAL=5

SCRATCH="$(mktemp -d /tmp/fleet-drill.XXXXXX)"
LOG_DIR="$SCRATCH/logs"; mkdir -p "$LOG_DIR"
HOME_A="$SCRATCH/home-a"; mkdir -p "$HOME_A"
DIR_A="$SCRATCH/units-a"; mkdir -p "$DIR_A"
DIR_B="$SCRATCH/units-b"; mkdir -p "$DIR_B"
DIR_C="$SCRATCH/units-c"; mkdir -p "$DIR_C"

PASS=0
FAIL=0

pass() { echo "  PASS  $*"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL  $*"; FAIL=$((FAIL + 1)); }
step() { echo; echo "=== $* ==="; }
note() { echo "        $*"; }
body() { echo "--- $1 ---"; cat; echo "--- end ---"; }

assert() {  # assert "<description>" <shell condition...>
    local desc="$1"; shift
    if "$@"; then pass "$desc"; else fail "$desc"; fi
}

# ---------------------------------------------------------------- live checklist
if [ "${1:-}" = "--live" ]; then
    cat <<'LIVE'
=== MVP8 live leg — boltzmann, read-only, federation over real TLS ===

The federation reads only, so the live row needs no arming and no token. Declare the
RUNNING instance as a fleet peer of a SECOND, throwaway instance — that proves the
fetch against the real certificate chain without restarting the enabled unit:

  cd ~/roundhouse-live/mvp1
  python3 roundhouse.py --serve --port 8098 --no-db \
      --fleet-peer boltzmann=https://boltzmann.fritz.box \
      --peer-interval 30

  curl -s http://127.0.0.1:8098/api/fleet
  curl -s http://127.0.0.1:8098/api/routing-config/fleet

Check:
  * the peer reaches state `up` and fed_state `fresh` — the fleet CA is in the system
    store, so https verifies with no flag and no exception
  * /api/fleet lists both hosts' units, each tagged with its source; the local rows
    are first and are never stale
  * BOTH instances run on boltzmann, so every model_name is `boltzmann-<alias>` on
    both sides: the merge reports a conflict for every shared alias and keeps the
    local one. That is the conflict rule working, not a fault.
  * /api/routing-config (the LOCAL route hossenfelder pulls) is unchanged: no peer
    entry, no contributor header
  * the 3 s tick and API latency are unaffected while fetches run
  * ampere, when it is awake: --fleet-peer ampere=https://ampere.fritz.box:8099 —
    away, the fleet view equals the local view; back, its models appear

Afterwards: kill the second instance, and confirm the enabled roundhouse.service is
still serving :8099 and the fleet's PIDs are untouched.
LIVE
    exit 0
fi

# ---------------------------------------------------------------- cleanup
PIDS_TO_KILL=()
cleanup() {
    for pid in "${PIDS_TO_KILL[@]:-}"; do
        [ -n "$pid" ] && kill "$pid" 2>/dev/null
    done
    wait 2>/dev/null
}
trap cleanup EXIT

# ---------------------------------------------------------------- helpers
get() {  # get <port> <path>  -> body on stdout (empty on error)
    python3 -c "
import sys, urllib.request
try:
    with urllib.request.urlopen('http://127.0.0.1:$1$2', timeout=5) as r:
        sys.stdout.write(r.read().decode())
except Exception as e:
    print('', end='')
    print('        fetch failed: %s' % e, file=sys.stderr)
"
}

wait_http() {  # wait_http <port> <path> <timeout>
    python3 -c "
import sys, time, urllib.request
deadline = time.time() + float('$3')
while time.time() < deadline:
    try:
        urllib.request.urlopen('http://127.0.0.1:$1$2', timeout=2).read()
        sys.exit(0)
    except Exception:
        time.sleep(0.2)
sys.exit(1)
"
}

# Wait until fleet peer <name> on port <port> reaches (state, fed_state), printing the
# peer block whenever it changes.
wait_fed() {  # wait_fed <port> <name> <state> <fed_state> <timeout> <why>
    local port="$1" name="$2" want_state="$3" want_fed="$4" timeout="$5" why="$6"
    local out rc
    out=$(python3 - "$port" "$name" "$want_state" "$want_fed" "$timeout" <<'PYEOF'
import json, sys, time, urllib.request
port, name, want_state, want_fed, timeout = sys.argv[1:5] + [float(sys.argv[5])]
deadline, last = time.time() + timeout, None
while time.time() < deadline:
    try:
        with urllib.request.urlopen('http://127.0.0.1:%s/api/fleet' % port, timeout=3) as r:
            doc = json.load(r)
    except Exception:
        time.sleep(0.25); continue
    row = next((p for p in doc['peers'] if p['name'] == name), None)
    snap = json.dumps(row, sort_keys=True)
    if snap != last:
        print('        ' + snap)
        last = snap
    if row and row['state'] == want_state and row['fed_state'] == want_fed:
        sys.exit(0)
    time.sleep(0.25)
print('        TIMEOUT waiting for %s -> (%s, %s)' % (name, want_state, want_fed))
sys.exit(1)
PYEOF
)
    rc=$?
    echo "$out"
    if [ $rc -eq 0 ]; then pass "$why"; else fail "$why"; fi
    return $rc
}

# A fixture unit, on-demand-marked so an OFF unit still routes (H-series inclusion).
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

git_init_units() {  # git_init_units <dir>
    (cd "$1" && git init -q && git add -A && \
     git -c user.name=drill -c user.email=drill@local commit -qm 'drill fixtures') \
        >/dev/null 2>&1
}

# Sets RH_PID. NOT a command substitution: $(...) runs in a subshell, so the
# PIDS_TO_KILL append would be lost and every instance would outlive the drill —
# and the NEXT run would then bind nothing, silently testing the stale one.
start_instance() {  # start_instance <label> <port> <unit-dir> <log> [extra args...]
    local label="$1" port="$2" dir="$3" log="$4"; shift 4
    HOME="$HOME_A" python3 "$RH" --serve --unit-dir "$dir" --no-db \
        --port "$port" --bind 127.0.0.1 --peer-interval "$INTERVAL" "$@" \
        > "$LOG_DIR/$log.out" 2> "$LOG_DIR/$log.err" &
    RH_PID=$!
    PIDS_TO_KILL+=("$RH_PID")
}

# A port must be free before an instance claims it, or wait_http would happily
# answer from somebody else's server.
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

# ---------------------------------------------------------------- environment
step "Environment"
note "roundhouse:  $RH"
note "instance A:  127.0.0.1:$PA  units $DIR_A   (the aggregator)"
note "instance B:  127.0.0.1:$PB  units $DIR_B   (the federated peer)"
note "instance C:  127.0.0.1:$PC  units $DIR_C   (TLS + hang legs)"
note "skew peer:   127.0.0.1:$PSKEW    tls peer: 127.0.0.1:$PTLS    hang: 127.0.0.1:$PHANG"
note "scratch:     $SCRATCH"
note "nodename:    $(uname -n)  (shared by both instances — see the header)"

# A: qwen3.6-coding (SHARED alias + SHARED unit name) + alpha-one (disjoint)
make_unit "$DIR_A" qwen3.6-coding.service      qwen3.6-coding.service qwen3.6-coding 8085
make_unit "$DIR_A" llama-server-gemma4.service alpha-one.service      alpha-one      8181
git_init_units "$DIR_A"
# B: qwen3.6-coding (the collision) + bravo-one (disjoint)
make_unit "$DIR_B" qwen3.6-coding.service      qwen3.6-coding.service qwen3.6-coding 8085
make_unit "$DIR_B" llama-server-gemma4.service bravo-one.service      bravo-one      8182
# C: one unit, so its local view is not empty
make_unit "$DIR_C" llama-server-gemma4.service charlie-one.service    charlie-one    8183

# ---------------------------------------------------------------- 0. the skew peer
step "0. A peer this version has never met (version skew + hostile input)"
python3 - <<PYEOF > "$LOG_DIR/skew.out" 2>&1 &
import http.server, json, socketserver

UNITS = {'host': 'skewhost', 'mode': 'read-only', 'units': [
    {'unit': 'oddball.service', 'rung': 'READY', 'port': 9001, 'alias': 'oddball',
     'enabled': True, 'on_demand': False, 'retired': False, 'strategy_note': None,
     'badges': [], 'description': 'a key outside the keep-list',
     'sensed_at': 1.0, 'model_file': 'nope.gguf'}]}
ROUTING = {'model_list': [
    {'model_name': 'skewhost-oddball',
     'litellm_params': {'model': 'openai/oddball', 'api_base': 'http://skewhost:9001/v1',
                        'api_key': 'none', 'rpm': 240},
     'model_info': {'unit': 'oddball.service', 'logical': 'oddball', 'host': 'skewhost',
                    'rung': 'READY', 'accelerator': 'npu'},
     'weight': 3},
    {'model_name': 'skewhost-broken', 'litellm_params': {'tags': ['a', 'b']}},
]}

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        doc = UNITS if self.path == '/api/units' else (
            ROUTING if self.path == '/api/routing-config.json' else None)
        if doc is None:
            self.send_error(404); return
        payload = json.dumps(doc).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
    def log_message(self, *a): pass

socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(('127.0.0.1', $PSKEW), H) as srv:
    srv.serve_forever()
PYEOF
PIDS_TO_KILL+=("$!")
sleep 0.6
note "skew peer advertises litellm_params.rpm, model_info.accelerator and a top-level"
note "weight — none of which this version knows — plus one entry with a list value."

# ---------------------------------------------------------------- 1. two instances
step "1. Start B, then A federating it"
require_free_port "$PB" || exit 1
start_instance B "$PB" "$DIR_B" b; B_PID=$RH_PID
if wait_http "$PB" /api/units 20; then pass "instance B answers on :$PB"
else fail "instance B never came up"; cat "$LOG_DIR/b.err"; exit 1; fi

require_free_port "$PA" || exit 1
start_instance A "$PA" "$DIR_A" a \
    --fleet-peer "bee=http://127.0.0.1:$PB" \
    --fleet-peer "skew=http://127.0.0.1:$PSKEW"; A_PID=$RH_PID
if wait_http "$PA" /api/units 20; then pass "instance A answers on :$PA"
else fail "instance A never came up"; cat "$LOG_DIR/a.err"; exit 1; fi

assert "A accepted a loopback fleet peer on an unmanaged port (D2/K1)" \
    test ! -s "$LOG_DIR/a.err"
[ -s "$LOG_DIR/a.err" ] && cat "$LOG_DIR/a.err"

wait_fed "$PA" bee up fresh 40 "bee reaches up + fresh within the cadence"
wait_fed "$PA" skew up fresh 40 "skew reaches up + fresh (it is not a Roundhouse, and need not be)"

# ---------------------------------------------------------------- 2. the roster
step "2. /api/fleet aggregates with source tags; local units are never stale"
get "$PA" /api/fleet | python3 -m json.tool | body "A: GET /api/fleet"

if get "$PA" /api/fleet | python3 -c "
import json, sys
doc = json.load(sys.stdin)
host = doc['host']
local = [u for u in doc['units'] if u['source'] == host]
bee   = [u for u in doc['units'] if u['source'] == 'bee']
skew  = [u for u in doc['units'] if u['source'] == 'skew']
ok = True
def check(cond, why):
    global ok
    print('        %s %s' % ('ok  ' if cond else 'BAD ', why))
    ok = ok and cond
check(doc['units'][:len(local)] == local, 'local units come first')
check(all(u['stale'] is False for u in local), 'no local unit is stale')
check({u['unit'] for u in local} == {'qwen3.6-coding.service', 'alpha-one.service'},
      'local roster is A: %s' % sorted(u['unit'] for u in local))
check({u['unit'] for u in bee} == {'qwen3.6-coding.service', 'bravo-one.service'},
      \"bee's units are tagged source=bee: %s\" % sorted(u['unit'] for u in bee))
check([u['unit'] for u in skew] == ['oddball.service'], 'skew contributes its one unit')
check('description' not in skew[0] and 'model_file' not in skew[0],
      'the keep-list dropped the peer keys outside it: %s' % sorted(skew[0]))
peers = {p['name']: p for p in doc['peers']}
check(set(peers) == {'bee', 'skew'}, 'only fleet peers appear in peers[]')
check(peers['bee']['kind'] == 'roundhouse' and peers['bee']['mode'] == 'read-only',
      \"bee's own mode is reported: %s\" % peers['bee']['mode'])
check(peers['bee']['unit_count'] == 2 and peers['bee']['invalid_entries'] == 0,
      'bee unit_count=%d invalid_entries=%d' % (peers['bee']['unit_count'],
                                                peers['bee']['invalid_entries']))
check(peers['skew']['invalid_entries'] == 1,
      'the malformed skew entry was dropped at ingestion, and counted')
check(all(p['fetched_at'] and p['attempted_at'] for p in doc['peers']),
      'every peer block carries fetched_at and attempted_at')
sys.exit(0 if ok else 1)
"; then pass "roster aggregates with source tags, keep-list and per-peer metadata"
else fail "roster aggregation is wrong"; fi

# ---------------------------------------------------------------- 3. the fragment
step "3. /api/routing-config/fleet merges VERBATIM, and names its contributors"
get "$PA" /api/routing-config/fleet | body "A: GET /api/routing-config/fleet"

if python3 - "$PA" "$PB" <<'PYEOF'
import json, sys, urllib.request
pa, pb = sys.argv[1], sys.argv[2]
def j(port, path):
    with urllib.request.urlopen('http://127.0.0.1:%s%s' % (port, path), timeout=5) as r:
        return json.load(r)
fleet = j(pa, '/api/routing-config/fleet.json')
theirs = j(pb, '/api/routing-config.json')
ok = True
def check(cond, why):
    global ok
    print('        %s %s' % ('ok  ' if cond else 'BAD ', why))
    ok = ok and cond

merged = {e['model_name']: e for e in fleet['model_list']}
# B authors one entry we can compare byte-for-byte in field terms: bravo-one. Its
# twin (the shared alias) is the conflict case and is checked in the next leg.
mine = [e for e in theirs['model_list'] if e['model_name'].endswith('-bravo-one')]
check(len(mine) == 1, "B advertises bravo-one: %s" % [e['model_name'] for e in theirs['model_list']])
if mine:
    name = mine[0]['model_name']
    check(name in merged, 'B\'s entry reached the fleet fragment: %s' % name)
    check(merged.get(name) == mine[0],
          'the entry is VERBATIM (field for field, no rewriting): %s' % json.dumps(merged.get(name)))
skew = merged.get('skewhost-oddball')
check(skew is not None, 'the version-skew entry is in the fragment')
if skew:
    check(skew['litellm_params'].get('rpm') == 240, 'an unknown litellm_params key survived (rpm)')
    check(skew['model_info'].get('accelerator') == 'npu', 'an unknown model_info key survived')
    check(skew.get('weight') == 3, 'an unknown top-level key survived')
check('skewhost-broken' not in merged, 'the malformed entry never reached the document')
names = [c['name'] for c in fleet['contributors']]
check(names[0] == j(pa, '/api/routing-config.json')['generated_by'].split('@')[1],
      'the local host leads the contributor list: %s' % names)
check(names[1:] == ['bee', 'skew'], 'peers follow, sorted by name: %s' % names)
sys.exit(0 if ok else 1)
PYEOF
then pass "the merge is verbatim and contributor-named"
else fail "the merge altered or dropped a peer's truth"; fi

if get "$PA" /api/routing-config/fleet | grep -q '^# contributor: bee (fetched '; then
    pass "the YAML header names bee and when it was fetched"
else fail "no contributor line for bee"; fi

if get "$PA" /api/routing-config/fleet | grep -q 'rpm: 240'; then
    pass "the YAML emitter renders the unknown key too (not just the JSON twin)"
else fail "the YAML emitter dropped an unknown key — it is walking fixed key lists"; fi

# ---------------------------------------------------------------- 4. the conflict
step "4. One alias on two hosts: first wins, loudly"
get "$PA" /api/routing-config/fleet | grep '^# conflict:' | body "A: conflict header lines"
get "$PA" /api/routing-config/fleet.json | python3 -c "
import json, sys; print(json.dumps(json.load(sys.stdin)['conflicts'], indent=2))" \
    | body "A: conflicts[] in the JSON twin"

if python3 - "$PA" "$PB" <<'PYEOF'
import json, sys, urllib.request
pa, pb = sys.argv[1], sys.argv[2]
def j(port, path):
    with urllib.request.urlopen('http://127.0.0.1:%s%s' % (port, path), timeout=5) as r:
        return json.load(r)
def t(port, path):
    with urllib.request.urlopen('http://127.0.0.1:%s%s' % (port, path), timeout=5) as r:
        return r.read().decode()
fleet = j(pa, '/api/routing-config/fleet.json')
local = j(pa, '/api/routing-config.json')
theirs = j(pb, '/api/routing-config.json')
yaml = t(pa, '/api/routing-config/fleet')
ok = True
def check(cond, why):
    global ok
    print('        %s %s' % ('ok  ' if cond else 'BAD ', why))
    ok = ok and cond

shared = [e['model_name'] for e in local['model_list']
          if e['model_name'].endswith('-qwen3.6-coding')][0]
check(any(e['model_name'] == shared for e in theirs['model_list']),
      'both hosts advertise %s (same nodename, same alias — deliberate)' % shared)
names = [e['model_name'] for e in fleet['model_list']]
check(names.count(shared) == 1, 'the merged document lists it exactly once')
kept = next(e for e in fleet['model_list'] if e['model_name'] == shared)
mine = next(e for e in local['model_list'] if e['model_name'] == shared)
check(kept == mine, 'the LOCAL entry is the one kept (first wins)')
check(fleet['conflicts'] and fleet['conflicts'][0]['model_name'] == shared,
      'the conflict is machine-readable: %s' % json.dumps(fleet['conflicts']))
check(fleet['conflicts'][0]['dropped_source'] == 'bee', "bee's entry is the one dropped")
check(("# conflict: model_name '%s' also advertised by bee" % shared) in yaml,
      'the conflict is in the YAML header too')
sys.exit(0 if ok else 1)
PYEOF
then pass "the shared alias is a surfaced conflict, not a silent overwrite"
else fail "the conflict rule did not hold"; fi

# ---------------------------------------------------------------- 5. the pin
step "5. /api/routing-config is untouched — the promise to a live consumer"
get "$PA" /api/routing-config | body "A: GET /api/routing-config (local only)"

if get "$PA" /api/routing-config | python3 -c "
import sys
text = sys.stdin.read()
bad = [w for w in ('contributor:', 'excluded:', 'conflict:', 'bravo-one', 'skewhost')
       if w in text]
print('        offending tokens: %s' % bad)
sys.exit(1 if bad else 0)
"; then pass "no peer entry and no fleet header in the local route, with federation live"
else fail "the local route was widened to the fleet"; fi

if get "$PA" /api/routing-config | grep -q '^# warm-hook: '; then
    pass "the local warm-hook header is still there"
else fail "the local header changed"; fi
if get "$PA" /api/routing-config/fleet | grep -q 'warm-hook'; then
    fail "the fleet fragment carries a warm-hook (K5 says it must not)"
else pass "the fleet fragment carries no warm-hook (one URL would lie for peers)"; fi

# ---------------------------------------------------------------- 6. the hazard
step "6. A peer's unit cannot be actuated here (the milestone's sharpest rule)"
# Arm A: unarmed, every action route answers 403 before the unit is ever looked up,
# so the 404 could not be proven. A only ever sees a PEER unit name below.
kill "$A_PID" 2>/dev/null; wait "$A_PID" 2>/dev/null
start_instance A "$PA" "$DIR_A" a2 --actuate \
    --fleet-peer "bee=http://127.0.0.1:$PB" \
    --fleet-peer "skew=http://127.0.0.1:$PSKEW"; A_PID=$RH_PID
if wait_http "$PA" /api/units 20; then pass "instance A restarted, armed, in a scratch unit dir"
else fail "armed instance A never came up"; cat "$LOG_DIR/a2.err"; exit 1; fi
TOKEN="$(cat "$HOME_A/.config/roundhouse/token" 2>/dev/null)"
assert "a bearer token was minted into the scratch HOME" test -n "$TOKEN"
wait_fed "$PA" bee up fresh 40 "bee is fresh again on the armed instance"

if python3 - "$PA" "$TOKEN" <<'PYEOF'
import json, sys, urllib.request
port, token = sys.argv[1], sys.argv[2]
PEER_ONLY = 'bravo-one.service'          # exists on B, never on A
SHARED = 'qwen3.6-coding.service'        # exists on BOTH
ok = True
def check(cond, why):
    global ok
    print('        %s %s' % ('ok  ' if cond else 'BAD ', why))
    ok = ok and cond

def call(method, path, payload=None):
    data = json.dumps(payload or {}).encode() if method == 'POST' else None
    req = urllib.request.Request('http://127.0.0.1:%s%s' % (port, path), data=data,
                                 method=method)
    req.add_header('Content-Type', 'application/json')
    req.add_header('Authorization', 'Bearer ' + token)

    def decode(raw):
        # The unit-detail route answers a plain 404 page; the action routes answer
        # JSON. Either way the status is the assertion.
        try:
            return json.loads(raw.decode() or '{}')
        except Exception:
            return {'body': raw.decode(errors='replace')[:60]}

    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, decode(r.read())
    except urllib.error.HTTPError as e:
        return e.code, decode(e.read())

# The roster is showing the name at this very moment — that is the adversarial bit.
with urllib.request.urlopen('http://127.0.0.1:%s/api/fleet' % port, timeout=5) as r:
    roster = json.load(r)
check(any(u['unit'] == PEER_ONLY and u['source'] == 'bee' for u in roster['units']),
      'the roster IS displaying %s while we try to actuate it' % PEER_ONLY)

for method, path, payload in (
        ('GET',  '/api/units/' + PEER_ONLY, None),
        ('POST', '/api/units/%s/edit' % PEER_ONLY, {'edits': {'ctx': '4096'}}),
        ('POST', '/api/units/%s/rollout' % PEER_ONLY, {'edits': {'ctx': '4096'}, 'confirm': 'x'}),
        ('POST', '/api/units/%s/enablement' % PEER_ONLY, {'enabled': True}),
        ('POST', '/api/switch/preview', {'target': PEER_ONLY}),
        ('POST', '/api/switch', {'target': PEER_ONLY, 'confirm': 'x'}),
        ('POST', '/api/switch/preview', {'target': SHARED, 'stops': [PEER_ONLY]}),
        ('POST', '/api/switch', {'target': SHARED, 'stops': [PEER_ONLY], 'confirm': 'x'}),
        ('POST', '/api/warm', {'unit': PEER_ONLY}),
        ('POST', '/api/warm', {'logical': 'bee-bravo-one'}),
        ('POST', '/api/rollouts/ro-from-bee/rollback', {}),
        ('POST', '/api/rollouts/ro-from-bee/dismiss', {}),
):
    status, resp = call(method, path, payload)
    label = '%s %s %s' % (method, path, json.dumps(payload) if payload else '')
    check(status == 404, '404 for %s -> %s %s' % (label.strip(), status, resp.get('error', '')))

# ... and B is untouched: its own API still shows the unit exactly as it was.
sys.exit(0 if ok else 1)
PYEOF
then pass "every action route answers 404 for a peer's unit, while the roster shows it"
else fail "an action route did not refuse a peer unit"; fi

if get "$PB" /api/units | python3 -c "
import json, sys
units = {u['unit']: u for u in json.load(sys.stdin)['units']}
row = units.get('bravo-one.service')
print('        B still reports: %s rung=%s' % (row['unit'], row['rung']))
sys.exit(0 if row and row['rung'] in ('OFF', 'STANDBY') else 1)
"; then pass "B's unit is untouched (its own API is the witness)"
else fail "B's unit changed state"; fi

# ---------------------------------------------------------------- 7. kill B
step "7. Kill B: its entries LEAVE the fragment, the roster keeps them stale"
kill "$B_PID" 2>/dev/null; wait "$B_PID" 2>/dev/null
note "B killed at $(date -u +%H:%M:%S)"

if python3 - "$PA" <<'PYEOF'
import json, sys, time, urllib.request
port = sys.argv[1]
deadline = time.time() + 40
while time.time() < deadline:
    with urllib.request.urlopen('http://127.0.0.1:%s/api/routing-config/fleet.json' % port,
                                timeout=3) as r:
        frag = json.load(r)
    names = [e['model_name'] for e in frag['model_list']]
    if not any(n.endswith('-bravo-one') for n in names):
        print('        fragment now: %s' % names)
        print('        excluded:     %s' % json.dumps(frag['excluded']))
        sys.exit(0)
    time.sleep(0.5)
print('        TIMEOUT: %s' % names)
sys.exit(1)
PYEOF
then pass "an absent host's entries vanish from the fleet fragment"
else fail "a dead peer's backends are still being handed to routers"; fi

if get "$PA" /api/fleet | python3 -c "
import json, sys
doc = json.load(sys.stdin)
bee = next(p for p in doc['peers'] if p['name'] == 'bee')
units = [u for u in doc['units'] if u['source'] == 'bee']
print('        peer block: %s' % json.dumps(bee, sort_keys=True))
print('        retained units: %s' % sorted(u['unit'] for u in units))
sys.exit(0 if bee['stale'] and units and all(u['stale'] for u in units) else 1)
"; then pass "the roster retains bee's last-known units and marks them stale"
else fail "the roster dropped or mislabelled the retained rows"; fi

wait_fed "$PA" bee down stale 40 "bee settles down + stale after two failed probes"

if get "$PA" /api/fleet | python3 -c "
import json, sys
bee = next(p for p in json.load(sys.stdin)['peers'] if p['name'] == 'bee')
print('        reason: %s' % bee['reason'])
sys.exit(0 if (bee['reason'] or '').startswith('down: ') else 1)
"; then pass "the reason carries the frozen 'down:' prefix once reachability settles"
else fail "the down transition did not write its reason"; fi

if get "$PA" /api/routing-config/fleet | grep -q '^# excluded: bee (down, stale'; then
    pass "the YAML header says why bee contributed nothing"
    get "$PA" /api/routing-config/fleet | grep '^# ' | body "A: fleet header while bee is down"
else fail "no excluded line for bee"; fi

# ---------------------------------------------------------------- 8. restore B
step "8. Restart B: fresh again within one round"
start_instance B "$PB" "$DIR_B" b2; B_PID=$RH_PID
wait_http "$PB" /api/units 20 || { fail "B did not come back"; }
wait_fed "$PA" bee up fresh 40 "bee returns to up + fresh"

if get "$PA" /api/routing-config/fleet.json | python3 -c "
import json, sys
names = [e['model_name'] for e in json.load(sys.stdin)['model_list']]
print('        fragment now: %s' % names)
sys.exit(0 if any(n.endswith('-bravo-one') for n in names) else 1)
"; then pass "B's entries are back in the fragment"
else fail "B came back but its entries did not"; fi

# ---------------------------------------------------------------- 9. TLS
step "9. An untrusted certificate: up by TCP, never trusted for data"
if ! command -v openssl >/dev/null 2>&1; then
    note "openssl not found — TLS leg skipped"
else
    openssl req -x509 -newkey rsa:2048 -keyout "$SCRATCH/key.pem" -out "$SCRATCH/cert.pem" \
        -days 1 -nodes -subj '/CN=127.0.0.1' >/dev/null 2>&1
    python3 - <<PYEOF > "$LOG_DIR/tls.out" 2>&1 &
import http.server, json, ssl, socketserver
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        payload = json.dumps({'host': 'tlshost', 'mode': 'read-only', 'units': []}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
    def log_message(self, *a): pass
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain('$SCRATCH/cert.pem', '$SCRATCH/key.pem')
socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(('127.0.0.1', $PTLS), H) as srv:
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    srv.serve_forever()
PYEOF
    PIDS_TO_KILL+=("$!")
    sleep 0.8

    require_free_port "$PC" || exit 1
    start_instance C "$PC" "$DIR_C" c \
        --fleet-peer "tlspeer=https://127.0.0.1:$PTLS" \
        --fleet-peer "hang=http://127.0.0.1:$PHANG"; C_PID=$RH_PID
    if wait_http "$PC" /api/units 20; then pass "instance C answers on :$PC"
    else fail "instance C never came up"; cat "$LOG_DIR/c.err"; fi

    wait_fed "$PC" tlspeer up never 40 \
        "the self-signed peer is up (TCP) and its data is never fresh"

    if get "$PC" /api/fleet | python3 -c "
import json, sys
doc = json.load(sys.stdin)
peer = next(p for p in doc['peers'] if p['name'] == 'tlspeer')
print('        %s' % json.dumps(peer, sort_keys=True))
sys.exit(0 if peer['state'] == 'up' and peer['stale'] and
         (peer['reason'] or '').startswith('tls: ') else 1)
"; then pass "the reason carries the frozen 'tls:' prefix and the verification error"
    else fail "an untrusted certificate did not surface as tls:"; fi

    if get "$PC" /api/routing-config/fleet | grep -q '^# excluded: tlspeer (up, never: tls)'; then
        pass "the fragment excludes it, and says the peer is up but never trusted"
        get "$PC" /api/routing-config/fleet | grep '^# ' | body "C: fleet header (TLS peer)"
    else fail "the excluded line for the TLS peer is wrong"
        get "$PC" /api/routing-config/fleet | grep '^# ' | body "C: fleet header (TLS peer)"
    fi
fi

# ---------------------------------------------------------------- 10. no bypass
step "10. There is no way to turn verification off"
if HOME="$HOME_A" python3 "$RH" --help 2>&1 | grep -Eqi 'insecure|no-verify|skip-verify'; then
    fail "--help advertises a verification bypass"
else pass "--help has no insecure/no-verify flag"; fi

if grep -Eqn '_create_unverified_context|CERT_NONE|check_hostname\s*=|verify_mode\s*=' "$RH"; then
    fail "an insecure-TLS construct exists in roundhouse.py"
    grep -En '_create_unverified_context|CERT_NONE|check_hostname\s*=|verify_mode\s*=' "$RH"
else pass "no insecure-TLS construct exists anywhere in roundhouse.py"; fi

assert "ssl.create_default_context appears exactly once (inside _fetch_peer)" \
    test "$(grep -c 'create_default_context' "$RH")" = 1

# ---------------------------------------------------------------- 11. the hang
step "11. A hanging peer costs only its own timeout"
python3 -c "
import socket
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', $PHANG))
s.listen(8)
conns = []
while True:
    try:
        c, _ = s.accept()
        conns.append(c)          # accept and never answer
    except Exception:
        break
" &
PIDS_TO_KILL+=("$!")
sleep 0.5

if [ -n "${C_PID:-}" ]; then
    if python3 - "$PC" <<'PYEOF'
import json, statistics, sys, time, urllib.request
port = sys.argv[1]
lat = []
deadline = time.time() + 25
while time.time() < deadline:
    t0 = time.monotonic()
    with urllib.request.urlopen('http://127.0.0.1:%s/api/units' % port, timeout=5) as r:
        r.read()
    lat.append(time.monotonic() - t0)
    time.sleep(0.25)
print('        %d samples over %.0fs while the hang peer was being fetched' % (len(lat), 25))
print('        latency  min %.1f ms  median %.1f ms  max %.1f ms'
      % (min(lat) * 1e3, statistics.median(lat) * 1e3, max(lat) * 1e3))
with urllib.request.urlopen('http://127.0.0.1:%s/api/fleet' % port, timeout=5) as r:
    hang = next(p for p in json.load(r)['peers'] if p['name'] == 'hang')
print('        hang peer: %s' % json.dumps(hang, sort_keys=True))
sys.exit(0 if max(lat) < 1.0 else 1)
PYEOF
    then pass "the API stayed responsive throughout (no lock crossed the fetch)"
    else fail "API latency degraded while a fleet peer hung"; fi
fi

# ---------------------------------------------------------------- 12. MCP
step "12. MCP: fleet_roster is a read, and it is tool 18"
if python3 - "$MCP" "$PA" <<'PYEOF'
import json, queue, subprocess, sys, threading
mcp_script, port = sys.argv[1], sys.argv[2]
proc = subprocess.Popen([sys.executable, mcp_script, '--url', 'http://127.0.0.1:%s' % port],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, bufsize=1)
lines = queue.Queue()
threading.Thread(target=lambda: [lines.put(l) for l in proc.stdout], daemon=True).start()
n = [0]
def req(method, params=None):
    n[0] += 1
    proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': n[0], 'method': method,
                                 'params': params or {}}) + '\n')
    proc.stdin.flush()
    return json.loads(lines.get(timeout=30))
ok = True
def check(cond, why):
    global ok
    print('        %s %s' % ('ok  ' if cond else 'BAD ', why))
    ok = ok and cond
init = req('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                          'clientInfo': {'name': 'fleet-drill', 'version': '8.0'}})
proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) + '\n')
proc.stdin.flush()
check(init['result']['serverInfo']['version'] == '8.0',
      'serverInfo.version is %s' % init['result']['serverInfo']['version'])
tools = [t['name'] for t in req('tools/list')['result']['tools']]
check(len(tools) == 18, 'the catalog is 18 tools')
check('fleet_roster' in tools, 'fleet_roster is in it')
res = req('tools/call', {'name': 'fleet_roster', 'arguments': {}})['result']
doc = json.loads(res['content'][0]['text'])
check(not res.get('isError'), 'fleet_roster answered without error')
check({p['name'] for p in doc['peers']} == {'bee', 'skew'},
      'it returned the roster: peers=%s' % sorted(p['name'] for p in doc['peers']))
proc.stdin.close(); proc.wait(timeout=10)
sys.exit(0 if ok else 1)
PYEOF
then pass "fleet_roster works over the real stdio transport"
else fail "the MCP leg failed"; fi

# ---------------------------------------------------------------- summary
step "Summary"
echo "  PASS: $PASS    FAIL: $FAIL"
echo "  logs: $LOG_DIR"
if [ "$FAIL" -eq 0 ]; then
    echo "  ALL GREEN"
    exit 0
fi
exit 1
