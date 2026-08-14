# Roundhouse MVP7 — Build Architecture & Work Breakdown

**File: `mvp1/MVP7-SPEC.md`** (beside `roundhouse.py`; `MVP7.md` at repo root stays the contract — its acceptance checklist is the definition of done, its Out-of-scope list is binding).

Grounded in: `MVP7.md` (contract), E-series (MVP2), F-series (MVP3), G-series (MVP4), H-series (MVP5), I-series (MVP6) — all stand; **this spec amends exactly one I-series decision: I3's frozen 16-tool catalog becomes 17 (`peer_status`), amended here, not by editing `MVP6-SPEC.md`.** Base: `mvp1/roundhouse.py` @ d1e42f9 (7062 lines), `roundhouse_mcp.py` (838 lines), 532 green tests. Recon findings that shaped this spec: **(1)** `cmd_serve` builds exactly one `ThreadingHTTPServer(('0.0.0.0', port), ...)` (line 6964) and runs `serve_forever()` **on the main thread**; the SIGTERM handler spawns a helper thread to call `server.shutdown()` because calling it from the handler would deadlock (comment at 6985–6988). Moving every listener into its own thread dissolves that deadlock entirely — the main thread becomes a plain `shutdown_event` waiter and can call `shutdown()` itself. **(2)** The server class (line 3393) already takes all shared state as ctor args (`watcher, event_bus, port, watcher_lock, rollout_engine, advertise_host`); N instances sharing one watcher/bus/engine/lock is a loop, not a redesign. `address_family` is a class attribute (`AF_INET`) consumed inside `TCPServer.__init__` — an instance assignment *before* `super().__init__` flips it per listener. **(3)** `take_snapshot` (3410) is where `mode` and `rollout` are merged **under `watcher_lock`** — `peers` merges at the same spot, and because `watcher_lock` is a non-reentrant `threading.Lock`, the PeerWatch read used there MUST NOT re-acquire it (the `_unlocked` naming convention in §4.4 exists for exactly this). `Watcher.snapshot()` itself is untouched, so `test_snapshot_shape` (test_watcher.py 487) needs no edit. **(4)** `poll_systemctl` publishes to the EventBus *inside* `watcher_lock` (6759–6795) — legal because `publish` is `put_nowait` and can never block; the peer thread follows the same pattern. **(5)** The route-table guard (test_server.py 954–986) has TWO allowlists — `from_frozen` and `get_only`; `/api/peers` joins `get_only` only (it is a pure read; `FROZEN_POST_ROUTES` stays at 9). **(6)** `_section_spans` (test_server.py 619–634) matches `^# ===== SECTION ([A-E])\b` — a new `SECTION F` banner placed between E and D would go *unmatched*, silently extending Section E's span over the peer code and licensing write verbs there. The regex must become `([A-F])` in the same commit that adds the banner; this is load-bearing, not cosmetic. **(7)** There is today **no outbound socket anywhere in `roundhouse.py`** — `import socket` serves one call, `socket.gethostname()` (3593); listeners are socketserver-internal. The new confinement guard therefore starts from a provably clean slate. **(8)** `TestFrozenCatalog` (test_mcp.py 707) checks `TOOLS` row-for-row against a local `FROZEN_CATALOG` dict (line 660) with rows shaped `(method, path, action, body_args, send_requester, has_shaper, schema)`; two literal `16`s (lines 185, 255) count the catalog; `serverInfo.version` `'6.0'` is asserted at line 209. **(9)** The mobile 44 px frozen selector set (`.unit-row, button, .off-section-toggle, .stop-tick-row`) binds *touch targets* — a non-interactive peer strip stays outside it by construction. **(10)** Fleet reality: `qwen3.6-coding` serves :8085 on boltzmann AND ampere, so a blanket "peer port must not be a managed port" rule would forbid the legitimate `ampere=…:8085` — the D2 rule must be host-AND-port, not port-only.

## 1. GLOBAL DECISIONS (J-series; implementers must not re-open them)

- **J1 — N servers, one thread each; the main thread owns lifecycle.** One `ThreadingHTTPServer` per bind address, every instance constructed with the *same* `watcher, event_bus, port, watcher_lock, rollout_engine, advertise_host` (recon 2) — shared state needs zero new plumbing, and the 409 operation-slot sharing across listeners is automatic (one engine). No socket multiplexing, no selectors rewrite: the server class is battle-tested and per-address instances keep the address-family problem local (`address_family` set per instance before `super().__init__`; §3.2). **Lifecycle, frozen:** all servers are constructed (= bound + listening) in the all-or-nothing loop (§3.3) *before* any `serve_forever` starts; each then runs `serve_forever()` in its own daemon thread; the **main thread** blocks on `while not shutdown_event.wait(1.0): pass`. SIGTERM/SIGINT handler: set `shutdown_event`, terminate the journal proc — nothing else (the MVP2 helper-thread workaround dies with this change; `shutdown()` is now safe from the main thread because `serve_forever` no longer runs there — recon 1). After the wait loop exits, the main thread calls `srv.shutdown()` then `srv.server_close()` for every listener in construction order, then joins each listener thread (timeout 5 s). One `Roundhouse listening on http://<display-addr>:<port>` line per address (IPv6 bracketed). **`--port` stays single; `watcher.self_port` and every port-board/self-claim behavior are unchanged; `--advertise-host` is unchanged and independent of the bind list** — it names how *others* reach this host, not where it listens.
- **J2 — `--bind`: repeatable AND comma lists; literal IPs only; canonicalize; duplicates and same-family wildcard overlap are configuration errors; bind failures are all-or-nothing with every address + errno named.** Grammar in §3.1. Hostnames are rejected (`not a literal IP address — bind to the address, not the name`): binding resolves once and silently pins whatever DNS said at boot, which is exactly the lying-prone behavior MVP7 exists to remove, and literals keep the D2 local-forms set (§6.3) enumerable. IPv6 accepted bare (`::1`) or bracketed (`[::1]`) — brackets stripped; with `--port` separate there is no colon ambiguity. All addresses canonicalized via `inet_pton`→`inet_ntop` before comparison. **Rejected as configuration errors (exit 1, every offense listed): duplicates after canonicalization, and a same-family wildcard beside any other address of that family** (`0.0.0.0` + `127.0.0.1`; `::` + `::1`) — the wildcard already covers the specific, so the operator's intent is ambiguous and half of it silently redundant; reject-with-reason beats guessing. Cross-family mixes are fine (`0.0.0.0` + `::1`; `0.0.0.0,::` is the "everything, both families" config — §3.2 pins `IPV6_V6ONLY=1` so `::` never shadows the v4 wildcard). Default: exactly `['0.0.0.0']` — packaged unit untouched.
- **J3 — `--peer NAME=HOST:PORT`, repeatable only (no comma lists — a peer decl already contains `=` and `:`; commas invite quoting bugs for zero gain).** NAME: `^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$`; duplicate NAME → configuration error. HOST: non-empty, no whitespace/`/`; DNS name or IP literal; IPv6 literal MUST be bracketed (`[fe80::1]:22`) because the port is attached here. PORT: integer 1–65535. **Cap: 8 peers**; worst case one sequential round = 8 × 2 s = 16 s, comfortably inside the 60 s cadence with the whole budget of DNS latency on top; > 8 → startup refusal `too many peers (N > 8): a probe round must finish well inside the 60 s cadence`. Malformed declaration → exit 1 echoing the offending argv text verbatim. All parse/validation errors across `--bind` and `--peer` are **collected and all reported** before the single exit — same loudness doctrine as bind failures.
- **J4 — The prober: sequential, one function, connect-and-close, nothing else.** Sequential because concurrency buys nothing under the 8-peer cap (16 s ≪ 60 s) and costs a thread pool, per-peer locking, and a new class of shutdown races; the deadlock-free version is the one with one thread. Exact sequence per peer: `sock = socket.create_connection((host, port), timeout=PEER_TIMEOUT_SEC)` then immediately `sock.close()` — **no bytes written, no bytes read, no TLS, no HTTP** (a peer's port may be an inference server; Roundhouse does not knock). `create_connection` calls `getaddrinfo` internally on every call — **re-resolution per probe comes free and is thereby guaranteed**; a roaming host returning on a new address is picked up the next round. Success = the call returns (something accepted the connect). Failure = **any** exception — `gaierror` (DNS), timeout, `ConnectionRefusedError`, unreachable, all of it; `last_error = f'{type(e).__name__}: {e}'[:200]`. A refused connect is a failure, not a soft state: *reachable* means something is listening, and refusal proves nothing is.
- **J5 — Peer state lives in a `PeerWatch` object; mutation and snapshot-reads under `watcher.lock`; THE PROBE ITSELF RUNS OUTSIDE EVERY LOCK.** One shared lock (not a private one) because `take_snapshot` already merges under `watcher_lock` (recon 3) and a second lock would either nest (ordering hazard) or leave the snapshot merge racing the probe writes. The cost of the shared lock is only acceptable because the lock is held for **dict mutation only** — a 2 s connect held under `watcher_lock` would stall every HTTP route, the SSE stream, and the 3 s sensing tick for up to 16 s per round; this is Risk #1 (§10.1) and the same lesson MVP5 paid for twice. Structure and the `_unlocked` method convention in §4.4.
- **J6 — Dedicated daemon thread, 60 s cadence on `shutdown_event.wait`, first round immediately.** `peer_watch_loop`: `while not shutdown_event.is_set(): run one round; shutdown_event.wait(interval)` — the first round runs at startup+0 so `unknown` lasts seconds, not a minute, and the UI is truthful almost immediately (a returning operator should not stare at `unknown` for 60 s when the answer costs 2 s). `wait(interval)`, never `sleep` — SIGTERM must not wait out a cadence. The thread exists only when ≥ 1 peer is declared. **Construction order (per MVP5 recon 3 discipline): engine → PeerWatch → HTTP servers (all bound) → poll/journal threads → peer thread.** The peer thread starts strictly AFTER the all-or-nothing bind succeeds: a startup that dies on a bind failure must have performed zero probes. Interaction with the three existing loops: none — no shared mutable state beyond the lock-guarded dict and the bus. `--peer-interval SECONDS` (default 60, min 1) exists solely so the container drill can prove hysteresis timing in seconds instead of minutes; the 2 s timeout is a constant (`PEER_TIMEOUT_SEC = 2.0`), never a flag.
- **J7 — Surfaces (frozen in §5): `GET /api/peers` (unauthenticated read), snapshot key `peers` (merged in `take_snapshot`, same rows), SSE event `peer` on transition ONLY, a non-interactive UI strip, and MCP tool #17 `peer_status` (passthrough, schema `{}`).** A client connecting mid-life needs no replay: the SSE stream's initial `snapshot` event (and every later one) carries current `peers` — transitions are deltas on top of a state the client already has; state it, build nothing. The UI strip is text-only and non-interactive, so the frozen 44 px touch-target selector list is **not** amended (recon 9; decided). The catalog goes 16 → 17: exact registry row, `FROZEN_CATALOG` transcription, count updates, and `docs/MCP.md` additions in §5.5; `serverInfo.version` bumps `'6.0'` → `'7.0'` (one literal in `roundhouse_mcp.py`, one in test_mcp.py:209).
- **J8 — Guard evolution: outbound connects are confined by AST to `_probe_peer`, `_probe_peer` is structurally incapable of targeting an undeclared endpoint, and the D2 local-collision rule is a startup refusal.** Full spec §6. Shape: (a) a new AST guard over `roundhouse.py` — every `create_connection` / `.connect` / `.connect_ex` node and every `socket.socket(...)` construction must sit inside `PROBE_CALLSITES = {'_probe_peer'}` (today's source has zero such nodes — recon 7 — so the guard is exact from day one); (b) `_probe_peer(peer_watch, name, connect=socket.create_connection)` takes **no host/port parameters** — it looks the endpoint up in `peer_watch.declared` (populated once at startup from validated `--peer` argv and never mutated), so an out-of-declaration probe is unrepresentable, plus a runtime `KeyError`/assert for belt; (c) the **D2 invariant** — the prober never targets a managed unit's declared port on the local host — is enforced at startup by `validate_peers` (§6.3): peer HOST textually/canonically ∈ LOCAL_FORMS **and** peer PORT ∈ MANAGED_PORTS → refusal naming both sides of the collision (`peer 'x' targets 127.0.0.1:8085, which is managed unit qwen3.6-coding.service's port on this host — peers are other hosts`). Host-AND-port, never port-only (recon 10). Residual risk accepted and documented: a DNS name that *later* resolves to this host is out of threat model (the Fritz!Box zone is operator-controlled; no runtime re-resolution vetting — sensing stays sensing).
- **J9 — Tests: a new `tests/test_peers.py` (pure logic: parsing, state machine table with injected clock + injected connect, D2 validation, lock discipline), multi-listener + bind-failure + real-socket hysteresis integration in `test_server.py`, guard evolution + seeds, MCP 17.** Full spec §7. Unit tests touch no real sockets (injected `connect` callable per J8's signature — the default-argument shape passes the AST guard because default expressions belong to `_probe_peer`'s node); exactly one integration class opens real sockets against ephemeral listeners.
- **J10 — Docs: NEW `docs/PEERS.md` covering BOTH features (the listen list and the peer watch are one operator story: "where Roundhouse listens, who it watches"); README one-liner; packaged `roundhouse.service` NOT edited.** Outline §5.6. The package keeps the wildcard default; `--bind 127.0.0.1` + the caddy front, `--peer` declarations, and `--advertise-host` are operator edits to ExecStart — stated in PEERS.md, consistent with "its own unit file is its configuration surface".

## 2. FILE / SECTION LAYOUT

```
mvp1/
  MVP7-SPEC.md                  # this file
  roundhouse.py                 # extended: NEW SECTION F; edits in C and D as listed
  roundhouse_mcp.py             # +1 registry row; serverInfo version bump
  static/index.html             # peer strip (additive)
  scripts/
    container-setup.sh          # unchanged
    peer-drill.sh               # NEW: container drill (§7.6) + live boltzmann checklist
  tests/
    test_peers.py               # NEW: §7.1–§7.3 (pure logic, no real sockets)
    test_server.py              # extended: §6 guards, §7.4 integration, §7.5 statics
    test_mcp.py                 # extended: catalog 17 (§5.5)
docs/
  PEERS.md                      # NEW (§5.6)
  MCP.md                        # extended (§5.5)
```

**`roundhouse.py` gains a new top-level section** — the peer watch is neither sensing-of-managed-units (B) nor actuation (E); it gets its own banner, placed **between the end of Section E and the Section D banner**:

```python
# ===== SECTION F: PEER WATCH + LISTEN LIST (sensing only: the ONE outbound-connect site is _probe_peer; no data on the wire, no subprocess, no writes, no actuation) =====
```

**Part contents (complete):** `PEER_TIMEOUT_SEC = 2.0`; `PEER_INTERVAL_SEC = 60`; `PEER_MAX = 8`; `parse_bind_list()`; `parse_peer_decls()`; `local_host_forms()`; `validate_peers()`; `class PeerWatch`; `_probe_peer()`; `peer_watch_round()`. **The `_section_spans` regex in test_server.py becomes `^# ===== SECTION ([A-F])\b` in the same task that adds the banner** (recon 6 — without it, Section E's span swallows F and the write-verb confinement guard silently widens; this pairing is load-bearing).

**Edits to existing code (exhaustive):**
- **Section C:** `do_GET` gains exact-match `elif route == '/api/peers': self.serve_peers()` (before the `/api/rollouts/` prefix branch; no prefix collision). `do_POST`: `is_get_route` gains `route == '/api/peers'` (POST → 405). New handler `serve_peers` (§5.1). `ThreadingHTTPServer.__init__` gains kwargs `peer_watch=None, address_family=None` (family assigned to `self.address_family` before `super().__init__`; §3.2 `server_bind` override). `take_snapshot` gains the `peers` merge line (§5.2).
- **Section D:** argparse gains `--bind` (append), `--peer` (append), `--peer-interval` (int, default 60). `cmd_serve`: parse+validate both lists (exit 1 on any error, all listed); construct `PeerWatch` after the engine; the all-or-nothing multi-bind loop replaces the single construction at 6964; signal handler simplified per J1; main-thread wait loop + ordered shutdown per J1; peer thread started last (J6).
- **Section E / Watcher / parser: zero edits.** `FROZEN_POST_ROUTES` unchanged (9).

## 3. LISTEN-LIST SPEC

### 3.1 `parse_bind_list(values: List[str]) -> Tuple[List[Tuple[str,int]], List[str]]` (addr+family pairs, errors)

Input: argparse `append` list, default `None` → `['0.0.0.0']`. Each value split on `,`, tokens stripped; empty token → error `empty bind address in '<value>'`. Per token: strip one layer of `[...]` if present; classify via `socket.inet_pton(AF_INET, t)` else `inet_pton(AF_INET6, t)` else error `'<token>': not a literal IP address — bind to the address, not the name`; canonicalize `inet_ntop(family, packed)`. Post-pass over the canonical list: duplicate → error `duplicate bind address: <addr>`; wildcard-overlap (canonical `0.0.0.0` present with ≥ 1 other AF_INET address, or `::` with ≥ 1 other AF_INET6) → error `'<wildcard>' already covers '<specific>' — bind one or the other`. Returns ordered unique `[(canonical_addr, family)]`. **Errors are collected, never short-circuited**; caller prints every line to stderr and exits 1.

### 3.2 Per-family construction

`ThreadingHTTPServer` accepts `address_family` (int or None): when given, `self.address_family = address_family` **before** `super().__init__` (recon 2). New `server_bind` override: `if self.address_family == socket.AF_INET6: self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)` then `super().server_bind()` — `::` means IPv6-only, explicitly, so `0.0.0.0,::` composes without dual-stack shadowing and the semantics of every bind line are exactly what it says. `allow_reuse_address` inherited (unchanged).

### 3.3 All-or-nothing startup (frozen)

```python
servers, failures = [], []
for addr, family in bind_list:
    try:
        servers.append(ThreadingHTTPServer((addr, port), RoundhouseRequestHandler,
                       watcher, event_bus, port, watcher_lock=watcher_lock,
                       rollout_engine=rollout_engine, advertise_host=advertise_host,
                       peer_watch=peer_watch, address_family=family))
    except OSError as e:
        failures.append(f"cannot bind {display(addr)}:{port}: [Errno {e.errno}] {e.strerror}")
if failures:
    for srv in servers: srv.server_close()      # nothing is left listening
    for line in failures: print(line, file=sys.stderr)
    return 1
```

Construction binds and listens (`bind_and_activate=True` default), so appending to `servers` == "this address is live". Every failing address is reported, not just the first; every successfully bound socket is closed before the non-zero exit. `display(addr)` brackets IPv6. Then: one listener thread per server (daemon, named `http-<addr>`), main-thread wait loop, ordered `shutdown()`/`server_close()`/join per J1. `watcher.self_port = port` unchanged — one port, N addresses; `self_port`, the port board's self claim, and `advertise_host` are single-valued and correct as-is (stated per J1).

## 4. PEER-WATCH SPEC

### 4.1 `parse_peer_decls(values: List[str]) -> Tuple[Dict[str, Tuple[str,int]], List[str]]`

Per value (one declaration per flag, J3): split on the **first** `=` → `name`, `endpoint`; missing `=` → error `malformed --peer '<value>': expected NAME=HOST:PORT`. `name` validated against `^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$`; duplicate → error naming it. `endpoint`: if it starts with `[` → must contain `]:`; host = inside brackets, must `inet_pton(AF_INET6, host)`; port = after `]:`. Else `host, sep, port = endpoint.rpartition(':')`; no `sep` → error `missing :PORT`; a remaining `:` in host → error `bracket IPv6 hosts: [addr]:port`. Host: non-empty, no whitespace, no `/`. Port: `int` in 1–65535 else error. Count > `PEER_MAX` → the J3 cap error. Returns insertion-ordered `{name: (host, port)}` — declaration order is probe order and UI order... no: **UI and API sort by name** (§5); probe order is declaration order (irrelevant to semantics, stated for determinism).

### 4.2 The prober (frozen; the ONLY outbound-connect site in the codebase)

```python
def _probe_peer(peer_watch, name, connect=socket.create_connection):
    """THE one outbound-socket site (guarded by AST, §6.1). Endpoint comes only
    from the startup-validated declaration table — unrepresentable otherwise (J8)."""
    host, port = peer_watch.declared[name]        # KeyError = programming error, loudly
    try:
        sock = connect((host, port), timeout=PEER_TIMEOUT_SEC)
        sock.close()
        return (True, None)
    except Exception as e:
        return (False, f'{type(e).__name__}: {e}'[:200])
```

No data written or read; DNS re-resolution is `create_connection`'s own `getaddrinfo` per call (J4). The injectable `connect` default is the unit-test seam and lives inside `_probe_peer`'s AST node, satisfying §6.1.

### 4.3 Hysteresis state machine (frozen; the whole table, no other transitions exist)

States `unknown | up | down`; per-peer counter `consecutive_failures` (cf). Asymmetric by contract: up on first success, down only after two consecutive failures, `unknown` until a probe completes decisively.

| state | probe result | new state | cf | transition event? |
|---|---|---|---|---|
| unknown | success | **up** | 0 | YES (unknown→up) |
| unknown | failure (cf becomes 1) | unknown | 1 | no |
| unknown | failure (cf becomes 2) | **down** | 2 | YES (unknown→down) |
| up | success | up | 0 | no |
| up | failure (cf becomes 1) | up | 1 | no |
| up | failure (cf becomes 2) | **down** | 2 | YES (up→down) |
| down | failure | down | cf+1 | no |
| down | success | **up** | 0 | YES (down→up) |

`since` = time the current state was entered (startup time for the initial `unknown`); it doubles as the last-transition time — one field, both meanings, stated in PEERS.md. `last_probe` = completion time of the most recent probe (`null` before the first). `last_error` = the failure string from the most recent failed probe; **cleared to `null` on success**. A single dropped packet leaves an `up` peer `up` (cf=1); an absent host earns exactly one `→down` event and then silence — no SSE traffic while state is unchanged, by construction of the table.

### 4.4 `PeerWatch` + locking discipline (J5; Risk #1)

```python
class PeerWatch:
    def __init__(self, declared, lock, event_bus, now=time.time):
        self.declared = dict(declared)      # {name: (host, port)} — never mutated after init
        self.lock = lock                    # watcher.lock, shared (J5)
        self.event_bus = event_bus
        self.now = now                      # injected clock (tests)
        self.peers = {name: {'state': 'unknown', 'since': now(), 'last_probe': None,
                             'consecutive_failures': 0, 'last_error': None}
                      for name in declared}
    def apply_result_unlocked(self, name, ok, error, ts) -> Optional[dict]:
        """§4.3 table. CALLER HOLDS self.lock. Returns the SSE payload on transition, else None."""
    def rows_unlocked(self) -> List[dict]:
        """§5 row shape, sorted by name, deep-copied. CALLER HOLDS self.lock (take_snapshot's
        with-block — self.lock is non-reentrant, acquiring here would deadlock; recon 3)."""

def peer_watch_round(peer_watch):
    for name in peer_watch.declared:                       # probe OUTSIDE any lock (J5)
        ok, error = _probe_peer(peer_watch, name)
        with peer_watch.lock:                              # lock for the dict write only
            payload = peer_watch.apply_result_unlocked(name, ok, error, peer_watch.now())
            if payload:
                peer_watch.event_bus.publish('peer', payload)   # put_nowait; recon 4 pattern
```

The `_unlocked` suffix is the convention that makes misuse visible in review: any call to an `_unlocked` method outside a `with lock:` block (or take_snapshot's) is a bug on sight. Lock is held per-peer for the mutation only — never across a probe, never across the round. The thread body: `peer_watch_round(pw)` then `shutdown_event.wait(args.peer_interval)`, first round before the first wait (J6). The watch reads nothing from the engine and writes nothing anywhere else: **no placement input, no warm input, no slot contact — it cannot fail a rollout or a switch** (contract Part 2.7; the three-leg proof in §7.4 makes it mechanical).

## 5. SURFACES SPEC

### 5.1 `GET /api/peers` (frozen shape; unauthenticated read like every GET)

```json
{"peers": [
   {"name": "ampere", "host": "ampere.fritz.box", "port": 8099,
    "state": "up", "since": 1755100000.0, "last_probe": 1755100060.0,
    "consecutive_failures": 0, "last_error": null}
 ],
 "probe": {"method": "tcp-connect", "timeout_seconds": 2.0, "cadence_seconds": 60},
 "means": "reachable, not healthy: a TCP connect proves something is listening on that port and nothing more"}
```

Rows sorted by name; no peers declared → `"peers": []` with the same envelope (the route always exists). `cadence_seconds` reflects `--peer-interval`. The `means` string is the contract's Part 2.5 statement made machine-visible — frozen verbatim. Handler: `with self.server.watcher_lock: rows = pw.rows_unlocked()` when a PeerWatch exists, else `[]`; serialize outside the lock. POST → 405.

### 5.2 Snapshot key

`take_snapshot`, inside the existing `with self.watcher_lock:` block, after the `rollout` merge: `snapshot['peers'] = self.peer_watch.rows_unlocked() if self.peer_watch else []`. Same row dicts as §5.1 field-for-field (they are the same function). `Watcher.snapshot()` untouched (recon 3). Any test asserting `take_snapshot`'s key set gains `'peers'`.

### 5.3 SSE `peer` event (transition-only)

Payload = the peer's full §5.1 row **plus** `"prev_state": "<up|down|unknown>"`. Published only when §4.3 says YES — a flat-lined absent peer generates zero events per the table, which is the checklist's no-traffic row. Mid-life clients: the initial `snapshot` SSE event already carries `peers` (§5.2) — current state arrives with the stream, transitions are deltas; **no replay mechanism exists or is needed** (J7, stated). UI handler: update `state.snapshot.peers` in place by `name` and re-render the strip.

### 5.4 UI strip (additive; textContent only; neutral)

`<div class="peer-strip" id="peer-strip" style="display: none;">` placed between `#degraded-banner` and `#rollout-stepper`. Rendered by new `renderPeerStrip()` from `state.snapshot.peers`: hidden when empty; else visible with a leading label span `peers (reachable)` — the word *healthy* never appears — then one `<span class="peer-cell">` per peer with textContent `name · state` (e.g. `ampere · up`, `dirac · down`, `cern · ?` for unknown). Colors: `up` = existing default text color; `down` and `unknown` = the existing muted-color variable — **no red, no amber, no new colors** (down is information, not an alarm; these are other people's hosts). Non-interactive: no buttons, no clicks, no 44 px amendment (J7/recon 9). Wire-up: `renderPeerStrip()` called from the `snapshot` SSE handler and from a new `eventSource.addEventListener('peer', …)` that patches the named row. No localStorage, no innerHTML — existing static assertions re-run over the grown file.

### 5.5 MCP catalog amendment: 16 → 17 (the I3 amendment, exact)

- **`roundhouse_mcp.py`:** one registry row inserted **after `warm_state`** (read tools stay contiguous, before `switch_preview`): `TOOLS['peer_status'] = {'description': 'Peer reachability watch: declared peers with up/down/unknown state, since, last probe, and last error — reachable means a TCP connect succeeded, not healthy.', 'schema': {'type': 'object', 'properties': {}, 'additionalProperties': False}, 'method': 'GET', 'path': '/api/peers', 'action': False, 'body_args': (), 'shaper': None, 'send_requester': False}`. Passthrough shaping per I2 (`http_status` injection, nothing else). `serverInfo.version` → `'7.0'`.
- **`tests/test_mcp.py` `FROZEN_CATALOG` transcription (exact row, same position after `warm_state`):** `'peer_status': ('GET', '/api/peers', False, (), False, False, _EMPTY_OBJ),` — and the two count literals become 17 (lines 185, 255), version literal `'7.0'` (line 209). Every other `TestFrozenCatalog` invariant (body-args⊆properties, GET⇒no-token, requester-never-body) holds for the new row with zero edits.
- **`docs/MCP.md`:** read-tools section gains `#8 peer_status` with one example call/result pair (fixture from the drill); the read-tools list at the token paragraph (line 386) gains `peer_status`; one sentence in the boundary statement's vicinity: *the catalog is frozen at 17 as of MVP7; `peer_status` is a read — peers are never MCP action targets (contract Out-of-scope).*

### 5.6 `docs/PEERS.md` outline (T2 deliverable; ~1 page)

1. **The listen list:** `--bind` grammar (repeat/comma, IPv4/IPv6, literals only), default wildcard, all-or-nothing loudness; **the caddy pattern** — `--bind 127.0.0.1` + caddy terminating TLS on :443 reverse-proxying to `127.0.0.1:8090` = no second unencrypted door; example ExecStart line. 2. **The peer watch:** `--peer NAME=HOST:PORT` grammar, cap 8, the probe (TCP connect, 2 s, 60 s cadence, re-resolves every round — why: Fritz!Box answers DNS for absent hosts, so resolution proves nothing), the §4.3 hysteresis table verbatim, `since` doubles as last-transition time. 3. **What reachable means and doesn't** (contract Part 2.5, verbatim). 4. **The D2 rule:** peers are other hosts — a peer that names this host on a managed unit's port refuses startup, with the exact error text. 5. **Surfaces:** `/api/peers`, snapshot key, SSE `peer` transition-only, `peer_status` MCP tool. 6. **Packaged unit note:** the package ships the wildcard default and no peers; binding and peers are operator edits to the unit's ExecStart (`roundhouse.service` file is not modified by MVP7). README: one line under features pointing here.

## 6. GUARD SPEC (J8 — security-critical, exact)

### 6.1 NEW AST guard: `test_outbound_connect_confined` (in `TestWriteGuards`)

Over `ast.parse(roundhouse.py)` with the existing `_parents`/`_enclosing_func` machinery: **(a)** every node that is an `ast.Attribute` with `attr in {'create_connection', 'connect', 'connect_ex'}` and every `ast.Call` whose callee resolves to `socket` (i.e. `socket.socket(...)` constructions — Attribute `socket` on Name `socket`) must have `_enclosing_func(node) in PROBE_CALLSITES` where `PROBE_CALLSITES = {'_probe_peer'}` (frozen class constant beside `ROLLOUT_CALLSITES`). **(b)** exactly ONE `create_connection` attribute node exists in the whole file (the default argument inside `_probe_peer` — its parent chain reaches `_probe_peer`'s `FunctionDef`, so leg (a) admits it). **(c)** within `_probe_peer`'s subtree, zero attribute nodes named in `{'send', 'sendall', 'sendto', 'recv', 'recv_into', 'makefile', 'sendfile'}` — connect-and-close means *nothing on the wire*, asserted, not promised. **(d)** the pre-existing single benign `socket.gethostname` use (recon 7) is outside the guarded attribute set and needs no exemption. This evolves the "server never connects out" posture into "the server connects out from exactly one function, which can only aim at declared peers": leg (a) confines the mechanism, §4.2's signature confines the targets (no host/port parameters exist to abuse — the lookup into the startup-frozen `declared` table is the only source), and §6.3 confines what may be declared.

### 6.2 Guard housekeeping

`_section_spans` regex → `([A-F])` (recon 6; same task as the banner — §2). `get_only` allowlist gains `'/api/peers'` (recon 5); `from_frozen` and `FROZEN_POST_ROUTES` unchanged at 9 — asserted by the existing equality simply staying green. Write-verb, subprocess-gateway, file-write, snapshot-lock, `ROLLOUT_CALLSITES`, arming guards: **zero edits, must stay green over the grown source** (Section F contains no write verbs, no subprocess, no writes, and its snapshot access is none at all).

### 6.3 The D2 startup refusal: `validate_peers(peers, units, self_port, bind_addrs, advertise_host, nodename) -> List[str]` (pure)

`MANAGED_PORTS = {u.port for u in units.values() if u.port} | {self_port}`. `LOCAL_FORMS` (`local_host_forms()`, all lowercased): `{'localhost', 'localhost.localdomain', nodename, nodename.split('.')[0], advertise_host}` ∪ `{a for a in bind_addrs if not wildcard}` (a specific bind address IS this host, by declaration) ∪ *loopback-literal rule*: any peer host that parses as an IP literal (`ipaddress.ip_address` after bracket-strip) with `.is_loopback` (covers all of `127.0.0.0/8` and `::1`) ∪ wildcards `{'0.0.0.0', '::'}` (nonsense as peer hosts; caught here) ∪ best-effort `getaddrinfo(nodename)` numeric results at startup wrapped in `try/except` (DNS down must not block a boot; the miss is covered by nodename/bind/advertise forms). **Rule: peer host ∈ LOCAL_FORMS (case-insensitive, canonicalized literals) AND peer port ∈ MANAGED_PORTS → error** `peer '<name>' targets <host>:<port> — port <port> is managed unit <unit>.service's port (or roundhouse's own) on this host; peers are other hosts`. Local host + unmanaged port is **allowed** (the drill depends on it: an ephemeral 127.0.0.1 listener is a legitimate fake peer). Failure mode: startup refusal, exit 1, every collision listed with the loud-config-error batch (J3). Runtime leg: none beyond §4.2's declared-table lookup — the DNS-flip residual is documented out of threat model (J8).

## 7. TEST SPEC

### 7.1 `tests/test_peers.py::TestBindParsing` / `TestPeerParsing` (T1)

Table-driven over §3.1/§4.1: repeats + comma mixes flatten in order; `[::1]` ≡ `::1` (canonical); `0:0:0:0:0:0:0:1` canonicalizes to `::1` and then collides as a duplicate; hostname rejected with the exact message; empty token; duplicate exact; `0.0.0.0`+`127.0.0.1` rejected, `::`+`::1` rejected, `0.0.0.0`+`::1` accepted, `0.0.0.0,::` accepted; default `['0.0.0.0']`. Peers: happy path; first-`=` split (`a=b=c` → name `a`, endpoint `b=c` → malformed endpoint error); name charset/length boundaries; dup name; bare-v6 endpoint → bracket error; `[::1]:22` parses; port 0/65536/garbage; 9 peers → cap error; **multiple errors all reported in one return**.

### 7.2 `TestPeerStateMachine` (T1; injected clock + injected connect, no sockets)

The §4.3 table row-for-row through `PeerWatch.apply_result_unlocked` with a fake clock: every state×result cell asserted on (new state, cf, since-changed-or-not, event-or-None, event `prev_state`); `last_error` set on failure and cleared on success; `since` unchanged on non-transitions; the flat-line case (10 consecutive failures from `down`) yields zero events after the first `→down`; unknown→1-failure stays `unknown` (the honest first minute); full lifecycle unknown→up→(1 fail, still up)→(2nd fail, down)→up with exactly three events. Round-level via `peer_watch_round` with `_probe_peer` monkeypatched/`connect`-injected: probe order = declaration order; a raising `connect` counts as failure with `last_error` prefix = exception class name.

### 7.3 `TestPeerValidation` + `TestPeerLockDiscipline` (T1)

Validation per §6.3: localhost + managed port → error naming unit and port; localhost + unmanaged port → ok; other host + managed port → ok (recon 10 — the ampere:8085 case, asserted as LEGAL); `127.0.0.2`, `::1`, nodename, advertise-host, specific-bind-address forms each × managed port → error; wildcard peer host → error; `getaddrinfo` raising → no crash. Lock discipline: an injected `connect` that asserts `peer_watch.lock.acquire(blocking=False)` succeeds (then releases) proves **the lock is not held during the probe** — the Risk-#1 regression trap, mechanical; `rows_unlocked` never acquires (call under an externally held lock; a re-acquire would deadlock a 1 s-timeout thread harness — asserted by completion).

### 7.4 `test_server.py` integration (T1 owns multi-listener/bind/shutdown; T2 owns route/SSE/three-leg)

- **Multi-listener:** two 127.0.0.1 ephemeral-port servers... (one port, two addresses is impossible on lo alone — use `127.0.0.1` + `::1`, guarded by an IPv6-availability skip): GET `/api/units` via both; armed harness (TestRoutesAuth pattern): claim the slot via one address, POST a second operation via the other → **409** (shared slot); SIGTERM-equivalent: call the shutdown path → both listeners refuse connects afterward.
- **Bind failure all-or-nothing:** occupy a port with a scratch socket; run the §3.3 loop for `['127.0.0.1', '::1']` on it → both failures... (only one fails — occupy for one family, assert the OTHER address's server was closed: connect refused) → non-zero result, stderr names the failing address with errno, nothing listening on either.
- **Real-socket hysteresis (the one sockets-allowed unit-integration):** ephemeral listener bound; PeerWatch with interval-free manual `peer_watch_round` calls: round 1 → up; close listener; round 2 → still up (cf=1); round 3 → down (the two-failure proof with real `ECONNREFUSED`); rebind... (ephemeral port reuse: bind with SO_REUSEADDR to the same port) → round 4 → up.
- **Route + SSE (T2):** GET `/api/peers` envelope frozen-key assert incl. `means`; empty-config envelope; POST → 405; snapshot `peers` key agrees field-for-field with `/api/peers` rows (same-call comparison); SSE leg — subscribe a bus queue, drive a transition, exactly one `peer` event with `prev_state`; drive a non-transition, zero events.
- **Three-leg no-actuation proof (T2, checklist row):** peers configured + flapping through a full up→down→up cycle with `run_actuate`/`run_git`/`_atomic_write` monkeypatched to raise and a subprocess recorder → zero calls, `engine.current` untouched before/after.
- **Guards (T1):** §6.1 legs a–d green on the grown source; §6.2 housekeeping; **seeded-violation acceptance** (seed → red → unseed at integration, red names in the commit message): **(s1)** a `socket.create_connection(('example.com', 80), timeout=1)` call inside `serve_peers` → §6.1(a) red; **(s2)** a `--peer x=127.0.0.1:<managed fixture port>` config through `validate_peers` asserted to *pass* — i.e. temporarily neuter the managed-port check → `TestPeerValidation` red (the guard-of-the-guard leg: prove the test actually bites by breaking the code, not the test).
- **Statics (T2):** `peer-strip` id present; `peers (reachable)` string present; the word `healthy` absent from the strip renderer; no new innerHTML/localStorage; existing static assertions green.

### 7.5 `test_mcp.py` (T2)

`FROZEN_CATALOG` row per §5.5; counts 17; version `'7.0'`; a `tools/call` of `peer_status` against a stub server returns the §5.1 envelope + `http_status` with `isError: false`; all existing invariant tests green over 17 rows unmodified.

### 7.6 `scripts/peer-drill.sh` (T2; container + live)

Container leg: start a fake-peer listener (`python3 -c` socket bind on 127.0.0.1:PFAKE, unmanaged port); launch roundhouse `--bind 127.0.0.1,::1 --peer good=127.0.0.1:PFAKE --peer gone=127.0.0.1:PGONE --peer-interval 5` (PGONE never bound); asserts via curl+python: both `/api/units` doors answer; `/api/peers` shows `good` up and `gone` `unknown`→`down` within 2 rounds; kill the fake listener → `down` after exactly 2 more rounds (hysteresis timing at 5 s cadence); restart it → `up` within 1 round; SSE capture across the cycle shows exactly 4 `peer` events (good: up, down, up; gone: down) and none while flat; D2 leg — relaunch with `--peer bad=127.0.0.1:<fake unit's port>` → exit 1, error names the unit; bind-failure leg — occupy the port → exit 1 naming the address; MCP leg — `peer_status` via the real stdio subprocess. Live leg (operator-run, may remain open per checklist): boltzmann with `--bind 0.0.0.0 --peer ampere=ampere.fritz.box:8099 --peer dirac=dirac.fritz.box:22`, observe states over a few minutes; confirm DNS answers for an absent host while the probe says `down` (the contract's reason-for-TCP row).

## 8. WORK BREAKDOWN — 2 tasks, sequential T1 → T2

Why 2: the seam is *mechanism* vs *surfaces*. T1 delivers every moving part (parsing, multi-bind lifecycle, PeerWatch, prober, thread, guards) with its logic proven against injected seams; T2 consumes T1's frozen shapes to expose them (route, snapshot, SSE-to-UI, MCP, docs, drill). No third ownership domain exists; sequential because both touch `roundhouse.py` and `test_server.py`.

**T1 — Listen list + peer-watch core + guards**
- **Writes:** Section F complete (§2 list); Section D edits (flags, validation-and-exit, multi-bind §3.3, lifecycle J1, thread start J6); `ThreadingHTTPServer` ctor kwargs + `server_bind` (Section C's class, this one edit is T1's — it is bind mechanism, not surface); `tests/test_peers.py` complete (§7.1–7.3); test_server.py: §6.1 guard + §6.2 regex + multi-listener/bind-failure/hysteresis integration (§7.4 legs 1–3, 6 minus seeds-run).
- **Must not touch:** `do_GET`/`do_POST`/handlers, `take_snapshot` body, `index.html`, `roundhouse_mcp.py`, `test_mcp.py`.
- **Frozen interfaces handed to T2:** `PeerWatch` ctor + `rows_unlocked()` + row shape (§5.1), `declared`, SSE `peer` payload (§5.3), `parse_*`/`validate_peers` signatures, `server.peer_watch` attribute.
- **Self-test:** `cd mvp1 && python3 -m unittest discover -s tests -v` — every pre-existing test green **unmodified except** the §6.2 regex/allowlist lines; scratch-run `--bind 127.0.0.1,::1 --peer x=127.0.0.1:1` and eyeball two listening lines + `down` on stderr-free flow; a bind of an occupied port exits 1 naming it.

**T2 — Surfaces + MCP + UI + docs + drill**
- **Writes:** `serve_peers` + `do_GET`/`do_POST` route lines + `take_snapshot` merge (Section C); `index.html` §5.4; `roundhouse_mcp.py` §5.5 row + version; `test_mcp.py` §7.5; test_server.py §7.4 legs 4–5, 7 (route/SSE/three-leg/statics); `docs/PEERS.md` §5.6; `docs/MCP.md` §5.5; `scripts/peer-drill.sh` §7.6. Runs the seeded-violation acceptance (§7.4 leg 6) once at integration.
- **May touch T1 code:** nothing but bug fixes with a failing test first.
- **Self-test:** full discover green; drill container leg green end-to-end; MCP scripted `peer_status` call by hand.

**Shared-file ownership:** `roundhouse.py` — T1 owns F + D + the server-class ctor/bind, T2 owns Section C handlers/routes/merge; `test_server.py` — T1 owns guards + lifecycle integration, T2 owns route/SSE/three-leg/statics classes; nobody edits the other's classes or any E/F/G/H/I-era test beyond the lines named in §6.2/§7.5.

## 9. TEST PLAN — mapped 1:1 to MVP7.md's acceptance checklist

| criterion | proven by |
|---|---|
| `--bind` repeats/commas, IPv4+IPv6, default unchanged; `127.0.0.1` unreachable off-host, serving locally | `TestBindParsing`; multi-listener integration; drill bind legs; live row (wildcard default untouched = packaged unit unaffected) |
| Multiple addresses, one shared state; cross-listener 409 on the slot | §7.4 multi-listener (shared engine 409 leg) + drill both-doors curl |
| Unbindable address → non-zero exit naming EVERY failing address; nothing left listening | §7.4 bind-failure (errno text + closed-good-listener assert) + drill occupied-port leg |
| Malformed `--peer` fails startup with the offending text | `TestPeerParsing` (exact messages, all-errors-reported) |
| up on first success; down after two consecutive failures; unknown until first completed probe; re-resolution every probe | `TestPeerStateMachine` (full table); §7.4 real-socket hysteresis; re-resolution = `create_connection` per §4.2 (stated + drill live row's roaming-DNS observation) |
| Absent peer for an hour → exactly the transitions earned, no SSE while unchanged | table rows (no-event cells) + flat-line test + drill SSE capture (event count exact) |
| `/api/peers` = snapshot key = `peer_status`, field-for-field; UI neutral, labels *reachable* never *healthy* | §7.4 route leg (same-call comparison); §7.5 MCP passthrough; §7.4 statics (`healthy` absent) |
| The watch cannot actuate (three-leg proof), `engine.current` untouched | §7.4 three-leg no-actuation proof |
| Probing never targets a managed unit's port on the local host — asserted in code | `validate_peers` refusal (§6.3) + `TestPeerValidation` + §6.1 confinement + seeds s1/s2 + drill D2 leg |
| Container drill: two peers, kill + restore, both transitions with hysteresis timing | `peer-drill.sh` container leg (5 s cadence, exact-round assertions) |
| Live boltzmann: ampere + dirac observed (may remain open) | drill live leg, operator-run |
| No build step; stdlib only; no German; no throughput figures | `test_module_imports_are_stdlib_only` (generic `sys.stdlib_module_names` — `ipaddress` passes untouched); review grep |

## 10. RISKS — top 3 mechanical-coder failure modes and the guards placed

1. **The probe runs under `watcher_lock` — the silent latency bomb.** The instinctive shape is `with lock: for peer: probe(); apply()` — compiles, passes every functional test, and stalls every HTTP route, the SSE stream, and the 3 s sensing tick for up to 16 s per round; or its dual, `rows_unlocked` re-acquiring the non-reentrant lock inside `take_snapshot` — an instant, permanent deadlock of every request. This is the same lock-discipline class MVP5 paid for twice (H5's tick placement, §4.4's fire-outside-the-lock). **Guards:** §4.4 spells the loop out line-by-line with the lock scoped to the dict write; the `_unlocked` naming convention makes the deadlock visible in review; `TestPeerLockDiscipline` proves lock-free-during-connect and no-reacquire mechanically; the drill's 5 s cadence with live curls would surface a stalled route as a red timeout.
2. **Guard erosion around the new outbound-socket permission.** Three sub-shapes: a helper that takes `(host, port)` parameters and gets called from somewhere new (the confinement holds but the *target* discipline dies); a probe that "helpfully" sends an HTTP HEAD or reads a banner (knocking on inference servers — exactly what the contract forbids); and the unmatched-banner trap — `SECTION F` added without the `[A-F]` regex update silently extends Section E's span over the peer code, widening the write-verb allowance (recon 6). **Guards:** §4.2 freezes the no-host/port-parameters signature with the declared-table lookup as the only endpoint source; §6.1(c) asserts zero send/recv attributes inside `_probe_peer`; §6.1(a)+(b) pin the callsite set and the single `create_connection` node; §6.2 pairs the regex change with the banner in one task; seeds s1/s2 prove both guard classes actually bite.
3. **Multi-listener lifecycle regressions — half-bound serving or a shutdown that strands listeners.** Failure shapes: serving the subset that bound (the exact operator trap the contract names); keeping the old main-thread `serve_forever` + helper-thread `shutdown()` pattern for one listener and threading the rest (asymmetric shutdown, SIGTERM leaves doors open past the stop timeout); forgetting `server_close()` on the bind-failure path (the "nothing is left listening" criterion fails invisibly — the socket lingers). **Guards:** §3.3 is written out verbatim including the close-all-on-failure loop; J1 freezes the all-threads + main-waiter lifecycle and explicitly retires the helper-thread workaround with the reason; §7.4's bind-failure test asserts the good listener is *closed* (connect refused), not just that exit was non-zero; the shutdown integration leg asserts both doors refuse connects after the shutdown path runs.

**Out of scope (do not build, per contract):** fetching/aggregating any peer roster or units; peer state feeding placement/warm/any decision; acting on peers (start/stop/wake/WoL); TLS or caddy config inside Roundhouse; per-peer credentials; ICMP or any second probe protocol; peers as MCP action targets; a config file; queue/priority/probe-history persistence; `do_DELETE`; runtime DNS vetting beyond §6.3 (documented residual).

---

**Relay-worthy findings for the committer:** (1) The section-banner regex in `_section_spans` matches only `[A-E]` — adding `SECTION F` without the one-character regex update silently folds the peer code into Section E's write-verb allowance; the pairing is mandatory and assigned to T1 (§6.2). (2) `roundhouse.py` today contains zero outbound-socket nodes (`import socket` feeds one `gethostname`), so the §6.1 confinement guard starts exact, with no exemption list to maintain. (3) `watcher.lock` is non-reentrant and `take_snapshot` merges under it — every PeerWatch read used there is `_unlocked` by contract; the probe itself never sees any lock (Risk #1). (4) The D2 rule must be host-AND-port: qwen3.6-coding claims :8085 on boltzmann *and* ampere, so a port-only ban would forbid the legitimate `ampere=…:8085` peer (§6.3, tested as LEGAL). (5) Moving `serve_forever` off the main thread retires the MVP2 helper-thread shutdown workaround — the signal handler shrinks to `shutdown_event.set()` + journal-proc terminate, and `shutdown()` becomes an ordinary main-thread call.
