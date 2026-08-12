# MVP1 — the boltzmann driver + roster (working name: *Stellwerk*)

**This is Milestone 1 made concrete on one host.** Per the README: *"Driver + roster
(read-only): the engine-driver interface plus a thermal roster that senses hot/warm/cold
across hosts. No actuation yet; this proves the sensing model."*

MVP1 implements exactly that for **boltzmann** — a read-only renderer and watcher over
its llama.cpp systemd units. No actuation, no write path.

Full design (8 sections, 4 ASCII wireframes): [`docs/design/stellwerk-design.html`](docs/design/stellwerk-design.html).
It was written before this README existed and is scoped as a standalone tool; read it
for the mechanisms, not for the architecture. **§Reconciliation below overrides it where
they disagree.**

---

## Why boltzmann needs a direct driver, not llama-swap

The README puts llama-swap in the actuation layer *"where it fits"*, and adds: *"A host's
own engine driver can actuate directly where llama-swap doesn't apply."* **Boltzmann is
that case**, for the same reason ds4 is:

| boltzmann unit carries | llama-swap's YAML model |
|---|---|
| `ExecCondition=[ "$(uname -r)" = "6.1.75-npu-port" ]` — NPU unit goes `inactive (condition)`, not `failed`, on the wrong kernel; no restart storm | no equivalent — a gate becomes a crash-loop |
| `taskset -c 4-7` — pin to the four A76 cores; the other four are A55s | expressible in the command, but the *reason* is not |
| ~80 lines of comments encoding measured decisions (`"MTP was MEASURED ~8% SLOWER (4.86 vs 5.26 tok/s)"`, `"@coder sends temperature 0.2 and a request parameter beats a server default"`) | discarded on migration |
| two units deliberately sharing `:8085`, one marked `[RETIRED]` with a DO-NOT-ENABLE guard | no concept of a unit that must not run |

`mixperten.service` is ~80 lines of decision record wrapped around 9 lines of command.
Migrating it to YAML throws away the 80 and keeps the 9.

**So: boltzmann gets a direct engine driver whose actuation substrate is systemd.** That
is not a rejection of llama-swap — it is the README's own escape hatch, used where it was
meant to be used. Hosts where llama-swap fits should still use it.

---

## What this driver contributes to the contract

README Decision 2 requires each engine driver to implement: supported artifact formats
and hardware, a parameter schema, a memory estimator, a launch command, a health check,
and lifecycle verbs. MVP1 delivers the **read-only half** and proves the shape:

| driver capability | MVP1 status |
|---|---|
| supported formats / hardware | GGUF on aarch64 CPU + RK3588 NPU (kernel-gated) |
| **parameter schema** | **derived by parsing real units** — see below. This is the interesting part. |
| **memory estimator** | measured-first, formula-fallback — see §Feasibility |
| health check | passive; 8-rung ladder mapped to the roster |
| launch command | *observed* in MVP1 (read from `ExecStart`), not issued |
| lifecycle verbs | **not in MVP1** — that is Milestone 2 |

### The parameter schema comes from the units, not a hardcoded list

The units already *are* the ParamProfile: `-c 65536`, `-t 4`, `-fa on`, `-ctk q8_0`,
`-ctv q8_0`, `--jinja`, `--chat-template-kwargs '{"enable_thinking":false}'`, sampling
flags. MVP1 parses them into a schema instance per Deployment. This is the concrete test
of whether "the params UI is generated, not hardcoded" survives contact with a real host.

---

## Reconciliation with the standalone design

The Stellwerk design takes three positions that must be **re-scoped** now that Roundhouse
owns the fleet layer. Where they conflict, this file wins:

| design says | inside Roundhouse |
|---|---|
| "Systemd unit files stay the single source of truth. The tool holds no config of its own. Ever." | **True for boltzmann's process config, and only that.** Roundhouse holds desired state; systemd holds observed state for this host. The driver reports observed, it does not own desired. |
| Push (units in git) vs Pull, framed as a fleet-wide answer | Applies to **this host's process config** only. Fleet routing is Pull via LiteLLM per the README. Not a global ruling. |
| A `/status` JSON that llm-proxy may consume | **Superseded.** The roster is Roundhouse's, and routing config is generated for LiteLLM. The driver reports upward to Roundhouse; it publishes nothing sideways. |
| "Renderer means render to the screen, never from a template" | **Keep this, and note the tension.** Roundhouse *will* eventually generate config (Milestone 5, LiteLLM). It must not generate boltzmann's unit files. That asymmetry is deliberate and worth defending: generated files are build artifacts, these units are documents. |

**Open, and worth an explicit decision before Milestone 2:** once the reconciliation loop
can actuate, does it edit unit files, or only `systemctl start/stop`? The design's answer
(splice bytes, never rewrite prose, verify readback, refuse rather than corrupt) is the
mechanism if the answer is "yes". If the answer is "no, ParamProfiles live in Roundhouse
and units become thin", the round-trip work disappears — and so does the decision record.

---

## The roster, from a systemd host

README roster: `configured → loading → hot ⇄ warm → cold`, plus `load-failed` and
`unavailable`. The design's 8-rung ladder maps onto it and adds two rungs this host needs:

| ladder rung | roster | why it is needed here |
|---|---|---|
| `OFF` | configured (cold) | unit disabled/stopped |
| **`STANDBY`** | **configured, not schedulable** | `inactive (condition)` from a kernel gate is **correct and healthy**, not a failure. Render neutral: *"waiting for kernel 6.1.75-npu-port"*. Never red. Roundhouse must not try to place onto a gated unit. |
| `STARTING` | loading | process up, model not mapped |
| **`LOADING`** | loading | **measured ~72 s** from restart to serving on a 19.7 GiB model, during which the unit is `active` but `/health` 503s. Without this rung the UI looks broken for over a minute. |
| `READY` | hot | serving |
| `BUSY` | hot | mid-request — this box serves **one at a time** |
| `FAILED` | load-failed | |
| `RETIRED` | (not a Deployment) | `[RETIRED]` units are documentation, never placement targets |

**The watcher does not knock.** Boltzmann is single-user and serialized. Sense passively —
tail the journal, read systemd state. **No periodic health polls**, no completion probes:
an active probe steals the box from a real request. Probe only at explicit moments.

---

## Feasibility: measured beats modeled

README Decision 4 wants `weights + KV-cache(context × cache-type) + engine overhead`
predicted before placement. On this host the formula **lies**, twice over:

1. **ARM runtime repack allocates a full anonymous copy beside the mmap.** A 19.7 GiB
   model peaked at **30.5 GB of 31.6 GB**; `RssAnon` 20.8 GiB against `RssFile` 11.2 GiB
   *falling*. `repack.cpp` covers `Q4_K/Q6_K/Q8_0` but **not** `IQ3_XXS/IQ4_XS`, so two
   models of equal file size have wildly different residency.
2. **The hybrid SSM + attention KV layout** (`qwen35moe`: 30 state-space blocks, 11
   attention) does not match a standard KV formula.

Measured consequence: prefill collapsed **18.4 → 9.9 tok/s** while decode was untouched —
i.e. **the failure mode is a prefill penalty, invisible in any decode benchmark**, and it
occurred in **2 runs of 3**.

So this driver's estimator records **cgroup `memory.peak` per (unit, model, ctx)** in
sqlite and returns measurements where it has them, formulas only as a labelled fallback.
Stall count for boltzmann is effectively **one big model at a time** (~30 GB, models
15–20 GiB).

**Never surface `llama-bench tg`-style numbers.** `tg128` reads **8.03 t/s** where the
real request gives **7.67** — `tg` generates from a near-empty context. Whole-prompt wall
clock only.

---

## The port board

Show every unit's declared port, and flag collisions **between units, enabled or not**.
Two real cases, verified 2026-08-12:

| port | units | state |
|---|---|---|
| `:8085` | `qwen3.6-coding` (enabled, active) · `mixperten` (disabled, `[RETIRED]`) | guarded by a DO-NOT-ENABLE comment |
| `:8086` | `llama-task` (enabled, active) · `llama-server-qwen35-npu` (disabled, kernel-gated) | **unguarded** |

The `:8086` pair justifies the feature: harmless *only* because both the disable **and**
the kernel gate hold. Enable that unit, boot the NPU kernel, and you get a bind race.
This is the "mechanical interlocking" the design is named for, and it is a **sensing**
feature — it belongs in Milestone 1.

---

## Shape

One Python file (house style of `/opt/llm-proxy.py`) + one static page + **SSE**.
Talks to systemd via `systemctl --user` / user D-Bus. Runs as `stellwerk.service` on
`:8090` (unclaimed today — the port board should confirm its own). **No node toolchain,
no build step.** Scope: **boltzmann only** — multi-host is Roundhouse's job, not this
driver's.

---

## Acceptance criteria

- [ ] Parses all 23 units; unknown directives preserved, never dropped.
- [ ] **Byte offsets retained per extracted token** — a future round-trip splices bytes; a parser that normalises forecloses that option.
- [ ] Comments render verbatim as operator's notes; none lost.
- [ ] `llama-server-qwen35-npu` renders **STANDBY**, not FAILED, on the running kernel.
- [ ] The `:8086` collision is visible without being told about it.
- [ ] A `qwen3.6-coding` restart shows **LOADING** for its ~72 s, then READY.
- [ ] Emits a per-Deployment record shaped for the README's entity spine (Artifact → HostArtifact → Deployment = Artifact + Engine + ParamProfile + Host + LoadStrategy).
- [ ] Zero write path — no `systemctl start/stop/enable`, no file writes. Verifiable by inspection.
- [ ] Runs without a build step.

**Fixtures** — [`docs/fixtures/`](docs/fixtures/), four real units chosen to be nasty:

| fixture | why it is hard |
|---|---|
| `mixperten.service` | 95 lines, mostly prose; `[RETIRED]`; multi-line `ExecStart`; `-Cr`/`-Crb` affinity flags |
| `qwen3.6-coding.service` | embedded JSON in `--chat-template-kwargs`; `%%`-escaped percent signs; measurement comments |
| `llama-server-qwen35-npu.service` | `ExecCondition` kernel gate — the STANDBY case |
| `llama-task.service` | the boring one; other half of the `:8086` collision |

---

## Hard rail

> **KEIN Paid-Offloading, nie.** No paid offloading, ever.

Structural for this driver: it manages **local units only**, so it cannot offload. Where
catalog context is ever shown, `[$]` entries are inert text — never an action, never a
fallback target. Assert it in code, not only in prose.

---

## Open questions

1. **Auth** — LAN-only bind like the llama-servers, or tailnet/basic-auth? From Milestone 2 it can stop models, so "whatever `:8085` does" may not suffice.
2. **Unit-dir git** — repo directly in `~/.config/systemd/user/`, or a mirror with the dir symlinked in? (Seven `.bak` files there are doing version control's job today.)
3. **Naming** — *Stellwerk* for the signal box, or fold it into Roundhouse's vocabulary as "the boltzmann driver"?
4. **The Milestone 2 question above** — does reconciliation edit unit files, or only start/stop them?
