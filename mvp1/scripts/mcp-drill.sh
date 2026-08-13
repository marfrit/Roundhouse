#!/bin/bash
# MCP drill — drives mvp1/roundhouse_mcp.py as a REAL SUBPROCESS over REAL PIPES
# against a running Roundhouse, and asserts every step's tool result.
#
# One MCP process holds the whole session (that is the point: framing drift and the
# `initialize`-derived requester tag only show up across a multi-message session).
#
#   Read leg      (always)      initialize, tools/list, the seven read tools.
#   Action leg    (--actions)   switch_preview -> switch_execute with the confirm FROM
#                               the preview -> warm (parks, because the switch holds the
#                               slot) -> warm_state -> warm_cancel -> operation_status
#                               polled to done -> one refusal.
#
#   Read-only leg (--read-only-url URL)
#                               read tools 200 + every action tool -> read_only_mode.
#
# --unmarked must name a unit that is NOT marked on-demand AND is currently INACTIVE:
# the warm route answers `already_warm` before it ever reaches the consent fence, so an
# active unit tests nothing. --warm-unit must be marked on-demand and inactive.
#
# The drill adds ZERO actuation of its own: everything it changes, it changes by
# calling an MCP tool, which calls a gated HTTP route. No systemd, no git, no files.
#
# Usage:
#   mvp1/scripts/mcp-drill.sh --url http://boltzmann.fritz.box:8090 \
#       --token "$(incus exec roundhouse-test -- su -l roundhouse -c 'cat ~/.config/roundhouse/token')" \
#       --actions --target llama-server-fake-b.service --stops qwen3.6-coding.service \
#       --warm-unit qwen3.6-coding.service --unmarked llama-server-gemma4-q4km.service \
#       --read-only-url http://boltzmann.fritz.box:8095

set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MCP_SCRIPT="$REPO_ROOT/mvp1/roundhouse_mcp.py"

MCP_URL="${ROUNDHOUSE_URL:-http://127.0.0.1:8090}"
MCP_TOKEN="${ROUNDHOUSE_TOKEN:-}"
RUN_ACTIONS=0
READ_ONLY_URL=""
TARGET=""
STOPS=""
UNMARKED=""
WARM_UNIT=""

while [ $# -gt 0 ]; do
    case "$1" in
        --url)            MCP_URL="$2"; shift 2 ;;
        --token)          MCP_TOKEN="$2"; shift 2 ;;
        --token-file)     MCP_TOKEN="$(cat "$2")"; shift 2 ;;
        --mcp-script)     MCP_SCRIPT="$2"; shift 2 ;;
        --actions)        RUN_ACTIONS=1; shift ;;
        --target)         TARGET="$2"; shift 2 ;;
        --stops)          STOPS="$2"; shift 2 ;;
        --unmarked)       UNMARKED="$2"; shift 2 ;;
        --warm-unit)      WARM_UNIT="$2"; shift 2 ;;
        --read-only-url)  READ_ONLY_URL="$2"; shift 2 ;;
        -h|--help)        sed -n '2,29p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [ ! -f "$MCP_SCRIPT" ]; then
    echo "error: MCP server not found at $MCP_SCRIPT" >&2
    exit 2
fi

echo "MCP drill"
echo "  server : $MCP_SCRIPT"
echo "  url    : $MCP_URL"
echo "  token  : $([ -n "$MCP_TOKEN" ] && echo 'present' || echo 'absent (read-only leg only)')"
echo "  actions: $([ "$RUN_ACTIONS" = 1 ] && echo yes || echo no)"
echo "  ro url : ${READ_ONLY_URL:-none}"
echo ""

MCP_SCRIPT="$MCP_SCRIPT" MCP_URL="$MCP_URL" MCP_TOKEN="$MCP_TOKEN" \
RUN_ACTIONS="$RUN_ACTIONS" READ_ONLY_URL="$READ_ONLY_URL" \
DRILL_TARGET="$TARGET" DRILL_STOPS="$STOPS" DRILL_UNMARKED="$UNMARKED" \
DRILL_WARM_UNIT="$WARM_UNIT" \
python3 - <<'PYEOF'
import json
import os
import queue
import subprocess
import sys
import threading
import time

MCP_SCRIPT = os.environ['MCP_SCRIPT']
URL = os.environ['MCP_URL']
TOKEN = os.environ['MCP_TOKEN']
RUN_ACTIONS = os.environ['RUN_ACTIONS'] == '1'
READ_ONLY_URL = os.environ['READ_ONLY_URL']
TARGET = os.environ['DRILL_TARGET']
STOPS = [s for s in os.environ['DRILL_STOPS'].split(',') if s]
UNMARKED = os.environ['DRILL_UNMARKED']
WARM_UNIT = os.environ['DRILL_WARM_UNIT']

GREEN, RED, YELLOW, NC = '\033[0;32m', '\033[0;31m', '\033[0;33m', '\033[0m'
PASSED = []
FAILED = []


def check(name, ok, detail=''):
    if ok:
        PASSED.append(name)
        print(f'{GREEN}PASS{NC} - {name}' + (f'  ({detail})' if detail else ''))
    else:
        FAILED.append(name)
        print(f'{RED}FAIL{NC} - {name}' + (f'  ({detail})' if detail else ''))
    return ok


def skip(name, why):
    print(f'{YELLOW}SKIP{NC} - {name}  ({why})')


class Mcp:
    """One MCP server subprocess for a whole session, over real pipes."""

    def __init__(self, url, token, client_name='drill'):
        env = dict(os.environ)
        env.pop('ROUNDHOUSE_TOKEN', None)
        if token:
            env['ROUNDHOUSE_TOKEN'] = token
        self.proc = subprocess.Popen(
            [sys.executable, MCP_SCRIPT, '--url', url],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env)
        self.lines = queue.Queue()
        self.err = []
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self._pump_err, daemon=True).start()
        self.n = 0
        self.client_name = client_name

    def _pump(self):
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _pump_err(self):
        for line in self.proc.stderr:
            self.err.append(line)

    def request(self, method, params=None, timeout=40.0):
        self.n += 1
        msg = {'jsonrpc': '2.0', 'id': self.n, 'method': method, 'params': params or {}}
        self.proc.stdin.write(json.dumps(msg, separators=(',', ':')) + '\n')
        self.proc.stdin.flush()
        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            raise SystemExit(f'{RED}FATAL{NC} no response to {method} within {timeout}s '
                             f'(framing deadlock?); stderr: {"".join(self.err)!r}')
        if line is None:
            raise SystemExit(f'{RED}FATAL{NC} MCP server exited; stderr: {"".join(self.err)!r}')
        if line.endswith('\n') is False or '\r' in line:
            raise SystemExit(f'{RED}FATAL{NC} malformed framing: {line!r}')
        return json.loads(line)

    def initialize(self):
        resp = self.request('initialize', {
            'protocolVersion': '2025-06-18', 'capabilities': {},
            'clientInfo': {'name': self.client_name, 'version': '6.0'}})
        self.proc.stdin.write(
            json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) + '\n')
        self.proc.stdin.flush()
        return resp

    def call(self, tool, args=None, timeout=40.0):
        resp = self.request('tools/call', {'name': tool, 'arguments': args or {}},
                            timeout=timeout)
        if 'error' in resp:
            return {'_jsonrpc_error': resp['error']}, True
        r = resp['result']
        return json.loads(r['content'][0]['text']), r['isError']

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def show(label, payload, limit=1600):
    text = json.dumps(payload, indent=2)
    if len(text) > limit:
        text = text[:limit] + '\n  ... [truncated]'
    print(f'--- {label} ---')
    print(text)
    print('--- end ---')


# ===================== READ LEG =====================
print('== read leg ==')
mcp = Mcp(URL, TOKEN)
init = mcp.initialize()
check('initialize', init.get('result', {}).get('serverInfo', {}).get('name') == 'roundhouse-mcp',
      init.get('result', {}).get('protocolVersion', '?'))

tools = mcp.request('tools/list')['result']['tools']
names = [t['name'] for t in tools]
FROZEN = ['fleet_status', 'unit_detail', 'port_board', 'deployments', 'routing_config',
          'operation_status', 'warm_state', 'switch_preview', 'switch_execute',
          'edit_preview', 'edit_rollout', 'set_boot', 'warm', 'warm_cancel',
          'operation_rollback', 'operation_dismiss']
check('tools/list is the frozen 16 in order', names == FROZEN, f'{len(names)} tools')
check('every tool carries a JSON-Schema inputSchema',
      all(t['inputSchema'].get('type') == 'object'
          and t['inputSchema'].get('additionalProperties') is False for t in tools))

fleet, err = mcp.call('fleet_status')
check('fleet_status', not err and fleet.get('http_status') == 200,
      f"mode={fleet.get('mode')} units={fleet.get('n_units')}")
show('fleet_status (trimmed)', {k: v for k, v in fleet.items() if k != 'units'})
for row in fleet.get('units', []):
    print('   {unit:42s} rung={rung:9s} port={port} alias={alias} on_demand={on_demand} '
          'enabled={enabled} retired={retired}'.format(**{**row, 'alias': row.get('alias')}))

first_unit = fleet['units'][0]['unit'] if fleet.get('units') else None
if first_unit:
    detail, err = mcp.call('unit_detail', {'unit': first_unit})
    check('unit_detail', not err and detail.get('http_status') == 200, first_unit)

ports, err = mcp.call('port_board')
check('port_board', not err and ports.get('http_status') == 200)
deps, err = mcp.call('deployments')
check('deployments', not err and deps.get('http_status') == 200)
routing, err = mcp.call('routing_config')
check('routing_config', not err and routing.get('http_status') == 200,
      f"{len(routing.get('model_list', []))} entries")
warm_state, err = mcp.call('warm_state')
check('warm_state', not err and warm_state.get('http_status') == 200)

# A read tool against a nonexistent id: 404 is DATA, not an error (I2).
missing, err = mcp.call('operation_status', {'rollout_id': 'no-such-op'})
check('operation_status of an unknown id is data, not an error',
      not err and missing.get('http_status') == 404, str(missing))

# A typo'd unit name hits the text/plain 404 path -> the I2 `raw` fallback.
typo, err = mcp.call('unit_detail', {'unit': 'definitely-not-a-unit.service'})
check('unit_detail of a typo survives the text/plain 404',
      not err and typo.get('http_status') == 404 and 'raw' in typo, str(typo)[:120])

# ===================== ACTION LEG =====================
if RUN_ACTIONS:
    print('\n== action leg ==')
    if not TOKEN:
        check('action leg needs a token', False, 'pass --token or --token-file')
    elif fleet.get('mode') != 'actuate':
        check('action leg needs an armed Roundhouse', False, f"mode={fleet.get('mode')}")
    elif not TARGET:
        check('action leg needs --target', False)
    else:
        preview, err = mcp.call('switch_preview', {'target': TARGET, 'stops': STOPS})
        show('switch_preview', preview)
        ok = check('switch_preview', not err and preview.get('http_status') == 200
                   and bool(preview.get('confirm')),
                   f"status={preview.get('http_status')} error={preview.get('error')}")
        ticked = [c['unit'] for c in preview.get('stop_candidates', []) if c['ticked']]
        check('the ticked stop came back on the preview', ticked == STOPS, str(ticked))

        rid = None
        if ok:
            confirm = preview['confirm']
            execute, err = mcp.call('switch_execute',
                                    {'target': TARGET, 'stops': STOPS, 'confirm': confirm})
            show('switch_execute', execute)
            ok = check('switch_execute with the confirm FROM the preview',
                       not err and execute.get('http_status') == 202
                       and bool(execute.get('rollout_id')),
                       str(execute)[:200])
            rid = execute.get('rollout_id')

        # The warm legs ride the window where the switch holds the operation slot, so
        # the request PARKS instead of firing a second switch — that is the queue
        # behaviour worth asserting, and it is deterministic while a switch is running.
        if rid and WARM_UNIT:
            w, err = mcp.call('warm', {'unit': WARM_UNIT})
            show('warm (slot held by the running switch -> parks)', w)
            check('warm parks behind the running operation',
                  not err and w.get('http_status') == 202 and w.get('queued') is True,
                  f"status={w.get('http_status')} {w.get('error') or w.get('status')}")

            ws, err = mcp.call('warm_state')
            show('warm_state', ws)
            check('warm_state shows the parked request',
                  not err and ws.get('http_status') == 200
                  and (ws.get('pending') or {}).get('unit') == WARM_UNIT,
                  str(ws.get('pending'))[:160])
            # I6: the requester tag defaults to `mcp-<clientInfo.name>`
            check("the parked request is attributed to mcp-drill",
                  (ws.get('pending') or {}).get('requester') == 'mcp-drill',
                  str((ws.get('pending') or {}).get('requester')))

            wc, err = mcp.call('warm_cancel')
            show('warm_cancel', wc)
            check('warm_cancel releases the park',
                  not err and wc.get('http_status') == 200 and wc.get('cancelled') is True,
                  str(wc)[:160])

            ws2, _err = mcp.call('warm_state')
            check('warm_state after cancel: no pending, last says cancelled',
                  ws2.get('pending') is None
                  and (ws2.get('last') or {}).get('disposition') == 'cancelled',
                  str(ws2)[:200])

        if rid:
            deadline = time.time() + 300
            status = {}
            while time.time() < deadline:
                status, err = mcp.call('operation_status', {'rollout_id': rid})
                if status.get('phase') in ('done', 'failed', 'restored', 'restore_failed'):
                    break
                print(f"   ... {status.get('phase')}: {status.get('detail')}")
                time.sleep(2)
            show('operation_status (terminal)', status)
            check('operation_status polled to done',
                  status.get('phase') == 'done', str(status.get('detail'))[:160])

    # The refusal row: warming an UNMARKED, INACTIVE unit must arrive as data.
    if UNMARKED and TOKEN:
        refusal, err = mcp.call('warm', {'unit': UNMARKED})
        show('REFUSAL: warm of an unmarked unit', refusal)
        check('not_on_demand arrives as data (isError:false)',
              not err and refusal.get('http_status') == 422
              and refusal.get('error') == 'not_on_demand',
              f"isError={err} status={refusal.get('http_status')} error={refusal.get('error')}")

mcp.close()

# ===================== READ-ONLY LEG =====================
if READ_ONLY_URL:
    print('\n== read-only leg ==')
    ro = Mcp(READ_ONLY_URL, TOKEN, client_name='mcp-drill-readonly')
    ro.initialize()
    ro_fleet, err = ro.call('fleet_status')
    check('read-only: fleet_status works', not err and ro_fleet.get('http_status') == 200,
          f"mode={ro_fleet.get('mode')}")
    check('read-only: mode says read-only', ro_fleet.get('mode') == 'read-only',
          str(ro_fleet.get('mode')))
    for tool, args in (('port_board', {}), ('deployments', {}), ('routing_config', {}),
                       ('warm_state', {})):
        r, err = ro.call(tool, args)
        check(f'read-only: {tool} works', not err and r.get('http_status') == 200)

    ro_target = ro_fleet['units'][0]['unit'] if ro_fleet.get('units') else 'x.service'
    action_probes = (
        ('switch_preview', {'target': ro_target}),
        ('set_boot', {'unit': ro_target, 'enabled': True}),
        ('warm', {'unit': ro_target}),
        ('warm_cancel', {}),
        ('edit_preview', {'unit': ro_target, 'edits': {'ctx': '4096'}}),
        ('operation_rollback', {'rollout_id': 'none'}),
        ('operation_dismiss', {'rollout_id': 'none'}),
    )
    for tool, args in action_probes:
        r, err = ro.call(tool, args)
        ok = (not err and r.get('http_status') == 403 and r.get('error') == 'read_only_mode')
        check(f'read-only: {tool} -> structured read_only_mode refusal', ok, str(r)[:140])
        if tool == 'set_boot':
            show('REFUSAL: set_boot against a read-only Roundhouse', r)
    ro.close()

print('')
print('========================================')
print(f'Drill results:  passed {len(PASSED)}   failed {len(FAILED)}')
if FAILED:
    for name in FAILED:
        print(f'  {RED}FAILED{NC}: {name}')
print('========================================')
sys.exit(1 if FAILED else 0)
PYEOF
