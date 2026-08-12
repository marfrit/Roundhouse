# MVP1 — Stellwerk v0.1 (read-only)

**Working name:** *Stellwerk* — the signal box to Roundhouse's engine shed. Its
defining property is **mechanical interlocking**: you physically cannot set two
conflicting routes. That is the missing safety layer here — two units both claiming
`:8085`, a retired unit one `systemctl enable` away from a collision, an edit one
`-c` away from an OOM.

Full design (8 sections, 4 ASCII wireframes): [`docs/design/stellwerk-design.html`](docs/design/stellwerk-design.html).

---

## Scope of MVP1

**A read-only renderer and watcher for llama.cpp systemd units on `boltzmann`.**
No write path at all. Zero risk while trust builds.

> "Renderer" means render **to the screen**, never **from a template**.

**Explicitly NOT in MVP1:** starting/stopping units, editing parameters, git
integration, multi-host, any Roundhouse catalog coupling. Those are v0.2–v0.5.

---

## The five positions this rests on

1. **Systemd unit files stay the single source of truth.** The tool holds no config
   of its own. Ever.
2. **Render to the screen, not from a template.** A generator emitting units from a
   database would silently make the database the truth and the operator's comments
   disposable. Rejected — the units are *documents*, not build artifacts.
   (`mixperten.service` is ~80 lines of measured decision record wrapped around
   9 lines of command.)
3. **Push for process config, Pull for routing metadata** (answers Q2).
4. **The watcher watches; it does not knock.** On a serialized single-user box,
   health is observed **passively**. Active probes only at explicit moments.
5. **The measurement loop proposes; the operator disposes** (answers Q3).

---

## Why nothing off the shelf does this

| prior art | why it does not fit |
|---|---|
| [llama-swap](https://github.com/mostlygeek/llama-swap) (+ its `/ui`), [llama-dash](https://github.com/szabo-agent/llama-dash), [llama-switch](https://github.com/architector1324/llama-switch) | **replace** systemd with their own supervisor + a YAML file. Adopting them discards `ExecCondition` kernel gates, `taskset` pinning, and every measurement comment. |
| [Cockpit](https://cockpit-project.org/), [Systemd-Service-Manager-Web-UI](https://github.com/1999AZZAR/Systemd-Service-Manager-Web-UI) | generic systemd admin — know nothing of llama.cpp semantics (model file, quant, ctx, alias, port, tok/s). |

**Nothing treats systemd units as the source of truth for llama.cpp servers.**
That gap is the whole product.

---

## What MVP1 must do

### 1. Scan and parse
All units in `~/.config/systemd/user/` (23 today). Extract, per unit: model file,
quant + size, `-c` context, `-t`/`-tb` threads, `taskset` pinning, `--alias`,
`--port`, `ExecCondition`, KV-cache flags, sampling flags, `Restart=`, and the
enabled/disabled state.

**Parse the concrete syntax with byte-range tokens.** Do not normalise, do not
reformat, do not round-trip through a model. MVP1 never writes, but the parser it
builds is what v0.3 will splice with — so it must retain byte offsets from day one.

### 2. Render the operator's notes
Comments are **first-class content**, not noise to strip. They carry the measured
decision record:

> `# MTP (--spec-type draft-mtp) was MEASURED ~8% SLOWER on this box (4.86 vs 5.26 tok/s, 2026-07-21)`
> `# NOTE: @coder sends "temperature": 0.2 in the request body, and a request parameter beats a server default`

Render them attached to the parameter they discuss where that is inferable.

### 3. The status ladder (8 rungs)

`OFF · STANDBY · STARTING · LOADING · READY · BUSY · FAILED · RETIRED`

Two rungs matter more than the rest:

- **STANDBY** — `inactive (condition)` from a kernel gate is **normal, not broken**.
  Render as neutral: *"waiting for kernel 6.1.75-npu-port"*. Never red.
- **LOADING** — measured: a 19.7 GiB model takes **~72 s** from restart to serving,
  during which the unit is `active` but `/health` 503s. Without this rung the UI
  looks broken for over a minute.

"Online" is four different things — unit active, port listening, `/v1/models`
answering, actually able to serve. Distinguish them.

### 4. The port board
Show every unit's declared port and **flag collisions between units**, enabled or
not. Two live examples, both verified 2026-08-12:

| port | units | state |
|---|---|---|
| `:8085` | `qwen3.6-coding` (enabled, active) · `mixperten` (disabled, `[RETIRED]`) | guarded by a `DO NOT ENABLE` comment |
| `:8086` | `llama-task` (enabled, active) · `llama-server-qwen35-npu` (disabled, kernel-gated) | **unguarded** — harmless only because *both* the disable and the kernel gate hold |

The `:8086` pair is the case that justifies the feature: harmless today, a boot-time
race the moment that unit is enabled before an NPU-kernel boot.

### 5. Memory fit
One big model fits at a time (~30 GB box, models 15–20 GiB). Show a gauge and warn
before a config cannot fit.

**Measured beats modeled.** Formulas lie here — the hybrid SSM KV layout is unusual,
and ARM repack allocates a *full anonymous copy* beside the mmap: a 19.7 GiB model
peaked at **30.5 GB of 31.6 GB** and prefill collapsed 18.4 → 9.9 tok/s while decode
was untouched. MVP1 displays measured peaks where known and labels estimates as
estimates.

**Never surface `llama-bench tg128`-style numbers.** Measured 8.03 t/s vs **7.67 t/s**
on a real request — `tg` generates from a near-empty context. Whole-prompt wall clock
only.

---

## Shape

One Python file (house style of `/opt/llm-proxy.py`) + one static page, **SSE** for
live updates, talking to systemd via `systemctl --user` / user D-Bus. Runs as a
systemd user unit, `stellwerk.service`, on a free port (`:8090` unclaimed today —
the port board should confirm its own). **No node toolchain, no build step.**

Fleet scope: **boltzmann only.** The design generalises (host column, N unit dirs),
but multi-host before the round-trip is trustworthy multiplies risk, not value.

---

## Acceptance criteria

- [ ] Parses all 23 units without error; unknown directives preserved, never dropped.
- [ ] Byte offsets retained for every extracted token (v0.3 depends on this).
- [ ] `llama-server-qwen35-npu` renders **STANDBY**, not FAILED, on the current kernel.
- [ ] The `:8086` collision is visible on the port board without being told about it.
- [ ] Comments render as operator's notes, verbatim, none lost.
- [ ] A restart of `qwen3.6-coding` shows **LOADING** for its ~72 s, then READY.
- [ ] Zero write path: no `systemctl start/stop/enable`, no file writes. Verifiable by inspection.
- [ ] Runs without a build step.

**Test fixtures** — [`docs/fixtures/`](docs/fixtures/), four real units chosen to be nasty:

| fixture | why it is hard |
|---|---|
| `mixperten.service` | 95 lines, mostly prose; `[RETIRED]` marker; multi-line `ExecStart`; `-Cr`/`-Crb` CPU-affinity flags |
| `qwen3.6-coding.service` | 52 lines; embedded JSON in `--chat-template-kwargs` (`'{"enable_thinking":false}'`); `%%`-escaped percent signs; measurement comments |
| `llama-server-qwen35-npu.service` | `ExecCondition` kernel gate — the STANDBY case |
| `llama-task.service` | the small, boring one; the other half of the `:8086` collision |

---

## The three Roundhouse questions

**Q1 · Who owns the truth?** *Two truths, two owners, one boundary.* `llm-proxy`
owns **catalog** truth (what exists, what it costs, what it can do). Stellwerk owns
**process** truth on the serving host (what is up, on which port, which ctx, which
state) — because that truth *is* the systemd state it renders. The boundary is
**one-way**: Stellwerk publishes a read-only `/status` JSON that `llm-proxy` *may*
consume to mark its 4 `[local]` entries live or dead instead of guessing. Stellwerk
never pushes routing decisions and never reads the catalog to decide anything.
Consumers keep talking to the gateway. *(v0.5 — not MVP1.)*

**Q2 · Push or Pull?** **Both, split by purpose.** *Push* for process config — a host
must boot from its own disk, and drift belongs in git. *Pull* for selection metadata
— a request already implies the gateway is reachable, so asking `/v1/models` at
request time adds no new dependency. Stellwerk is the Push horn done right, with the
"generator" temptation explicitly rejected.

**Q3 · May the loop influence the ladder?** **Close it through the operator, not
around him.** `bullpen-evals` verdicts feed a recommendation inbox — *"rung 2 solved
14/15 tasks that escalated to rung 3 last month — demote the default?"* One click
applies; every application is a commit with the evidence linked. What makes
self-reconfiguration undebuggable is not feedback, it is **unattributed change**. The
unit files already set the standard: every decision carries a date, a measurement and
a signature. Hold the loop to it. Fully automatic re-rating is a separate, later
decision. *(v0.5 — gated on the `/v1/models` enrichment landing first.)*

---

## Roadmap after MVP1

| stage | content |
|---|---|
| **v0.2** | operations — start/stop/restart/enable with port+alias **interlocks**; switch flow; git init + absorb the 7 `.bak` files; journal tail |
| **v0.3** | **the round-trip** — splice edits, diff preview, readback verified against `systemctl show -p ExecStart` that *refuses rather than corrupts*, compare-and-swap against hand edits, stale-prose flagging. Ships only after the parser has proven itself on all 23 files. |
| **v0.4** | memory intelligence — cgroup `memory.peak` per (unit, model, ctx) in sqlite; prefill-collapse tripwire |
| **v0.5** | Roundhouse hooks — `/status` for llm-proxy; bullpen-evals inbox |

---

## Open questions for the operator

1. **Auth** — LAN-only bind like the llama-servers, or tailnet/basic-auth? From v0.2
   it can stop and start models, so "whatever `:8085` does" may not be enough.
2. **Git location** — repo directly in `~/.config/systemd/user/`, or a mirror repo
   with the unit dir symlinked in? Direct is simpler.
3. **Naming** — *Stellwerk*, or another railway-family name? The metaphor is
   load-bearing in the UI copy ("turn the table", "standing by"), so settle it before
   implementation.
