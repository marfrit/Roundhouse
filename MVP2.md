# MVP2 — human-initiated rollouts: edit params, roll out, watch to READY

**Milestone 2 made concrete on boltzmann, scoped to the operator's hand.** The vLLM-style
admin flow: observe running services (MVP1), edit the configured runtime parameters in a
generated form, and roll out — stop the old process, splice the edit into the unit file,
`daemon-reload`, start, and watch the roster carry it to READY.

**The boundary that makes this safe:** every mutation is a deliberate human action through
the UI. There is **no autonomous reconciler in MVP2** — nothing edits parameters or starts/
stops units on its own. The loop (desired-state reconciliation, README Decision 1) remains
Milestone 3+, and when it arrives it gets lifecycle verbs only, never the splice machinery.

---

## What MVP1 already provides

- ParamProfile per unit with a **byte span per parameter** (`ctx -> {flag:[s,e], value:[s,e]}`),
  including `unknown_flags` with spans — the edit form is *generated* from this (README
  Decision 2), never hardcoded. Unknown flags render as raw text inputs, not uneditable rows.
- Span-exactness proven by property tests over the whole corpus (433 tokens slice exactly) —
  the precondition for splice-writes.
- The watcher/roster: LOADING with elapsed counter, READY, FAILED, the port board, MemStore
  measured peaks. This is the rollout's feedback half, already built and live-tested.

## The rollout, precisely

1. Operator opens a unit's detail pane, clicks **edit**. The form shows one input per
   ParamProfile field **with the unit's operator comments rendered adjacent** — you see the
   *why* you are about to override.
2. Submit runs **pre-flight** (server-side, hard failures, no override):
   - port change → checked against the **declared**-port board (all parsed units, any enable
     state) — never against listening sockets. A collision with anything not `[RETIRED]` rejects.
   - ctx / KV-type / model change → memory estimate (MemStore measured-first) vs `MemAvailable`
     plus the resident set of currently-READY units; exceeding it rejects with the numbers.
   - the edited unit `[RETIRED]` → reject outright. Gated (STANDBY) → allowed to edit, with
     a notice that it cannot be started on this kernel.
3. **Preview**: the exact unified diff of the splice (old bytes → new bytes per span, plus the
   provenance comment). Nothing is written until the operator confirms the diff.
4. **Apply**: stop unit (if active) → splice value bytes at the recorded spans → append one
   provenance line (`# roundhouse: <ISO date> <field> <old> -> <new> via UI`) → re-parse and
   verify the file: new ParamProfile differs in exactly the edited fields, comment set is
   unchanged (minus the appended line), all span invariants hold. **Any verify failure restores
   the original bytes and aborts before daemon-reload.**
5. `systemctl --user daemon-reload` → start → the roster watches: READY = rolled out
   (record load_seconds + new measured peak); FAILED, or LOADING past `TimeoutStartSec`
   with `no_ready_marker` → offer **one-click rollback** (restore prior file from git,
   daemon-reload, start the old config, watch again).

## Git is the unit dir's memory (resolves MVP1 open question 2)

`~/.config/systemd/user` becomes a git repository (init in place; `.gitignore` covers
`*.bak*` and non-unit noise). Every apply is one commit with a generated message
(`roundhouse: qwen3.6-coding -c 65536 -> 32768`); rollback is a revert of that commit.
The seven `.bak` files were version control's job — after MVP2 lands, new `.bak`s stop
appearing (existing ones are left alone). Roundhouse commits with its own author string
(`roundhouse <roundhouse@<host>>`) so `git log` separates tool edits from human edits.

## The actuation gate (ends the zero-write era, deliberately)

- MVP1's `run_ro` stays. A sibling gateway `run_actuate` allows exactly:
  `systemctl --user stop/start/daemon-reload -- <selected unit>` and the splice-write +
  git commit inside the unit dir. Nothing else — no enable/disable (load strategy stays
  human), no edits outside the unit dir, no restart verb (stop→start keeps the state
  machine honest).
- The whole actuation layer is **armed only by an explicit `--actuate` launch flag**;
  default launch is byte-for-byte MVP1 read-only behavior. UI shows which mode it is in.
- **Auth** (MVP1 open question 1, now due): mutating routes require a bearer token read
  from a **mode-600 file** (`~/.config/roundhouse/token`, generated on first `--actuate`
  launch; never in argv, per house rule). Read routes stay open on the LAN as in MVP1.
  The UI stores the token in memory only (operator pastes it once per session).
- `[RETIRED]` units are structurally unreachable by every actuation path — asserted in
  code, tested. The paid-offload rail extends to edits: a spliced value may not introduce
  a remote scheme into ExecStart.

## Acceptance criteria

- [ ] Edit form is generated from ParamProfile spans; every field of `qwen3.6-coding.service`
      renders as an input; unknown flags render as raw inputs; operator comments visible
      alongside the form.
- [ ] Splice-write: after an apply, the file differs ONLY in the edited value bytes plus the
      one provenance line; comments byte-identical; re-parse passes all span invariants.
      Proven by tests on fixture copies for: `-c` (numeric), `--port`, `-ctk` (enum-ish),
      the embedded-JSON `--chat-template-kwargs` token, and a flag on a continuation line
      of a multi-line ExecStart (mixperten shape).
- [ ] Verify-or-restore: a corrupted splice (fault injected in test) restores the original
      file bit-exactly and never reaches daemon-reload.
- [ ] Pre-flight rejects: a port change onto a declared claim (any enable state); a ctx
      increase whose estimate exceeds available memory; any edit to a `[RETIRED]` unit.
- [ ] Full rollout in the container: edit fake unit's ctx → diff preview → apply → stop/
      splice/reload/start → roster shows LOADING → READY; git log shows the commit;
      /api/mem shows a fresh measured row.
- [ ] Rollback path in the container: rollout onto `FAKE_EXIT_1` → FAILED → one-click
      rollback → old config READY again; git log shows the revert.
- [ ] Without `--actuate`: every mutating route returns 403, the UI shows read-only mode,
      and the process provably cannot write (MVP1's zero-write guards still pass against
      the default launch mode).
- [ ] With `--actuate` but no/wrong token: 401; nothing written.
- [ ] Live on boltzmann (operator-authorized drill): one real rollout on
      `llama-server-gemma4.service` (:8093, model on disk) — edit a parameter, roll out to
      READY, roll back, restore fleet state (`qwen3.6-coding` READY at the end).
- [ ] Runs without a build step; stdlib only; no German; no throughput figures surfaced.

## Out of scope (MVP2)

Autonomous reconciliation of any kind; enable/disable (load-strategy changes); creating or
deleting unit files (new-profile-as-new-file remains a human/bullpen authoring act — the UI
may link to the pattern but not perform it); multi-host anything; drop-in parsing; editing
non-selected units; llama-swap integration.
