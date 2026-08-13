# MVP4 — load strategy: the enabled checkbox, and systemd as the reconciler

**Milestone 4 answers the reconciliation question by dissolving it.** README Decision 1
asks for a desired-state controller. On a systemd host, desired state for the boot case
already has a home: **unit enablement**. An enabled unit returns at boot because systemd
brings it back; a crashed unit returns because `Restart=` says so. Roundhouse therefore
holds **no desired-state store of its own** — it renders systemd's, and edits it through
one new control: a per-unit checkbox.

> **enabled** — starts on boot (systemd reconciles it there).
> **disabled** — starts manually, via Roundhouse's turntable.

Owner decisions folded in (mfritsche, 2026-08-13): every LLM on a Roundhouse host is a
systemd unit — the unit file **is** the configuration surface, full stop. **systemd only;
no openrc, no init-system abstraction, ever** — the engine-driver contract on this host is
systemd-native by decision, not by accident.

---

## The toggle, precisely

1. Each selected unit shows its load strategy as a checkbox: checked = `UnitFileState`
   enabled ("starts on boot"), unchecked = disabled ("manual via turntable"). RETIRED
   units render no checkbox — structurally unreachable, as everywhere else.
2. Toggling runs a **preflight**:
   - **Enabling into a boot-time bind race is blocked.** If the unit's declared port is
     also declared by another *enabled*, non-RETIRED unit, the enable is refused with the
     claimants named. This mechanizes the fleet's hand-written DO-NOT-ENABLE guards: the
     port board's "armed" class stops being a warning and becomes an interlock.
   - Kernel-gated units may be enabled freely (the `ExecCondition` guards boot — that is
     its job; enabling a gated unit on the wrong kernel yields STANDBY, which is healthy).
   - Disabling is always allowed (it removes a boot claim; it cannot create a race).
3. The toggle executes `systemctl --user enable|disable -- <unit>` — two new verbs in the
   actuation allowlist with the same exact-shape rules, behind the same `--actuate` +
   bearer gate. It does **not** start or stop anything (`--now` is forbidden by shape);
   run state stays the turntable's job. No file writes, no git, no daemon-reload.
4. No operation slot: a toggle is atomic and does not conflict with a running rollout or
   switch. It is auth-gated, preflight-gated, and logged to the SSE stream as an
   `enablement` event; the unit row updates via the normal 3 s tick.

## Drift, rendered not reconciled

Two mismatch states get explicit, neutral rendering (no autonomous action — ever):

| state | rendering |
|---|---|
| enabled + not running | note on the row: `returns at boot` |
| running + disabled | note on the row: `manual — will not survive reboot` |

The fleet audit view: every selected unit's strategy is visible at a glance; the
checkbox column IS the audit that "all LLMs are units with a declared load strategy."

## Roundhouse eats its own dogfood

`roundhouse.service` (the repo's own unit) gets the same treatment: the container demo
stops running as a raw `nohup` process and runs as an enabled user unit; the live-host
install instructions in the unit file are updated to the enable-based flow. Roundhouse
appears on its own board with its own strategy visible.

## Inherited debt this milestone pays down (review defer ledger)

- Unlocked `watcher.snapshot()` calls unified under `watcher_lock` (the MVP2-era wart,
  now in six call sites — one helper, all sites).
- Guard residue: `os.system` / `os.popen` added to the subprocess-gateway guard;
  `pathlib.Path.open` / `.write_text` / `.write_bytes` name-forms added to the
  file-write confinement guard (regression guards, not anti-malice — the docstring
  says so explicitly).
- `START_TIMEOUT_SEC` actually applied to `_start_unit`; `_watch_unit`'s literal 900
  replaced by `WATCH_TIMEOUT_SEC`.
- UI cosmetics from the ledger: GB labels consistent; terminal stepper states keep the
  reached-phase icons honest; token input added to the 44 px selector list.

## Acceptance criteria

- [ ] Checkbox on every selected non-RETIRED unit reflecting live `UnitFileState`;
      RETIRED units render none.
- [ ] Enable into a declared collision with another enabled unit → 422 with claimants
      named (the mixperten DO-NOT-ENABLE case is the fixture: enabling it must be
      refused twice over — RETIRED and collision). Disable always succeeds.
- [ ] Enabling a kernel-gated unit succeeds; row shows STANDBY + `returns at boot`
      (gate note unchanged).
- [ ] The two drift notes render per the table; both appear in the container drill
      (enable a stopped unit; start a disabled unit via the turntable).
- [ ] `run_actuate` accepts exactly `enable|disable -- <selected unit>`; `--now`,
      unit lists, and non-selected/RETIRED units are rejected by shape/membership;
      the AST call-site whitelist grows by exactly one method name.
- [ ] Toggle requires `--actuate` + token (403/401 rows); a toggle performs zero file
      writes and zero git operations (same three-leg proof pattern as the switch).
- [ ] Toggle allowed while a rollout/switch runs (no slot), and vice versa.
- [ ] Container: full toggle drill — enable, reboot the container, unit came back;
      disable, reboot, unit stayed down. (A container reboot is cheap: this row is the
      whole point of the milestone — systemd reconciling per the checkbox.)
- [ ] Debt rows: snapshot-lock unification (no unlocked snapshot() outside
      take_snapshot/backfill), the two guard additions red-tested via seeds, timeout
      constants applied.
- [ ] `roundhouse.service` enabled in the container; demo survives a container reboot.
- [ ] Live boltzmann (operator drill, may remain open at push): flip one real unit's
      checkbox both ways from the phone; verify with `systemctl --user is-enabled`.
- [ ] Runs without a build step; stdlib only; no German; no throughput figures.

## Out of scope (MVP4)

Autonomous reconciliation beyond what systemd itself does; any runtime desired-state
store; `--now` semantics (strategy and run state stay orthogonal); mask/unmask;
system-level (non-user) units; multi-host; openrc or any init-system abstraction
(decided: never); template/instance units.
