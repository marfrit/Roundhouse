# Roundhouse MVP3 — Build Architecture & Work Breakdown

**File: `mvp1/MVP3-SPEC.md`** (beside `roundhouse.py`, which it governs; `MVP3.md` at repo root stays the contract — its acceptance checklist is the definition of done, its Out-of-scope list is binding).

Grounded in: `MVP3.md` (contract), `MVP2.md` + `mvp1/MVP2-SPEC.md` (E1–E12 stand; nothing here re-opens them), `mvp1/roundhouse.py` @ 66cbfec (4757 lines; Section order in file: A parser → B watcher → C server → E actuation (two banners: line 2689 and "PART 2" line 3442) → D main), `mvp1/static/index.html` (1621 lines), `mvp1/tests/` (196 green). Recon findings that shaped this spec: **(1)** the S8 debt is real and precisely located — `test_actuate_armed_assignment_once` asserts only `len ≥ 1`, `test_post_routes_require_bearer` is a `pass` placeholder, `ROLLOUT_CALLSITES` is defined in `TestWriteGuards` but never enforced, and there is no write-verb-section guard, no route-table completeness check, no file-write confinement guard. **(2)** The snapshot unit row does NOT expose `ExecMainStartTimestampMonotonic` (only the unstable `since`, which is `now()` for OFF units) — the F3 fingerprint needs a new snapshot key. **(3)** `tests/test_watcher.py::test_snapshot_shape` freezes the exact unit-row key set and must be updated in the same task that adds the key. **(4)** `mvp1/scripts/switch-drill.sh` already exists but is the MVP1-era manual `systemctl` script — MVP3 replaces its content (UI-driven drill). **(5)** `ACTIVE_RUNGS` is currently local to `classify_port_claims` (line 1184) and gets hoisted.

## GLOBAL DECISIONS (F-series; implementers must not re-open them)

- **F1 — One engine, one slot, one record dict; NO rename.** The switch is implemented inside the existing `RolloutEngine` class as a second operation kind. No `OperationEngine` rename, no sibling class. Rationale, weighed as instructed: the AST call-site whitelist stays exactly `{'_stop_unit', '_start_unit', '_daemon_reload'}` because the switch performs stop-multiple + start-target **through the same three methods** — a sibling class would either duplicate them (whitelist grows, guard weakens to name-matching across classes) or share them awkwardly; and a rename would churn `cmd_serve` wiring, `self.server.rollout_engine`, `rollout_public_record`, the snapshot `"rollout"` key, and dozens of green MVP2 tests for zero contract value, executed by Haiku-class coders. The class docstring is updated to "operation engine (kinds: rollout, switch)". Records gain `"kind": "rollout"|"switch"`; switch ids use prefix `sw-` (`sw-<epoch-int>-<seq>`, same counter). Both kinds live in `self.rollouts` and compete for `self.current` — E6 generalizes for free.
- **F2 — Switch phases** `preflight → stopping → starting → watching → done`; any phase → `failed`; `failed → restoring → restored | restore_failed`. Stops are **sequential** in submitted order (one box, deterministic narrative, and sequential stops mean each freed-memory increment is real before the next stop). "Confirmed OFF" = after `run_actuate(stop)` returns, poll the roster (1 s interval) until the unit's rung ∉ `{STARTING, LOADING, READY, BUSY}` in a sample with `sensed_at` > the moment the stop command returned; rung `FAILED` after a stop counts as stopped (process dead, memory freed) but appends a notice to the record detail. Timeouts (module constants, §2.3): stop command 150 s per unit (MVP2's `applying` precedent — a big-model stop can take ~90 s; `_stop_unit` now passes `timeout=STOP_TIMEOUT_SEC`, which also fixes the rollout path's inconsistency where `applying` budgeted 150 s but `run_actuate` defaulted to 90 s), roster-confirm 30 s per unit (10× the 3 s tick), start 30 s, watch cap 900 s (hoisted from the literal in `_watch_unit` to `WATCH_TIMEOUT_SEC`, shared).
- **F3 — Switch confirm hash covers the whole fleet's lifecycle state.** `confirm = sha256(canonical_json({"kind": "switch", "target": name, "stops": sorted(ticked names), "fingerprint": {unit: ts_mono_str for every non-retired selected unit}})).hexdigest()` where `canonical_json` = `json.dumps(obj, sort_keys=True, separators=(",", ":"))` and `ts_mono_str` = the unit's `ExecMainStartTimestampMonotonic` as a string, with `None`/`''`/absent canonicalized to `"0"`. Fingerprinting **all** units (not just target+stops) is deliberate: any unit starting or stopping between preview and execute invalidates the memory arithmetic and the runtime port picture, so it must 409. `MemAvailable` is deliberately **outside** the hash (it fluctuates continuously; execute re-runs the full preflight anyway — same reasoning as E5's timestamp exclusion). Mismatch at execute → `409 preview_stale`.
- **F4 — Preview is server-computed and complete; the UI stays dumb.** `POST /api/switch/preview` returns stop candidates (every currently-active selected unit) with labelled residency, the full fit arithmetic, the runtime port check, `suggested_stops`, and `confirm` — `confirm` present **iff every check passes** (200); otherwise 422 with the identical body minus `confirm` (this is how "cannot be submitted until it fits" is enforced server-side). The **runtime port rule** (distinct from the rollout's declared-board rule, and the spec says so in a code comment): the target's declared port is blocked iff claimed by Roundhouse itself (`self_port`) or **declared by a selected unit whose rung is currently active and which is not among the ticked stops**. Declared collisions with units that are (or will be) not running are notices. Honest limitation, stated in the check detail wording: Roundhouse only knows its selected units — a non-selected process bound to the port is invisible here and will surface as a start failure in `watching`.
- **F5 — `/api/rollouts` IS the operations namespace; no rename, no new event name.** New POST routes: `/api/switch/preview` and `/api/switch` (target in body — a switch involves N units, so it doesn't belong under one unit's path). `GET /api/rollouts/<id>` serves both kinds (records share the dict). The reverse-offer route stays `POST /api/rollouts/<id>/rollback` for both kinds and the record sub-object stays `rollback` — same offer/dismiss semantics, same slot race handling, one code path; the UI labels it "restore" when `kind === 'switch'`. SSE event name stays `rollout` (one stream; events gain `"kind"` and `"unit"` fields — additive). Snapshot key stays `"rollout"`. The id field stays `rollout_id` for both kinds. Every one of these is a documented naming wart accepted to keep 196 tests and the whole MVP2 wire surface untouched; `kind` is the single discriminator.
- **F6 — Mobile: two breakpoints, overlay-by-media-query, body-scroll-lock class.** Breakpoints: `@media (max-width: 700px)` = phone (overlays, sticky compact header, collapse, 44 px targets); the existing `@media (max-width: 1200px)` block = narrow/tablet (edit form already stacks there); ≥ 1200 px byte-wise-unchanged rendering. No new markup pattern: the existing `.detail-pane.active` and `.modal` become full-screen via `position: fixed; inset: 0` **inside the 700 px media query only**; JS toggles `body.overlay-open { overflow: hidden }` on open/close (the scroll lock). The 44 px criterion is tested statically as: (a) the 700 px media block contains `min-height: 44px` rules for the frozen selector list, (b) every `onclick` in the file is on a `<button>` element (the current dismiss `<a href="#">` is converted), (c) the viewport meta exists — **stated honestly in the test docstring: static tests prove the rules and markup exist, not computed layout; the manual phone check in the drill covers rendering.**
- **F7 — `suggested_stops` is computed server-side, greedy-by-residency.** In every preview response: if the fit check fails with the submitted ticks, walk the active, un-ticked, non-target candidates in order (resident_bytes descending, name ascending as tie-break), hypothetically adding each to the freed sum until `estimate + HEADROOM ≤ MemAvailable + Σfreed` or candidates are exhausted; return the added names in walk order. If the fit passes, `suggested_stops: []`. The UI renders these as tagged suggestions and a hint line — **it never ticks a box itself** (contract: nothing preselected by policy; suggestion ≠ selection).
- **F8 — The three debt guards are implemented against the current layout exactly as §7.1 below specifies**, with the seeded-violation acceptance run as a documented one-off integration procedure (seed → red → unseed), never a committed test.
- **F9 — Target eligibility doctrine: rung must be exactly `OFF`.** `RETIRED` → 422 (retired check row); rung `STANDBY` → 422 with the gate detail (`waiting for kernel <wants> (running: <running>)`) — STANDBY is by construction "gate unsatisfied", so no separate gate evaluation is needed; active rungs → 422 `already active`; `FAILED` → 422 `unit is FAILED — clear the failure by hand first` (a switch starts clean stalls only; `reset-failed` is not in the verb set and stays out). Unknown unit names (target or any stop) → 404. Known-but-ineligible stops (not active, retired, duplicates, or equal to target) → 422 check rows. All eligibility is enforced at preview AND at execute AND in the engine worker (same layering as MVP2).
- **F10 — A switch writes nothing, provably.** The switch worker calls only `_stop_unit`/`_start_unit` — never `_daemon_reload` (lifecycle verbs only; the contract excludes daemon-reload), never `run_git`, never `_atomic_write`. Asserted three ways: engine test with a raising `run_git`/`_atomic_write` (§7.2), the AST call-site guard (unchanged whitelist), and the container drill's `git rev-parse HEAD` + unit-file-mtime comparison.
- **F11 — Switch slot-collision error code is `operation_in_progress`.** The rollout route keeps `rollout_in_progress` (frozen by MVP2 tests); the two switch routes answer `409 {"error": "operation_in_progress", "rollout_id": <current id>, "kind": <current kind>}` when the slot is held. `_slot_free` / `_rollback_offered` are reused unchanged in logic; the terminal set is extended (§2.2).
- **F12 — Switch UI entry lives in the detail pane only; no dedicated TURNTABLE section.** An eligible unit's detail pane (mode `actuate`, rung `OFF`, not retired) shows **`switch to this`** beside `edit`. The OFF section heading becomes `OFF · turntable (n) [show]` — the OFF list *is* the turntable. Rationale: a new always-rendered section duplicates the OFF list, competes with ACTIVE for above-the-fold space on the phone (the contract requires ACTIVE + stepper visible on load), and appears in no acceptance criterion. Rejected as gold-plating.

---

## 1. FILE / SECTION LAYOUT

```
mvp1/
  MVP3-SPEC.md                  # this file
  roundhouse.py                 # extended: SECTION E PART 3 (switch), small edits in B/C/D/E as listed
  static/index.html             # extended in place (ONE file, inline CSS+JS, textContent-only)
  scripts/
    container-setup.sh          # extended: switch scenario wiring (units A/B + FAKE_EXIT_1 target)
    switch-drill.sh             # REWRITTEN: UI-driven phone drill checklist (performs no actuation itself)
  tests/
    test_parser.py              # UNTOUCHED
    test_watcher.py             # extended: snapshot-shape test gains 'start_ts_mono'
    test_server.py              # extended: TestWriteGuards strengthened (§7.1), frozen route table, mobile static checks
    test_actuation.py           # extended: switch preflight/engine/slot/route/zero-write classes (§7.2)
```

`roundhouse.py` gains, immediately after the current `dismiss()` method (i.e. between line ~4266 and the Section D banner):

```python
# ===== SECTION E PART 3: SWITCH (lifecycle verbs only; no file writes, no git, no daemon-reload) =====
```

The §7.1 section-boundary rule treats every banner matching `^# ===== SECTION ([A-E])\b` as opening that section, so `PART 2`/`PART 3` banners keep the whole span E (from line 2689's banner to Section D's banner) as one region.

**Section E Part 3 contents (complete list):** `SWITCH_PHASES`; `OPERATION_TERMINAL_PHASES`; timeout constants `STOP_TIMEOUT_SEC = 150`, `CONFIRM_OFF_TIMEOUT_SEC = 30`, `START_TIMEOUT_SEC = 30`, `WATCH_TIMEOUT_SEC = 900`; `fleet_fingerprint()`; `compute_switch_confirm()`; `_estimate_start_bytes()`; `_freed_bytes()`; `switch_preflight()`; `suggest_stops()`; `switch_public_record_fields` (folded into `rollout_public_record`, see §2.4); `RolloutEngine.start_switch()`, `._run_switch()`, `._run_restore()`, `._confirm_off()`, `._watch_to_ready()` (methods are defined in the class body inside Section E Part 2's class — the *new pure functions* live under the Part 3 banner; the class methods are added to the existing class, which is already inside Section E).

**Edits to existing code (exhaustive):**
- Section B: `snapshot()` unit rows gain `'start_ts_mono': self._state[unit_name].get('exec_main_start_ts_mono') or '0'`. `ACTIVE_RUNGS = {'STARTING', 'LOADING', 'READY', 'BUSY'}` hoisted to a module-level constant near the top of Section B; `classify_port_claims`, `preflight_memory`, and the engine's `was_active` capture reference it. **No other watcher changes** (§6).
- Section E Part 2: `_slot_free` reads `OPERATION_TERMINAL_PHASES` (§2.2); `preflight_memory` refactored to call the extracted `_estimate_start_bytes`/`_freed_bytes` with **behavior byte-identical** (existing `TestFreedMemoryTerm`/`TestPreflight` stay green untouched — that is the refactor's acceptance test); `_stop_unit` passes `timeout=STOP_TIMEOUT_SEC`; `_watch_unit` refactored onto `_watch_to_ready` (§2.5); `_update_phase`/`_fail_rollout` publish `kind` and `unit` in the SSE dict; `_fail_rollout`'s offer condition per §2.6; `rollout_public_record` gains the kind branch (§2.4); rollout records gain `"kind": "rollout"` at creation; `git_startup_check` step 4 gains the second warning (`'*.roundhouse-tmp' not in ignore_text` → its own warning line) — the third debt defect.
- Section C: `do_POST` gains the two switch routes in `is_post_route` and dispatch (§4); `do_GET` answers 405 for `/api/switch` and `/api/switch/preview` (POST-only doctrine); handlers `handle_switch_preview`, `handle_switch`.
- Section D: none (no new flags; arming is unchanged per contract).
- Section A: **unchanged.**

---

## 2. SWITCH ENGINE SPEC

### 2.1 Phase table

```python
SWITCH_PHASES = ("preflight", "stopping", "starting", "watching",
                 "done", "failed", "restoring", "restored", "restore_failed")
```

| phase | does (in order) | success → | failure | budget |
|---|---|---|---|---|
| `preflight` | re-run `switch_preflight` (world may have drifted); recompute `compute_switch_confirm` from a fresh snapshot's fingerprint, compare to submitted (route already checked; the worker is the final authority) | `stopping` (or `starting` if no ticks) | `failed(preflight | preview_stale)`, nothing touched, `restored: true` (slot free) | 10 s |
| `stopping` | for each ticked unit i of n, in submitted order: SSE detail `"stopping <unit> (i/n)"` → `_stop_unit(unit)` (150 s) → `_confirm_off(unit)` (poll roster 1 s, 30 s cap; confirmed per F2; rung FAILED-after-stop → confirmed + notice appended to detail) → append to `record['stopped']` under the lock | `starting` | stop rc≠0 → `failed(stop_error)`; confirm timeout → `failed(stop_unconfirmed)`. Offer restore iff `stopped` non-empty (§2.6); else `restored: true`, slot free | (150+30) s × n |
| `starting` | SSE detail `"starting <target>"` → `_start_unit(target)`; set `record['target_started'] = True` **before** the call (a start that errors mid-flight may still have started the job) | `watching` | `failed(start_error)`, restore offered | 30 s |
| `watching` | `_watch_to_ready(target, prior_start_ts, WATCH_TIMEOUT_SEC)` — identical rules to the rollout: rung `READY`/`BUSY` → done; `FAILED` → fail; badge `no_ready_marker` → fail; cap → fail. `prior_start_ts` = target's snapshot `since` captured before `_start_unit` (for an OFF unit this is a `now()` value that never equals a live sample, so the freshness gate degrades to a no-op — documented in a comment, not special-cased) | `done`, detail `"switched: <target> ready in <x>s"` | `failed(unit_failed | no_ready_marker | watch_timeout)`, restore offered | 900 s |
| `restoring` (entered only from `failed` via the `/rollback` route) | stop target if its rung is active (`_stop_unit` + `_confirm_off`, failures logged into detail but not fatal — the target may already be dead); then for each unit in `record['stopped']` in **original stop order**: `_start_unit` → `_watch_to_ready` | `restored` when every previously-stopped unit is READY/BUSY (detail `"restored: <k> unit(s) back to READY"`) | `restore_failed` — terminal; detail carries the exact manual commands (`systemctl --user start <remaining units>`) | 900 s total, shared clock |

The slot is claimed in `start_switch` under `watcher_lock` exactly like `start_rollout` (same `_slot_free` gate, `ActuationError("operation_in_progress")` per F11); the worker is one `threading.Thread(name="switch", daemon=True)`.

### 2.2 Slot generalization (the only `_slot_free` change)

```python
OPERATION_TERMINAL_PHASES = ROLLOUT_TERMINAL_PHASES + ("restored", "restore_failed")
```

`_slot_free` tests `phase in OPERATION_TERMINAL_PHASES`; the `failed` branch (restored / no live offer / dismissed offer) is reused verbatim — switch records use the same `rollback` sub-object (`{'offered': True}` → after restore `{'offered': False, 'phase': 'restored'}` / after dismiss `{'offered': False, 'dismissed': True}`), so `_rollback_offered`, `rollback()`'s claim-under-lock, and `dismiss()` work on switch records **unchanged** except one dispatch line in `rollback()`: after claiming the offer, spawn `_run_restore` when `record.get('kind') == 'switch'`, else `_run_rollback` (and the claimed phase written under the lock is `'restoring'` vs `'rolling_back'` accordingly). The `not rollout.get('commit')` guard in `rollback()` becomes: rollouts require `commit` non-null (unchanged); switch records are eligible iff the offer is live (a switch never has a commit).

### 2.3 Switch record shape (in-memory; `old_raw`-equivalent state is `stopped`)

```json
{"rollout_id": "sw-1765612345-2", "kind": "switch",
 "unit": "llama-server-gemma4.service",            // = target; kept so every existing record consumer works
 "target": "llama-server-gemma4.service",
 "stops": ["qwen3.6-coding.service"],               // ticked, submitted order
 "stopped": [],                                     // grows as each stop is confirmed OFF
 "target_started": false,
 "phase": "stopping", "detail": "stopping qwen3.6-coding.service (1/1)",
 "failure": null, "rollback": null, "restored": false,
 "started_at": 1765612345.0, "updated_at": 1765612347.2}
```

### 2.4 Public record + SSE

`rollout_public_record(record)` branches on `record.get('kind', 'rollout')`: rollout emits the existing 12 keys plus `kind`; switch emits `rollout_id, kind, unit, target, stops, stopped, target_started, phase, detail, failure, rollback, restored, started_at, updated_at`. One function, both consumers (snapshot merge and `GET /api/rollouts/<id>`) — unchanged wiring. SSE stays `event: rollout`; the published dict from `_update_phase`/`_fail_rollout` gains `"kind"` and `"unit"` (read from the record under the lock). **The full record still rides only in snapshots and GET** — the UI re-fetch rule (§5.6) depends on that, deliberately.

### 2.5 `_watch_to_ready` — the shared watch primitive (refactor, exact contract)

```python
def _watch_to_ready(self, unit_name: str, prior_start_ts, deadline_ts: float
                    ) -> tuple:   # ('ready', elapsed) | ('failed', reason, detail) | ('timeout',)
```

Extracted from the body of `_watch_unit`'s loop: sample rung/badges/since under `watcher_lock` (lock released before any action — the existing deadlock comment moves with it), apply the freshness gate, classify. `_watch_unit` becomes a thin wrapper mapping results to rollout/rollback terminals exactly as today — **the refactor's acceptance test is that every existing `TestRolloutMachine`/`TestSlotRelease` test passes unmodified.** The switch worker and `_run_restore` call `_watch_to_ready` directly and map results per §2.1.

### 2.6 Failure/offer rule for switches

`_fail_rollout` gains no new parameters; the switch worker passes `offer_rollback=True` on `stop_error`/`stop_unconfirmed` (with stops made)/`start_error`/watch failures, and `_fail_rollout`'s internal condition `if offer_rollback and rollout['commit']` becomes:

```python
reversible = (rollout.get('commit') if rollout.get('kind', 'rollout') == 'rollout'
              else (rollout.get('stopped') or rollout.get('target_started')))
if offer_rollback and reversible: rollout['rollback'] = {'offered': True}
```

A switch that failed before anything changed (preflight, first stop's rc≠0 with nothing yet stopped) carries `restored: true` and frees the slot — nothing to reverse, mirroring the rollout's preflight path.

---

## 3. PREVIEW / PREFLIGHT SPEC

### 3.1 Extracted helpers (pure, injectable; Section E Part 3)

```python
def _estimate_start_bytes(unit_name: str, profile: Dict, mem_store) -> tuple  # (bytes, source_label)
    # exact measured (unit, file_id, ctx) -> "measured"
    # newest measured (unit, file_id, any ctx) -> "measured at ctx <c>; target ctx unproven"
    # formula int(size*1.10 + 1.5 GiB) -> "formula"
    # 9 GiB default -> "default"          # identical fallback ladder to preflight_memory today

def _freed_bytes(unit_name: str, unit_row: Dict, cgroup_cache: Dict) -> tuple  # (bytes, source_label)
    # rung not in ACTIVE_RUNGS -> (0, 'none (unit not active)')
    # else cgroup memory.current -> last_peak -> measured row -> (0, 'active, but no cgroup sample yet')
    # byte-for-byte the E9 ladder currently inlined in preflight_memory
```

`preflight_memory` is refactored onto both (its own tests are the no-regression proof).

### 3.2 `switch_preflight(target, stops, watcher, units, self_port, meminfo_reader=None) -> Dict`

Returns `{"ok": bool, "checks": [...], "target": {...}, "stop_candidates": [...], "fit": {...}, "port": {...}, "suggested_stops": [...], "notices": [...]}`. Check order and rules:

1. **`retired`** — target retired → fail (same wording as `preflight_retired`).
2. **`target`** — F9 rung rule: fail unless rung == `OFF`, with the per-rung details of F9.
3. **`stops`** — every ticked name: selected, not retired, not the target, no duplicates, rung ∈ `ACTIVE_RUNGS`. Any violation → fail, detail enumerates offenders. (Unknown names never reach here — 404 at the route.)
4. **`memory`** — `estimate, estimate_source = _estimate_start_bytes(target, target_profile, store)`; `freed = Σ _freed_bytes(u, ...)` over ticked stops with a per-unit breakdown; `budget = MemAvailable(fresh read or injected) + freed`; fail iff `estimate + HEADROOM_BYTES > budget`. The check dict carries the same labelled number fields as MVP2's (`estimate_bytes`, `estimate_source`, `mem_available_bytes`, `freed_bytes`, `freed_by: [{"unit", "bytes", "source"}]`, `headroom_bytes`, `budget_bytes`) and a human detail naming every ticked unit with its source label — house rule: every surfaced number carries its source.
5. **`port`** — F4 runtime rule. Fail detail enumerates blockers with rungs (`"port 8093 will still be bound after the plan: qwen3.6-coding.service (READY, not ticked)"`); notices for non-blocking declared collisions (stopped/STANDBY/ticked/RETIRED claimants).

`suggested_stops` per F7, computed inside this function so preview and execute agree. `stop_candidates` = every selected unit with rung ∈ `ACTIVE_RUNGS`, each `{"unit", "rung", "resident_bytes", "resident_source", "port", "alias", "ticked": bool}` — residency via `_freed_bytes` (same ladder, same labels: `cgroup memory.current` / `cgroup memory.peak (last sample)` / `measured peak row` / `active, but no cgroup sample yet`).

### 3.3 Fingerprint + confirm

```python
def fleet_fingerprint(snapshot: Dict) -> Dict[str, str]:
    # {u['unit']: str(u.get('start_ts_mono') or '0') for u in snapshot['units'] if not u['retired']}
def compute_switch_confirm(target: str, stops: List[str], fingerprint: Dict[str, str]) -> str
```

Canonicalization exactly per F3. Preview computes it from the same snapshot used for the checks (one `take_snapshot()` per request — no torn reads). Execute route recomputes from a fresh snapshot; mismatch → `409 preview_stale` with detail `"fleet state changed since preview (a unit started or stopped); re-preview"`.

### 3.4 Wire shapes

`POST /api/switch/preview` body: `{"target": "<unit>", "stops": ["<unit>", ...]}` (`stops` optional, default `[]`). Success 200:

```json
{"target": {"unit": "...", "rung": "OFF", "port": 8093,
            "estimate_bytes": 9800000000, "estimate_source": "measured"},
 "stop_candidates": [{"unit": "qwen3.6-coding.service", "rung": "READY",
                      "resident_bytes": 16400000000, "resident_source": "cgroup memory.current",
                      "port": 8085, "alias": "qwen3.6-coding", "ticked": true}],
 "checks": [...five rows...],
 "fit": {"ok": true, "estimate_bytes": ..., "estimate_source": "...", "mem_available_bytes": ...,
         "freed_bytes": ..., "freed_by": [...], "headroom_bytes": 1073741824, "budget_bytes": ...},
 "port": {"ok": true, "port": 8093, "blockers": [], "notices": []},
 "suggested_stops": [],
 "notices": [],
 "confirm": "<64-hex>"}
```

Failure 422: same body with `"error": "preflight_failed"` and **no `confirm`**. `POST /api/switch` body: `{"target", "stops", "confirm"}` → 202 `{"rollout_id": "sw-..."}`; errors per §4.

---

## 4. ROUTES + STATUS DOCTRINE + FROZEN TABLE

| route | method | success | errors |
|---|---|---|---|
| `/api/switch/preview` | POST | 200 preview (confirm present) | 400 `bad_json`/`bad_body` (target missing/not a string, stops not a list of strings); 404 unknown target or stop name; 422 `preflight_failed` (full preview body, no confirm); 403/401 per E8 |
| `/api/switch` | POST | 202 `{"rollout_id"}` | as preview, plus 409 `operation_in_progress` (F11) and 409 `preview_stale` (F3 mismatch — checked at the route from a fresh snapshot before spawning) |
| `/api/rollouts/<id>` | GET | 200 record (either kind) | 404 — unchanged code path |
| `/api/rollouts/<id>/rollback` | POST | 202 (phase → `rolling_back` or `restoring` by kind) | 404; 409 `not_rollbackable` — unchanged condition (`failed` + live offer; commit requirement is rollout-only per §2.2) |
| `/api/rollouts/<id>/dismiss` | POST | 200, slot freed | 404; 409 `not_dismissable` — unchanged |

All MVP2 routes byte-identical. `do_POST` gating order unchanged (bearer before routing, before body read). `do_GET` gains explicit 405 for the two switch paths. Status doctrine unchanged: 400 malformed, 401/403 per E8, 404 unknown resource, 405 wrong method, 409 concurrency/staleness, 422 validation-against-the-world.

**Frozen POST route table (the §7.1 guard's data):**

```python
FROZEN_POST_ROUTES = (
    "/api/units/<name>/edit", "/api/units/<name>/rollout",
    "/api/rollouts/<id>/rollback", "/api/rollouts/<id>/dismiss",
    "/api/switch/preview", "/api/switch",
)
```

---

## 5. UI SPEC (`static/index.html`; textContent-only rule stands; no localStorage/sessionStorage)

1. **Switch entry (F12):** in `showDetail`, when `state.mode === 'actuate' && !unit.retired && unit.rung === 'OFF'`, render a `switch to this` button beside `edit`. OFF heading text: `OFF · turntable (n) [show]`.
2. **Switch preview modal:** a second modal (`#switch-modal`, same `.modal`/`.modal-content` classes) built by `showSwitchModal(previewData)`: target line with estimate + source; candidate rows — each a `<label class="stop-tick-row">` wrapping `<input type="checkbox">` + unit name + rung + labelled residency + `(suggested)` tag when in `suggested_stops`; fit line rendering the arithmetic with all sources (fits → neutral, doesn't fit → amber + the suggestion hint `"suggestion: also tick <units>"`); port line with blockers/notices; buttons `switch` (disabled unless `confirm` present) and `cancel`. **Every checkbox change re-POSTs `/api/switch/preview`** with the current ticks and re-renders (server recomputes fit/suggestions/confirm — the UI does no arithmetic). Submit POSTs `/api/switch`; on 409 `preview_stale` → message + one automatic re-preview (MVP2 §7.5 pattern); on 409 `operation_in_progress` → alert with the running kind.
3. **Stepper generalization:** `renderRolloutStepper` picks the chip list by `state.rollout.kind`: rollout → existing six; switch → `preflight stopping starting watching done`, with `restoring` appended while active (as `rolling_back` is today). Offer button label: `roll back to previous config` vs `restore previous fleet`; dismiss becomes a `<button>` (F6b). Terminal colors: `restored` reuses the `rolled_back` neutral class; `restore_failed` red.
4. **UI defect 1 (gate notice):** `showEditForm`'s condition `unit.gate && unit.gate.kind === 'STANDBY'` → `unit.gate && unit.rung === 'STANDBY'` (the detail JSON carries `rung`; `gate.kind` is `'kernel'|'opaque'` and the notice never rendered). The same condition gates a notice line in the switch modal path — but note an OFF-rung requirement means a STANDBY target never reaches the modal; the notice appears in the detail pane instead (already rendered via the Gate row — no new work beyond the edit-form fix).
5. **UI defect 2 (202 stores only the id):** new single fetch path `refreshOperation(id)` = `GET /api/rollouts/<id>` → `state.rollout = record; renderRolloutStepper()`. Called (a) after any 202 from `/rollout` or `/api/switch`, (b) on **every** SSE `rollout` event (replace the current `state.rollout = data` merge — the SSE payload is a phase ping, not a record; this also fixes the latent bug that `rollback.offered` never arrives via SSE and the offer only rendered after a manual refresh). Snapshot handling unchanged (already carries the full record).
6. **Mobile (F6) — all inside one appended `@media (max-width: 700px)` block plus small markup/JS touches:**
   - Markup: wrap `· kernel <span id="kernel">…</span>` in `<span class="kernel-wrap">`; wrap the port-board cells in `<div class="port-board-body" id="port-board-body">` with a `[show]` toggle span in its heading (same pattern/JS as the OFF toggle); convert the dismiss link to `<button id="dismiss-button" class="link-button">`.
   - Sticky compact header: `.header { position: sticky; top: 0; z-index: 30; background: var(--bg); margin-bottom: 1em; padding: 8px 0; } .kernel-wrap { display: none; }` — host, mode badge, mem gauge, degraded banner remain (contract's four items).
   - Collapse: `.port-board-body { display: none; } .port-board-body.expanded { display: block; }` inside the media query; **outside it** `.port-board-body { display: flex; flex-wrap: wrap; gap: 1em; }` and the toggle span is hidden ≥ 701 px (`display: none` default, shown in the media query) — desktop unchanged. OFF/RETIRED already collapse at all widths.
   - Overlays: `.detail-pane.active, .modal.active .modal-content { position: fixed; inset: 0; width: auto; max-width: none; max-height: none; margin: 0; overflow-y: auto; z-index: 40; }`; JS adds/removes `overlay-open` on `<body>` in `showDetail/closeDetail/showDiffModal/closeDiffModal/showSwitchModal/closeSwitchModal`; `body.overlay-open { overflow: hidden; }` (defined outside the media query — harmless on desktop, where it also stops background scroll under modals).
   - Touch targets: `.unit-row, button, .off-section-toggle, .stop-tick-row, .preflight-item? no — actionable only: .unit-row, button, .off-section-toggle, .stop-tick-row { min-height: 44px; align-items: center; } button { padding: 0.6em 1em; } .stop-tick-row input[type="checkbox"] { width: 24px; height: 24px; }` — the frozen selector list for the static test is exactly `{'.unit-row', 'button', '.off-section-toggle', '.stop-tick-row'}`.
   - Stepper compaction: each chip is built as `<div class="phase-chip"><span class="chip-icon">…</span><span class="chip-label">…</span></div>` (icons: done `✓`, current `●`, pending `·`, failed `✗`, set by the existing class logic); media query: `.phase-chip:not(.current) .chip-label { display: none; }`.
   - No horizontal body scroll: `.diff-output pre, .operator-notes, .stepper-detail { overflow-x: auto; max-width: 100%; }` (the first two mostly exist; make all three explicit); `.detail-table { table-layout: fixed; word-break: break-all; }` inside the media query; `body { overflow-x: hidden; }` inside the media query as the backstop.
   - `.standby` never-red structural rule and the palette variables gain **no** new sharers; no theming changes (contract Out-of-scope).
7. Desktop ≥ 1200 px: no rule outside the two media queries changes rendering; the two-column edit form (`@media (max-width: 1200px)` stacking) is untouched.

---

## 6. WATCHER

**Verified sufficient; two additive lines, no new machinery.** Confirmed-OFF detection needs nothing new: rungs `OFF`/`STANDBY`/`FAILED` are computed from `ActiveState` on the 3 s tick, `sensed_at` is already stamped per unit per tick (Section B line ~1313), and `_confirm_off` polls `snapshot()` under the lock exactly as `_watch_unit` does. The `no_ready_marker` badge and badge-change emission from MVP2 §8 already serve the switch's `watching` phase unchanged. The two additions: `start_ts_mono` in snapshot unit rows (F3) and the module-level `ACTIVE_RUNGS` hoist (§1) — plus the `test_snapshot_shape` key-set update they force.

---

## 7. TEST / GUARD SPEC

### 7.1 Debt guards (F8) — exact implementations in `test_server.py::TestWriteGuards`

1. **§9.1 call-site whitelist, actually enforced** — `test_write_verbs_only_in_section_e`: read the source; build section spans from lines matching `^# ===== SECTION ([A-E])\b` (a letter's span runs to the next banner with a *different* letter, so E's two/three PART banners stay one span). Parse the AST once. Assert: (a) every `ast.Constant` with `value in WRITE_VERBS` (the existing set) has `lineno` inside span E **or** inside `cmd_serve`'s `--actuate` argparse block? — no exceptions needed today: verify at implementation time and hard-fail on any occurrence outside E; (b) every such constant that appears among a `Call`'s arguments (walk each `Call`, check containment by position) belongs to a call whose callee name is `run_actuate`; (c) every `Call` whose callee name is `run_actuate` is lexically inside a `FunctionDef` named in `ROLLOUT_CALLSITES = frozenset({'_stop_unit', '_start_unit', '_daemon_reload'})` — **unchanged by MVP3, which is the point: the switch reuses the three methods**; (d) `_daemon_reload`'s only callers (Calls whose attr name is `_daemon_reload`) are inside `{'_run_rollout', '_run_rollback'}` — the switch/restore workers must not appear (F10's static leg). Also strengthen the arming test: replace `assertGreaterEqual(len(assignments), 1)` with: exactly two `ACTUATE_ARMED` assignments — one module-level with constant `False`, one whose enclosing `FunctionDef` is `cmd_serve`.
2. **Frozen POST route table completeness** — `test_post_route_table_frozen_and_complete`: (behavioral) boot the unarmed test server (the `TestRoutesAuth` harness in `test_actuation.py` already builds one; this test lives beside it or reuses the helper) and POST to a concrete instantiation of every `FROZEN_POST_ROUTES` entry → all 403, nothing invoked (monkeypatched `_atomic_write` + `subprocess.run` recorders empty). (Structural) parse `do_POST` from the AST; collect every string `ast.Constant` starting with `'/'` inside it; assert that set equals the set of path literals derivable from the frozen table (`{'/api/units/', '/edit', '/rollout', '/api/rollouts/', '/rollback', '/dismiss', '/api/switch/preview', '/api/switch'}` — the frozen literal set is written into the test). Adding a dispatch string without touching the table fails; removing a route likewise.
3. **§9.9 file-write confinement** — `test_file_writes_confined`: walk the AST; flag (a) every `Call` to name `open` whose second positional arg or `mode=` keyword is a string constant containing any of `w a x +`, (b) every `Attribute` call `os.replace` / `os.rename` / `.write_text` / `.write_bytes`; assert every flagged node's enclosing `FunctionDef` is in the frozen allowlist `WRITE_FUNCS = frozenset({'_atomic_write'})` (survey at implementation time; `ensure_token` writes via `_atomic_write`, MemStore writes via sqlite3, so the allowlist is one name — if the survey finds another legitimate writer, the task FAILS and escalates rather than growing the list).

**Seeded-violation acceptance (one-off, integration phase, not a committed test):** documented as a checklist block at the top of `switch-drill.sh`'s companion notes and executed once before merge: seed (i) `subprocess.run(["systemctl", "--user", "restart", "--", "x.service"])` inside a Section C helper, (ii) a `'/api/units/<name>/kick'` dispatch branch in `do_POST` without touching the table, (iii) `open(path, "w")` inside a Section B method; run `python3 -m unittest tests.test_server -v`; confirm guards 1, 2, 3 respectively go red (and only they); `git checkout -- roundhouse.py` to unseed; record the three red test names in the integration commit message.

### 7.2 Switch tests (`test_actuation.py`, new classes; ownership in §8)

- `TestSwitchPreflight` (T1): eligibility matrix per F9 (OFF passes; STANDBY/FAILED/READY/RETIRED fail with the specified details); multi-stop freed arithmetic — 2 ticked units with injected cgroup current/last_peak → `freed_bytes` is the sum, `freed_by` labels each; fit both sides of the boundary (`estimate + 1 GiB` vs budget, off-by-one values); port rule truth table (blocker: active+unticked claimant; notice: ticked claimant, STANDBY claimant, RETIRED claimant; self_port blocks); `suggest_stops` greedy order, tie-break, exhaustion (suggestion list ≠ fit guarantee when even all candidates don't cover), and empty-when-fits.
- `TestSwitchConfirm` (T1): canonicalization is order-independent in `stops`, sensitive to any unit's `ts_mono` change, `None`/`''` → `"0"`, and `fleet_fingerprint` excludes retired units.
- `TestSwitchEngine` (T1, on the `_EngineHarness` pattern with stubbed `run_actuate` recorder + scripted watcher): full happy path — recorded argvs are exactly `stop A`, `stop B`, `start T` in order (no daemon-reload — F10); phase/SSE sequence; confirmed-OFF gate holds the `stopping` phase until the stub roster reports OFF with fresh `sensed_at`; stop-confirm timeout → `failed(stop_unconfirmed)` with offer; `FAILED`-after-stop counts as confirmed + notice; target watch failure → offer; restore replays `stopped` in original order and terminates `restored`; restore failure → `restore_failed` terminal with the manual-commands detail; dismiss frees the slot.
- `TestSwitchZeroWrites` (T1): run the full switch and the restore with `run_git` monkeypatched to raise on ANY call and `_atomic_write` monkeypatched to raise — both complete; plus fixture unit files' mtimes unchanged across an engine-level switch.
- `TestSwitchSlot` (T2): both directions — rollout holds slot (scripted non-terminal record) → `POST /api/switch` 409 `operation_in_progress`; switch holds slot → `POST .../rollout` 409 `rollout_in_progress`; switch-while-switch 409; `restored`/`restore_failed`/dismissed-offer free the slot (extend the existing `test_slot_free_matrix` with the new phases).
- `TestSwitchRoutes` (T2): 403 unarmed / 401 bad token on both switch routes (extends the frozen-table behavioral run); 404 unknown target and unknown stop; 422 bodies carry the full preview object without `confirm`; 200 carries `confirm`; execute with stale fingerprint (mutate the stub's `start_ts_mono` between preview and execute) → 409 `preview_stale`; GET `/api/rollouts/<sw-id>` returns the switch public record; GET on `/api/switch` → 405.
- `test_watcher.py` (T1): `test_snapshot_shape` gains `start_ts_mono`; a test that `start_ts_mono` is `'0'` for a never-started unit and stable across snapshots (unlike `since`).
- Static UI tests (T3, `test_server.py`): 700 px media block exists; `min-height: 44px` present for each of the four frozen selectors within that block's text span; no `<a href="#"` in the file; `onclick=` occurs only on `<button` tags (regex over tag substrings); `overlay-open` string present in both CSS and script; existing no-innerHTML/no-localStorage assertions re-run over the grown file unchanged; switch modal ids present (`switch-modal`, `stop-tick-row` class, `port-board-body`); the F12 button string `switch to this` present.

---

## 8. WORK BREAKDOWN — 3 tasks, composable without coder contact

All cross-task interfaces are frozen in §§2–4 (function signatures, record/JSON/SSE shapes, route table, constants). Merge order **T1 → (T2 ∥ T3) → integration** (seeded-violation run + container drills + live drill).

### T1 — Engine + preflight + watcher key (first; T2/T3 depend only on its frozen shapes)
- **Writes:** Section B (`start_ts_mono`, `ACTIVE_RUNGS` hoist); Section E Part 3 pure functions (`SWITCH_PHASES`, `OPERATION_TERMINAL_PHASES`, the four timeout constants, `fleet_fingerprint`, `compute_switch_confirm`, `_estimate_start_bytes`, `_freed_bytes`, `switch_preflight`, `suggest_stops`); Section E Part 2 edits (`_slot_free` terminal set, `preflight_memory` refactor, `_stop_unit` timeout, `_watch_to_ready` extraction + `_watch_unit` rewrap, `_fail_rollout` reversible rule, SSE `kind`/`unit`, `rollout_public_record` kind branch, rollout records' `"kind"`); `RolloutEngine.start_switch/_run_switch/_run_restore/_confirm_off`; `rollback()`/record dispatch per §2.2. **Tests owned:** `TestSwitchPreflight`, `TestSwitchConfirm`, `TestSwitchEngine`, `TestSwitchZeroWrites` in `test_actuation.py`; the two `test_watcher.py` items. **Must not touch:** Section C/D, `index.html`, `TestWriteGuards`.
- **Self-test:** `cd mvp1 && python3 -m unittest discover -s tests -v` — every pre-existing test green **unmodified except** `test_snapshot_shape` (the refactors' no-regression proof), plus the new classes green.

### T2 — Routes + guards + debt tests + server-side defect (parallel with T3)
- **Writes:** Section C (`do_POST` switch dispatch + `is_post_route`, `handle_switch_preview`, `handle_switch`, `do_GET` 405s); `git_startup_check` `.roundhouse-tmp` warning; `FROZEN_POST_ROUTES` constant (module level, Section E, beside `GIT_VERBS`). **Tests owned:** `TestWriteGuards` strengthening (§7.1 items 1–3, the arming exact-count), `TestSwitchSlot`, `TestSwitchRoutes`; may extend the `TestRoutesAuth` harness. Documents the seeded-violation procedure (checklist text) but does not run it.
- **Self-test:** full discover green; `curl`-level manual smoke against `--serve --actuate` in a scratch repo dir: preview 200 with confirm, execute 202, 409 on double-submit.

### T3 — UI + mobile + scripts (depends only on frozen JSON/SSE shapes)
- **Writes:** `static/index.html` complete §5 (switch button + modal + re-preview-on-tick, stepper generalization, `refreshOperation`, defect fixes 1–2, all F6 mobile work, dismiss→button, port-board disclosure markup); `scripts/container-setup.sh` (switch scenario: fake unit A on :8085 + fake unit B on :8093 + the `FAKE_EXIT_1` unit as failing target; no new fake-server features needed — `FAKE_EXIT_1` env and the ctx sentinel already exist); `scripts/switch-drill.sh` rewritten as the operator checklist: pre-state capture via `/api/units`, phone-URL prompt, step prompts for switch qwen3.6-coding → llama-server-gemma4 (:8093) → back, post-state verification that `qwen3.6-coding` is READY — the script performs **zero** actuation itself. **Tests owned:** the static UI tests in `test_server.py` (§7.2 last block).
- **Self-test:** `python3 -m unittest tests.test_server -v`; container drill rows 3–5 of §9 end-to-end; desktop visual check at ≥ 1200 px against a pre-change screenshot.

**Shared-file ownership:** `test_actuation.py` — T1 owns `TestSwitchPreflight/Confirm/Engine/ZeroWrites`, T2 owns `TestSwitchSlot/Routes`; neither edits the other's classes nor any MVP2 class. `test_server.py` — T2 owns `TestWriteGuards`, T3 owns the static-UI class; `roundhouse.py` — T1 owns Sections B/E, T2 owns Section C + the one `git_startup_check` edit.

---

## 9. TEST PLAN — mapped 1:1 to MVP3.md's acceptance checklist

| criterion | proven by |
|---|---|
| Preview: labelled residency, arithmetic with sources, unsubmittable until fit, 409 on drift | **Unit:** `TestSwitchPreflight` (labels, sums, boundary), `TestSwitchRoutes` (no confirm on 422; fingerprint-drift 409). **Container:** preview with/without ticks, tick → re-preview → confirm appears. |
| Ineligible targets refused at preview AND execute (RETIRED/gated/active → 422 + details) | **Unit:** F9 matrix at `switch_preflight`, both routes, and `start_switch` (three layers). **Container:** curl each case. |
| Container full switch: A READY → switch to B ticking A → SSE stop(A OFF) → start(B) → LOADING → READY; A's port freed; no git commit | **Container (`container-setup.sh` scenario):** drive via UI; assert SSE phase sequence incl. `stopping (1/1)` detail, `curl` on A's port fails after, `git -C <unit_dir> rev-parse HEAD` identical before/after, unit-file mtimes identical. |
| Container failed switch (`FAKE_EXIT_1` target) → restore offer → A back to READY; dismiss also frees slot | **Unit:** `TestSwitchEngine` restore paths. **Container:** both branches (restore run; separate run for dismiss + verify a new switch is then accepted). |
| Slot exclusivity both directions | **Unit:** `TestSwitchSlot`. **Container:** switch during a long fake rollout → 409, and inverse. |
| 403/401 on all switch routes; zero writes in a switch | **Unit:** frozen-table behavioral test + `TestSwitchZeroWrites`. **Container:** bad-bearer curls; the rev-parse/mtime assertions above. |
| Guard debt: three guards red on seeded violations; two UI defects fixed | **Integration:** §7.1 seeded-violation procedure (recorded in commit message). **Unit:** guards green on clean source; static test asserts the fixed gate-notice condition string (`unit.rung === 'STANDBY'`) and `refreshOperation` presence; `git_startup_check` warning test for `*.roundhouse-tmp`. |
| Mobile at 390×844 and 768×1024; 44 px; desktop unchanged ≥ 1200 px | **Unit (static, honest scope per F6):** §7.2 static assertions. **Manual (in drill):** phone-sized viewport over LAN — no horizontal scroll, ACTIVE + stepper above the fold, targets tappable; desktop side-by-side check. |
| Live boltzmann drill: qwen3.6-coding → llama-server-gemma4 (:8093) → back, fleet ends qwen3.6-coding READY | `scripts/switch-drill.sh` (operator-authorized; may remain open at push per contract). |
| No build step; stdlib only; no German; no throughput figures | Existing stdlib-import AST test still green; review grep; no new t/s surfaces exist by construction. |

---

## 10. RISKS — top 3 mechanical-coder failure modes and the guards placed

1. **Switch worker "helpfully" borrows rollout machinery it must not have** — a daemon-reload after starting (cargo-culted from `_run_rollout`), or a git status/commit "for safety". **Guards:** F10's three interlocking assertions — the §7.1(d) AST rule that `_daemon_reload` calls appear only in `{_run_rollout, _run_rollback}`, the raising-stub `TestSwitchZeroWrites`, and the container rev-parse/mtime check — plus the spec's §2.1 table which simply has no such step.
2. **Slot/offer semantics regress when generalizing `_slot_free`** — the historically wedge-prone spot (the MVP2 fix-pass exists because of it): a coder adds switch phases by editing the `failed` branch, or forgets `restored`/`restore_failed` in the terminal set, wedging the slot after every restore. **Guards:** the change is confined to one constant (`OPERATION_TERMINAL_PHASES`) with `_slot_free`'s body otherwise untouched (spec §2.2 says so explicitly); the extended `test_slot_free_matrix` enumerates every (phase × offer × restored) cell for both kinds; both-directions 409 tests.
3. **Preview and execute disagree** — fit or fingerprint computed from different snapshots (torn reads), `suggest_stops` reimplemented in JS, or the UI ticking suggestions automatically (policy creep the contract forbids). **Guards:** §3.3's one-snapshot-per-request rule stated as a MUST; `confirm` minted only inside `switch_preflight`'s single code path; the UI spec routes every tick change through the server (no client arithmetic exists to drift); a static test asserts no `estimate` / byte-arithmetic tokens in the JS; `TestSwitchConfirm` pins canonicalization; the drift 409 test mutates exactly one `start_ts_mono`.

**Out of scope (do not build, per contract):** autonomous anything, pinned/protected-unit metadata, multi-host, llama-swap, autoscale, operation queueing, PWA/offline, theming beyond the existing palette, `reset-failed`/enable/disable verbs, a TURNTABLE section (F12), renamed routes/events/records (F5).

---

**Relay-worthy findings for the committer:** the S8 guard-debt characterization in MVP3.md is accurate and worse than "weak" in two spots — `test_post_routes_require_bearer` in `/home/mfritsche/src/roundhouse/mvp1/tests/test_server.py` (line 655) is literally `pass`, and `ROLLOUT_CALLSITES` (line 584) is dead data. The F3 fingerprint cannot be built from the existing snapshot's `since` field because `snapshot()` substitutes `now()` for OFF units (`roundhouse.py` line 1560) — hence the new `start_ts_mono` key, which forces the `test_snapshot_shape` update in `tests/test_watcher.py` (line 487). The existing `scripts/switch-drill.sh` is an MVP1 manual script and is rewritten, not created.
