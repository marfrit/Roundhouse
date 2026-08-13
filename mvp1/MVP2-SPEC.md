# Roundhouse MVP2 — Build Architecture & Work Breakdown

**File: `mvp1/MVP2-SPEC.md`** (decided: beside `mvp1/roundhouse.py`, which it governs; `MVP2.md` at repo root stays the contract, this file is the build spec, mirroring the MVP1.md / mvp1/SPEC.md split).

Grounded in: `MVP2.md` (contract — its acceptance checklist is the definition of done, its Out-of-scope list is binding), `mvp1/SPEC.md` (all MVP1 decisions stand; nothing here re-opens them), `mvp1/roundhouse.py` as committed (d1d7b8c), and live recon 2026-08-13: **boltzmann has git 2.55.0; the test container now has git 2.47.3 (freshly installed — it was absent); `~/.config/systemd/user` on boltzmann is NOT yet a git repo and contains ~7 `*.bak*` files plus 100+ unrelated desktop units (plasma/pipewire).**

## GLOBAL DECISIONS (E-series; implementers must not re-open them)

- **E1 — Value-only edits.** MVP2 edits change the *value bytes* of parameters that already exist in the file. Adding a flag, removing a flag, and toggling arity-0 flags (`--jinja`) are **not** MVP2 operations: they change tokenization and span structure, and the contract's own wording is "edit the *configured* runtime parameters". Boolean flags and absent fields render read-only in the form. Unknown flags **with a value span** are editable (their value only). A submitted field with no `value` span → `400 field_not_editable`.
- **E2 — Git init is an OPERATOR step, never performed by Roundhouse.** Argument for auto-init, per instruction: the contract says "init in place", and auto-init would make first `--actuate` launch zero-friction. Rejected because: a silent `git init` inside a user's config dir is a surprise write before any token has ever been checked, and the *contents of the baseline commit* (which of 100+ files to track) is a judgment call that must be visible to the operator, not buried in a launch log. `--actuate` launch checks for the repo and, when missing, **prints the exact command sequence (including the concrete selected-unit filenames) and exits 2**. See §2.4.
- **E3 — Scoped tracking, never `git add -A`.** The repo tracks only `.gitignore` + the selected units (plus anything the operator adds by hand). Roundhouse's own `git add` is always `git add -- <one unit file>`. The dirty-check uses `--untracked-files=no`, so the desktop-unit noise (untracked) never blocks actuation. `.gitignore` (operator-created at init, per printed instructions): `*.bak*` and `*.roundhouse-tmp`.
- **E4 — Rollback = `git revert --no-edit <rollout-sha>`** (contract: "rollback is a revert of that commit"; acceptance: "git log shows the revert"), with a byte-restore fallback if revert errors (§3.6). `checkout` is NOT in the git allowlist; restore paths use in-memory pre-edit bytes.
- **E5 — Confirm token = content hash, stateless.** `confirm = sha256(canonical_json({"unit": name, "base": sha256(old_raw).hexdigest(), "edits": sorted [field, old_text, new_text] triples})).hexdigest()`. Apply recomputes from the file **as on disk at apply time** + the submitted edits; mismatch → `409 preview_stale`. This binds apply to the previewed diff without server-side preview state, and detects any disk change between preview and apply. The provenance timestamp is deliberately *outside* the hash (it is applied-at time; the previewed provenance line may differ from the committed one in the timestamp only — documented, accepted).
- **E6 — One rollout globally.** The rollout slot lives in a `RolloutEngine` singleton whose state mutates only under the existing `watcher_lock`. Second `POST .../rollout` while the current rollout is non-terminal → `409 rollout_in_progress`.
- **E7 — Start only what was running.** `was_active` is captured in preflight (rung ∈ {STARTING, LOADING, READY, BUSY}). If false — including every STANDBY/gated case — the rollout skips `stop`, skips `start`/`watching`, and terminates `done` with detail `"applied; unit was not running — not started"` (gated units: `"applied; kernel gate unsatisfied — not started"`). This is how "gated → allowed to edit, with a notice it cannot be started" is honored without ever watching for a READY that cannot come.
- **E8 — 403 vs 401 exactly as the contract orders:** no `--actuate` → **403** on every POST; `--actuate` but missing/wrong bearer → **401**. (Inverted from RFC habit; the contract's acceptance checklist pins it, so it is pinned here.) Uniform rule: **every `do_POST` is gated, first thing, before routing** — there are no ungated POST routes, including preview.
- **E9 — Freed-memory term uses `memory.current`, not peak.** The tasking note says "peak of the unit's own current deployment"; decided deviation: stopping a unit frees what is *resident now* (`memory.current`, last cached tick sample), which is ≤ peak — using peak would inflate the budget and re-create exactly the 30.5 GB swap incident MVP1.md documents. Fallback order when the current sample is absent: cached `last_peak`, then MemStore measured row, then 0.
- **E10 — Sanctioned writes are enumerable.** Post-MVP2 the process may write exactly: the sqlite DB (MVP1), the token file (once), unit files **inside the unit dir via the splice path**, and git's own objects via `run_git`. All file-write primitives are confined to two named functions (§9).
- **E11 — Git is a runtime prerequisite of `--actuate` mode only.** Startup check `git --version` (via `run_git`); missing binary → printed refusal + exit 2. Default (read-only) launch must run on a git-less box byte-for-byte as MVP1 — no git subprocess is ever spawned unless armed.
- **E12 — Provenance and commit-message formats** (frozen, §3.4): one provenance line per apply even for multi-field edits; commit message uses the *flag* spelling (contract example `roundhouse: qwen3.6-coding -c 65536 -> 32768`), provenance uses the *canonical field name* (contract example `ctx`).

---

## 1. FILE / SECTION LAYOUT

```
mvp1/
  MVP2-SPEC.md                  # this file
  roundhouse.py                 # extended: new SECTION E between C and D
  static/index.html             # extended in place (still ONE file, inline CSS+JS)
  scripts/
    container-setup.sh          # extended: git present check, container repo init (operator's hand), rollout scenarios
    fake-llama-server.py        # extended: ctx sentinel 424242 -> print error line, exit 1  (FAKE_EXIT_1)
    rollout-drill.sh            # NEW: live boltzmann gemma4 drill checklist (operator-driven)
  tests/
    test_parser.py              # UNTOUCHED
    test_watcher.py             # extended: no_ready_marker badge tests, badge-change event tests
    test_server.py              # extended: guard evolution (§9), POST auth/route tests, UI static checks
    test_actuation.py           # NEW: splice engine, verify contract, git gateway, run_actuate, preflight, rollout machine
```

`roundhouse.py` gains, between Sections C and D:

```python
# ===== SECTION E: ACTUATION (armed only by --actuate; run_actuate + run_git are the only mutation gateways) =====
```

**Section E contents (complete list):**
`ACTUATE_ARMED` module global (initial `False`); `ACTUATE_SYSTEMCTL_VERBS`; `GIT_VERBS`; exceptions `ActuationError`, `EditError`, `VerifyError`; `run_actuate()`; `run_git()`; `git_startup_check()`; `print_git_init_instructions()`; `ensure_token()`; `check_bearer()`; `parse_timeout_start_sec()`; `Edit` dataclass; `plan_edits()`; `render_value_bytes()`; `splice()`; `assert_span_invariants()`; `verify_splice()`; `unified_diff_text()`; `provenance_line()`; `commit_message()`; `_atomic_write()`; preflight functions `preflight()`, `preflight_port()`, `preflight_memory()`, `preflight_retired()`, `preflight_git()`; `compute_confirm()`; `RolloutEngine` class; `ROLLOUT_PHASES` tuple.

Section D changes: `--actuate` flag; `cmd_serve` arming sequence (§2.4); handler `do_POST` replaced (§6). Section B changes: badge machinery only (§8). Section A: **unchanged** (splice consumes its spans; it does not modify the parser).

---

## 2. ACTUATION GATEWAY SPEC

### 2.1 `run_actuate`

```python
ACTUATE_SYSTEMCTL_VERBS = {"stop", "start", "daemon-reload"}

def run_actuate(argv: List[str], units: Dict[str, "UnitFile"], timeout: float = 90) -> str:
```

Raises `ActuationError` (never returns) unless **all** of:

1. `ACTUATE_ARMED is True` (the flag, not truthiness of something else).
2. `argv == ["systemctl", "--user", "daemon-reload"]`, **or** `argv == ["systemctl", "--user", verb, "--", unit]` with `verb in {"stop","start"}` — exact shape, exact length; no other flags, never more than one unit.
3. For stop/start: `unit in units` (the selected set) **and** `units[unit].retired is False`. This is the structural RETIRED-unreachability assertion at the last line of defense (the routes and the engine also check; this one cannot be routed around).

Runs via `subprocess.run(..., capture_output=True, text=True, timeout=timeout)`; non-zero returncode → `ActuationError(f"{' '.join(argv)} rc={rc}: {stderr.strip()}")`. `run_actuate` is called **only** by `RolloutEngine` methods (enforced by AST guard, §9). No `enable`, `disable`, `restart`, `reset-failed` — not in the verb set, and the verb set is asserted disjoint from everything outside itself by test.

### 2.2 `run_git`

```python
GIT_VERBS = {"version", "rev-parse", "status", "ls-files", "log", "show",
             "diff", "add", "commit", "revert"}
GIT_MUTATING_VERBS = {"add", "commit", "revert"}
GIT_FORBIDDEN_TOKENS = {"push", "pull", "fetch", "remote", "clone", "init",
                        "checkout", "reset", "clean", "submodule"}

def run_git(args: List[str], unit_dir: str, timeout: float = 30) -> subprocess.CompletedProcess:
```

Builds `["git", "-C", unit_dir] + args`. Raises `ActuationError` unless: `ACTUATE_ARMED`; `args[0] in GIT_VERBS`; no element of `args` is in `GIT_FORBIDDEN_TOKENS`; for `add`: shape exactly `["add", "--", basename]` where `basename` is a selected unit's filename (no path separators); for `revert`: shape exactly `["revert", "--no-edit", sha]` or `["revert", "--abort"]`. For verbs in `GIT_MUTATING_VERBS`, env is extended with `GIT_AUTHOR_NAME=roundhouse`, `GIT_AUTHOR_EMAIL=roundhouse@<hostname>`, `GIT_COMMITTER_NAME/EMAIL` same — this both produces the contract's author string and makes commits work in a fresh repo with no `user.name` configured. Returns the `CompletedProcess`; callers check `.returncode`. **No push, no remote ops, ever** — structurally impossible via the allowlist.

### 2.3 Token: generation and check

- Path: `~/.config/roundhouse/token` (constant `TOKEN_PATH`, expanded at startup; no CLI override).
- `ensure_token() -> str`, called once during arming: if the file is missing **or empty** → create `~/.config/roundhouse` with mode `0o700` (if needed), write `secrets.token_urlsafe(32)` + `\n` via `_atomic_write`, `os.chmod(path, 0o600)`, print `generated bearer token at ~/.config/roundhouse/token — paste its contents into the UI`. If the file exists: `stat.st_mode & 0o077 != 0` → refusal message `token file ~/.config/roundhouse/token is group/world-readable; chmod 600 it and relaunch` + exit 2. Token = file content stripped; loaded **once** at startup (rotation = restart).
- `check_bearer(handler) -> Optional[int]` returns the HTTP status to fail with, or None: not armed → `403`; armed and (`Authorization` header missing, not `Bearer <t>`, or `not hmac.compare_digest(t, TOKEN)`) → `401`. 401 responses carry `WWW-Authenticate: Bearer`. Bodies: `{"error": "read_only_mode", "detail": "launch with --actuate to enable rollouts"}` / `{"error": "unauthorized"}`.

### 2.4 `--actuate` plumbing and startup sequence

`main()` gains `--actuate` (store_true; meaningful only with `--serve`; ignored by `--scan`). In `cmd_serve`, after units are parsed and **before** any thread starts, iff `args.actuate`:

1. `git --version` runs (bypassing the armed check via a `bootstrap=True` parameter on `run_git` that permits exactly `["version"]` and the read-only repo checks below while arming is in progress). Failure → print `--actuate requires git on PATH (read-only mode does not); install git and relaunch` → exit 2.
2. Repo present: `git -C <unit_dir> rev-parse --show-toplevel` must succeed **and** its output must `os.path.realpath`-equal the unit dir. A parent repo (e.g. a dotfiles repo above `~/.config`) fails this check — refusing beats committing unit edits into an unrelated repo. On failure, print (`print_git_init_instructions()`, with the real selected filenames substituted):

```
--actuate refused: /home/<u>/.config/systemd/user is not a git repository (contract §git).
Roundhouse never runs `git init` itself. Initialize it as the operator, once:

  cd ~/.config/systemd/user
  git init
  printf '%s\n' '*.bak*' '*.roundhouse-tmp' > .gitignore
  git add .gitignore <unit1>.service <unit2>.service ... <unitN>.service
  git commit -m "roundhouse baseline: N managed units"

Then relaunch with --actuate.
```

Exit 2.
3. Clean worktree: `git status --porcelain --untracked-files=no` output must be empty (untracked desktop units and `.bak` noise are invisible to this check by design, E3). Non-empty → the **crash-recovery refusal** (this is also the mid-rollout-death recovery rule — a died rollout leaves a modified tracked file):

```
--actuate refused: the unit-dir git worktree has uncommitted changes to tracked files
(a previous rollout may have died mid-apply):
  <porcelain output, verbatim>
Inspect:   git -C ~/.config/systemd/user diff
Resolve:   commit the change, or discard it with
           git -C ~/.config/systemd/user restore -- <file>
Then run:  systemctl --user daemon-reload
Relaunch with --actuate when the worktree is clean.
```

Exit 2. (Exit, not silent fallback to read-only: the operator explicitly asked for actuation; failing loudly beats a mode they didn't ask for.)
4. Warn (do not fail) if `.gitignore` is missing or lacks `*.bak*`.
5. `ensure_token()`; then `ACTUATE_ARMED = True`. This assignment is the **only** one outside the module-level `ACTUATE_ARMED = False` (AST-asserted, §9). `RolloutEngine` is constructed and attached to the server object; snapshot gains `"mode": "actuate"` (else `"read-only"`).

Post-crash cases not caught by the dirty check: rollout died after commit but before `start` → worktree clean, unit OFF; startup proceeds, roster shows OFF, operator starts by hand. Died between `stop` and splice → clean, unit OFF, same. Both are visible states, not corruption; no special code.

---

## 3. SPLICE ENGINE SPEC (pure functions; no gating knowledge — RETIRED/auth gates live above)

### 3.1 Types and signatures

```python
@dataclass
class Edit:
    field: str                 # canonical field name, or "unknown:<flag-text>"
    flag: str                  # flag as written, e.g. "-c" (for commit msg)
    old_text: str              # current decoded value (Token.text)
    new_text: str              # submitted value
    span: Tuple[int, int]      # value byte span in the CURRENT raw
    quote: str                 # '' | "'"  — quoting style of the existing token

def plan_edits(unit: UnitFile, changes: Dict[str, str]) -> List[Edit]          # raises EditError
def render_value_bytes(edit: Edit) -> bytes                                     # new token raw bytes
def splice(raw: bytes, edits: List[Edit], provenance: str) -> bytes
def assert_span_invariants(unit: UnitFile) -> None                              # raises AssertionError
def verify_splice(old_unit: UnitFile, new_raw: bytes,
                  edits: List[Edit], provenance: str) -> UnitFile               # raises VerifyError
def unified_diff_text(old: bytes, new: bytes, name: str) -> str
def provenance_line(edits: List[Edit], now_utc: datetime) -> str
def commit_message(unit_name: str, edits: List[Edit]) -> str
def compute_confirm(unit_name: str, old_raw: bytes, edits: List[Edit]) -> str   # E5
def _atomic_write(path: str, data: bytes) -> None
```

### 3.2 `plan_edits` — resolution and validation

For each `(key, new_value)` in `changes` (values are **always strings** on the wire):

1. Resolve the span: canonical key → `profile['spans'][key]['value']`; `"unknown:<flag>"` → the first `unknown_flags` entry with that flag text and a non-null `value_span` (a second entry with the same flag text → `EditError duplicate_unknown_flag`). No span / no `value` sub-span / field absent → `EditError field_not_editable` (E1).
2. Type-validate against the §2.5 flag table: int fields must `int()` (ctx additionally `> 0`; port `1..65535`), float fields `float()`. `chat_template_kwargs`: `json.loads(new_value)` must succeed.
3. Byte-safety validate: for unquoted tokens `new_value` must match `^[A-Za-z0-9._:/=,+-]+$` (non-empty; no whitespace, quotes, backslash, `%`, `#`, `;`) — anything else would change tokenization. For single-quoted tokens (the embedded-JSON case): any bytes except `'`, `\n`, `\\`. Violation → `EditError invalid_value`.
4. Paid-offload rail on the input: `new_value` containing `://` → `EditError remote_scheme` (no `file://` exemption for edit values — no legitimate runtime parameter on this host is a URL).
5. No-op edits (`new_text == old_text`) → dropped; if *all* dropped → `EditError no_change`.

`quote` is `"'"` iff the existing token raw starts with `b"'"`, else `''` (no double-quoted values exist in the corpus; a double-quoted token → `EditError field_not_editable`, honest refusal over guessing escapes). `render_value_bytes`: `quote + new_text + quote`, UTF-8.

### 3.3 `splice` — multi-field mechanics

1. Sort edits by `span[0]` **descending**; assert spans are pairwise disjoint (`EditError overlapping_spans` — cannot happen from one profile, guards a confused caller).
2. For each edit in that order: `raw = raw[:s] + render_value_bytes(e) + raw[e_end:]` — descending order means earlier offsets stay valid; **no span arithmetic, no re-tokenization between splices**.
3. Append provenance: if `raw` non-empty and doesn't end `b"\n"`, append `b"\n"`; then `provenance.encode() + b"\n"`.

### 3.4 Provenance and commit formats (frozen, E12)

```
# roundhouse: 2026-08-13T14:02:11Z ctx 65536 -> 32768, port 8085 -> 8087 via UI
```

`provenance_line`: `"# roundhouse: "` + UTC ISO-8601 seconds (`Z` suffix) + `", "`-joined `f"{field} {old} -> {new}"` per edit (file order) + `" via UI"`. One line per apply, always appended at EOF (after any trailing content, never interleaved — EOF is the only position that cannot disturb any span).

`commit_message`: `f"roundhouse: {unit stem} "` + `"; "`-joined `f"{flag} {old} -> {new}"` — e.g. `roundhouse: qwen3.6-coding -c 65536 -> 32768`.

### 3.5 `verify_splice` — the a/b/c contract (exact)

Parse `new_unit = parse_unit(path, new_raw)`; raise `VerifyError` with a reason string on the first failure:

- **(a) Profile equality except edited fields.** Compare `extract_param_profile` outputs old vs new as dicts after deleting keys `spans` and `raw_argv` (offsets legitimately shift when a value changes length — comparing them is the classic false-failure; see Risk 2) and normalizing `unknown_flags` to `[(flag, value)]` pairs. Then for every canonical edited field: new value must equal the typed parse of `new_text` (and old must equal old); for every non-edited key: byte-for-byte dict equality. `raw_argv` is compared as a *list of texts* with exactly the edited positions changed.
- **(b) Comments.** `[c['text'] for c in new_unit.comments] == [c['text'] for c in old_unit.comments] + [provenance]` — verbatim, order-preserving, exactly one new line, and it is the provenance line.
- **(c) Span invariants** via `assert_span_invariants(new_unit)`, which packages MVP1's three property checks as a callable: (1) `b"".join(raw[l.start:l.end] for l in lines) == raw`; (2) every ExecStart token: `raw[t.start:t.end] == t.raw`; (3) every profile span `raw[s:e]` decodes/re-derives to the recorded token raw. Plus one MVP2-specific check: every byte of `new_raw` outside the replaced spans and the appended EOF region equals the corresponding old byte (computed by replaying the splice arithmetic — this is the "splice never touches bytes outside spans+EOF" guarantee as a runtime assertion, not only a test).

Returns the parsed `new_unit` (the caller installs it into `watcher.units` — see §4.4 step S4).

### 3.6 Restore semantics

- Pre-edit bytes (`old_raw`) are held in the rollout record in memory for the whole rollout **and** retrievable from git (`git show HEAD:<name>` pre-commit, `<sha>^:<name>` post-commit).
- **Pre-commit restore** (verify failure, write failure): `_atomic_write(path, old_raw)` — write to `<path>.roundhouse-tmp` in the same directory, `flush`+`fsync`, `os.replace`, fsync dir fd best-effort. Then re-read and compare to `old_raw`; then `watcher.units[unit]` is left at the old parse. **Never reaches daemon-reload** (contract). Worktree returns to clean; no commit ever existed.
- **Post-commit restore** (rollback): `git revert --no-edit <sha>` (E4), then read the file and assert bytes `== old_raw` (belt and braces — nothing else committed in between, this must hold). If revert exits non-zero: `git revert --abort` (best effort), `_atomic_write(path, old_raw)`, `git add -- <name>`, `git commit -m "roundhouse: rollback <stem> (byte restore; revert failed)"`.

---

## 4. ROLLOUT STATE MACHINE

### 4.1 Phases (frozen tuple `ROLLOUT_PHASES`)

```
preflight -> applying -> reloading -> starting -> watching -> done
                                   \______________________-> done   (E7: was_active == False path skips starting+watching)
any phase -> failed
failed -> rolling_back -> rolled_back | rollback_failed
```

The global slot is `idle` when `RolloutEngine.current` is None or terminal (`done`, `rolled_back`, `rollback_failed`, or `failed` with `restored: true`). `failed` with `rollback.offered: true` **holds the slot** — a new rollout while a rollback is still offered → `409` (decide the failure first; the offer is dismissible via the rollback route or by `POST /api/rollouts/<id>/dismiss` — see §6). Preview is stateless and is not a phase.

### 4.2 Transition triggers, sub-steps, timeouts

| phase | does (in order) | success → | failure (any step or timeout) | timeout |
|---|---|---|---|---|
| `preflight` | re-run all §5 checks (state may have drifted since preview); recompute confirm against disk bytes, compare to submitted (`409` path happens at route level before the machine starts); capture `was_active`, `old_raw`, resolved edits | `applying` | `failed(preflight)`, nothing touched, slot freed (`restored: true` trivially) | 10 s |
| `applying` | SSE details `"stopping unit"` → if `was_active`: `run_actuate(stop)`; `"splicing"` → `new_raw = splice(...)`, `_atomic_write`; `"verifying"` → re-read file, compare to intended bytes, `verify_splice`; `"committing"` → `run_git add`, `run_git commit`, `commit_sha = rev-parse HEAD` | `reloading` | pre-commit failure → automatic byte-restore (§3.6), then if `was_active`: `run_actuate(start)` of the old config; terminal `failed(reason, restored=true)`. Commit-step failure after a good verify → restore bytes the same way (worktree back to clean) | 150 s (stop can legitimately take ~90 s) |
| `reloading` | `run_actuate(daemon-reload)` | `starting` if `was_active`, else `done` (detail per E7) | `failed(daemon_reload)`, rollback offered (commit exists) | 30 s |
| `starting` | `run_actuate(start)` | `watching` | `failed(start_error)`, rollback offered | 30 s |
| `watching` | poll under `watcher_lock` every 1 s: rung + badges of the unit | rung `READY` or `BUSY` → `done` (record `load_seconds` display from the unit's fresh MemStore row; the measured peak row arrives via the existing watcher machinery — a changed ctx is a new `(unit, file_id, ctx)` key, so `/api/mem` shows a fresh measured row with no new code) | rung `FAILED` → `failed(unit_failed)`; badge `no_ready_marker` present while LOADING → `failed(no_ready_marker)` — **this is the deferred-from-MVP1 badge doing its MVP2 job as the rollback trigger** (§8); absolute cap → `failed(watch_timeout)` | 900 s cap |
| `rolling_back` (entered only from `failed` with commit ≠ None, only via the rollback route) | `run_actuate(stop)` (ignore failure if already dead); §3.6 post-commit restore; `run_actuate(daemon-reload)`; if `was_active`: `run_actuate(start)`, then watch the old config with the same `watching` rules | `rolled_back` (old config READY, or not-started case done) | `rollback_failed` — terminal; SSE detail carries the exact manual commands (`git -C ... log -n 3`, `git revert`, `systemctl --user daemon-reload`, `start`) | 900 s |

Every transition and every sub-step detail publishes an SSE `rollout` event (§4.3) and updates `RolloutEngine.current` under `watcher_lock` (publish after releasing). The worker is one `threading.Thread(name="rollout", daemon=True)` spawned per accepted rollout — the fourth thread class in `cmd_serve`'s model.

### 4.3 SSE: dedicated `rollout` event (decided: yes, minimal)

Rung events already cover the unit's own ladder; the stepper needs phase identity, so:

```
event: rollout
data: {"rollout_id": "ro-1765612345-1", "unit": "llama-server-gemma4.service",
       "phase": "applying", "detail": "splicing", "ok": true, "ts": 1765612347.2}
```

`ok: false` on `failed`/`rollback_failed`, with `detail` = reason + human text. The full rollout record (below) is embedded in every snapshot as `"rollout"` (or `null`), so a mid-rollout page refresh reconstructs the stepper.

Rollout record (returned by `GET /api/rollouts/<id>` and in snapshots):

```json
{"rollout_id": "ro-<epoch-int>-<seq>", "unit": "...", "phase": "watching",
 "detail": "elapsed 41s", "edits": [{"field": "ctx", "flag": "-c", "old": "65536", "new": "32768"}],
 "was_active": true, "commit": "<sha or null>", "restored": false,
 "failure": null,
 "rollback": null,
 "started_at": 1765612345.0, "updated_at": 1765612386.0}
```

`failure`: `{"reason": "no_ready_marker", "detail": "..."}`; `rollback`: `{"offered": true}` → after rollback: `{"offered": false, "phase": "rolled_back", "revert_commit": "<sha>"}`. Records live in an in-memory dict for the process lifetime (history's system of record is `git log`, not Roundhouse).

### 4.4 Crash recovery + state consistency rules

- Process death mid-rollout: recovery is entirely the §2.4 startup rule — dirty tracked worktree → refuse actuation with the exact message; clean worktree → normal start (see §2.4 post-crash cases). No journal/UNDO files; the memory of a live rollout dies with the process, git and the printed instructions carry the operator.
- **S4 (state refresh, do not skip):** on successful verify, `watcher.units[unit] = new_unit` under `watcher_lock` — subsequent edits must see the NEW spans; a stale `UnitFile` here would splice at dead offsets. On restore, the old object stays/is restored. This line is asserted by a test (Risk 2).

---

## 5. PRE-FLIGHT SPEC

`preflight(unit_name, edits, watcher, snapshot) -> dict` runs all checks, returns `{"ok": bool, "checks": [...]}`. Order: `retired`, `git`, `port`, `memory`. All are hard failures except where noted; there is **no override parameter** anywhere in the call chain.

**Check `retired`:** `unit.retired` → fail, detail `"unit is [RETIRED] — structurally excluded from every actuation path"`. (Also enforced at route, engine, and `run_actuate` layers.)

**Check `git`:** `git ls-files --error-unmatch -- <name>` rc 0, else fail with detail `"unit file is not tracked in the unit-dir git repo; run: git -C ~/.config/systemd/user add <name> && git commit -m 'track <name>'"`. (Covers units created after the baseline commit without blocking the whole mode.) Also re-checks the clean-worktree rule for this file.

**Check `port`** (only when `port` is among the edited fields): the new port collides iff any **other parsed unit** — any enable state, any rung, `port_source` flag *or* default — declares it and is not RETIRED, **or** it equals Roundhouse's own `self_port`. Declared board only; never sockets. Fail detail enumerates claimants: `"port 8086 already declared by llama-task.service (enabled, READY), llama-server-qwen35-npu.service (disabled, kernel-gated STANDBY)"`. A collision only with a `[RETIRED]` claimant passes with a warning notice (mixperten's :8085 must not block re-porting onto 8085's active claimant being edited — but note an edit that *lands on* the active claimant's own port obviously fails via that claimant).

**Check `memory`** (only when any of `ctx`, `cache_type_k`, `cache_type_v`, `model_path` is edited): with `new_profile` = current profile overlaid with edits:

```
estimate  = MemStore measured (unit, new_file_id, new_ctx)          # exact row
          | else newest measured (unit, new_file_id, any ctx)       # label: "measured at ctx <c>; edited ctx unproven"
          | else int(new_model_size * 1.10 + 1.5 GiB)               # MVP1 formula, same label
freed     = cgroup memory.current (last cached tick) of THIS unit if it is active
          | else last_peak | else measured row | else 0             # E9
budget    = MemAvailable (fresh /proc/meminfo read) + freed
HEADROOM  = 1 GiB (module constant)
fail iff  estimate + HEADROOM > budget
```

`model_path` edits additionally require the target file to exist (`stat` succeeds) — a missing model is an automatic fail, detail `"model file not found: <path>"`. Fail detail with the numbers, always labelled with the estimate source (house rule: `source` accompanies every surfaced number):

```json
{"check": "memory", "ok": false,
 "detail": "estimated 21.4 GiB (measured peak, this (unit, model, ctx)) + 1.0 GiB headroom exceeds budget 18.4 GiB (MemAvailable 12.1 GiB + 6.3 GiB freed by stopping llama-server-gemma4.service)",
 "estimate_bytes": 21400000000, "estimate_source": "measured",
 "mem_available_bytes": 12100000000, "freed_bytes": 6300000000,
 "headroom_bytes": 1073741824, "budget_bytes": 18400000000}
```

`preflight_memory` takes injectable `meminfo_reader` and reads the cgroup cache through the watcher — unit-testable without a live host.

**Notices (non-blocking, returned alongside):** gate unsatisfied → `"unit is kernel-gated (STANDBY): the edit can be applied but the unit cannot be started on this kernel"`; port collision with only-RETIRED claimants; estimate source not `measured`.

---

## 6. API SPEC

Existing GET routes: byte-identical behavior, still unauthenticated (contract: read routes unchanged, LAN-open). Snapshot payload gains two keys: `"mode": "read-only"|"actuate"`, `"rollout": <record|null>`.

**`do_POST` (replaces the flat 405):** step 1 `check_bearer` (E8: 403 unarmed, 401 bad token — before routing, before body read); step 2 route; unknown POST path → 404; body = JSON, malformed → 400 `{"error":"bad_json"}`. `do_PUT/DELETE/HEAD` stay 405. **GET on a POST-only route → 405.**

| route | method | body | success | errors |
|---|---|---|---|---|
| `/api/units/<name>/edit` | POST | `{"edits": {"ctx": "32768", "unknown:--mlock": "..."}}` (values always strings) | `200` preview: `{"unit", "edits":[Edit-echo], "diff": <unified text>, "confirm": <64-hex>, "preflight": {"ok":true,"checks":[...]}, "notices":[...], "provenance_preview": "..."}` | 404 unknown/unselected unit; 400 `bad_json` / `field_not_editable` / `invalid_value` / `remote_scheme` / `no_change`; **422** `{"error":"preflight_failed", "checks":[...], "diff":...}` (no `confirm` — cannot proceed); plus 403/401 |
| `/api/units/<name>/rollout` | POST | `{"edits": {...}, "confirm": "<hex>"}` | `202` `{"rollout_id": "..."}`, machine starts at `preflight` | as above, plus **409** `{"error":"rollout_in_progress","rollout_id":...}` (E6) and **409** `{"error":"preview_stale","detail":"unit file or edits changed since preview; re-preview"}` (E5 mismatch) |
| `/api/rollouts/<id>` | GET | — | `200` rollout record | 404 |
| `/api/rollouts/<id>/rollback` | POST | `{}` | `202` (phase → `rolling_back`) | 404; **409** `{"error":"not_rollbackable"}` unless that rollout is `current`, phase `failed`, `commit` non-null, `rollback.offered` |
| `/api/rollouts/<id>/dismiss` | POST | `{}` | `200` (marks `rollback.offered=false`, frees the slot; the failed config stays — operator chose to keep/fix by hand) | 404; 409 same condition |

Status-code doctrine (frozen): **400** malformed input, **401/403** per E8, **404** unknown resource, **405** wrong method, **409** concurrency/staleness (`rollout_in_progress`, `preview_stale`, `not_rollbackable`), **422** preflight/validation-against-the-world failures. Preview is allowed while a rollout runs (stateless); only `rollout` takes the slot.

---

## 7. UI SPEC (`static/index.html`, still one file; `textContent`-only rule stands — no `innerHTML` anywhere, asserted by the existing static test extended to the new code)

1. **Mode indication:** header gains a badge after the kernel: `[READ-ONLY]` (neutral) or `[ACTUATE]` (amber) from `snapshot.mode`. In actuate mode a token field appears in the header: `<input type="password" id="token">` + hint `token: ~/.config/roundhouse/token`; value held in a JS variable only (no localStorage/sessionStorage — static test asserts those strings are absent), sent as `Authorization: Bearer` on every POST. A 401 turns the field's border red and shows `token rejected` via textContent.
2. **Edit entry:** detail pane gains an `edit` button — rendered only when `mode === "actuate"` and `!unit.retired`. RETIRED detail panes render a static note `[RETIRED] — not editable` instead.
3. **Edit form (generated, never hardcoded):** built from `param_profile.spans` + `unknown_flags` of the freshly-fetched detail. Field order = **source order** (sort by `spans[field].value[0]`; unknown flags interleaved by their `value_span[0]`). Per row: label = canonical field name + flag text (`ctx (-c)`), input by type from the §2.5 table — int → `type="number"` (step 1), float → `type="number" step="any"`, str/model_path/chat_template_kwargs → `type="text"`. Value-less entries (bool flags like `--jinja`, unknown flags with null `value_span`) render as **disabled** rows titled `flag add/remove is not an MVP2 edit` (E1). Unknown flags with values: text inputs labelled with the raw flag text, visually tagged `unknown`. Gate notice (STANDBY) shown atop the form when applicable.
4. **Comments adjacent (contract §rollout-1):** the edit form is a two-column layout — form left, the full OPERATOR'S NOTES `<pre>` (same verbatim `textContent` block the detail pane already builds) right (stacked below at narrow widths). You see the *why* beside what you're overriding; per-flag comment correlation is out of scope (not in the acceptance checklist — do not build it).
5. **Diff preview modal:** on submit, POST `/edit`; render `diff` in a `<pre class="diff">` via textContent (single block; no per-line coloring required), the preflight check list (✓/✗ rows with details), notices, and two buttons: `apply` (disabled unless `preflight.ok`, POSTs `/rollout` with `{edits, confirm}`) and `cancel`. On 422, same modal, apply disabled. On 409 `preview_stale`: message `file changed on disk — re-previewing` and re-POST `/edit` once.
6. **Rollout stepper:** a fixed strip (below the header) rendered whenever `snapshot.rollout` is non-null or a `rollout` SSE event arrives: phase chips `preflight → applying → reloading → starting → watching → done` with the current phase pulsing, current `detail` text beside, terminal states colored (done green, failed red, rolled_back neutral). Bound to the `rollout` SSE events; snapshot rebuild covers refresh.
7. **Rollback button:** rendered in the stepper strip when `failure` non-null and `rollback.offered`: `roll back to previous config` (POSTs `/rollback`) + a `dismiss` link (POSTs `/dismiss`). Rollback progress reuses the same stepper (`rolling_back` phase chip appears).
8. Existing rung/ports/mem rendering untouched. New CSS stays inside the existing palette variables; `.failed`-red rules gain no new sharers (the STANDBY structural rule keeps holding).

---

## 8. WATCHER ADDITION — `no_ready_marker` (and ONLY that; `retired_but_running` and per-unit staleness age stay deferred)

- `parse_timeout_start_sec(val: Optional[str]) -> int`: `None`/empty → **90**; `^\d+$` → seconds; `^(\d+)\s*(s|sec|min)$` → seconds/minutes; anything else → 90. Source: `unit.known.get('timeoutstartsec')` (already captured by Section A).
- `_compute_badges` gains: rung `LOADING` and `exec_main_start_ts` set and `now() - ts > parse_timeout_start_sec(...)` → append `'no_ready_marker'`. Semantics unchanged from the MVP1 spec text: the unit **stays LOADING, never FAILED**; amber, "active but no ready marker seen — sensing may be incomplete".
- **Badge-change emission (required for both the UI and the rollout watcher):** `Watcher` caches `_badges` per unit; `apply_systemctl_show` (the 3 s tick) recomputes badges for every unit and emits a `rung` event when the badge *set* changed even if the rung did not. (Without this, a badge that appears mid-LOADING would never reach SSE, and the rollout `watching` poll is the only consumer that would see it.) `RolloutEngine.watching` reads the badge via the same `_compute_badges` call under the lock — one implementation, two consumers.

---

## 9. TEST / GUARD EVOLUTION

The MVP1 AST guards must keep proving *the default launch mode writes nothing* while permitting the gated path. Exact changes in `test_server.py::TestZeroWritePath` (class renamed `TestWriteGuards`, same file):

1. **`test_no_write_verb_reaches_a_subprocess_call` — kept verbatim** for `run_ro`/`spawn_ro_stream` (they stay pure read gateways forever). **New sibling `test_write_verbs_only_in_the_actuation_section`:** walking the AST, every `ast.Constant` whose value ∈ `WRITE_VERBS` must be located (by line number) inside Section E — section boundaries found by locating the `# ===== SECTION` banner lines in the source text — and, when it appears in a `Call`'s arguments, that call's callee name must be `run_actuate`. Additionally, every `Call` to `run_actuate` must be lexically inside a `FunctionDef`/`AsyncFunctionDef` whose name is in the frozen allowlist `ROLLOUT_CALLSITES = {'_stop_unit', '_start_unit', '_daemon_reload'}` — three `RolloutEngine` methods, the *only* callers (this is the "AST guard that whitelists call-sites" shape).
2. **`test_daemon_reload_never_appears_in_source` — replaced** by the section-scoped assertion above (`daemon-reload` ∈ WRITE_VERBS, so it's covered; a literal-string scan version additionally asserts every occurrence's line is inside Section E or its tests-facing constants).
3. **`test_only_the_subprocess_gateway_spawns_processes`:** gateway set becomes `{'run_ro', 'spawn_ro_stream', 'run_actuate', 'run_git'}` — still the only functions containing `subprocess.` attributes.
4. **New `test_default_mode_cannot_actuate` (the mode guard):** `ACTUATE_ARMED` is `False` at import; `run_actuate(["systemctl","--user","stop","--","x.service"], units)` and `run_git(["add","--","x.service"], d)` both raise `ActuationError` when unarmed; AST: the only `ast.Assign`/`ast.AugAssign` targets named `ACTUATE_ARMED` are the module-level `False` and exactly one assignment inside `cmd_serve` guarded by the `args.actuate` branch. Plus a behavior test: server booted without arming → every POST route in a frozen route table → 403, and (monkeypatched `_atomic_write` + `subprocess`) nothing was invoked.
5. **New `test_retired_unreachable_from_every_entry_point`:** with a RETIRED fixture: (a) `run_actuate(stop|start)` raises; (b) `plan_edits`-level is deliberately pure, so (c) POST `/edit` → 422 retired check, (d) POST `/rollout` → 422, (e) `RolloutEngine.start_rollout` raises. Entry-point list frozen in the test.
6. **New `test_token_required` / `test_wrong_token_401`:** armed server, no header → 401; wrong token → 401; right token → 2xx path; token file created `0o600` (assert `stat`), regenerated when empty, refused when `0o644`.
7. **Splice-bounds property test** (`test_actuation.py`): for every fixture in the contract's list (`-c`, `--port`, `-ctk`, `--chat-template-kwargs`, mixperten continuation-line flag) and a 3-field simultaneous edit: every byte outside the edited value spans and the appended EOF line is identical old-vs-new (independent reimplementation of the §3.5(c) arithmetic — the test must not call the same helper it checks).
8. **Paid-offload on spliced values:** `plan_edits` with `new_value` `"https://api.openai.com/v1"` (and bare `"x://y"`) raises `EditError remote_scheme`; a spliced+verified deployment still passes `assert_no_paid_offload` (called from `verify_splice`'s caller in the engine — assert via fault-injection that a hostile value cannot reach the write).
9. **File-write confinement (new AST guard):** calls to `open(...)` with a mode string containing `w`/`a`/`x`/`+`, plus `os.replace`, `os.rename`, `Path.write_text/write_bytes`, may appear only inside `{'_atomic_write', 'ensure_token'}` and `MemStore` methods. (Sqlite writes are inside MemStore already; `_atomic_write` is the single byte-writer for token, splice, restore.)

`test_watcher.py` additions: badge appears at `elapsed > TimeoutStartSec` (fake `now`), not before; badge-change emits a `rung` event with unchanged rung; `parse_timeout_start_sec` table; snapshot-shape test updated for `mode`/`rollout` keys.

---

## 10. WORK BREAKDOWN — 3 tasks, composable without coder contact

All interfaces named below are **frozen exactly as written in §§2–6**; every cross-task type/JSON shape is in this spec.

### T1 — Gateways + splice engine + watcher badge (first; T2/T3 depend only on its signatures)

- **Writes:** `roundhouse.py` Section E part 1: `ACTUATE_ARMED`, `ActuationError/EditError/VerifyError`, `run_actuate`, `run_git`, `git_startup_check`, `print_git_init_instructions`, `ensure_token`, `check_bearer`, `parse_timeout_start_sec`, `Edit`, `plan_edits`, `render_value_bytes`, `splice`, `assert_span_invariants`, `verify_splice`, `unified_diff_text`, `provenance_line`, `commit_message`, `compute_confirm`, `_atomic_write`; Section B badge machinery (§8). New `tests/test_actuation.py` (splice/verify/gateway/token halves); **may touch:** `test_watcher.py` (badge + snapshot-shape), `test_server.py` guard class (items §9.1–4, 7–9). Must not touch Section C/D beyond nothing.
- **Self-test:** `cd mvp1 && python3 -m unittest tests.test_actuation tests.test_watcher tests.test_server -v` green; plus a throwaway-dir round trip: init a scratch git repo, splice a fixture copy's `-c`, verify, commit via `run_git`, `git log --format=%an` prints `roundhouse`; and `python3 -c "import roundhouse; roundhouse.run_actuate(['systemctl','--user','stop','--','x.service'],{})"` raises `ActuationError` (unarmed).

### T2 — RolloutEngine + preflight + routes + CLI arming (depends on T1 signatures; parallel with T3)

- **Writes:** Section E part 2: `preflight*`, `RolloutEngine` (methods incl. exactly `_stop_unit/_start_unit/_daemon_reload`), `ROLLOUT_PHASES`; Section C: `do_POST`, new routes, snapshot `mode`/`rollout` merge (in `take_snapshot`, keeping the lock discipline), SSE `rollout` publishing; Section D: `--actuate`, arming sequence §2.4 in exact order. **May touch:** `test_server.py` (route/auth/409/422 tests, `StubWatcher` extension, §9.5–6), `test_actuation.py` (rollout-machine and preflight halves — file shared with T1 but disjoint test classes: T1 owns `TestSplice*`, `TestGateways`, `TestToken`; T2 owns `TestPreflight`, `TestRolloutMachine`, `TestRoutesAuth`). Drives the machine with a stubbed `run_actuate`/`run_git`/watcher — no live systemd in unit tests.
- **Self-test:** `cd mvp1 && python3 -m unittest discover -s tests -v` (all files green); `python3 roundhouse.py --serve --actuate --unit-dir /tmp/x` against a non-repo dir prints the §2.4 instructions and exits 2; against a dirty repo prints the recovery message and exits 2.

### T3 — UI + container/live drill scripts (depends only on frozen JSON/SSE shapes)

- **Writes:** `static/index.html` (§7 complete), `scripts/fake-llama-server.py` (ctx-424242 sentinel → stderr line + exit 1), `scripts/container-setup.sh` (git presence check + operator-hand repo init inside the container + scenario wiring for full-rollout and rollback drills), `scripts/rollout-drill.sh` (live gemma4 checklist: pre-state capture, URL prompts, post-state verification that `qwen3.6-coding` is READY at the end — the script performs **no** actuation itself beyond what the operator types). **May touch:** `test_server.py` static-analysis tests only (textContent/no-localStorage assertions, §7 items).
- **Self-test:** `python3 -m unittest tests.test_server -v`; container drill per §11 rows 5–6 end-to-end.

Merge order: T1 → (T2 ∥ T3) → integration = the two container drills.

---

## 11. TEST PLAN — mapped 1:1 to MVP2.md's acceptance checklist

| MVP2 acceptance criterion | proven by |
|---|---|
| Edit form generated from spans; every `qwen3.6-coding` field renders; unknown flags raw inputs; comments visible | **Unit:** form-model test — a JS-free assertion that the detail JSON for the fixture contains a span for every §2.5 field present. **Container:** open edit form on the fake qwen unit, count inputs vs profile fields, notes pane visible. |
| Splice-write differs ONLY in edited bytes + provenance; comments byte-identical; span invariants | **Unit (`test_actuation.py`):** the §9.7 property test over the contract's five cases (`-c`, `--port`, `-ctk`, embedded-JSON `--chat-template-kwargs`, mixperten continuation-line flag) on fixture copies + a 3-field multi-splice. |
| Verify-or-restore: corrupted splice restores bit-exactly, never reaches daemon-reload | **Unit:** fault-inject (monkeypatch `render_value_bytes` to emit garbage / truncate the written file) → `VerifyError`, file bytes == original, stubbed `run_actuate` records prove `daemon-reload` never invoked, worktree clean. |
| Pre-flight rejects: port onto declared claim (any enable state); ctx estimate over budget; RETIRED edit | **Unit:** `preflight_port` against the fixture board (incl. default-8080 claims + self-port); `preflight_memory` with injected meminfo/cgroup values both sides of `estimate + 1 GiB` vs budget; retired fixture → 422 at both routes. |
| Full container rollout: edit ctx → preview → apply → stop/splice/reload/start → LOADING → READY; git log commit; fresh /api/mem row | **Container:** operator-hand `git init` per printed instructions, `--actuate` launch, UI drill; assert SSE phase sequence, `git -C <dir> log --oneline -1` matches `commit_message` format, `/api/mem` shows a measured row at the new ctx. |
| Container rollback: rollout onto `FAKE_EXIT_1` → FAILED → one-click rollback → old READY; git log shows revert | **Container:** edit ctx to 424242 (sentinel) → fake server exits 1 → `failed(unit_failed)` → rollback button → old config READY; `git log` shows `Revert "roundhouse: ..."`. |
| Without `--actuate`: 403 on every mutating route, UI shows read-only, provably cannot write | **Unit:** §9.4 (route table → 403; AST arming guard; unarmed gateways raise). **Container:** default launch, POST via curl → 403, header badge READ-ONLY, and the whole MVP1 guard class still green against the source. Also: default launch on the container works with git *removed* (E11 — read-only never needs git; check runs before container git install). |
| `--actuate` + no/wrong token: 401, nothing written | **Unit:** §9.6 + write-recorder stubs. **Container:** curl with bad bearer → 401; `git status` unchanged. |
| Live boltzmann drill on `llama-server-gemma4.service` | `scripts/rollout-drill.sh` (operator-authorized): operator inits the repo per printed instructions (first real execution of E2), one parameter rollout to READY, rollback, fleet restored (`qwen3.6-coding` READY, script verifies via `/api/units`). |
| No build step; stdlib only; no German; no throughput figures | Existing stdlib-import AST test (still green); review grep for German; no new t/s surfaces exist by construction. |

---

## 12. RISKS — top 3 mechanical-coder failure modes and the guard placed

1. **Ascending-order or re-derived-span splicing.** The natural loop applies edits first-to-last and "helpfully" re-runs `extract_param_profile` between edits — corrupting every offset after the first length-changing splice. **Guards:** `splice()` is specified as sort-descending + pure byte arithmetic with disjointness asserted (§3.3); the §9.7 property test includes a *multi-field* edit on the multi-line mixperten shape where ascending order provably corrupts bytes, so the wrong loop fails the suite, not review.

2. **Verify weakened because spans "changed", and stale `watcher.units` after apply.** The coder sees `spans` differ old-vs-new, concludes the a-check is broken, and deletes it — or passes verify but forgets S4, so the *next* edit splices at dead offsets. **Guards:** §3.5(a) explicitly excludes `spans`/`raw_argv` from dict equality (the false-failure is pre-solved in the spec), with both a passing multi-field case and a fault-injected failing case pinning the check's strength; a dedicated test performs two sequential rollouts on one server instance and asserts the second diff is computed against the *new* file (fails if S4 is skipped).

3. **Gate bypass on a new code path** — a helper calling `subprocess` directly, a POST route added without `check_bearer`, or arming leaking (`ACTUATE_ARMED` set outside `cmd_serve`). **Guards:** three interlocking AST assertions (§9.1/3/4: write-verb constants only in Section E and only as `run_actuate` args from the three whitelisted call-sites; `subprocess` only in the four gateways; exactly one arming assignment) plus the behavioral frozen-route-table test (every POST route × unarmed → 403 × bad token → 401) — adding a route without adding it to the table fails the table-completeness check (the test enumerates `do_POST`'s dispatch strings from the AST and asserts the table covers them).

**Out of scope (do not build, per contract):** autonomous reconciliation, enable/disable, creating/deleting unit files, multi-host, drop-in parsing, editing non-selected units, llama-swap, flag add/remove (E1), per-flag comment adjacency mapping, rollout history persistence beyond git.

---

**Key spec-input findings worth relaying:** the MVP1 AST guards live in `/home/mfritsche/src/roundhouse/mvp1/tests/test_server.py` (`TestZeroWritePath`, lines 72–129) — the `daemon-reload`-never-in-source test and the subprocess-gateway test are the two that *must* change, and §9 pins their exact successors. The `spans` dict shape in `extract_param_profile` (`roundhouse.py` lines 740–743) stores tuples keyed by canonical field with `flag`/`value` sub-keys, and `unknown_flags` carry their own `value_span` — both are sufficient for the splice engine with no parser changes. The watcher currently emits events only on rung change (line 1281–1287), which is why §8's badge-change emission is called out as new machinery rather than a one-line badge.
