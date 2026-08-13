# MVP3 — the turntable: model switching, and a mobile view

**The reason this project exists.** The roundhouse metaphor finally earns its name: the
operator picks which model should occupy the stall, and Roundhouse rotates it in —
stop what the operator ticked, start the target, watch the roster carry it to READY.
One tap from a phone, which is why this milestone also ships the mobile view.

Same boundary as MVP2: **every switch is a deliberate human action.** No autonomous
reconciler, no auto-eviction, no policy engine. Roundhouse *suggests* (it knows the
measured peaks), the operator *decides*, the machine *executes and narrates*.

---

## The switch, precisely

1. Operator taps a startable unit (OFF; not `[RETIRED]`; kernel gate satisfied) and
   chooses **switch to this**.
2. **Preview** (server-side, same auth as MVP2 rollouts):
   - Roundhouse lists the currently-active selected units with their measured residency
     (cgroup `memory.current`, fallback labelled) as stop candidates. **Nothing is
     preselected by policy**; the UI *suggests* ticking the largest resident model when
     the target does not fit, but the operator ticks the boxes.
   - Memory arithmetic per E9 discipline: `estimate(target)` (measured-first, formula
     fallback, source always labelled) vs `MemAvailable + Σ freed(ticked stops)`
     + 1 GiB headroom. Doesn't fit → the preview says so with the numbers; the switch
     cannot be submitted until it fits.
   - Port check: the target's declared port must not be bound by any unit that will
     still be running after the ticked stops. Declared-board collisions with stopped
     units are shown as notices, not blockers.
   - The preview returns a confirm hash binding `(target, ticked stops, the plan)` —
     same staleness discipline as MVP2 E5: state drifts between preview and execute →
     409, re-preview.
3. **Execute**: sequential `stop` for each ticked unit (roster confirms each reaches
   OFF) → `start` target → watch to READY with the full stepper (LOADING elapsed,
   `no_ready_marker`, FAILED). No unit file is written; a switch is lifecycle verbs
   only — `daemon-reload` is not part of it.
4. **Failure → offer the reverse**: target FAILED or `no_ready_marker` → one-click
   **restore**: stop the target (if up), restart exactly the units that were stopped in
   step 3, watch them back to READY. Same offer/dismiss semantics as MVP2 rollback.
5. Switches and rollouts share **one operation slot** (E6 generalizes): no concurrent
   actuation of any kind. A switch while a rollout holds the slot → 409, and vice versa.

## Auth and arming: unchanged

`--actuate` + bearer token gate switches exactly as they gate rollouts. The default
launch stays read-only. A switch performs no file writes and no git operations —
**but the arming gate stays global and unchanged**: one gate, one mental model. (The
unit-dir git requirement is part of arming, not of the switch path; do not special-case
it away.)

## The mobile view

The page must be genuinely usable on a phone (390 px wide) — not merely unbroken:

- Single column; sections stack. On small screens OFF/RETIRED/port-board collapse by
  default; ACTIVE and the operation stepper stay visible.
- Sticky compact header: host · mode badge · mem gauge · sensing banner.
- Touch targets ≥ 44 px for every actionable element (unit rows, switch/edit buttons,
  stepper actions, tick boxes).
- The detail pane and the edit/switch previews become full-screen overlays on small
  widths, with an explicit close.
- No horizontal body scroll at 390 px; wide content (operator notes, diffs, the port
  strip) scrolls inside its own container.
- The stepper compacts to icons + current phase text on small widths.
- Same single file, inline CSS/JS, no framework, textContent-only. The desktop layout
  must not regress (the two-column edit form stays at wide widths).

## Inherited debt this milestone pays down (from the MVP2 review defer list)

- Test-guard debt: the §9.1 AST call-site whitelist (`ROLLOUT_CALLSITES` actually
  enforced, extended to the switch engine's call sites), the frozen POST route table
  completeness check, and the §9.9 file-write confinement guard.
- UI defects: gate-notice condition checks `gate.kind === 'STANDBY'` (kind is
  `'kernel'` — never renders); `state.rollout = data` on 202 stores only the id;
  `.gitignore` startup warning only checks `*.bak*` (should also check
  `*.roundhouse-tmp`).

## Acceptance criteria

- [ ] Switch preview: stop candidates listed with labelled residency; memory
      arithmetic with sources; unsubmittable while it does not fit; confirm hash
      staleness (409 on drift).
- [ ] Ineligible targets refused at preview AND execute: `[RETIRED]` (422), gate
      unsatisfied (422 with the gate detail), already-active (422).
- [ ] Container: full switch — fake unit A READY, switch to fake unit B ticking A →
      SSE shows stop(A confirmed OFF) → start(B) → LOADING → READY; A's port freed; no
      git commit created (a switch writes nothing).
- [ ] Container: failed switch (target = `FAKE_EXIT_1` unit) → offer restore →
      restore brings A back to READY; dismiss also frees the slot.
- [ ] Slot exclusivity both directions: switch during rollout → 409; rollout during
      switch → 409.
- [ ] 403 unarmed / 401 bad token on all switch routes; zero writes in a switch
      (no new git commits, no file mtime changes on unit files).
- [ ] Guard debt: the three test guards implemented and failing on seeded violations
      (temporarily seed, confirm red, unseed); the two UI defects fixed.
- [ ] Mobile: at 390×844 (phone) and 768×1024 (tablet) — no horizontal body scroll;
      ACTIVE + stepper visible without scrolling on load; all actionable elements
      ≥ 44 px (asserted by a static CSS/markup test where possible, and a manual
      container check over the LAN from a phone-sized viewport); desktop layout
      unchanged at ≥ 1200 px.
- [ ] Live boltzmann (operator drill, may remain open at push like MVP2's):
      `scripts/switch-drill.sh` — via the UI from a phone: switch qwen3.6-coding →
      llama-server-gemma4 (:8093) → back, fleet ends with qwen3.6-coding READY.
- [ ] Runs without a build step; stdlib only; no German; no throughput figures.

## Out of scope (MVP3)

Autonomous anything (reconciliation, auto-eviction, scheduling); pinned/protected unit
metadata (the operator's ticks ARE the protection); multi-host; llama-swap; on-demand
autoscale; queueing more than one operation; PWA/service-worker/offline; dark/light
theming beyond the existing palette.
