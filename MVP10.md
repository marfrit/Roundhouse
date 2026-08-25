# MVP10 — OpenArc units as first-class citizens

**A roster that only sees one engine is not a roster; it is a habit.** Since
2026-08-24, `openarc-coder.service` (OpenVINO on the A770, the B5 int4 IR of
qwen3.6-coder) has been the coder production on dirac:8080 — and Roundhouse
could not see it. Its roster showed :8080 as effectively free, and a switch to
`llama-coder` would have stopped production through systemd `Conflicts=`
without Roundhouse knowing what it did. A visibility gap with a tripwire
attached.

MVP10 teaches Roundhouse the engine kind `openarc` end to end: roster, port
board, rungs, switch preflight. Three small steps.

## Part 1 — recognition (T1, `c1ba2bb`)

1. ExecStart basename `openarc` **with a `serve` subcommand** classifies as
   `{kind: 'openarc', variant: 'openvino'}`. Other subcommands (`openarc
   download …`) run and exit; they must not enter the roster.
2. `select_units` accepts the basename, so the unit is managed without a
   `# roundhouse: manage` marker.
3. Param profile: `--port N` was already in the flag table; `--load-models
   NAME` maps to **alias** — OpenArc's model name doubles as the serving alias
   (it is the string a consumer sends as `model`). Default port is **8000**
   for openarc when `--port` is absent; the llama.cpp family keeps 8080.
4. `Conflicts=` joined `KNOWN_KEYS`, so the preflight (Part 3) can read it.
5. `snapshot()` no longer assumes a `-m` flag exists: `model_file` tolerates
   `model_path=None`. OpenArc names its artifact indirectly (an IR directory
   resolved via its own config), so `mem` falls back to cgroup measurement.

## Part 2 — readiness (T2, `5e90531`)

LOADING vs READY, two sources with a clear hierarchy:

1. **Journal fast-positive:** uvicorn's `Application startup complete.` fires
   only after `--load-models` finished compiling (~2 min for the MoE IR — the
   OV compile cache is *forbidden* for it, see ARCstory §B6), so it is a safe
   READY marker. Deliberately **no** busy/req-done patterns: Roundhouse's own
   probe shows up in the access log, and a request-done rule would re-mark an
   auto-unloaded model as ready.
2. **HTTP probe, authoritative both ways:** `_openarc_ready_probe` GETs
   `/v1/models` on loopback; ready iff HTTP 200 and at least one model listed.
   The negative direction is the one journal markers cannot see — OpenArc
   auto-unloads a model that faulted (the `--cache-dir` MoE trap produced
   exactly that) while uvicorn stays up and `active`. Probe verdict False
   drops the unit back to LOADING.
3. Cadence in the 3 s poll loop: every tick while LOADING, every 10th tick
   (~30 s) once READY. Targets are collected under the watcher lock, probed
   outside it, applied under it.
4. The probe is the **one unit-local outbound site** (loopback, read-only) —
   the §8.1 AST guard in `test_server.py` now allowlists exactly
   `{_fetch_peer, _openarc_ready_probe}`.

## Part 3 — Conflicts= surfacing in switch preflight (T3, `7add5a4`)

Starting a target stops every unit it declares `Conflicts=` against **and**
every unit that declares `Conflicts=` against the target — systemd performs
those stops itself, silently. The preflight now appends a notice per affected
ACTIVE unit; un-ticked ones carry "systemd performs this stop regardless".

The live dirac trap is doubly covered: same port, so the port check already
**blocks** (openarc-coder READY on :8080, not ticked → 422) — the notice
covers conflicts on *different* ports (llama-agent:8087), which previously
vanished without a trace. Full switch integration fell out of the existing
mechanics: an ACTIVE openarc unit is a normal stop candidate, appears in
`suggested_stops`, and an executed switch stops it explicitly instead of
letting `Conflicts=` do it behind Roundhouse's back.

## Verification (2026-08-25, both hosts)

- Before: dirac fleet_status 7 units, no openarc-coder, :8080 conflict
  two-way. After: 8 units, openarc-coder READY :8080 alias qwen3.6-coder,
  conflict three-way. Regression diff over the 7 pre-existing dirac units and
  all 22 boltzmann units (volatile fields excluded): **only** the two :8080
  conflict lists gained openarc-coder; two boltzmann rung flaps were restart
  artifacts (BUSY in flight; journal markers lost on Roundhouse restart), not
  parser changes.
- `switch_preview(llama-coder)` on dirac now refuses: port blocker naming
  openarc-coder, conflicts notice, `suggested_stops: [openarc-coder.service]`.
- 910 tests green (`cd mvp1 && python3 -m unittest discover -s tests`).

## Deployment (how the repo reaches /usr/share — documented 2026-08-25)

There is **no package**. The backing repo is `noether:~/src/roundhouse`
(remotes: gitea `marfrit/Roundhouse`, github `marfrit/Roundhouse`); the
installed copy is a root-owned file that must stay byte-identical to
`mvp1/roundhouse.py` at HEAD:

```
scp mvp1/roundhouse.py <host>:/tmp/roundhouse.py.new
ssh <host> 'sudo install -o root -g root -m 755 /tmp/roundhouse.py.new \
                 /usr/share/roundhouse/roundhouse.py \
            && rm /tmp/roundhouse.py.new \
            && systemctl --user restart roundhouse'
md5sum mvp1/roundhouse.py   # compare against the host's /usr/share copy
```

Hosts: **dirac** and **boltzmann** (federated, both `--actuate`). The MCP
wrapper `mvp1/roundhouse_mcp.py` deploys the same way to
`/usr/share/roundhouse/roundhouse_mcp.py` when it changes. `/usr/bin/roundhouse`
is a thin wrapper and rarely changes. Before any restart remember the
`--actuate` gate: `~/.config/systemd/user` (a git repo on each host) must have
no uncommitted changes to *tracked* files, or Roundhouse enters a refusal
restart loop (see memory `reference_roundhouse_mcp`).
