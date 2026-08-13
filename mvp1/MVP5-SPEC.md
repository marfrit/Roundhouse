# Roundhouse MVP5 — Build Architecture & Work Breakdown

**File: `mvp1/MVP5-SPEC.md`** (beside `roundhouse.py`; `MVP5.md` at repo root stays the contract — its acceptance checklist is the definition of done, its Out-of-scope list is binding).

Grounded in: `MVP5.md` (contract), E-series (MVP2), F-series (MVP3), G-series (MVP4) — all stand, nothing here re-opens them; `mvp1/roundhouse.py` @ 118478e (6099 lines, Sections A → B → C → E [PARTs 1–4] → D); `mvp1/static/index.html` (2259 lines); 350 green tests. Recon findings that shaped this spec: **(1)** The override-comment precedent (`select_units`, lines 896–900) is a substring scan of the whole decoded file — the on-demand marker extends the *same mechanism*, but must land on the parsed `UnitFile` (a new field), because `select_units` returns only paths and the consent fence needs the flag at engine level. **(2)** `extract_param_profile` initializes `'alias': None` (line 711), so `profile.get('alias', unit_name)` at snapshot line 1602 returns `None` for units without `--alias` — **snapshot rows can carry `alias: null`**; every alias consumer in MVP5 needs an explicit stem fallback, not a `.get` default. **(3)** In `cmd_serve`, `poll_systemctl` starts (~line 5984) *before* `rollout_engine` is constructed (~5996) — tick-checked warm firing requires reordering engine construction above the thread starts (a pure-assignment constructor; safe). **(4)** `start_switch` (4581) claims the slot inside one `with self.watcher_lock:` block — the ideal single point to make queue-pop + consent re-check + slot claim atomic, killing the park→fire TOCTOU outright. **(5)** The route-table guard (test_server.py 870–985) has two allowlists that must both grow: `from_frozen` (POST fragments) and `get_only` (GET paths do_POST recognizes for 405). **(6)** Snapshot `mem` rows already carry `load_seconds` when (and only when) the source is a measured MemStore row (`lookup`, 1957–1965) — the fragment's "peak + load seconds, labelled" costs zero new sensing. **(7)** A value-only UI edit cannot add or remove comments (E1), so the marker genuinely cannot change through Roundhouse itself — the restart-to-change rule is airtight, not just policy. **(8)** `_slot_free`-relevant terminal writes are spread over six sites (`_update_phase` done, `_fail_rollout` restored/no-offer, `dismiss`, `_finish_rollback`, `_run_rollback` rollback_failed, `_run_restore` restored/restore_failed) — hooking each is six chances to deadlock under `watcher_lock`; polling `_slot_free` from the existing 3 s tick is one chance and zero new locks.

## 1. GLOBAL DECISIONS (H-series; implementers must not re-open them)

- **H1 — Marker: `# roundhouse: on-demand`, parsed in Section A, anywhere in the file, restart to change.** `UnitFile` gains field `on_demand: bool = False`. `parse_unit` sets it via the *same* substring rule as manage/ignore: `raw_str = raw.decode('utf-8', errors='replace')`; `on_demand = ('# roundhouse: on-demand' in raw_str) or ('; roundhouse: on-demand' in raw_str)`. Line position does not matter (consistency with the existing overrides beats a stricter comment-line scan; the shared wart — a marker string inside a quoted ExecStart argument would count — is inherited and stated in a code comment, accepted). Surfaced as snapshot row key `'on_demand'` (from `watcher.units[name].on_demand`; `test_snapshot_shape` frozen key set grows in the same task), in `build_deployment`'s `load_strategy` dict **and** in `serve_deployments`' overlaid `load_strategy` (it rebuilds the dict — both sites or the API lies). **Re-read semantics, stated honestly here, in the spec'd docstring, and in docs/ROUTING.md:** units are parsed once at startup; adding or removing the marker requires a Roundhouse restart. The UI cannot edit it (E1 value-only edits never touch comments — recon 7). No marker for `roundhouse.service` (not a selected unit, G8).
- **H2 — YAML by hand: a two-level shape-specific emitter with a frozen quoting rule; routes `/api/routing-config` (text/yaml) + `/api/routing-config.json` (application/json).** No generic YAML library grows in the codebase, not even hand-rolled: the emitter (§3.2) serializes exactly the fragment shape — a header comment block, one `model_list:` key, a list of entries, each entry a dict whose values are scalars or one-level dicts of scalars. Any deeper nesting is a programming error and asserts. Every string passes through one quoting function (§3.3); numbers/bools/None have fixed spellings. Content-Type: `text/yaml; charset=utf-8` and `application/json; charset=utf-8`. Both are unauthenticated GETs (contract Part 1.3) and pure reads: no file writes, no git, no subprocess beyond what `take_snapshot` already does (a `/proc/meminfo` read).
- **H3 — Entry shape: LiteLLM's standard `model_name` + `litellm_params` + `model_info` triple; engine prefix `openai/<bare alias>`; roundhouse metadata flattened into `model_info`.** `litellm_params.model = "openai/<logical>"` (the OpenAI-compatible provider prefix; llama-server registered the alias via `--alias`, so the upstream model param matches), `api_base = "http://<advertise-host>:<port>/v1"`, `api_key: "none"` (LiteLLM requires the key field for openai-provider entries; the backend ignores it). `model_info` carries flat keys (no `roundhouse:` sub-map — keeps the emitter at depth 2): `unit, logical, host, rung, on_demand, load_strategy, peak_bytes, peak_source, load_seconds`. Keys whose value would be `null` are **omitted** (peak/load unknown), except `on_demand`, which is always present. `peak_source` is the mem row's `label` string (house rule: every surfaced number carries its source; `load_seconds` exists only on measured rows — recon 6 — so `peak_source` labels both). Header comment lines, frozen: `# generated-by: roundhouse@<host>`, `# generated-at: <UTC ISO-8601 seconds, Z>`, `# warm-hook: POST http://<advertise-host>:<self-port>/api/warm`.
- **H4 — Warm resolution is dumb and total; the consent fence has exactly two layers, and the engine layer is atomic with the slot claim.** Resolution (§4.1): `unit` xor `logical`; both or neither → 400. `logical` matches against the same value the fragment advertises — `row['alias'] or <unit stem>` (recon 2) — over non-RETIRED selected units, after stripping a leading `<host>-` namespace prefix when present (so the fragment's `model_name` is directly usable); 0 matches → 404 `unknown_alias`, >1 → 422 `ambiguous_alias`. **Fence layer 1 (route/preflight):** `handle_warm` rejects unmarked targets (422 `not_on_demand`) and `warm_plan` (a new pure function, §4.2) selects stops **only** from on-demand-marked active units, feeding the *existing* `suggest_stops` F7 greedy rule with a filtered candidate pool; the resulting `(target, stops)` then goes through `switch_preflight` + `start_switch` **unchanged**. **Fence layer 2 (engine):** `start_switch` grows kwargs `origin='human', requester=None, warm_seq=None`; when `origin == 'warm'` it re-validates, *inside the same `with self.watcher_lock:` block that claims the slot* (recon 4): target marked, every stop marked (else `ActuationError('warm_consent...')`), and — when `warm_seq` is given — that the parked record with that seq is still pending (else `ActuationError('warm_cancelled')`), clearing `pending_warm` in the same lock hold. There is **no window** between consent check, cancel check, queue pop, and slot claim: this is the TOCTOU answer. Markers cannot change at runtime (H1), so the two layers are sufficient; the worker's generic re-preflight stays consent-blind by design (stated).
- **H5 — Queue: `pending_warm` engine field (depth 1), mutated only under `watcher_lock`; fired by the existing 3 s tick; in-memory only.** Fields on `RolloutEngine.__init__`: `self.pending_warm = None`, `self.last_warm = None`, `self.warm_seq = 0`. **Firing trigger: tick-checked** — `poll_systemctl` calls `rollout_engine.tick_pending_warm()` once per loop, *outside* `watcher_lock` (the method takes the lock itself; the slot-freeing writes are six scattered sites — recon 8 — and hooking them under the lock invites deadlock; max 3 s added latency, stated and accepted). Requires the cmd_serve reorder (recon 3). Fire flow §4.4: fresh snapshot, full re-plan + re-preflight, `start_switch(origin='warm', warm_seq=seq)`. Slot stolen by a human meanwhile → the parked warm stays parked and retries next tick (a human operation always outranks the queue, per contract). Any plan/preflight failure at fire time → the parked warm is **dropped** (no retry loop) with a disposition in `last_warm` — the proxy re-requests. Cancel = `POST /api/warm/cancel` (H7). Crash semantics: process death loses the queue — in-memory only, stated in spec, ROUTING.md, and the drill; the proxy retries. **Corollary invariant:** while `pending_warm` is non-None, every new warm goes through queue rules (dup → 200 `already_queued`, distinct → 409 `warm_queue_full`) even if the slot happens to be free (no queue-jumping; the tick fires the parked one within ≤ 3 s). A failed warm switch that carries a live restore offer **holds the slot** like any failed switch — the parked warm waits until a human restores or dismisses; deliberate: a failed actuation needs a human eye before the next autonomous one.
- **H6 — `/api/warm` status doctrine (frozen, §4.5):** 200 `already_warm` (resolved target rung ∈ `ACTIVE_RUNGS` — STARTING/LOADING count: it is warm or becoming warm, which also answers "warm the target of the running switch" naturally) | 200 `already_queued` (idempotency key: resolved unit name equality with the parked record); 202 `{"rollout_id"}` started | 202 `{"queued": true}` parked; 400 `bad_json`/`bad_body`; 401/403 per E8; 404 `unknown_unit`/`unknown_alias`; 409 `warm_queue_full`; 422 `not_on_demand` | `ambiguous_alias` | `consent_unfittable` (full fit arithmetic + consenting-candidates list + excluded-unmarked list) | `preflight_failed` (the switch_preflight body, as on `/api/switch`). **GET `/api/warm` is a read**: 200 `{"pending": <record|null>, "last": <record|null>}`, unauthenticated like every read (it exposes nothing beyond snapshot-grade state), served even unarmed (`pending: null`). GET on `/api/warm/cancel` → 405.
- **H7 — Cancel is `POST /api/warm/cancel`; no `do_DELETE` is introduced.** Keeps the all-mutations-are-gated-POSTs invariant, the frozen table shape, and the guard mechanics simple (`do_DELETE` stays the blanket 405 at line 2828). 200 `{"cancelled": true, "unit": ...}` | 404 `no_pending`; bearer-gated like every POST. `FROZEN_POST_ROUTES` grows by exactly `"/api/warm"` and `"/api/warm/cancel"` (9 entries). The routing-config GETs do **not** enter the POST table; they join do_POST's `is_get_route` list (405 on POST) and the guard's `get_only` set (recon 5). Requester attribution: optional header `X-Roundhouse-Requester` on `/api/warm`; sanitized (strip; keep only `[A-Za-z0-9._@ -]`; truncate to 64; empty result → discard) with fallback `'token'`. Switch records gain `origin: 'human'|'warm'` and `requester: <str|null>` (human switches: `'human'`/`null`); rollout records are untouched (no origin key).
- **H8 — UI is additive-minimal: an origin tag on the stepper, on-demand text on unit rows + detail pane; the parked warm has no UI.** Stepper: when `state.rollout.origin === 'warm'`, a neutral `.origin-tag` span renders `· warm (<requester>)` beside the phase chips. Unit rows: `detailSpan` appends `'· on-demand '` when `unit.on_demand` (same pattern as `strategy_note`; neutral color, no new red/amber sharers). Detail pane: one table row `Warm-up` → `on-demand (marker present)`, rendered only when true. No new pages, no queue widget — the parked warm is observable via `GET /api/warm` only ("nothing autonomous is invisible" binds *actions*: every fired warm is a full switch record in the stepper/SSE/GET; a parked warm has not acted). SSE payloads unchanged (the UI refetches the full record via `refreshOperation`, which carries `origin`).
- **H9 — Guard evolution is enumerable and `ROLLOUT_CALLSITES` does not change.** The warm path reuses `start_switch` → `_stop_unit`/`_start_unit`; no new verbs, no new gateways, no new arming, no new write functions. Grows: `FROZEN_POST_ROUTES` (+2), the guard's `from_frozen` fragment set (+`'/api/warm'`, `'/api/warm/cancel'`), `get_only` (+`'/api/routing-config'`, `'/api/routing-config.json'`, and `'/api/warm'` appears via from_frozen), the behavioral 403 list (+2 POSTs). Everything else in `TestWriteGuards` is untouched and must stay green against the grown source.
- **H10 — Advertised host is a CLI flag: `--advertise-host`, default `os.uname()[1]`.** The contract's `api_base` example (`boltzmann.fritz.box`) is LAN-DNS-qualified; the kernel hostname is not, and `socket.getfqdn()` is environment-dependent lying-prone. One optional flag on `cmd_serve`, stored as `server.advertise_host`, used by the fragment's `api_base` and warm-hook header. ROUTING.md instructs the operator to add `--advertise-host boltzmann.fritz.box` to the unit's ExecStart; the default keeps container drills self-contained. No other behavior reads it.

## 2. FILE / SECTION LAYOUT

```
mvp1/
  MVP5-SPEC.md                  # this file
  roundhouse.py                 # extended: SECTION E PART 5; small edits in A/B/C/D/E as listed
  static/index.html             # extended in place (one file, textContent-only)
  scripts/
    container-setup.sh          # extended: on-demand markers on fake A + fake B units
    warm-drill.sh               # NEW: container warm/queue/fence drills + live pull checklist
  tests/
    test_parser.py              # extended (first time since MVP1): marker tests
    test_watcher.py             # extended: snapshot shape gains 'on_demand'
    test_server.py              # extended: guard evolution (§7.1), static UI checks
    test_actuation.py           # extended: warm route/queue/fence classes (§7.3)
    test_routing.py             # NEW: emitter, quoting, entries, inclusion, resolution, warm_plan
docs/
  ROUTING.md                    # NEW: hossenfelder wiring note (§6)
```

`roundhouse.py` gains, after `enablement_preflight` (end of Part 4, before the Section D banner):

```python
# ===== SECTION E PART 5: ROUTING-CONFIG + WARM (generation is a pure read; warm reuses start_switch; no new verbs, no file writes, no git) =====
```

**Part 5 contents (complete list):** `YAML_AMBIGUOUS`; `SAFE_BARE_RE`; `_yaml_str()`; `_yaml_scalar()`; `emit_routing_yaml()`; `logical_of()`; `include_in_routing()`; `build_routing_entries()`; `routing_meta()`; `resolve_warm_target()`; `warm_plan()`. Engine methods (`warm_state`, `tick_pending_warm`, `_fire_warm`) are added to the existing `RolloutEngine` class body (lexically Part 2), exactly as MVP3 did for the switch methods.

**Edits to existing code (exhaustive):**
- **Section A:** `UnitFile` gains `on_demand: bool = False`; `parse_unit` sets it per H1 (two added lines + constructor arg). `select_units`, `build_deployment`'s `load_strategy` dict gains `'on_demand': unit.on_demand`. Nothing else.
- **Section B:** `snapshot()` unit rows gain `'on_demand': unit.on_demand` (read from `self.units[unit_name]`, already in scope at line 1590). Nothing else.
- **Section C:** `do_GET`: routes `/api/routing-config` → `serve_routing_config()`, `/api/routing-config.json` → `serve_routing_config_json()`, `/api/warm` → `serve_warm_state()`, `/api/warm/cancel` → `error_405()` (all exact-match `elif`s before the `/api/units/` prefix branch — none of the new paths collides with existing prefixes). `do_POST`: `is_post_route` gains `route == '/api/warm'` and `route == '/api/warm/cancel'`; `is_get_route` gains the two routing-config paths; dispatch gains `handle_warm()` / `handle_warm_cancel()`. New handlers: `serve_routing_config`, `serve_routing_config_json`, `serve_warm_state`, `handle_warm`, `handle_warm_cancel`. `serve_deployments`' overlaid `load_strategy` gains `'on_demand': unit.on_demand`.
- **Section E Part 1:** `FROZEN_POST_ROUTES` grows to 9 (`"/api/warm"`, `"/api/warm/cancel"` appended).
- **Section E Part 2:** `RolloutEngine.__init__` gains `self.pending_warm = None; self.last_warm = None; self.warm_seq = 0`. `start_switch` signature → `start_switch(self, target, stops, confirm, origin='human', requester=None, warm_seq=None)`; the warm branch per H4/§4.3 inside the existing lock block; the record dict gains `"origin": origin, "requester": requester`. `rollout_public_record`'s switch branch adds `origin` (default `'human'`) and `requester` (default `None`). No change to `_run_switch`, `_slot_free`, `_fail_rollout`, `dismiss`, `rollback`.
- **Section D:** `--advertise-host` argparse flag (H10; passed through to the server object). **Reorder:** `rollout_engine = RolloutEngine(...) if args.actuate else None` moves *above* `poll_thread.start()` (recon 3; constructor is pure assignment — safe). `poll_systemctl` gains, after the `with watcher_lock:` block and before the sleep: `if rollout_engine: rollout_engine.tick_pending_warm()` (outside the lock — MUST, the method locks internally). `ThreadingHTTPServer.__init__` stores `advertise_host`.
- **`static/index.html`:** §5 only.

## 3. GENERATION SPEC

### 3.1 Inclusion + entry derivation (pure; snapshot-in)

```python
def logical_of(row: Dict) -> str:
    # row['alias'] can be None (recon 2): fall back to the unit stem
    return row.get('alias') or row['unit'][:-len('.service')]

def include_in_routing(row: Dict) -> bool:
    # Contract Part 1.2. Hot always; cold only if marked; an on-demand entry stays
    # listed through its own warm-up (STARTING/LOADING); STANDBY/FAILED/RETIRED never.
    if row.get('retired'): return False
    rung = row.get('rung')
    if rung in ('READY', 'BUSY'): return True
    if row.get('on_demand') and rung in ('OFF', 'STARTING', 'LOADING'): return True
    return False

def build_routing_entries(snapshot: Dict, advertise_host: str) -> List[Dict]
def routing_meta(snapshot: Dict, advertise_host: str, self_port: int, now_utc) -> Dict
    # {"generated_by": "roundhouse@<host>", "generated_at": "<ISO Z>",
    #  "warm_hook": "POST http://<advertise-host>:<self_port>/api/warm"}
```

An unmarked STARTING/LOADING unit is deliberately absent (contract wording is literal: hot = READY/BUSY); it appears within one tick of READY — stated in a code comment. Each entry (host = `snapshot['host']`, row-derived; entries sorted by `model_name` for determinism):

```python
{"model_name": f"{host}-{logical_of(row)}",
 "litellm_params": {"model": f"openai/{logical_of(row)}",
                    "api_base": f"http://{advertise_host}:{row['port']}/v1",
                    "api_key": "none"},
 "model_info": {"unit": row['unit'], "logical": logical_of(row), "host": host,
                "rung": row['rung'], "on_demand": row['on_demand'],
                "load_strategy": "on-boot" if row['enabled'] else "manual",
                # only when known (H3 null-omission):
                "peak_bytes": mem['bytes'], "peak_source": mem['label'],
                "load_seconds": mem.get('load_seconds')}}
```

`mem = row.get('mem') or {}`; `peak_bytes`/`peak_source` emitted only when `mem.get('bytes')` is non-null; `load_seconds` only when non-null.

### 3.2 Emitter contract (`emit_routing_yaml(meta: Dict, entries: List[Dict]) -> str`)

Output, byte-exact template (2-space indent steps; `\n` line endings; trailing newline):

```yaml
# generated-by: roundhouse@boltzmann
# generated-at: 2026-08-13T12:00:00Z
# warm-hook: POST http://boltzmann.fritz.box:8090/api/warm
model_list:
  - model_name: boltzmann-qwen3.6-coding
    litellm_params:
      model: openai/qwen3.6-coding
      api_base: "http://boltzmann.fritz.box:8085/v1"
      api_key: "none"
    model_info:
      unit: qwen3.6-coding.service
      ...
```

Rules: header lines are emitted verbatim from `routing_meta` (server-controlled strings only — no user data reaches a comment line; asserted by construction, the function takes no row input). Empty fleet → `model_list: []` after the header. Entry emission walks the dict in the fixed key order above (`model_name`, `litellm_params`, `model_info`); sub-dict values must be scalars — a dict or list value below depth 2 raises `AssertionError` (programming error, not a 500 path: entry construction cannot produce one). Keys are emitted bare (all are fixed identifiers matching `[a-z_]+`; asserted).

### 3.3 Scalar quoting (`_yaml_scalar` / `_yaml_str`) — the injection-critical part, frozen

- `bool` → `true` / `false`; `int` → `str(v)`; `float` → `repr(v)`; `None` never reaches the emitter (H3 omission policy; assert).
- `str` → bare **iff** `SAFE_BARE_RE = re.fullmatch(r'[A-Za-z0-9._/-]+', s)` **and not** `re.fullmatch(r'[0-9.+-]+', s)` (would parse as a number) **and** `s.lower() not in YAML_AMBIGUOUS = {'true','false','yes','no','on','off','null','none','~'}`. Everything else → double-quoted: `"` + escaped + `"`, escaping exactly: `\` → `\\`, `"` → `\"`, any char with codepoint < 0x20 or == 0x7f → `\xNN` (two lowercase hex digits). No other escapes, no single quotes, no block scalars, ever. **Consequence a coder must not "optimize" away:** a hostile alias like `evil\n  - model_name: pwned` or `x: y #z` is a single double-quoted token on one line — colons, hashes, newlines, leading `-`, and YAML-keyword strings cannot change document structure because the *only* unquoted strings are those matching the safe class. `api_base` and `api_key` always quote (they contain `:` / are ambiguous-adjacent) — visible in the golden file.

### 3.4 Routes + handlers

| route | method | success | notes |
|---|---|---|---|
| `/api/routing-config` | GET | 200 `text/yaml; charset=utf-8`, body = `emit_routing_yaml(...)` | unauthenticated read; POST → 405 |
| `/api/routing-config.json` | GET | 200 `application/json; charset=utf-8`, body = `{**routing_meta(...), "model_list": entries}` | same entries (JSON twin, same null-omission) |

Both handlers: `snapshot = self.server.take_snapshot()` (one snapshot per request), `now_utc = datetime.now(timezone.utc)`; zero writes, zero subprocess, zero git (§7.2 three-leg proof). `advertise_host = getattr(self.server, 'advertise_host', None) or snapshot['host']`.

## 4. WARM SPEC

### 4.1 Resolution (`resolve_warm_target(logical, unit, snapshot, units) -> tuple`)

Pure; returns `('ok', unit_name)` or `('error', status:int, code:str, extra:Dict)`. Rules in order: exactly one of `logical`/`unit` non-None (route already 400'd otherwise — function asserts). `unit` path: `unit in units` else `(404, 'unknown_unit')` (RETIRED units resolve here; the retired refusal happens next in the handler, with the standard check-row wording). `logical` path: if it starts with `f"{snapshot['host']}-"`, strip that prefix; match the remainder (and, failing that, the unstripped original) for equality against `logical_of(row)` over non-RETIRED rows; 0 matches → `(404, 'unknown_alias')`; >1 → `(422, 'ambiguous_alias', {"units": [names]})`; 1 → ok.

### 4.2 `warm_plan(target, snapshot, units, cgroup_cache, mem_store) -> Dict` (pure; the consent-filtered F7)

```python
consenting = [u for u in snapshot['units']
              if u['unit'] != target and not u.get('retired')
              and u.get('rung') in ACTIVE_RUNGS
              and units[u['unit']].on_demand]          # THE FENCE, layer 1
excluded  = [same predicate but NOT on_demand]          # named in the 422, with residency
estimate, estimate_source = _estimate_start_bytes(target, profile, mem_store)
budget = snapshot['mem']['available_bytes'] (None -> 0, with source note)
stops = suggest_stops(target, [], consenting, estimate, budget, [], cgroup_cache, mem_store)
freed_by = [_freed_bytes(...) per stop]; fits = estimate + HEADROOM_BYTES <= budget + sum(freed)
```

Returns `{"fits": bool, "stops": [...], "estimate_bytes", "estimate_source", "mem_available_bytes", "headroom_bytes", "freed_by": [{"unit","bytes","source"}], "shortfall_bytes": max(0, estimate+HEADROOM−budget−freed), "consenting": [{"unit","rung","resident_bytes","resident_source"}], "excluded_unmarked": [{"unit","rung","resident_bytes"}]}`. The greedy walk itself is the **unmodified** `suggest_stops` (F7: resident-bytes descending, name ascending, until fit or exhaustion) — only the candidate pool is filtered. `stops` may be empty (target fits without stopping anything): still a valid warm.

### 4.3 `handle_warm` — exact sequence (frozen order)

1. Parse body → 400 `bad_json`. `logical`/`unit` extraction: present values must be strings; **exactly one** given, else 400 `bad_body` (`"give exactly one of logical or unit"`).
2. `engine` absent → 500 `no_engine` (unreachable unarmed — 403 fires first).
3. `resolve_warm_target` → 404/422 per §4.1. `requester` per H7 sanitization.
4. `snap = locked_snapshot(watcher)`; `u = units[target]`.
5. `u.retired` → 422 `{"error": "preflight_failed", "checks": [<the standard retired row>]}`.
6. Target row rung ∈ `ACTIVE_RUNGS` → 200 `{"status": "already_warm", "unit": target, "rung": rung}`.
7. `not u.on_demand` → 422 `{"error": "not_on_demand", "unit": target, "detail": "unit is not marked '# roundhouse: on-demand' — a warm request may neither start nor stop it (add the marker and restart roundhouse)"}`.
8. **Queue gate**, under `watcher_lock`: if `engine.pending_warm` is not None → same unit → 200 `{"status": "already_queued", "unit", "pending": rec}`; different → 409 `{"error": "warm_queue_full", "pending": rec}`. Else if `not _slot_free(engine.current)` → park: `engine.warm_seq += 1`; `engine.pending_warm = {"seq", "unit": target, "logical": <as given|null>, "requester", "requested_at": time.time()}` → 202 `{"queued": true, "unit": target}`. Else fall through to 9 (slot free, nothing pending).
9. `plan = warm_plan(target, snap, units, cgroup_cache, mem_store)`; `not plan['fits']` → 422 `{"error": "consent_unfittable", "unit": target, "detail": <human sentence naming the shortfall and that only consenting units may be stopped>, **plan-minus-fits-and-stops}`.
10. `pf = switch_preflight(target, plan['stops'], watcher, units, self_port)`; not ok → 422 `{"error": "preflight_failed", **pf-minus-confirm}` (identical body policy to `/api/switch`).
11. `engine.start_switch(target, plan['stops'], pf['confirm'], origin='warm', requester=requester)` → 202 `{"rollout_id": sw_id, "stops": plan['stops']}`. `ActuationError` containing `operation_in_progress` (a human claimed the slot between 8 and 11) → re-enter step 8's locked block once (park / dup / full); `warm_consent` (cannot happen from this path; belt) → 422 `not_on_demand`; anything else → 400 `warm_error` with the message.

`handle_warm_cancel`: body ignored; under `watcher_lock`: `pending_warm` None → 404 `{"error": "no_pending"}`; else record → `last_warm = {..., "disposition": "cancelled", "at": now}`, `pending_warm = None` → 200 `{"cancelled": true, "unit": ...}`.

`serve_warm_state` / `engine.warm_state()`: under `watcher_lock`, return copies: `{"pending": rec|None, "last": last|None}`.

### 4.4 Engine methods + firing

`start_switch` warm branch (inside the existing `with self.watcher_lock:`, before the slot check is fine but **after** is wrong — order: slot check first (unchanged), then, `if origin == 'warm'`): (a) target in `self.units` and `.on_demand`, else raise `ActuationError("warm_consent: target <t> is not marked on-demand")`; (b) every stop in `self.units` and `.on_demand`, else raise `ActuationError("warm_consent: stop <s> is not marked on-demand")`; (c) `if warm_seq is not None`: `self.pending_warm` non-None and `['seq'] == warm_seq`, else raise `ActuationError("warm_cancelled")`; then `self.pending_warm = None`. Record gains `"origin"`, `"requester"`.

```python
def tick_pending_warm(self):
    with self.watcher_lock:
        p = self.pending_warm
        if p is None or not _slot_free(self.current):
            return
        seq, target, requester = p['seq'], p['unit'], p['requester']
    self._fire_warm(target, requester, seq)     # outside the lock — plan/preflight snapshot inside
```

`_fire_warm(target, requester, seq)`: mirrors handler steps 5–11 from a **fresh** `locked_snapshot` (contract: re-preflight at fire time), with drop semantics instead of HTTP: retired/not-marked (only possible after a restart repopulated `pending`? impossible — restart empties the queue; kept as belt) / rung-active → drop `already_warm` / `not_on_demand`; `warm_plan` unfittable → drop `consent_unfittable`; `switch_preflight` fail → drop `preflight_failed`; `start_switch(..., origin='warm', requester=requester, warm_seq=seq)` success → `last_warm = {"unit", "requester", "disposition": "started", "rollout_id", "at"}` (pending already cleared atomically in start_switch); `ActuationError` `operation_in_progress` → **leave parked** (return; retry next tick); `warm_cancelled` → nothing to do (`last_warm` already written by cancel). "Drop" = under `watcher_lock`: `if self.pending_warm and self.pending_warm['seq'] == seq: self.pending_warm = None; self.last_warm = {..., "disposition": <code>, "detail": <human>, "at": now}`.

### 4.5 Route table additions (frozen)

| route | method | success | errors |
|---|---|---|---|
| `/api/warm` | POST | 200 `already_warm`\|`already_queued`; 202 `{"rollout_id","stops"}`\|`{"queued":true,"unit"}` | 400 `bad_json`/`bad_body`; 401/403 per E8; 404 `unknown_unit`/`unknown_alias`; 409 `warm_queue_full`; 422 `not_on_demand`/`ambiguous_alias`/`consent_unfittable`/`preflight_failed` |
| `/api/warm` | GET | 200 `{"pending","last"}` | — (read; works unarmed) |
| `/api/warm/cancel` | POST | 200 `{"cancelled":true,"unit"}` | 404 `no_pending`; 401/403; GET → 405 |
| `/api/routing-config`(.json) | GET | 200 per §3.4 | POST → 405 |

Status doctrine unchanged (400 malformed / 401-403 E8 / 404 unknown resource / 405 method / 409 concurrency / 422 validation-against-the-world). No slot 409 exists on `/api/warm` — a busy slot *parks*, by contract.

## 5. RECORDS / UI SPEC

1. **Record:** switch records carry `origin` + `requester` from creation; `rollout_public_record` switch branch emits both (`rollout.get('origin', 'human')`, `rollout.get('requester')` — old in-memory records degrade gracefully). SSE `rollout` event payload unchanged (kind/unit/phase/detail/ok/ts); the UI already refetches the full record on every event (`refreshOperation`), so `origin` arrives with no stream change.
2. **Stepper tag:** in `renderRolloutStepper`, after the phase chips: if `rollout.origin === 'warm'`, append `<span class="origin-tag">` with textContent `'· warm (' + (rollout.requester || 'token') + ')'`; class styled neutral (existing muted color variable), defined outside the media queries. Nothing else in the stepper changes; restore-offer button text/behavior identical for warm-origin switches.
3. **Unit rows:** in `renderUnitList`'s detail assembly, after the `strategy_note` block: `if (unit.on_demand) { detail += '· on-demand '; }`.
4. **Detail pane:** one added table row (`Warm-up` / `on-demand (marker present)`) rendered only when `unit.on_demand`; absent otherwise (no "off" row — unmarked is the default, not a state worth a line).
5. **No queue UI, no new pages, no localStorage; textContent-only rule stands** over all new code (existing static assertions re-run over the grown file).

## 6. docs/ROUTING.md OUTLINE (documentation only; touching hossenfelder is out of scope)

1. **What Roundhouse serves:** the two endpoints, content types, that generation is live (pull any time), unauthenticated reads; the namespacing rule (`boltzmann-<alias>`, bare alias in `model_info.logical`).
2. **Wiring LiteLLM (hossenfelder :4000):** LiteLLM loads a static config — pull-then-merge pattern: a cron/systemd-timer `curl -fsS http://boltzmann.fritz.box:8090/api/routing-config -o /etc/litellm/fragments/boltzmann.yaml` plus a merge step into the served config and a LiteLLM reload; note the fragment is a complete `model_list` document, merge = concatenating the lists. **Warning box: never point two hosts' fragments at the same `model_name` without namespacing** — the host prefix exists because `qwen3.6-coding` serves on both boltzmann and ampere :8085; a future ampere fragment must carry `ampere-`.
3. **Wiring the bespoke llm-proxy:** fetch the `.json` twin; map `model_list[*].model_name` → `litellm_params.api_base`; treat `model_info.on_demand == true` + rung `OFF` as "call the warm hook first".
4. **The warm hook:** URL comes from the fragment's own header comment; request/response contract (the §4.5 table, with example bodies); the cold-path recipe: POST warm → 202 → poll the model endpoint (or `GET /api/warm` + `/api/rollouts/<id>`) until READY → send the original request; 200 `already_warm` means go straight through; 409 `warm_queue_full` / 422 → surface to the caller, do not spin.
5. **Token provisioning:** the warm hook is a mutation — copy the content of boltzmann's `~/.config/roundhouse/token` into the proxy host's secret store; send `Authorization: Bearer <token>` and optionally `X-Roundhouse-Requester: llm-proxy`; rotation = replace file + restart Roundhouse + update the secret.
6. **Marker how-to:** add `# roundhouse: on-demand` anywhere in the unit file; **restart roundhouse afterwards — units are parsed once at startup**; the marker means BOTH may-be-started AND may-be-stopped (symmetric consent, verbatim from the contract); the always-on trio stays unmarked.
7. **Operational notes:** queue depth 1, in-memory (a Roundhouse restart empties it — retry); a failed warm switch holds the slot until a human restores/dismisses; `--advertise-host boltzmann.fritz.box` belongs in the ExecStart so generated URLs resolve off-box.

## 7. TEST / GUARD SPEC

### 7.1 Guard evolution (`test_server.py::TestWriteGuards`; T2)

1. Route-table test: behavioral list gains `'/api/warm'`, `'/api/warm/cancel'` (→ 403 unarmed); expected frozen set → the 9 entries; `from_frozen` gains the two literals; `get_only` gains `'/api/routing-config'`, `'/api/routing-config.json'`.
2. `ROLLOUT_CALLSITES` **unchanged** — asserted by the existing frozen 4-name literal simply staying as-is; the spec states it so no task "helpfully" adds a name. Write-verb, subprocess-gateway, file-write, snapshot-lock, arming guards: zero edits, must stay green over the grown source (the warm/generation code contains no write verbs, no subprocess, no writes, and every new `snapshot()` access goes through `locked_snapshot` or `take_snapshot`).
3. **Seeded-violation acceptance** (one-off at integration, seed → red → unseed, red names in the commit message): (s1) a `'/api/warm/force'` dispatch branch in `do_POST` → route-table guard red; (s2) a bare `watcher.snapshot()` in `serve_routing_config` → snapshot-lock guard red; (s3) `run_actuate(["systemctl","--user","start","--",target], ...)` call added inside `tick_pending_warm` → call-site whitelist red (the warm path may actuate only through `start_switch`).

### 7.2 Generation tests (`tests/test_routing.py`; T1)

- `TestYamlQuoting`: table-driven over the frozen rule — bare survivors (`qwen3.6-coding`, `boltzmann-x`, `on-boot`, `a/b_c.d`); forced-quote: `''` (empty), `none`, `NULL`, `Yes`, `8085`, `3.14`, `-x`… wait `-x` matches safe class → bare, and a leading `-` inside a flow value is safe only mid-line — **decide: `-` loses bare status when it is the first character** (add `not s.startswith('-')` to the bare rule; frozen); strings with `:` (`http://…`), `#`, spaces, `"`, `\`, newline, tab, `\x7f`, a full injection payload (`x"\n  - model_name: pwned`) — each asserting the exact emitted token. Independent-checker leg: every emitted value line in a hostile-fleet document either ends in a safe-bare token or a balanced double-quoted token with no raw control bytes (the test reimplements the check; it must not call `_yaml_str`).
- `TestRoutingEntries`: `logical_of` alias-null fallback; inclusion matrix (READY/BUSY unmarked in; OFF unmarked out; OFF/STARTING/LOADING marked in; STANDBY marked out; FAILED marked out; RETIRED marked out); namespacing `host-`; metadata null-omission (no `peak_bytes` key when mem unknown; `on_demand` always present); sort order; `api_key` present and quoted.
- `TestRoutingGolden`: a fixture snapshot (3 hot + 1 cold-marked + 1 cold-unmarked + 1 STANDBY + 1 RETIRED) → byte-exact YAML golden string (frozen in the test) + JSON twin dict equality; header lines incl. warm-hook URL built from advertise-host + port.
- `TestWarmResolution`: unit hit/miss; alias hit; alias-null unit resolvable via stem; namespaced `boltzmann-<alias>` strip; two units sharing an alias → `ambiguous_alias` with both names; retired excluded from alias pool.
- `TestWarmPlan`: consent filter (unmarked active units never in `stops`, always in `excluded_unmarked`); F7 order preserved (byte-identical picks vs calling `suggest_stops` directly on the filtered pool); fits-without-stops → `stops: []`, `fits: true`; unfittable arithmetic (shortfall, consenting list, labels) both sides of the boundary.

### 7.3 Warm engine/route tests (`test_actuation.py`; classes per §8 ownership)

- `TestWarmConsentFence` (T1, the adversarial matrix): **layer 2 direct** — `start_switch(target_marked, stops=[unmarked], confirm, origin='warm')` raises `warm_consent`, slot stays free, recorder saw zero actuation; unmarked *target* likewise; the same calls with `origin='human'` succeed (the fence binds warm only); **TOCTOU** — park, then cancel, then `start_switch(..., warm_seq=old_seq)` raises `warm_cancelled` and `pending_warm` stays None; park, fire with matching seq → `pending_warm` cleared inside the call (assert None immediately after, no tick involved).
- `TestWarmEngineQueue` (T1, engine level, `_EngineHarness`): park while slot busy; `tick_pending_warm` no-ops while busy; fires after the running switch terminates (scripted terminal write then one tick call) with a *fresh* plan (stub watcher mutated between park and fire; assert the fired stops reflect the new snapshot); slot stolen between check and start → still parked, fires on a later tick; fire-time preflight failure → dropped, `last_warm.disposition` correct, `pending` None; target became READY while parked → dropped `already_warm`; restart-empty (new engine → `pending_warm is None`).
- `TestWarmRoutes` (T2): 403 unarmed / 401 bad token on both POSTs; GET `/api/warm` 200 unauthenticated (and while unarmed); GET `/api/warm/cancel` 405; 400 both/neither/non-string; 404 unknown unit + unknown alias; 422 ambiguous / not_on_demand (exact detail string) / consent_unfittable (arithmetic keys frozen: `estimate_bytes, estimate_source, mem_available_bytes, headroom_bytes, freed_by, shortfall_bytes, consenting, excluded_unmarked`) / preflight_failed (no `confirm` key); 200 already_warm for READY **and** LOADING targets; 202 started body carries `stops`; requester sanitization table (header absent → `'token'`; hostile header stripped).
- `TestWarmQueueRoutes` (T2): the full matrix — busy slot → 202 queued; dup → 200 already_queued; distinct → 409 with `pending` echoed; cancel → 200 then GET shows `last.disposition == 'cancelled'`; cancel with nothing → 404; pending-non-None-but-slot-free → still queue rules (no queue-jump).
- `TestGenerationZeroWrites` (T2): GET both generation routes and GET `/api/warm` with `_atomic_write`/`run_git`/`run_actuate` monkeypatched to raise and a `subprocess` recorder → 200s, recorders empty. A full warm-triggered switch with `run_git`/`_atomic_write` raising → completes (engine leg; the switch worker already proves this for kind=switch, re-asserted with origin='warm').
- `TestWarmRecord` (T1): record + `rollout_public_record` carry `origin`/`requester` for warm and `'human'`/`None` for the plain route; old dict without the keys → defaults, no KeyError.
- `test_parser.py` (T1): marker positive (`#` and `;` forms, any line), negative (absent; `on-demandX` guarded by… substring rule — `'# roundhouse: on-demand'` is a prefix of `'# roundhouse: on-demandX'`: **accepted wart, tested as matching**, consistent with manage/ignore), default False, RETIRED file can carry it (parse-level; consent still refused at retired checks). `test_watcher.py` (T1): `test_snapshot_shape` gains `'on_demand'`.

### 7.4 Static UI (T3, `test_server.py`): `origin-tag` class + `'· warm ('` string present; `on-demand` row string present; no new `innerHTML`/localStorage; existing assertions green over the grown file.

## 8. WORK BREAKDOWN — 3 tasks, composable without coder contact

Cross-task interfaces frozen in §§3–5 (signatures, JSON/YAML shapes, route table, status doctrine, exact strings). Merge order **T1 → (T2 ∥ T3) → integration** (seeded run §7.1(3) + container drills §9 + live pull).

**T1 — Marker + generation pure functions + warm plan + engine** *(first; T2/T3 depend only on its frozen shapes)*
- **Writes:** Section A (`UnitFile.on_demand`, `parse_unit`, `build_deployment.load_strategy`); Section B (snapshot row key); Section E Part 5 complete (`YAML_AMBIGUOUS`…`warm_plan`); Part 2 engine edits (`__init__` fields, `start_switch` kwargs + warm branch + record keys, `warm_state`, `tick_pending_warm`, `_fire_warm`, `rollout_public_record` origin/requester); Section D (`--advertise-host`, engine-construction reorder, tick hook line, server attr).
- **Tests owned:** all of `tests/test_routing.py`; `TestWarmConsentFence`, `TestWarmEngineQueue`, `TestWarmRecord` in test_actuation.py; test_parser.py marker tests; test_watcher.py snapshot shape. **Must not touch:** Section C, `index.html`, `TestWriteGuards`.
- **Self-test:** `cd mvp1 && python3 -m unittest discover -s tests -v` — every pre-existing test green unmodified **except** `test_snapshot_shape` (one-key update); plus `python3 -c` proof that `start_switch(..., origin='warm')` with an unmarked stop raises on a scratch harness.

**T2 — Routes + handlers + guards** *(parallel with T3)*
- **Writes:** Section C complete (§2 list: two GET servers, `serve_warm_state`, `handle_warm`, `handle_warm_cancel`, `do_GET`/`do_POST` edits, `serve_deployments` on_demand); Section E Part 1 (`FROZEN_POST_ROUTES` 9 entries).
- **Tests owned:** `TestWarmRoutes`, `TestWarmQueueRoutes`, `TestGenerationZeroWrites`; §7.1 guard updates; documents seeds s1–s3 without running them.
- **Self-test:** full discover green; curl smoke against `--serve --actuate` in a scratch repo: routing-config YAML parses by eye + `.json` twin, warm on a marked OFF fixture 202, dup-queue 409/200 legs, cancel, 401/403.
- **Wire freeze note for T2:** `warm_plan`/`resolve_warm_target`/`start_switch(origin=...)`/`engine.warm_state()` signatures are §4-frozen; T2 calls, never modifies, Section E.

**T3 — UI + docs + scripts** *(depends only on frozen JSON shapes)*
- **Writes:** `index.html` §5 complete; `docs/ROUTING.md` per §6; `scripts/container-setup.sh` (append the marker comment to fake-A and fake-B unit files; llama-task fake stays unmarked — it is the fence-drill victim); `scripts/warm-drill.sh` NEW (checklist pattern: performs zero actuation itself beyond curls the operator triggers; §9 rows 4–7 + live row).
- **Tests owned:** §7.4 static assertions.
- **Self-test:** `python3 -m unittest tests.test_server -v`; container drills end-to-end; desktop ≥ 1200 px side-by-side check.

**Shared-file ownership:** `roundhouse.py` — T1 owns A/B/E/D, T2 owns C (+Part 1 constant); `test_actuation.py` — T1 owns Fence/EngineQueue/Record, T2 owns Routes/QueueRoutes/ZeroWrites; `test_server.py` — T2 owns `TestWriteGuards`, T3 owns statics; nobody edits another task's classes or any E/F/G-era test beyond the listed frozen-set updates.

## 9. TEST PLAN — mapped 1:1 to MVP5.md's acceptance checklist

| criterion | proven by |
|---|---|
| Golden YAML + JSON twin: hot in, cold+marked in, cold+unmarked out, STANDBY/FAILED/RETIRED out, namespaced, metadata + sources, warm-hook header | **Unit:** `TestRoutingGolden` + `TestRoutingEntries`. **Container:** pull both routes over the fake fleet; diff against expected entry set. |
| Marker parsed (Section A), surfaced in snapshot rows + deployment records; unmarked → `on_demand: false` | **Unit:** test_parser marker tests; snapshot-shape; a deployments-route assertion on both `load_strategy` sites. **Container:** `/api/units` + `/api/deployments` show the flags. |
| Consent fence both directions, preflight AND engine; unfittable-with-only-consenting → 422 arithmetic | **Unit:** `TestWarmPlan` (filter), `TestWarmRoutes` (422s), `TestWarmConsentFence` (engine layer + TOCTOU). **Container:** fence drill below. |
| Warm drill: A (marked) READY, B (marked) cold; warm B → auto-stop A, B READY, record `origin: warm`; repeat → 200 already_warm | **Container (`warm-drill.sh`):** curl warm B with bearer; watch SSE/stepper; assert `GET /api/rollouts/<id>` has `origin: 'warm'`, requester; re-curl → `already_warm`. |
| Queue drill: long human switch; warm parks 202; second 409; dup 200; fires after slot frees with re-preflight | **Unit:** `TestWarmEngineQueue` + `TestWarmQueueRoutes`. **Container:** long fake switch (slow-loading fake), the four curls, then observe the fired switch ≤ 3 s after terminal. |
| Fence drill: only fit requires stopping unmarked llama-task → 422, llama-task untouched | **Container:** size fixture estimates so consenting stops cannot cover; assert 422 `consent_unfittable` names llama-task under `excluded_unmarked`; `systemctl --user is-active` unchanged. |
| 403/401 on `/api/warm`; generation unauthenticated; zero writes in generation (three legs) | **Unit:** `TestWarmRoutes` auth legs; `TestGenerationZeroWrites`; AST guards unchanged. **Container:** bad-bearer curls; `git rev-parse HEAD` + unit mtimes identical across 50 pulls. |
| `docs/ROUTING.md` ships | T3 deliverable per §6 outline; review against the outline. |
| Live boltzmann pull (may remain open): 3 hot entries, no live warm until marker set | `warm-drill.sh` live section: pull only; operator-authorized. |
| No build step; stdlib only (hand-rolled YAML); no German; no throughput figures | Existing stdlib AST test green (no new imports beyond stdlib `re`/`datetime` already imported); review grep; fragment carries bytes/seconds, never t/s. |

## 10. RISKS — top 3 mechanical-coder failure modes and the guards placed

1. **The consent fence leaks under concurrency** — the classic shape: a coder implements the engine re-check as a *separate* lock acquisition before `start_switch`, or fires the parked warm by popping it in `tick_pending_warm` and passing plain stops, leaving a window where cancel/human-ops/consent drift interleave (park → cancel → fire executes anyway; or the fence checked at plan time only and never at claim time). **Guards:** H4 pins the re-check *inside* `start_switch`'s existing lock block, atomic with slot claim and queue pop (`warm_seq` handshake); §4.4 forbids popping outside `start_switch`; `TestWarmConsentFence` executes the exact interleavings (cancel-then-fire raises `warm_cancelled`; unmarked stop raises at the engine even when the route is bypassed); the seeded s3 violation proves the call-site guard would catch a direct-actuation shortcut.
2. **YAML quoting "simplified" into an injection or a parse-ambiguity** — a coder emits aliases bare because "they're just names", or quotes with `str.replace('"','\\"')` only, letting newlines/controls/leading-`-`/`none`/numeric-looking strings break document structure or type. **Guards:** §3.3 is a closed rule (one safe-bare class, one escape table, nothing else exists); the hostile-string table pins exact output bytes including the full injection payload; the independent-checker test re-verifies every value line without calling the code under test; the golden file freezes the honest cases.
3. **Queue plumbing grows beyond depth-1 semantics or wedges the tick** — retry-forever on preflight failure, a "fairness" list, firing inside `watcher_lock` (deadlock: `_fire_warm` → `locked_snapshot` on a non-reentrant lock), or hooking the six terminal-write sites. **Guards:** H5 names the single trigger (tick, outside the lock — the hook line's position is spec'd in §2 Section D) and the drop-don't-retry rule per failure class; `tick_pending_warm`'s lock discipline is written out in §4.4; `TestWarmEngineQueue` covers park/dup/full/fire/steal/drop/restart-empty so any added state breaks a frozen assertion; the route-test harness socket timeouts turn a deadlock into a red test.

**Out of scope (do not build, per contract):** multi-host aggregation; touching hossenfelder (docs only); LiteLLM process management; autoscale-to-COLD or any idle eviction; crash-restart reconciliation; queue depth > 1 or priorities; per-model tokens; TTL; the MCP interface (Milestone 6); a queue UI; `do_DELETE`; pyyaml or any YAML import.

---

**Relay-worthy findings for the committer:** (1) snapshot rows can carry `alias: null` (profile initializes the key to `None` at `roundhouse.py` line 711, so the `.get` default at 1602 never fires) — every MVP5 alias consumer uses the explicit `row['alias'] or stem` rule (§3.1), and warm resolution matches what the fragment advertises, including the `boltzmann-` prefix. (2) `cmd_serve` starts `poll_systemctl` before the engine exists — the tick-checked queue requires the small reorder in §2 Section D; without it the hook line would reference `None` forever. (3) The park→fire TOCTOU is closed structurally, not by care: `start_switch` already claims the slot in one lock block (line 4581), and H4 puts consent re-check + `warm_seq` validation + queue pop inside that same block. (4) The route-table guard has *two* allowlists (`from_frozen` and `get_only`, test_server.py ~970–985); both must grow or the guard goes red on the new GETs. (5) `test_parser.py` is edited for the first time since MVP1 — the contract itself assigns marker parsing to Section A.
