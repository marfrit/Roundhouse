#!/bin/bash
# Roundhouse MVP1 — throwaway-container fixture install.
#
# Runs ON the incus host (boltzmann), from a checkout of this repo:
#     mvp1/scripts/container-setup.sh [CONTAINER_NAME]
#
# Builds the ladder scenarios of SPEC.md §8 inside a Debian container:
#   qwen3.6-coding              normal start, 15 s fake load   -> LOADING -> READY (switch source)
#   llama-server-qwen35-npu     ExecCondition kernel gate kept -> STANDBY (never FAILED)
#   llama-task                  enabled + started, shares :8086 with the gated unit -> armed
#   llama-server-gemma4-q4km    FAKE_EXIT_1 + Restart=on-failure -> FAILED, NRestarts climbing
#   llama-server-fake-b         fake unit B on :8093 (switch target for MVP3 drill)
#   mixperten                   installed byte-for-byte, NEVER enabled/started -> RETIRED
#
# Each unit is a *copy* of the real fixture with only its engine binary token spliced out
# for the fake server; comments, ExecCondition, flags and ports stay byte-identical, so the
# parser sees the real thing. Byte offsets differ from the pristine fixtures — that is fine,
# the offset invariants are proven against docs/fixtures/ by the unit tests.

set -euo pipefail

CONTAINER="${1:-roundhouse-test}"
CUSER=roundhouse
CHOME="/home/$CUSER"
DEST="$CHOME/roundhouse"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

echo "== Roundhouse container setup: $CONTAINER (repo: $REPO) =="

if ! incus info "$CONTAINER" >/dev/null 2>&1; then
    echo "Error: container $CONTAINER does not exist" >&2
    exit 1
fi

echo "-- pushing tree to $DEST"
incus exec "$CONTAINER" -- rm -rf "$DEST/mvp1" "$DEST/docs"
incus file push --create-dirs -rq "$REPO/mvp1" "$CONTAINER$DEST/"
incus file push --create-dirs -rq "$REPO/docs" "$CONTAINER$DEST/"
incus exec "$CONTAINER" -- chown -R "$CUSER:$CUSER" "$DEST"

# The fake engine must be reachable under a name that starts with "llama-server":
# unit selection (D1) and engine detection both key off the ExecStart basename, so a
# script called fake-llama-server.py would be parsed as "not ours" / unknown engine.
echo "-- installing fake engine as llama-server-fake"
incus exec "$CONTAINER" -- install -m 0755 -o "$CUSER" -g "$CUSER" \
    "$DEST/mvp1/scripts/fake-llama-server.py" "$DEST/mvp1/scripts/llama-server-fake"

echo "-- generating fixture-copy units"
incus exec "$CONTAINER" --env "DEST=$DEST" --env "CHOME=$CHOME" --env "CUSER=$CUSER" \
    -- python3 - <<'PYEOF'
import os, shutil, sys

DEST  = os.environ['DEST']
CHOME = os.environ['CHOME']
CUSER = os.environ['CUSER']
sys.path.insert(0, DEST + '/mvp1')
import roundhouse

FIX   = DEST + '/docs/fixtures'
UNITS = CHOME + '/.config/systemd/user'
FAKE  = DEST + '/mvp1/scripts/llama-server-fake'

# (fixture, Environment= to inject, extra [Service] directives, output_name, port_override, on_demand_marker)
SCENARIOS = [
    ('qwen3.6-coding.service',           {'FAKE_LOAD_SECONDS': '15'}, [], None, None, True),
    ('llama-server-qwen35-npu.service',  {'FAKE_LOAD_SECONDS': '5'},  [], None, None, False),
    ('llama-task.service',               {'FAKE_LOAD_SECONDS': '3'},  [], None, None, False),
    ('llama-server-gemma4-q4km.service', {'FAKE_EXIT_1': '1'},
     ['Restart=on-failure', 'RestartSec=2'], None, None, False),
    ('llama-server-gemma4-q4km.service', {'FAKE_LOAD_SECONDS': '10'}, [], 'llama-server-fake-b.service', 8093, True),
]
VERBATIM = ['mixperten.service']          # RETIRED render; never enabled, never started

os.makedirs(UNITS, exist_ok=True)
model_paths, workdirs = set(), set()

for scenario in SCENARIOS:
    name, env, extra = scenario[0], scenario[1], scenario[2]
    output_name = scenario[3] if len(scenario) > 3 else None
    port_override = scenario[4] if len(scenario) > 4 else None
    on_demand = scenario[5] if len(scenario) > 5 else False

    src = os.path.join(FIX, name)
    raw = open(src, 'rb').read()
    unit = roundhouse.parse_unit(src, raw)

    tok = unit.exec_start.engine_argv[0]
    out = raw[:tok.start] + FAKE.encode() + raw[tok.end:]

    # Port override (the switch scenario needs fake B on its own port).
    #
    # This used to be guarded by `unit.known.get('execstart')` — a key `known` does not
    # have (it holds Description/after/type/wantedby/... only), so the guard was always
    # false and the override silently did nothing: fake B shipped on the fixture's own
    # :8087, colliding with the FAKE_EXIT_1 unit and never on the :8093 the drill wants.
    # Substitute unconditionally and FAIL LOUDLY if the flag is not there.
    if port_override:
        import re
        out, n = re.subn(rb'--port\s+\d+', f'--port {port_override}'.encode(), out)
        if n != 1:
            raise SystemExit(f'{name}: expected exactly one --port to override, found {n}')
        # the human-readable port in Description= would otherwise contradict the flag
        out = re.sub(rb'\(port \d+\)', f'(port {port_override})'.encode(), out)

    inject = [f'Environment={k}={v}' for k, v in env.items()] + list(extra)
    marker = b'[Service]\n'
    i = out.index(marker) + len(marker)

    # Inject on-demand marker if needed
    on_demand_line = '# roundhouse: on-demand\n' if on_demand else ''
    out = out[:i] + (on_demand_line + '\n'.join(inject) + '\n').encode() + out[i:]

    unit_file_name = output_name if output_name else name
    open(os.path.join(UNITS, unit_file_name), 'wb').write(out)
    print(f'  {unit_file_name}: engine -> llama-server-fake' +
          (f', port -> {port_override}' if port_override else '') +
          (', on-demand' if on_demand else '') +
          f', {" ".join(inject) or "no extra env"}')

    profile = roundhouse.extract_param_profile(unit.exec_start.engine_argv)
    if profile.get('model_path'):
        model_paths.add(profile['model_path'])
    if unit.known.get('workingdirectory'):
        workdirs.add(unit.known['workingdirectory'])

for name in VERBATIM:
    shutil.copyfile(os.path.join(FIX, name), os.path.join(UNITS, name))
    print(f'  {name}: installed verbatim (never enabled, never started)')

# Stand-in model files so host_artifact.exists / file_id are real, and the
# WorkingDirectory= paths the fixtures declare, so the units can actually start.
for d in workdirs:
    os.makedirs(d, exist_ok=True)
for p in model_paths:
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if not os.path.exists(p):
        with open(p, 'wb') as f:
            f.truncate(1 << 20)
    print(f'  stand-in model: {p}')

for root, dirs, files in os.walk(CHOME + '/.config'):
    for n in dirs + files:
        shutil.chown(os.path.join(root, n), CUSER, CUSER)
PYEOF

echo "-- enabling user lingering (required for user units at boot)"
incus exec "$CONTAINER" -- loginctl enable-linger "$CUSER"

echo "-- checking for git in container"
if ! incus exec "$CONTAINER" -- su -l "$CUSER" -c 'git --version' >/dev/null 2>&1; then
    echo "Error: git not found in container. Install with: incus exec $CONTAINER -- apt-get install -y git" >&2
    exit 1
fi

echo "-- generating roundhouse.service in the container"
incus exec "$CONTAINER" -- install -m 0644 -o "$CUSER" -g "$CUSER" \
    "$DEST/mvp1/roundhouse.service" "$CHOME/.config/systemd/user/roundhouse.service"
incus exec "$CONTAINER" -- sed -i "s|^ExecStart=.*|ExecStart=/usr/bin/python3 $DEST/mvp1/roundhouse.py --serve --actuate --port 8090|" \
    "$CHOME/.config/systemd/user/roundhouse.service"
incus exec "$CONTAINER" -- chown "$CUSER:$CUSER" "$CHOME/.config/systemd/user/roundhouse.service"

echo "-- reloading and arming the :8086 pair"
incus exec "$CONTAINER" -- su -l "$CUSER" -c 'systemctl --user daemon-reload'
# llama-task enabled + running is one half of the :8086 collision; the other half
# (llama-server-qwen35-npu) stays disabled behind its kernel gate. Nothing else is
# enabled: qwen3.6-coding is started by hand during the drill, gemma4-q4km is the
# crash-loop scenario, mixperten must never run.
incus exec "$CONTAINER" -- su -l "$CUSER" -c 'systemctl --user enable --now llama-task.service'

echo
echo "-- installed units"
incus exec "$CONTAINER" -- su -l "$CUSER" -c 'systemctl --user list-unit-files --no-pager | grep -E "llama|qwen|mixpert" || true'
echo
echo "Setup complete."
echo
echo "FOR MVP4 DRILLS (with --actuate enabled by default):"
echo "1. Initialize the git repo in the container (execute as $CUSER in the container unit dir):"
echo "   incus exec $CONTAINER -- su -l $CUSER -c 'cd ~/.config/systemd/user && git init && printf \"%s\\n\" \"*.bak*\" \"*.roundhouse-tmp\" > .gitignore && git add .gitignore qwen3.6-coding.service llama-server-qwen35-npu.service llama-task.service llama-server-gemma4-q4km.service mixperten.service roundhouse.service && git -c user.name=roundhouse -c user.email=roundhouse@\$(hostname) commit -m \"roundhouse baseline: 5 managed units + self\"'"
echo "   (the -c identity flags are load-bearing: a fresh container has no git identity,"
echo "    git refuses the commit, and --actuate then never arms)"
echo
echo "2. Retire any hand-started process, then enable the unit (it owns :8090 from now on):"
echo "   incus exec $CONTAINER -- su -l $CUSER -c \\"
echo "     \"pkill -u $CUSER -f '[r]oundhouse.py --serve' || true\""
echo "   (the [r] bracket is load-bearing: a plain -f 'roundhouse.py --serve' pattern also"
echo "    matches the su/bash wrapper carrying it, so pkill SIGTERMs its own shell)"
echo "   incus exec $CONTAINER -- su -l $CUSER -c \\"
echo "     'systemctl --user daemon-reload && systemctl --user enable --now roundhouse.service'"
echo
echo "3. Find the token:"
echo "   incus exec $CONTAINER -- su -l $CUSER -c 'cat ~/.config/roundhouse/token'"
echo
echo "FOR READ-ONLY (without --actuate):"
echo "  Edit ~/.config/systemd/user/roundhouse.service (remove --actuate from ExecStart)"
echo "  incus exec $CONTAINER -- su -l $CUSER -c 'systemctl --user daemon-reload && systemctl --user restart roundhouse.service'"
echo
echo "Then, from the host: curl http://127.0.0.1:8090/api/units"
