# Roundhouse MVP1 — Build Architecture & Work Breakdown

Grounded in: `MVP1.md` (contract), `docs/design/roundhouse-design.html` (mechanisms, overridden
where noted), the fixtures, and the 2026-08-13 boltzmann recon. Every decision below is decided;
implementers must not re-open them.

## ORCHESTRATOR AMENDMENTS (validated against live boltzmann journals, 2026-08-13 — these OVERRIDE §3.4 where they differ)

The llama-server build actually deployed on boltzmann (llama.cpp-latest) was measured over 33k+
journal lines. Its marker vocabulary:

- READY: `llama_server: model loaded` (followed microseconds later by `llama_server: listening on http://...`).
  It NEVER prints `all slots are idle` or any `update_slots:` line.
- BUSY start: `slot launch_slot_:` / `processing task`.
- BUSY end: `slot      release:` — **variable-width whitespace**; the regex MUST be `slot\s+release:`.
- FAILED (bind race): `couldn't bind HTTP server socket` then `exiting due to HTTP server error`.
- systemd itself logs `... Consumed ... CPU time ..., 21.5G memory peak ...` on unit stop (bonus signal, not required).

Therefore the §3.4 llama-server tables are amended to:

```python
LS_READY      = [ r"model loaded", r"update_slots: all slots are idle" ]   # 2nd kept for other builds
LS_LISTENING  = [ r"listening on http" ]        # recorded as last_marker only, NOT a ready signal
LS_BUSY_START = [ r"launch_slot_", r"update_slots: .*new prompt", r"processing task" ]
LS_BUSY_END   = [ r"slot\s+release:", r"update_slots: all slots are idle" ]
LS_REQ_DONE   = [ r"request: (GET|POST) [^ ]+ [0-9.]+ 200" ]
```

(`main: model loaded` from the original spec text matches nothing in this build — the real line is
`llama_server: model loaded`; `r"model loaded"` covers both.)

Golden samples captured from the live host are committed at `mvp1/tests/journal_samples/`:
`llama-server-start.jsonl` contains a REAL full sequence Stopped -> Started -> `load_model: loading
model` -> (46 s) -> `model loaded` -> `listening on` -> `launch_slot_` -> `print_timing`.
No llamafile unit has journal history on the host, so llamafile regex tests use synthetic lines
constructed to the LF_* table (document that in the test file).

Second amendment — the legacy repo-root parser: `parser.py` + `tests/test_roundhouse_parser.py`
were produced by the bullpen (16/16 green under pytest) and are KEPT as an independent contract
suite. T1 additionally: (a) convert `tests/test_roundhouse_parser.py` to run under plain
`python3 -m unittest` (its only pytest uses are one `pytest.fail` and the `pytest.main` footer);
(b) translate its German comments/docstrings to English; (c) in `parser.py` rename `_anreichern`
-> `_enrich` and translate comments — behavior unchanged, suite stays green. `docs/fixtures/
MANIFEST.txt` is a deliberate sha256 integrity guard over the fixtures; keep it. `mvp1/roundhouse.py`
Section A (below) is the canonical parser going forward; the root files are the frozen v0 contract.

---

**Global decisions made up front:**

- **D1 — Unit selection: ExecStart-binary match.** A unit is "ours" iff any token of its tokenized
  `ExecStart` has a basename matching `^llama-server` or containing `llamafile` (covers
  `foo.llamafile` self-exec files), with a per-file override comment `# roundhouse: manage` /
  `# roundhouse: ignore` as the escape hatch. Justification: the criterion is derived from the file
  itself, so new/renamed units are picked up automatically and there is no allowlist to rot.
  `--scan` prints the selected set so a miscount against the known 23 is visible immediately.
- **D2 — Probe moments in MVP1: NONE.** Journal + `systemctl show` + `/sys`/`/proc` reads only.
  Not one HTTP request is ever made to any unit's port. "Cheap property read" = subprocess to
  `systemctl show`/`journalctl`/local file reads. "Health poll" = any socket connection to a
  managed unit's port; forbidden, enforced in code (see §3, §9).
- **D3 — Server model: `http.server.ThreadingHTTPServer` + threads,** not asyncio. The two event
  sources are blocking subprocess pipes (journalctl tail, systemctl poll loop), which map 1:1 onto
  threads; SSE clients are long-lived handler threads holding a `queue.Queue`; matches the
  `/opt/llm-proxy.py` flat-script house style.
- **D4 — sqlite lives at** `${XDG_STATE_HOME:-~/.local/state}/roundhouse/roundhouse.sqlite`
  (directory created on first write — the one sanctioned write, together with the sqlite journal
  beside it; default rollback journal, no WAL).
- **D5 — Artifact identity without hashing.** No sha256 of 15–20 GiB files.
  `file_id = "sz{size}:mt{int(mtime)}"`, `sha256: null` in the spine record.
- **D6 — LOADING is detected from the journal only** (regex tables §3.4 as amended), never from `/health`.

---

## 1. FILE LAYOUT

```
mvp1/
  roundhouse.py                 # THE file: Section A parser, B watcher+mem, C server, D main
  roundhouse.service            # its own user unit (repo content; operator installs by hand)
  static/
    index.html                  # one page, inline CSS + inline vanilla JS, EventSource
  scripts/
    switch-drill.sh             # operator's gemma4 <-> qwen3.6 exercise (read-only for us)
    container-setup.sh          # incus throwaway-container fixture install
    fake-llama-server.py        # emits real llama-server log lines, sleeps, listens; container only
  tests/
    test_parser.py
    test_watcher.py
    test_server.py
    fixtures-extra/
      synthetic-unknown.service # unknown-directive + edge-case fixture (spec'd in §8)
    journal_samples/
      llama-server-start.jsonl  # captured on boltzmann (COMMITTED — real data)
      systemctl-show.txt        # captured multi-unit `systemctl show` output (COMMITTED)
```

- The real fixtures stay in `docs/fixtures/`; tests reference them via
  `REPO = Path(__file__).resolve().parents[2]; FIXTURES = REPO / "docs" / "fixtures"`.
- Test runner: `cd mvp1 && python3 -m unittest discover -s tests` — each test file begins with
  `sys.path.insert(0, str(Path(__file__).resolve().parents[1]))` then `import roundhouse`.
  No pytest, no `__init__.py`.
- `roundhouse.py` CLI: `--serve` (default; binds `:8090`), `--scan [DIR]` (parse + print selection,
  deployments, port board as text, exit 0/1 — the acceptance-audit mode), `--unit-dir DIR`
  (default `~/.config/systemd/user`), `--port N` (default 8090), `--db PATH` (default per D4),
  `--no-db` (skip sqlite entirely; used in `--scan` and container).

---

## 2. PARSER SPEC (Section A — pure functions, zero subprocess, zero I/O beyond the initial read)

### 2.1 Reading and the line model

Files are read **once as `bytes`**. All offsets are absolute byte offsets into that buffer.
Decoding for display is `utf-8, errors="replace"`, per token/line, never applied to the buffer as
a whole for parsing purposes.

```python
@dataclass
class Line:
    kind: str          # 'comment' | 'section' | 'directive' | 'continuation' | 'blank'
    start: int         # inclusive byte offset
    end: int           # exclusive; includes the trailing \n if present
    lineno: int        # 1-based physical line number
```

**Invariant (tested):** `b"".join(raw[l.start:l.end] for l in unit.lines) == raw`. Every byte
belongs to exactly one Line. This is the "nothing lost" guarantee.

Classification per physical line, in order — inspect, strip nothing: first non-whitespace byte
`#` or `;` -> `comment`; first non-ws byte `[` -> `section`; empty/whitespace-only -> `blank`;
a continuation of a previous directive (2.2) -> `continuation`; else `directive`. Comments are
NEVER continuation candidates and never join logical lines (systemd rule: a comment line inside a
continuation sequence is skipped but the continuation continues — implement exactly that: while
assembling a logical value, a `comment` line is emitted as a `comment` Line and skipped from the
value).

### 2.2 Directives and logical lines

```python
@dataclass
class Directive:
    section: str            # 'Unit' | 'Service' | 'Install' | ...
    key: str                # decoded, exact case as written
    key_span: tuple[int,int]
    value_raw: bytes        # everything after '=', across continuations, verbatim incl. '\' and '\n'
    value_span: tuple[int,int]   # from byte after '=' to end of last physical line of the logical line
    lineno: int
```

Continuation rule (systemd's): if the last byte of the line (before `\n`) is `\`, the logical line
continues on the next non-comment line. For **value assembly** each `\`+newline (+ following
line's content) contributes: the `\` and newline are replaced by a single space `b" "`. This makes
the fixture's no-space form `llama-server\` + newline + `     -m` assemble to
`llama-server      -m` -> tokens split cleanly. `value_raw` keeps the verbatim bytes; the
assembled form exists only inside the tokenizer.

Repeated keys: kept as a list in file order. Multiple `ExecStart=` -> parse warning
`multiple ExecStart; last one used` (none exist today).

### 2.3 UnitFile

```python
@dataclass
class UnitFile:
    path: str
    name: str                  # basename, e.g. 'qwen3.6-coding.service'
    raw: bytes
    lines: list[Line]
    directives: list[Directive]        # ALL of them, in file order
    comments: list[dict]               # [{lineno, start, end, text}]  text = verbatim decoded, incl. '#'
    warnings: list[str]
    # semantic extracts (each also still present in .directives):
    description: str | None
    retired: bool                      # Description matches r'^\[RETIRED'
    retired_note: str | None           # full '[RETIRED ...]' bracket content
    exec_start: ExecStart | None
    exec_condition: Directive | None
    gate: dict | None                  # see 2.6
    install_wanted_by: str | None
    known: dict                        # Restart, RestartSec, TimeoutStartSec, Type,
                                       # WorkingDirectory, LimitNOFILE, After, Wants, Documentation
    other_directives: list[Directive]  # everything not in the known set — PRESERVED
```

**"Unknown directive preserved" means, concretely:** every directive whose key is not in the
known-semantics set (`Description, Documentation, After, Wants, ExecStart, ExecCondition, Type,
Restart, RestartSec, TimeoutStartSec, WorkingDirectory, LimitNOFILE, WantedBy`) lands in
`other_directives` with its exact key casing, verbatim `value_raw`, and spans; it appears in the
unit-detail JSON under `"other_directives"` and renders in the UI. Nothing is skipped,
warned-and-dropped, or normalized.

### 2.4 ExecStart tokenization

```python
@dataclass
class Token:
    text: str                  # decoded, quotes stripped, %% -> %, escapes resolved
    raw: bytes                 # exact source bytes, quotes and all
    start: int; end: int       # absolute byte offsets of `raw` in the file
    has_specifier: bool        # a lone %x (x != %) was left literal in .text

@dataclass
class ExecStart:
    directive: Directive
    tokens: list[Token]        # full argv incl. wrapper prefix
    wrapper: dict | None       # {"kind":"taskset","cpus":"4-7","tokens":[...]} — argv[0] basename in
                               # {'taskset','nice','env','ionice'} consumed with its options up to the
                               # first token that is an absolute path
    engine_argv: list[Token]   # tokens from the engine binary onward
    engine: dict               # {"kind": 'llama-server'|'llamafile', "binary": str, "variant": str}
                               # variant: 'rk-llama.cpp' if '/rk-llama.cpp/' in binary else
                               #          'llama.cpp' for llama-server; 'llamafile' otherwise
```

Tokenizer rules (systemd command-line syntax, exactly these, no cleverness):

1. Split the assembled logical value on unquoted whitespace (space/tab; newlines already replaced).
2. Quotes respected **only at the start of a word**: `'...'` single-quoted — content literal, no
   escapes; `"..."` double-quoted — `\"` `\\` `\n` `\t` resolved. A quote mid-word is a literal
   character. Unterminated quote -> warning, rest of line becomes one token.
3. Specifiers: `%%` -> `%` in `.text`. Any other `%x` stays literally in `.text` and sets
   `has_specifier=True`. (`%%` appears only in *comments* in the fixtures — comments are verbatim
   and never unescaped; this rule applies to directive values only.)
4. Token spans: `start` = offset of the first byte of the word in source (including opening
   quote); `end` = after the last byte (including closing quote). A trailing `\` continuation char
   belongs to **no** token (it belongs to the Line). **Invariant (tested):
   `raw[tok.start:tok.end] == tok.raw` for every token, and `tok.text` re-derives from `tok.raw`
   alone.**

Embedded JSON: `--chat-template-kwargs '{"enable_thinking":false}'` is just token #n+1 with
`.text == '{"enable_thinking":false}'`. The ParamProfile step additionally does `json.loads` on it.

### 2.5 ParamProfile extraction

```python
def extract_param_profile(engine_argv: list[Token]) -> dict
```

Walk `engine_argv[1:]`. A token starting with `-` is a flag; its arity comes from the
**known-flag table**, else heuristic: next token exists and does not start with `-` -> arity 1,
else 0.

Known-flag table (flag -> canonical field, type):

| flags | field | type |
|---|---|---|
| `-m`, `--model` | `model_path` | str |
| `-c`, `--ctx-size` | `ctx` | int |
| `-t`, `--threads` | `threads` | int |
| `-tb`, `--threads-batch` | `threads_batch` | int |
| `-fa`, `--flash-attn` | `flash_attn` | str |
| `-ctk`, `--cache-type-k` | `cache_type_k` | str |
| `-ctv`, `--cache-type-v` | `cache_type_v` | str |
| `--jinja` | `jinja` | bool (arity 0) |
| `--chat-template-kwargs` | `chat_template_kwargs` | str; plus `chat_template_kwargs_json` via `json.loads` (parse failure -> field null + warning, raw kept) |
| `--temp` `--top-p` `--top-k` `--min-p` `--presence-penalty` `--repeat-penalty` | `sampling.{temp,top_p,top_k,min_p,presence_penalty,repeat_penalty}` | float/int |
| `-n`, `--predict` | `n_predict` | int |
| `--reasoning-budget` | `reasoning_budget` | int |
| `--reasoning` | `reasoning` | str |
| `-Cr` / `-Crb` | `cpu_range` / `cpu_range_batch` | str |
| `--cpu-strict` / `--cpu-strict-batch` | `cpu_strict` / `cpu_strict_batch` | int |
| `--alias` | `alias` | str |
| `--host` | `host_bind` | str |
| `--port` | `port` | int |

Result dict fields: all of the above (absent -> null), plus `port_source: "flag"|"default"` (no
`--port` -> `port: 8080, port_source: "default"`), `pinning` (merged view: taskset wrapper and/or
`-Cr`/`-Crb`, e.g. `{"decode":"4-7","batch":"0-7","via":["taskset"]}` — null when none),
`unknown_flags: [{"flag": str, "value": str|null, "flag_span":[s,e], "value_span":[s,e]|null}]`,
and `raw_argv: [str]` (decoded engine argv). Every extracted field also gets an entry in
`spans: {"ctx": {"flag":[s,e], "value":[s,e]}, ...}` — **this is the byte-offset retention the
acceptance criterion demands**, keyed by canonical field name so a future splicer can find `-c`'s
value bytes without re-tokenizing.

### 2.6 Kernel gate

```python
def parse_gate(unit: UnitFile) -> dict | None
```

If `ExecCondition` present: try regex `uname -r[^=]*=\s*"?([0-9A-Za-z._-]+)"?` against its decoded
value -> `{"kind":"kernel","wants":"6.1.75-npu-port","raw": <value text>}`. No match ->
`{"kind":"opaque","wants":null,"raw":...}`. Also honor `ConditionKernelVersion=` (same shape,
`"kind":"kernel"`). Null when no condition directives.

### 2.7 Selection and deployment record

```python
def select_units(unit_dir: str) -> list[str]       # sorted paths; rule D1; only files matching *.service exactly (never *.bak, *.service.bak-*)
def build_deployment(unit: UnitFile, host: str, statf=os.stat) -> dict   # shape in §4.4
def quant_hint(filename: str) -> str | None        # regex ((?i)(IQ\d_[A-Z]+|Q\d_K_[MSL]|Q\d_K|Q\d_0|UD-Q\d_K_XL|BF16|F16)) against filename
def assert_no_paid_offload(dep: dict) -> None      # see §9 R-guard; called by build_deployment
```

`assert_no_paid_offload`: raises `AssertionError` unless `model_path` is an absolute local path
and neither the binary path nor any argv token contains any of `("api.openai.com",
"openrouter.ai","api.anthropic.com","googleapis.com","://")` — the structural "no paid offloading"
rail, in code as MVP1.md requires. Called for every deployment on every scan; a module-level
constant `PAID_OFFLOAD = None  # hard rail: never implemented` marks the intent greppably.

---

## 3. WATCHER SPEC (Section B — a synchronous, lock-guarded state machine fed by two threads)

### 3.1 Event sources — exactly these, nothing else

| source | invocation | cadence | feeds |
|---|---|---|---|
| systemd properties | `systemctl --user show -p ActiveState,SubState,UnitFileState,Result,NRestarts,ExecMainPID,ExecMainStartTimestamp,ExecMainStartTimestampMonotonic,ConditionResult,ControlGroup -- <all selected units>` (one subprocess, blocks separated by blank lines, in argument order) | poll every **3 s** | OFF/STANDBY/STARTING/FAILED, active-ness, cgroup path, restart counts |
| journal tail | `journalctl --user -f -o json -n 0 --no-pager` (ONE subprocess for all units; filter in-process on `_SYSTEMD_USER_UNIT` in selected set) | continuous | LOADING->READY, BUSY, per-line notes |
| journal backfill | at watcher start, per currently-active selected unit: `journalctl --user -u <name> -o json -n 300 --no-pager`, feed lines with `__REALTIME_TIMESTAMP >= ExecMainStartTimestamp` through the same `apply_journal_line` | once at startup | correct rung when Roundhouse starts after a model already loaded |
| kernel/cgroup files | read `/proc/meminfo`; for each **active** unit read `/sys/fs/cgroup<ControlGroup>/memory.peak` and `memory.current` | piggybacked on the 3 s tick | header gauge, mem estimator (§6) |

The four rows above are **cheap local property reads — allowed**. A **health poll** is any socket
connection to a managed unit's port — **there are zero in MVP1** (D2). The code contains no
`urllib`/`http.client`/`socket` use except the listening server itself; `run_ro()` is the only
subprocess gateway.

```python
READONLY_SYSTEMCTL_VERBS = {"show", "cat", "list-units", "list-unit-files"}
def run_ro(argv: list[str], timeout=10) -> str
    # asserts argv[0] in {"systemctl","journalctl"} and, for systemctl,
    # that the verb is in READONLY_SYSTEMCTL_VERBS. The ONLY subprocess entry point.
def spawn_ro_stream(argv: list[str]) -> subprocess.Popen   # same asserts; used for journalctl -f
```

Both subprocesses are supervised: if journalctl dies, restart with backoff 1 s -> 2 -> 4 -> … ->
30 s cap; while down, every unit snapshot carries `"stale": true` and the reason
`"journal tail down"`. Same for the poll loop.

### 3.2 The Watcher class (testable core — no threads, no subprocess inside)

```python
class Watcher:
    def __init__(self, units: dict[str, UnitFile], running_kernel: str,
                 mem_store: "MemStore|None", now=time.time): ...
    def apply_systemctl_show(self, props: dict[str, dict[str, str]]) -> list[dict]  # events
    def apply_journal_line(self, rec: dict) -> list[dict]                            # events
    def apply_cgroup_sample(self, unit: str, peak: int|None, current: int|None) -> list[dict]
    def snapshot(self) -> dict          # full state, shape §4.4(a)
```

Threads (Section C) call these under one `threading.Lock`; returned events go to the SSE bus.
Tests drive the class directly with recorded inputs — no mocking framework.

### 3.3 The 8-rung state machine — exact rules, evaluated top-down per unit, first match wins

Per-unit journal state: `ready: bool`, `busy: bool`, `busy_since`, `last_marker: str`, all
**reset to False whenever `ExecMainStartTimestamp` changes** (new process => back to LOADING) and
whenever ActiveState leaves `active`.

| # | rung | rule |
|---|---|---|
| 1 | RETIRED | `unit.retired`. Display-terminal. If ActiveState=`active` anyway, keep RETIRED and add badge `"retired_but_running": true` (red). |
| 2 | FAILED | `ActiveState == "failed"`, OR (`ActiveState=="activating"` AND `SubState=="auto-restart"`) -> detail `restart-looping, NRestarts=<n>`. |
| 3 | STARTING | `ActiveState == "activating"` (any other SubState). |
| 4 | BUSY | `ActiveState == "active"` AND journal `busy` flag. Detail: `since busy_since`. **No timeout demotion** — a 94-minute runaway is a real documented case; show elapsed, add badge `"long_running": true` after 30 min (amber, informational). |
| 5 | READY | `ActiveState == "active"` AND journal `ready` flag. |
| 6 | LOADING | `ActiveState == "active"`, not ready. Detail: `elapsed = now − ExecMainStartTimestamp`, plus `last_load_seconds` from sqlite when a row exists (renders "48 s elapsed (last load: 72 s)"). If elapsed > parsed `TimeoutStartSec` (default 90), add badge `"no_ready_marker": true` (amber "active but no ready marker seen — sensing may be incomplete"), still LOADING, never FAILED. |
| 7 | STANDBY | `ActiveState in ("inactive","dead")` AND `unit.gate` not null AND gate unsatisfied: for `kind=="kernel"`, unsatisfied iff `gate.wants != running_kernel` (compared against `os.uname().release` — a syscall, not a probe); for `kind=="opaque"`, unsatisfied iff `ConditionResult == "no"` or never started — render `"gated (condition unverified): <raw>"`. Detail for kernel kind: `waiting for kernel 6.1.75-npu-port (running: 6.12.x)`. **Neutral color, never red — enforced by giving STANDBY its own CSS class sharing no red styling (§5).** |
| 8 | OFF | everything else (`inactive` without gate, or gate satisfied but stopped; `deactivating` -> OFF with detail "stopping"). |

Roster mapping (emitted alongside the rung, per MVP1 table): READY/BUSY -> `hot`,
STARTING/LOADING -> `loading`, OFF/STANDBY -> `configured` (STANDBY additionally
`schedulable: false`), FAILED -> `load-failed`, RETIRED -> `null` (not a Deployment target).

### 3.4 Journal regexes — the LOADING/READY/BUSY detectors

**USE THE AMENDED TABLES FROM THE TOP OF THIS FILE for llama-server.** Matched with `re.search`
against the record's `MESSAGE` string. Engine kind comes from the parser
(`exec_start.engine.kind` + `variant`).

**llamafile (older llama.cpp server core — listening comes *after* load, so it IS the ready
signal there):**

```python
LF_READY      = [ r"ll?ama server listening at", r"all slots are idle", r"model loaded" ]
LF_BUSY_START = [ r"slot \d+ is processing", r"processing task" ]
LF_BUSY_END   = [ r"slot \d+ released", r"all slots are idle" ]
```

Transition logic in `apply_journal_line` (per unit, engine-appropriate table):

1. `BUSY_END` match -> `busy=False`; if it is also a READY marker, `ready=True`.
2. else `READY` match -> `ready=True`, `busy=False`.
3. else `BUSY_START` match -> `busy=True` (and implies `ready=True` — a slot processing means the
   model is loaded; covers a missed ready line).
4. else `REQ_DONE` match -> `ready=True`, `busy=False`.
5. else: no state change; line ignored (MVP1 does not stream a journal tail to the UI).

**Fallback hierarchy for LOADING detection, explicitly:** primary = READY markers; secondary =
BUSY/REQ activity implying ready (rules 3–4); tertiary = the amber `no_ready_marker` badge after
TimeoutStartSec while staying in LOADING. Under no circumstance does failure to detect READY
produce a FAILED render or an HTTP probe.

DEFERRED TO MILESTONE 2 (decided at MVP1 review): the `no_ready_marker` and
`retired_but_running` badges are specified above but not implemented; per-unit `stale`
is currently derived from global source health, not per-unit sensed_at age.

Staleness: every unit in every snapshot carries `sensed_at` (epoch float of last input that
touched it); the global snapshot carries `sources: {"journal": "ok"|"down since <t>",
"systemctl": ...}`. UI shows an amber banner "sensing degraded" when either source is down;
per-unit rungs render dimmed with `?` suffix (e.g. `READY?`).

---

## 4. SERVER SPEC (Section C)

`ThreadingHTTPServer` (D3), bind `0.0.0.0:8090` (matching the llama-servers' LAN-only posture;
read-only tool, LAN bind — decided as sufficient for MVP1). `:8090` appears on the port board as
`roundhouse (self)`.

Routes (all GET; anything else -> 405):

| route | returns |
|---|---|
| `/` | `static/index.html` |
| `/api/units` | (a) unit list |
| `/api/units/<name>` | (b) unit detail; 404 if not selected |
| `/api/ports` | (c) port board |
| `/api/deployments` | (d) Deployment records |
| `/api/mem` | measured peaks table (rows of §6 schema + estimates) |
| `/api/events` | (e) SSE stream |

### SSE format

```
retry: 3000
id: <monotonic int>
event: snapshot | rung | ports | mem
data: <single-line JSON>
```

- On connect: one `snapshot` event (full §4.4a payload), then deltas.
- `rung`: `{"unit":"qwen3.6-coding.service","rung":"LOADING","roster":"loading","since":1765600000.1,"detail":"elapsed 12s (last load: 72s)","badges":[],"stale":false}`
- `ports`: full port-board payload (c), re-emitted whenever any claim's state class changes.
- `mem`: `{"unit":...,"peak_bytes":19110000000,"phase":"ready","source":"measured"}`
- Heartbeat comment line `: ping` every 15 s. Client queues bounded (`queue.Queue(maxsize=256)`);
  a full queue drops the client (it reconnects and resnapshots).

### 4.4 JSON shapes

**(a) `/api/units` — also the SSE snapshot:**

```json
{ "host": "boltzmann", "kernel": "6.12.x-...", "now": 1765600000.0,
  "mem": {"total_bytes": 32840000000, "available_bytes": 14000000000},
  "sources": {"journal": "ok", "systemctl": "ok"},
  "self_port": 8090,
  "units": [ { "unit": "qwen3.6-coding.service", "description": "...", "retired": false,
      "rung": "READY", "roster": "hot", "since": 1765590000.0, "detail": "",
      "badges": [], "stale": false, "sensed_at": 1765599999.0,
      "enabled": true, "active_state": "active", "sub_state": "running", "n_restarts": 0,
      "port": 8085, "port_source": "flag", "alias": "qwen3.6-coding",
      "gate": null, "model_file": "qwen36-27b-a3b-coder-Q4_K_M.gguf",
      "quant_hint": "Q4_K_M", "ctx": 65536,
      "mem": {"bytes": 19110000000, "source": "measured", "label": "measured peak, this (unit, model, ctx)"},
      "port_conflict": {"class": "armed", "with": ["llama-task.service"]} } ] }
```

**(b) `/api/units/<name>` — detail:** everything in (a)'s row plus:

```json
{ "path": "/home/mfritsche/.config/systemd/user/qwen3.6-coding.service",
  "param_profile": { "...": "full §2.5 dict incl. spans, unknown_flags, raw_argv" },
  "engine": {"kind": "llama-server", "variant": "llama.cpp", "binary": "..."},
  "wrapper": {"kind": "taskset", "cpus": "4-7"},
  "comments": [ {"lineno": 7, "start": 123, "end": 210, "text": "# MODEL SWAPPED 2026-08-12: ..."} ],
  "other_directives": [ {"section": "Service", "key": "LimitNOFILE", "value": "65536", "span": [0,0]} ],
  "lines": [ {"kind": "comment", "start": 0, "end": 88, "lineno": 1} ],
  "warnings": [], "raw_size": 3084,
  "known": {"restart": "on-failure", "restart_sec": "5", "timeout_start_sec": null},
  "history_mem": [ {"ctx": 65536, "peak_bytes": 19110000000, "sampled_at": "...", "phase": "ready", "load_seconds": 72.4} ] }
```

**(c) `/api/ports`:**

```json
{ "ports": [ { "port": 8086, "claims": [
      {"unit": "llama-task.service", "enabled": true, "rung": "READY", "retired": false, "gate": null},
      {"unit": "llama-server-qwen35-npu.service", "enabled": false, "rung": "STANDBY", "retired": false,
       "gate": {"kind": "kernel", "wants": "6.1.75-npu-port"}} ],
    "class": "armed", "note": "harmless only while BOTH the disable and the kernel gate hold" } ],
  "self": {"port": 8090, "claims_by_units": []} }
```

Class rules: `active` = >=2 claims with rung in (STARTING, LOADING, READY, BUSY) — red; `armed` =
>=2 claims that are enabled OR whose only blocker is a currently-unsatisfied gate — amber (this is
:8086); `latent` = >=2 claims, all others (mixperten's :8085) — grey footnote; single claim -> no
entry beyond the row itself. Claims counted across ALL parsed units regardless of enable state.

**(d) `/api/deployments` — the entity-spine records:**

```json
{ "host": "boltzmann", "deployments": [ {
    "deployment_id": "boltzmann/qwen3.6-coding.service",
    "unit": "qwen3.6-coding.service",
    "artifact": { "model": null, "path": "/home/mfritsche/models/qwen36-27b-a3b-coder-Q4_K_M.gguf",
      "filename": "qwen36-27b-a3b-coder-Q4_K_M.gguf", "format": "gguf",
      "quant_hint": "Q4_K_M", "sha256": null, "file_id": "sz16040000000:mt1765500000" },
    "host_artifact": { "host": "boltzmann", "path": "...", "exists": true,
      "size_bytes": 16040000000, "mtime": 1765500000 },
    "engine": { "kind": "llama-server", "variant": "llama.cpp", "binary": "..." },
    "param_profile": { "...": "§2.5 dict (spans included)" },
    "load_strategy": { "kind": "on-boot", "enabled": true, "gate": null },
    "roster": { "rung": "READY", "state": "hot", "since": 1765590000.0 },
    "memory": { "bytes": 19110000000, "source": "measured", "label": "..." },
    "retired": false } ] }
```

`load_strategy.kind`: `"on-boot"` iff UnitFileState=enabled, else `"manual"`; gated units keep
their gate object inside load_strategy. RETIRED units still emit a record with `"retired": true`
and `roster.state: null` — consumers must filter on `retired`.

**(e) roster events** — the SSE `rung` events; `/api/events` is the roster feed.

---

## 5. UI SPEC (`static/index.html` — one file, inline CSS+JS, monospace, no framework, no build)

Layout top to bottom (single column, `<pre>`-adjacent aesthetic, system monospace, dark
background, works at 100 cols):

1. **Header bar:** `ROUNDHOUSE · boltzmann · kernel <x>` + mem gauge
   `mem ████░░ 17.2 / 30.6 GiB` + sensing banner slot (hidden unless a source is down -> amber
   "sensing degraded (journal tail down)").
2. **ACTIVE section:** units with rung in STARTING/LOADING/READY/BUSY, one row each:
   `● READY  qwen3.6-coding  :8085  alias qwen3.6-coding  Q4_K_M · ctx 65536 · peak 17.8 GiB (measured)`
   LOADING rows show `◐ LOADING 48s (last load: 72s)` with a CSS pulse on the glyph. BUSY shows
   `◉ BUSY 2m14s`.
3. **STANDBY section** (own heading "STANDBY — kernel-gated, normal on this kernel"):
   `◌ llama-server-qwen35-npu  waiting for kernel 6.1.75-npu-port (running: 6.12.x)  :8086 ⚠ shared with llama-task`.
   CSS class `.standby { color: var(--neutral) }` — the stylesheet defines red **only** on
   `.failed` and `.conflict-active`, so STANDBY structurally cannot render red.
4. **OFF section** (collapsed count + expandable list).
5. **FAILED section** (only when non-empty; red; shows `NRestarts`).
6. **RETIRED section** (folded by default:
   `RETIRED (1)  mixperten [2026-08-12, claims :8085] [show]`; expanded row is grey with full
   detail available).
7. **PORT BOARD strip:** one cell per claimed port:
   `8082 ✓ · 8085 ✓ (+1 retired claim) · 8086 ⚠ 2 claims · 8090 roundhouse (self) ✓`
   **The :8086 collision appears as:** amber `⚠` cell; clicking expands:
   `8086 — llama-task (enabled, READY) + llama-server-qwen35-npu (disabled, kernel-gated STANDBY) — armed: harmless only while BOTH the disable and the kernel gate hold.`
   Additionally both unit rows carry the inline `⚠ shared :8086` badge — the collision is visible
   from three places without being told about it.
8. **Detail pane** (click a unit row -> fetch `/api/units/<name>`, render below the list, no SPA):
   - "WHAT RUNS" table: model file (+exists ✓/✗, size), quant, ctx, KV types, flash-attn,
     threads/pinning (text form: `pinned A76 (4-7) via taskset`), sampling, alias, gate, restart
     policy, port.
   - "OTHER DIRECTIVES" table (the preserved unknowns).
   - **"OPERATOR'S NOTES — rendered verbatim; the file is never rewritten":** all comment blocks
     concatenated in file order inside a `<pre>`, assigned via `textContent` (never `innerHTML` —
     verbatim means byte-faithful and XSS-safe), original `#` markers kept, `white-space: pre;
     overflow-x: auto`.
   - mem history rows (`measured` rows; if none: the estimate with its full label).

JS: one `EventSource('/api/events')`; `snapshot` rebuilds the whole list (state kept in one JS
object, re-render is a pure function of it); `rung`/`ports`/`mem` patch the object and re-render.
On `error`, EventSource auto-reconnects; show the degraded banner until the next snapshot.

Theme: fixed dark palette, explicit colors (deliberate single look — a LAN ops tool).

---

## 6. MEMORY ESTIMATOR SPEC

**Sampling:** `ControlGroup` comes from the existing 3 s poll; `memory.peak` (cgroup v2) is a
plain file read at `/sys/fs/cgroup<ControlGroup>/memory.peak`. Reading two sysfs files for <=3
active units every 3 s is piggybacked on the existing tick — no new wakeups. Missing file (cgroup
torn down, old kernel) -> sample None, skip silently.

**When sqlite is written (only these two moments):**

1. **Transition into READY:** upsert phase=`ready` row with current `memory.peak` and
   `load_seconds = ready_time − ExecMainStartTimestamp` (yields the "last load: 72 s" display).
2. **Transition out of active:** upsert phase=`exit` row using the **last cached** peak from the
   tick reads (the cgroup may already be gone — that is why every tick caches `last_peak` in
   memory).

Per-tick reads only update the in-memory cache; they never touch sqlite. Max writes per model
lifecycle: 2.

**Schema** (`roundhouse.sqlite`, D4 path; `--no-db` skips all of this):

```sql
CREATE TABLE IF NOT EXISTS mem_peak (
  unit          TEXT NOT NULL,
  model_path    TEXT NOT NULL,
  model_file_id TEXT NOT NULL,          -- "sz<bytes>:mt<mtime>"  (D5)
  ctx           INTEGER,                -- null when no -c flag
  ctk           TEXT, ctv TEXT,
  phase         TEXT NOT NULL CHECK (phase IN ('ready','exit')),
  peak_bytes    INTEGER NOT NULL,
  load_seconds  REAL,                   -- ready rows only
  boot_id       TEXT NOT NULL,          -- /proc/sys/kernel/random/boot_id
  sampled_at    TEXT NOT NULL,          -- ISO 8601 UTC
  PRIMARY KEY (unit, model_file_id, ctx, boot_id, phase)
);
```

**API:**

```python
class MemStore:
    def __init__(self, db_path: str|None): ...        # None => inert (lookup None, record no-op)
    def record(self, *, unit, model_path, file_id, ctx, ctk, ctv, phase, peak_bytes, load_seconds=None): ...
    def lookup(self, unit, file_id, ctx) -> dict|None  # newest 'exit' row preferred, else 'ready':
        # {"peak_bytes":..,"load_seconds":..,"source":"measured","label":"measured peak, this (unit, model, ctx)"}
    def history(self, unit) -> list[dict]

def estimate_memory(dep: dict, store: MemStore) -> dict
    # measured row exists -> it, verbatim.
    # else: {"bytes": int(size*1.10 + 1.5*2**30), "source": "estimate",
    #        "label": "estimate (file size + 10% + 1.5 GiB overhead; no measured peak, no KV model)"}
    # model file missing -> {"bytes": null, "source": "unknown", "label": "model file not found"}
```

The `source` field is mandatory everywhere a number surfaces; the UI prints the label with the
number. Deliberately no KV-cache formula: MVP1 has no GGUF metadata reader and MVP1.md's own point
is that the formula lies on this host. **No token/s anywhere; no llama-bench-style figures;
whole-prompt wall clock is out of scope for MVP1 display entirely.**

---

## 7. WORK BREAKDOWN — 3 tasks, composable without coder-to-coder contact

All three code tasks write into `mvp1/roundhouse.py`, pre-seeded (by T1) with section banners:

```python
# ===== SECTION A: PARSER (pure; no I/O beyond bytes in) =====
# ===== SECTION B: WATCHER + MEMSTORE (no threads inside; run_ro is the only subprocess gate) =====
# ===== SECTION C: SERVER + SSE + STATIC =====
# ===== SECTION D: MAIN / CLI =====
```

### T1 — Parser + deployments + legacy cleanup (first; T2/T3 depend only on its signatures)

- **Files:** `mvp1/roundhouse.py` Section A (+ Section D `--scan` path), `mvp1/tests/test_parser.py`,
  `mvp1/tests/fixtures-extra/synthetic-unknown.service`; PLUS legacy cleanup per the Orchestrator
  Amendments (root `parser.py` + `tests/test_roundhouse_parser.py`).
- **Interfaces (exact, frozen):** `parse_unit(path: str, raw: bytes) -> UnitFile`,
  `tokenize_execstart(directive, raw) -> ExecStart`, `extract_param_profile(engine_argv) -> dict`,
  `parse_gate(unit) -> dict|None`, `select_units(unit_dir) -> list[str]`,
  `build_deployment(unit, host, statf=os.stat) -> dict` (shape §4.4d),
  `quant_hint(filename) -> str|None`, `assert_no_paid_offload(dep) -> None`; dataclasses
  `Line/Directive/Token/ExecStart/UnitFile` exactly as §2.
- **Self-test:** `cd mvp1 && python3 -m unittest tests.test_parser -v` and
  `python3 roundhouse.py --scan ../docs/fixtures --no-db` (prints 23 units, port board showing
  8085x2 + 8086x2, exits 0); `python3 -m unittest tests.test_roundhouse_parser` from repo root
  stays green (16 tests).

### T2 — Watcher + MemStore (depends on T1 types; parallel with T3)

- **Files:** `roundhouse.py` Section B, `mvp1/tests/test_watcher.py` (uses committed
  `journal_samples/`), `scripts/fake-llama-server.py`.
- **Interfaces consumed:** `UnitFile`, `build_deployment`. **Provided (frozen):** `Watcher` per
  §3.2 verbatim, `MemStore`/`estimate_memory` per §6, `run_ro`/`spawn_ro_stream`/
  `READONLY_SYSTEMCTL_VERBS` per §3.1,
  `parse_show_blocks(text, unit_order: list[str]) -> dict[str, dict[str,str]]`, regex constants
  per §3.4 AS AMENDED. Event dict shape: §4 `rung` payload.
- **Self-test:** `cd mvp1 && python3 -m unittest tests.test_watcher -v` — drives `Watcher` with
  `journal_samples/*.jsonl` + `systemctl-show.txt`, asserts the full rung table of §3.3
  including: qwen35-npu fixture + `running_kernel="6.12.0"` -> STANDBY never FAILED; new
  `ExecMainStartTimestamp` resets to LOADING; recorded start log reaches READY;
  `run_ro(["systemctl","--user","start","x"])` raises.

### T3 — Server + SSE + UI (depends only on frozen shapes in §3.2/§4, not on T2 internals)

- **Files:** `roundhouse.py` Sections C+D (threads: systemctl poll loop, journal tail loop, HTTP
  server; wiring `Watcher`+`MemStore`+lock+`EventBus`), `mvp1/static/index.html`,
  `mvp1/tests/test_server.py`, `mvp1/roundhouse.service`, `scripts/switch-drill.sh`,
  `scripts/container-setup.sh`.
- **Interfaces provided:** `class EventBus: subscribe() -> queue.Queue; publish(event, data)`;
  HTTP routes and JSON exactly §4.4 (test asserts key sets against golden dicts).
- **Self-test:** `cd mvp1 && python3 -m unittest tests.test_server -v` — boots the server on an
  ephemeral port with a stub `Watcher` (same `snapshot()` shape — the shape is in this spec, so no
  dependency on T2 landing), asserts `/`, `/api/units`, `/api/ports`, `/api/deployments` payload
  shapes, and that `/api/events` yields a `snapshot` event then a published `rung` event via
  `http.client`.

Integration (end of T3 or T2, whichever lands last):
`python3 roundhouse.py --serve --unit-dir <dir>` against the container. No task requires talking
to another coder: every cross-task type and JSON shape is frozen in §2–§6.

## 8. TEST PLAN — mapped 1:1 to the MVP1.md acceptance checklist

| acceptance criterion | proven by |
|---|---|
| Parses all 23 units; unknown directives preserved | **Unit test** on the 23 fixtures + `synthetic-unknown.service` (contains `MemoryHigh=`, `Environment="A=b c"`, `FrobnicateWidget=yes`, a `;`-comment, an unterminated quote, a `%h` specifier -> asserts each lands in `other_directives`/warnings, none dropped). **Live boltzmann:** `python3 roundhouse.py --scan ~/.config/systemd/user --no-db`; assert count == 23, zero fatal parse errors. |
| Byte offsets retained per token | **Unit test properties** on every fixture: (1) `b"".join(raw[l.start:l.end] for l in lines) == raw`; (2) every Token: `raw[t.start:t.end] == t.raw`; (3) every ParamProfile span: `raw[s:e]` re-tokenizes to the same value. |
| Comments verbatim, none lost | **Unit test:** set of comment texts == set of physical lines in the raw file whose first non-ws byte is `#`/`;`, byte-identical. **Live:** open mixperten detail, eyeball the KLD table and `%%` lines rendered untouched. |
| qwen35-npu renders STANDBY, not FAILED | **Unit test** (fixture + `running_kernel="6.12.x"`), **container** (unit installed, gate false), **live boltzmann** (mainline kernel). Also assert the CSS: `.standby` shares no rule with `.failed`. |
| :8086 collision visible unprompted | **Unit test:** port board from fixtures yields `{8086: class "armed", 2 claims}` and `{8085: latent}`. **Live:** open the page — the ⚠ cell and both row badges present with no configuration. |
| Restart shows LOADING ~72 s then READY | **Container:** fixture copies exec `fake-llama-server.py` (prints listening line, sleeps 10 s, prints `llama_server: model loaded`); assert SSE sequence STARTING->LOADING->READY. **Live boltzmann:** switch drill, watching the UI: LOADING with elapsed counter, then READY, and a `mem` event recording peak + load_seconds. |
| Per-Deployment spine records | **Unit test:** `build_deployment` on qwen3.6-coding fixture equals a golden dict (Artifact/HostArtifact/Engine/ParamProfile/Host/LoadStrategy keys exactly §4.4d). |
| Zero write path | **Unit test:** `run_ro` rejects every systemctl verb outside the read-only set; a source-grep test asserts none of `(" start", " stop", " enable", " disable", " restart", "daemon-reload")` occur as systemctl arguments in `roundhouse.py`; monkeypatched `socket.socket.connect` (raising) while the Watcher chews all recorded samples proves zero outbound connections. **Inspection:** sqlite path outside the repo (D4); the only write-mode opens are inside `MemStore`. |
| Runs without a build step | Clean checkout: `python3 mvp1/roundhouse.py --scan docs/fixtures --no-db` and `cd mvp1 && python3 -m unittest discover -s tests` on stock Python, stdlib only (a test imports `roundhouse` and asserts its module set is stdlib-only). |

**`scripts/switch-drill.sh`** (operator's shell does all actuation; Roundhouse only watches):

```sh
#!/bin/sh
# Live LOADING->READY exercise on boltzmann. Roundhouse must be running on :8090.
# Roundhouse itself performs ZERO actuation; this script is the operator's hand.
set -eu
echo "== watch http://boltzmann:8090 while this runs =="
systemctl --user stop qwen3.6-coding.service
systemctl --user start llama-server-gemma4-q4km.service
echo "expect: gemma4-q4km STARTING -> LOADING (elapsed counter) -> READY; qwen3.6-coding -> OFF"
printf "verify in UI, then press enter to revert... "; read _
systemctl --user stop llama-server-gemma4-q4km.service
systemctl --user start qwen3.6-coding.service
echo "expect: qwen3.6-coding LOADING ~72s -> READY (and a measured-peak row in /api/mem)"
```

**Container** (`scripts/container-setup.sh`): copies fixture *copies* with `ExecStart` rewritten
to `fake-llama-server.py` (offsets differ from originals — irrelevant, offset criteria are proven
on pristine fixtures by unit tests), installs `roundhouse.service`, runs the ladder scenarios:
normal start (LOADING->READY), gate false (STANDBY), fake server `exit 1` (FAILED with NRestarts
climbing), both :8086 claimants installed (port board armed class).

## 9. RISKS — top 3 mechanical-coder failure modes and their guards

1. **The tokenizer normalizes instead of preserving** — decoding the whole file to str, splitting
   on whitespace, rebuilding values, corrupting offsets, `%%`, or the no-space `llama-server\`
   continuation. **Guards:** parsing is defined over `bytes` with absolute offsets (§2.1); three
   property tests are acceptance-mapped, so any normalization fails the suite on the real
   fixtures, which were chosen to break exactly this.

2. **"Server is listening" becomes a READY signal, or someone adds a /health check "just to be
   sure"** — the classic way to break the LOADING rung and the no-knock principle. **Guards:**
   per-engine regex tables where `LS_LISTENING` is explicitly excluded from READY with the reason
   stated inline; D2 written into the spec; `run_ro` whitelist plus the socket-monkeypatch test
   and the source-grep test make an added probe or actuation verb a test failure, not a review
   catch.

3. **Rung-precedence inversions** — rendering the kernel-gated unit FAILED (or red), letting
   RETIRED-but-active show as READY, or demoting a long BUSY to READY on a timer. **Guards:** the
   state machine is a single ordered table (§3.3) with first-match-wins semantics; STANDBY's
   "never red" is enforced structurally in CSS class separation (§5); "no BUSY timeout demotion"
   is an explicit rule with the documented incident as rationale; `test_watcher` asserts every row
   of the table.

Out of scope (do not build): journal tail streaming to the UI, t/s displays of any kind, GGUF
metadata reading, sha256 hashing, auth, any actuation, drop-in handling beyond a warning badge
"drop-in overrides not parsed (MVP1)", and multi-host anything.
