# Roundhouse MVP8 — Build Architecture & Work Breakdown

**File: `mvp1/MVP8-SPEC.md`** (beside `roundhouse.py`; `MVP8.md` at repo root stays the contract — its acceptance checklist is the definition of done, its Out-of-scope list is binding).

Grounded in: `MVP8.md` (contract), E-series (MVP2), F-series (MVP3), G-series (MVP4), H-series (MVP5), I-series (MVP6), J-series (MVP7) — all stand; **this spec amends exactly one I-series decision: the 17-tool catalog becomes 18 (`fleet_roster`), amended here, not by editing `MVP6-SPEC.md`.** Base: `mvp1/roundhouse.py` @ 575e1c8 (7685 lines), `roundhouse_mcp.py` (852 lines), 637 green tests. Recon findings that shaped this spec: **(1)** `take_snapshot` (3458) merges `mode`/`rollout`/`peers` under the non-reentrant `watcher_lock`; every PeerWatch read used there is `_unlocked` by the J5 convention — the federated state joins the same object, the same lock, the same convention. **(2)** `peer_watch_round` (3938) already probes outside every lock and locks only for the dict write — the fetch leg slots into the identical pattern, and the ONE thread that runs it is the peer thread, so a slow fetch can never touch the 3 s tick, any HTTP route, or the operation slot (they live on other threads). **(3)** `_probe_peer` is a bare TCP connect with no TLS (J4) — probing an `https://` peer's host:port succeeds without touching certificates, which is *exactly* the mechanism behind the contract's "up by TCP but federated data stale on an untrusted cert" row; nothing needs designing, only asserting. **(4)** `run_actuate` (4045) already raises `ActuationError(f"{unit} not in selected units")` — hazard layer 3 exists; MVP8 asserts it against peer names, it does not build it. Route handlers key on `watcher.units` at every action site (2274/2381/2481/2678/2760/3173) — layer 1 exists too. The hazard work is proof, not plumbing. **(5)** `do_GET` dispatches `/api/routing-config` by **exact match**, so `/api/routing-config/fleet` collides with nothing; the route-table guard's `get_only` set (test_server.py 974) and `is_get_route` must both gain the three new paths; `FROZEN_POST_ROUTES` stays 9. **(6)** `emit_routing_yaml` (6783) walks **fixed key lists** — a peer entry with a key the local emitter doesn't know would be silently dropped, violating verbatim merging; the fleet emitter needs a generalized entry walker (§5.3), while the `.json` twin is trivially verbatim. **(7)** `snapshot['host']` is `os.uname()[1]` (1663) and `build_routing_entries` namespaces `model_name` with it — **two container instances share one hostname**, so the drill proves the clean merge with disjoint aliases and gets the collision leg for free from one shared alias (a feature, not a blocker). **(8)** `urllib.request` follows redirects by default and `HTTPError` subclasses `URLError` — the no-redirect handler and the exception-classification order in §3.3 are load-bearing. **(9)** `rows_unlocked` is the single producer for both `/api/peers` and the snapshot `peers` key — additive fields (`kind`, `fed`) appear on both surfaces from one edit. **(10)** `TestFrozenCatalog` counts live at test_mcp.py 185/255, the name list at 263, version `'7.0'` at 209 (and 628 in `roundhouse_mcp.py`), `FROZEN_CATALOG` rows end with `peer_status` at 704; docs tests assert the literal heading `17-Tool` (1985–1987).

## 1. GLOBAL DECISIONS (K-series; implementers must not re-open them)

- **K1 — `--fleet-peer NAME=URL`, repeatable only; one PeerWatch, one state table, two declaration columns.** NAME: the J3 regex `^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$`; **the namespace is SHARED with `--peer` and a name may NOT be both** — one hysteresis row, one SSE identity, one UI cell per name; declaring both is redundant (a fleet peer is reachability-watched anyway) and would make `/api/peers` ambiguous. URL: scheme `http`|`https`, host (DNS name, IPv4, or bracketed IPv6), optional port (default 80/443); **path, query, fragment, and credentials rejected** — a fleet peer is an instance root, not an endpoint. Normalized storage: `scheme://host:port` (port always explicit, IPv6 bracketed). **Caps: `FLEET_PEER_MAX = 4` fleet peers, and `PEER_MAX = 8` becomes the COMBINED cap** (tcp + fleet) — arithmetic in K3. All errors batched with the J3 loudness doctrine. Storage: `PeerWatch.declared` keeps its exact J-series shape `{name: (host, port)}` and now contains **every** peer's probe endpoint (a fleet peer's is derived from its URL) — `_probe_peer` is untouched; a new startup-frozen `PeerWatch.fleet = {name: url}` names the subset that may be fetched; state rows gain `kind: 'tcp'|'roundhouse'` (derived: `name in fleet`). Hysteresis, SSE, UI strip: one code path, as required. **The D2 rule applies to fleet peers identically** (probe endpoint through `validate_peers`, host-AND-port): self-federation on the own port is refused because `self_port ∈ MANAGED_PORTS`, while loopback + unmanaged port stays legal — the container drill depends on it, same as MVP7's did.
- **K2 — The federated HTTP client is `urllib.request` + `ssl.create_default_context()`, in ONE function `_fetch_peer`, verifying against the system trust store, following nothing.** `urllib` over raw `http.client` because TLS-with-system-store is the deciding factor and `create_default_context()` is the honest, audited default (hostname checking + CERT_REQUIRED + system CAs, in one documented call). **There is no insecure flag, no context parameter, no env override — asserted absent by AST (§8).** Redirects are NOT followed: a redirect is data (`http: 301`), because silently following one would let a compromised or misconfigured peer point the aggregator at an arbitrary URL. Timeout: `FETCH_TIMEOUT_SEC = 4.0` passed to `opener.open` (bounds connect and each socket read; a byte-dribbling peer could stretch wall time — out of threat model, peers are operator-declared, stated here once). Response cap `FETCH_MAX_BYTES = 4 MiB` — oversized is a fetch failure, not a memory balloon. Exact construction frozen in §3.3.
- **K3 — Fetches ride the existing peer round, sequentially, on the peer thread, OUTSIDE every lock.** In `peer_watch_round`, per peer: probe (outside locks) → apply under lock → **if fleet peer AND post-apply state is `up`**: fetch `/api/units`, then (only if that succeeded) `/api/routing-config.json`, both outside every lock → apply the fetch result under lock. Sequential (J4's rationale carries: concurrency buys seconds and costs a thread pool plus shutdown races). **Budget arithmetic, frozen:** worst round = 8 probes × 2 s + 4 fleet peers × 2 fetches × 4 s = **48 s < 60 s cadence**; typical round ≪ 1 s. The cadence is a floor between rounds (`round; wait(interval)`), not a deadline — a pathological round delays the *next round only*; the tick, the API, and the slot are on other threads and provably unaffected (recon 2; measured leg in §10). Risk #1 is inherited verbatim from MVP7: **no fetch, and no `_fetch_peer` call of any kind, ever happens while any lock is held.**
- **K4 — Federated data per fleet peer is a three-state machine: `never` | `fresh` | `stale`; failure classes are distinguished by frozen reason prefixes; retention is roster-yes, fragment-no.** `never` = no successful fetch yet (failed attempts leave `state='never'` with `reason` set — the operator sees the cert error without Roundhouse pretending it once had data); `fresh` = the LAST attempt succeeded for BOTH documents in the same round; `stale` = had data, and the last attempt failed OR the peer left `up`. Reason prefixes, frozen: `tls:` (any `ssl.SSLError`, incl. verification), `http:` (non-200, incl. unfollowed 3xx), `timeout:`, `connect:` (DNS, refused, unreachable), `body:` (oversized, non-JSON, wrong shape), `down:` (reachability left `up` — written at the up→down transition, so staleness is event-driven, no TTL clock). A TLS-broken peer is therefore `up` (bare-TCP probe, recon 3) with `state='never'|'stale'` and `reason='tls: …'` — the contract's row, mechanically. **Retention:** last-known units persist and serve the roster with `stale: true` + `fetched_at` (age computable by the consumer); the fleet routing fragment includes a peer **only when reachability is `up` AND fed state is `fresh`** — stale entries never reach a router, and an absent host's entries vanish, which is the point. Full transition table in §4.
- **K5 — The merge is verbatim concatenation with first-wins conflict surfacing.** Order: local entries first (from the same `build_routing_entries` the local route uses), then peers sorted by name, each contributor's entries sorted by `model_name` (sorted once at ingestion). **Verbatim means field names and values exactly as the peer authored them** — no re-namespacing, no api_base rewriting, no recomputation of another host's truth; YAML quoting is ours (every string still passes `_yaml_scalar` — peer data is untrusted input to OUR document, §5.3). **`model_name` collision = surfaced conflict: the first occurrence in merge order is kept, every later one is dropped**, and the conflict is recorded in BOTH the YAML header (`# conflict:` line) and a machine-readable `conflicts` list in the `.json` twin. First-wins (local leads the order) because refusing to emit would let one misconfigured peer kill the whole fleet fragment, and emitting duplicates would hand a router an ambiguous table — the conflict is loud, the fragment stays usable. The rule is uniform: it also catches a local-local alias duplicate. **No `# warm-hook:` header on the fleet fragment** — a single hook URL would lie for peer entries; consumers get each host's hook from that host's own `/api/routing-config` (stated in ROUTING.md).
- **K6 — `GET /api/fleet` (unauthenticated read), frozen shape in §6.1; peer unit rows are keep-listed at INGESTION.** Per-unit `source` = the local `snapshot['host']` for local rows, the **declared peer name** for peer rows (operator-chosen, unique by construction — the peer's self-reported host is its own claim and appears only inside its entries). Local units first, never stale (`stale: false`, literally, on every local row). Per-peer block: `name, kind, url, state, mode, fed_state, stale, reason, fetched_at, attempted_at, unit_count, invalid_entries`. **The keep-list is I5's `fleet_status` list verbatim** (`unit, rung, port, alias, enabled, on_demand, retired, strategy_note, badges` + `port_conflict` only when non-null), applied when the fetch is ingested — bounded memory, bounded payload, and a peer cannot inject arbitrary keys into our roster. `/api/fleet` lists **fleet peers only** in `peers` (tcp peers are reachability-only; `/api/peers` remains their surface).
- **K7 — THE HAZARD RULE: peer units are structurally absent from every actuation table, asserted at three layers, and every action answer names its host.** Structure: federated units live ONLY in `PeerWatch.fed` — no code path writes them into `watcher.units`, `engine.units`, or the snapshot `units` list (asserted by the disjointness test, §7). Layer 1 (routes): every action handler already keys on `watcher.units` → peer-only names answer 404. Layer 2 (engine): `start_switch`/preflight lookups miss → refusal. Layer 3: `run_actuate`'s membership check (recon 4). **Both-hosts-share-a-name: the action binds to the LOCAL unit — automatic, since lookup hits `watcher.units` — and the response says so: every 2xx body of the seven action POSTs (`/api/switch`, edit, rollout, enablement, warm, warm/cancel, rollback, dismiss) gains top-level `"host": <snapshot host>`, and `rollout_public_record` gains the same key (injected at serialization from `os.uname()[1]`, so old in-memory records need no migration).** The field is the same identity string `/api/fleet` uses for local `source`, and MVP9 forwarding will key on it. Test matrix in §7.
- **K8 — Surfaces: peer-strip cells grow, a read-only fleet section renders from `/api/fleet`, MCP catalog 17 → 18.** Strip cell for a fleet peer: `name · state · N units` with a trailing ` · stale` marker when fed state ≠ fresh — muted color, **no new red sharers, no interactivity, no 44 px amendment** (the frozen selector list at index.html 751 is untouched). New `#fleet-section` below the local unit lists, visibly separated, heading `fleet (read-only)`, one non-interactive text row per peer unit, hidden when no fleet peers are declared; rendered by `renderFleetSection()` from a `GET /api/fleet` fetch triggered on the SSE `snapshot` event and on each `peer` event (transition-only, so the fetch rate is bounded by transitions). MCP: **`fleet_roster`** — `('GET', '/api/fleet', False, (), False, False, _EMPTY_OBJ)`, passthrough (the route already emits a bounded keep-list; no shaper), inserted after `peer_status` (read tools stay contiguous); counts 17→18 (test_mcp.py 185/255/263), `FROZEN_CATALOG` row after 704, `serverInfo.version` `'7.0'`→`'8.0'` (mcp 628, test 209), docs/MCP.md gains read tool #9 and the `18-Tool` heading (docs tests 1964–1987 updated).
- **K9 — Guard evolution: `FETCH_CALLSITES = {'_fetch_peer'}` confines the federated client the way `PROBE_CALLSITES` confines the probe, and insecure-TLS constructs are asserted ABSENT.** New AST legs (§8): every connection-opening `urllib`/`http.client` symbol confined to `_fetch_peer`; `ssl.create_default_context` appears in exactly one place, inside `_fetch_peer`; zero occurrences anywhere of `_create_unverified_context`, `CERT_NONE`, `SSLContext(`, or any assignment to `.check_hostname` — not "insecure only outside the client", absent, full stop. `_fetch_peer` takes **no URL parameter**: the base comes from the startup-frozen `peer_watch.fleet[name]` table and the path must be a member of `FLEET_PATHS` (asserted) — the same unrepresentability discipline as `_probe_peer`. `PROBE_CALLSITES` and its guard are unchanged. Seeded violations in §8.
- **K10 — Docs: `docs/PEERS.md` gains the federation section; `docs/ROUTING.md` gains the fleet-fragment consumer section.** PEERS.md: declaring `--fleet-peer`, the trust requirement (system store, fleet CA package, no bypass flag — a federation that accepts any certificate is worse than none, contract verbatim), what `never/fresh/stale` mean and the reason prefixes, **why `/api/routing-config` did not change** (hossenfelder is a live consumer; stable contract, stable meaning) **and that the fleet view lives at `/api/routing-config/fleet`**. ROUTING.md: consuming the fleet fragment (same pull-then-merge pattern, one URL now covers the fleet), the conflict semantics, and the warning **that an absent host's entries vanish from the fragment — which is the point: a router must never be handed a backend on an absent host**; per-host warm hooks come from each host's own local route.

## 2. FILE / SECTION LAYOUT

```
mvp1/
  MVP8-SPEC.md                  # this file
  roundhouse.py                 # extended: Section F grows; Part 5 gains merge fns; edits in C and D as listed
  roundhouse_mcp.py             # +1 registry row; version bump
  static/index.html             # strip amendment + fleet section (additive)
  scripts/
    fleet-drill.sh              # NEW: two-instance container drill (§10) + live boltzmann checklist
  tests/
    test_fleet.py               # NEW: pure logic — URL parsing, fed state machine, ingestion, merge (§9 T1)
    test_peers.py               # extended: shared-namespace + cap + D2-for-fleet legs
    test_server.py              # extended: §8 guards, hazard matrix, integration, statics
    test_mcp.py                 # extended: catalog 18
docs/
  PEERS.md                      # extended (K10)
  ROUTING.md                    # extended (K10)
  MCP.md                        # extended (K8)
```

**Placement inside `roundhouse.py`:** new imports `ssl`, `urllib.request`, `urllib.error` join the header block (stdlib guard passes untouched). **Section F** gains: `FLEET_PEER_MAX = 4`; `FETCH_TIMEOUT_SEC = 4.0`; `FETCH_MAX_BYTES = 4 * 1024 * 1024`; `FLEET_PATHS = ('/api/units', '/api/routing-config.json')`; `FLEET_UNIT_KEEP` (the K6 keep-list tuple); `parse_fleet_peer_decls()`; `validate_peer_sets()`; `class _FleetNoRedirect`; `_fetch_peer()`; `_fed_unit_row()`; `validate_peer_entry()`; PeerWatch growth (`fleet`, `fed`, `apply_fetch_unlocked`, `fed_rows_unlocked`, `kind`+`fed` in `rows_unlocked`, down-transition staleness in `apply_result_unlocked`); the fetch leg in `peer_watch_round`. **Section E Part 5** gains the pure merge: `build_fleet_merge()`, `_emit_entry()`, `emit_fleet_yaml()` (beside the existing emitter; both use `_yaml_scalar`). **Section C** gains: `serve_fleet()`, `serve_routing_config_fleet()`, `serve_routing_config_fleet_json()`, three exact-match `do_GET` elifs (before the `/api/rollouts/` prefix branch), `is_get_route` + guard-`get_only` growth, the `"host"` key on the seven action 2xx bodies, `rollout_public_record` host injection. **Section D**: argparse `--fleet-peer` (append); parse + `validate_peer_sets` + D2 over the merged endpoint table, batched exit; `PeerWatch(declared_all, watcher_lock, event_bus, fleet=fleet_urls)`. **Section banner text (F) is edited in place** to add `; _fetch_peer is the ONE federated-HTTP site` — the `[A-F]` regex already matches (no `_section_spans` change this time).

**Frozen-set edits permitted to existing tests (exhaustive):** `/api/peers` + snapshot peer-row key assertions gain `kind` (+`fed` on fleet rows); action-route body assertions gain `host`; `rollout_public_record` shape tests gain `host`; test_mcp counts/version/rows per K8. Nothing else in the E–J era test files may change.

## 3. FLEET-PEER + FETCH SPEC

### 3.1 `parse_fleet_peer_decls(values: Optional[List[str]]) -> tuple[dict[str, tuple[str, int, str]], List[str]]`

Per value: split on first `=` → name, url (missing `=` → `malformed --fleet-peer '<v>': expected NAME=URL`). Name: J3 regex, duplicates within the flag → error. URL via `urllib.parse.urlsplit`: scheme ∉ {`http`,`https`} → `URL scheme must be http or https`; empty hostname → error; `parsed.path not in ('', '/')` or query or fragment or username/password present → `URL must not carry a path, query, fragment, or credentials — declare the instance root (scheme://host[:port])`; invalid port (urlsplit raises `ValueError` on `.port`) → error. Port default: 80/443 by scheme. Returns `{name: (host, port, normalized_url)}` where `normalized_url = f'{scheme}://{host_disp}:{port}'` (`host_disp` brackets IPv6). Count > `FLEET_PEER_MAX` → `too many fleet peers (N > 4): two fetches per peer per round must finish well inside the 60 s cadence`.

### 3.2 `validate_peer_sets(tcp: dict, fleet: dict) -> List[str]` (pure)

Cross-flag duplicate name → `'<name>' is declared as both --peer and --fleet-peer — one watch row per name; a fleet peer is reachability-watched already`. Combined `len(tcp) + len(fleet) > PEER_MAX` → the J3 cap message with the combined count. Called in `cmd_serve` alongside `validate_peers` over the **merged** `{name: (host, port)}` endpoint table (D2, K1); all errors batched into the single loud exit.

### 3.3 `_fetch_peer` (frozen; the ONE federated-HTTP site in the codebase)

```python
class _FleetNoRedirect(urllib.request.HTTPRedirectHandler):
    """K2: a redirect is data, never followed — returning None makes 3xx raise HTTPError."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def _fetch_peer(peer_watch, name, path, opener=None):
    """THE one federated-HTTP call site (AST-guarded, §8). Base URL comes only from
    the startup-frozen fleet table; path only from FLEET_PATHS — unrepresentable
    otherwise (K9). Returns (ok, payload_dict_or_None, error_str_or_None); error
    strings carry a frozen class prefix: tls:/http:/timeout:/connect:/body:."""
    assert path in FLEET_PATHS
    url = peer_watch.fleet[name] + path          # KeyError = programming error, loudly
    if opener is None:                            # test seam, same shape as _probe_peer's
        ctx = ssl.create_default_context()        # system trust store; NEVER weakened (K2/§8)
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx), _FleetNoRedirect())
    try:
        with opener.open(url, timeout=FETCH_TIMEOUT_SEC) as resp:
            body = resp.read(FETCH_MAX_BYTES + 1)
    except urllib.error.HTTPError as e:           # before URLError — HTTPError subclasses it (recon 8)
        return (False, None, f'http: {e.code}')
    except urllib.error.URLError as e:
        r = getattr(e, 'reason', e)
        if isinstance(r, ssl.SSLError):
            return (False, None, f'tls: {type(r).__name__}: {r}'[:200])
        if isinstance(r, (TimeoutError, socket.timeout)):
            return (False, None, f'timeout: {r}'[:200])
        return (False, None, f'connect: {type(r).__name__}: {r}'[:200])
    except ssl.SSLError as e:                     # a bare SSLError can escape mid-read
        return (False, None, f'tls: {type(e).__name__}: {e}'[:200])
    except (TimeoutError, socket.timeout) as e:
        return (False, None, f'timeout: {e}'[:200])
    except Exception as e:
        return (False, None, f'connect: {type(e).__name__}: {e}'[:200])
    if len(body) > FETCH_MAX_BYTES:
        return (False, None, f'body: oversized (> {FETCH_MAX_BYTES} bytes)')
    try:
        payload = json.loads(body.decode('utf-8'))
    except Exception as e:
        return (False, None, f'body: invalid JSON: {type(e).__name__}'[:200])
    if not isinstance(payload, dict):
        return (False, None, 'body: not a JSON object')
    return (True, payload, None)
```

### 3.4 The round (the only orchestration; K3)

`peer_watch_round` grows, per peer, after the existing apply-under-lock block (which also reads `state_now = peer_watch.peers[name]['state']` before releasing):

```python
        if name in peer_watch.fleet and state_now == 'up':
            ok_u, units_doc, err = _fetch_peer(peer_watch, name, '/api/units')          # NO lock held
            ok_r, routing_doc, err_r = (_fetch_peer(peer_watch, name, '/api/routing-config.json')
                                        if ok_u else (False, None, err))               # skip 2nd on 1st failure
            with peer_watch.lock:
                fed_payload = peer_watch.apply_fetch_unlocked(
                    name, ok_u and ok_r, units_doc, routing_doc, err if not ok_u else err_r,
                    peer_watch.now())
                if fed_payload:
                    peer_watch.event_bus.publish('peer', fed_payload)
```

Nothing else is fetched — no detail routes, no operations, no MCP-to-MCP (contract). Failure never raises: `_fetch_peer` returns, `apply_fetch_unlocked` records.

## 4. STALENESS SPEC

### 4.1 The fed record (per fleet peer, created at PeerWatch init, mutated only under the lock)

```python
self.fed = {name: {'state': 'never',        # never | fresh | stale (K4)
                   'mode': None,            # peer's own snapshot['mode'] from last good fetch
                   'units': [],             # keep-listed rows (K6), sorted as fetched
                   'entries': [],           # validated verbatim model_list entries, sorted by model_name
                   'invalid_entries': 0,    # entries dropped at ingestion (§5.1)
                   'fetched_at': None,      # last SUCCESSFUL fetch (both docs)
                   'attempted_at': None,    # last attempt, success or not
                   'reason': None}          # frozen-prefix failure string; None when fresh
            for name in fleet}
```

### 4.2 `apply_fetch_unlocked(name, ok, units_doc, routing_doc, error, ts) -> Optional[dict]` — the whole table; no other transitions exist

On success: validate shapes — `units_doc['units']` must be a list and `units_doc['mode']` a string, `routing_doc['model_list']` a list, else the round is a `body:` failure. Then units are keep-listed via `_fed_unit_row` (K6 list; `port_conflict` kept only when non-null), entries validated per §5.1 (invalid dropped + counted), entries sorted by `model_name`, `mode`/`units`/`entries`/`invalid_entries`/`fetched_at` replaced **atomically in one lock hold** (both documents from the same round or neither — no torn view), `reason = None`.

| fed state | event | new state | reason | SSE `peer` event? |
|---|---|---|---|---|
| never | fetch success | **fresh** | None | YES |
| never | fetch failure, reason was None | never | set | YES (the operator must see the first cert error) |
| never | fetch failure, reason already set | never | updated | no (no chatter on a flat-lining failure) |
| fresh | fetch success | fresh | None | no |
| fresh | fetch failure | **stale** | set | YES |
| fresh | reachability up→down (from `apply_result_unlocked`) | **stale** | `down: peer unreachable (tcp probe failed)` | rides the existing reachability event |
| stale | fetch success | **fresh** | None | YES |
| stale | fetch failure | stale | updated | no |
| stale/never | reachability up→down | unchanged/`stale` | `down: …` | rides the reachability event |

`attempted_at = ts` on every fetch outcome. The SSE payload is the peer's full row (§6.3) plus `prev_state` (reachability, unchanged here — equals `state`) plus `fed_prev` (the fed state before). Reachability-transition events on fleet peers also carry `fed_prev` (unchanged value) so the payload shape is uniform per kind. **Surfaced boolean everywhere: `stale = (fed state != 'fresh')`** — a `never` peer is stale-with-no-data, honestly.

### 4.3 Interaction with reachability (the contract's sharp edge, stated)

Reachability and freshness are independent axes: `up` + `fresh` (normal), `up` + `never/stale` with `tls:`/`http:`/`body:` reason (reachable but not trustworthy or not a Roundhouse — **the untrusted-cert acceptance row lives here**), `down` + `stale` (absent host, last-known retained for the roster). A fetch outcome NEVER changes reachability state (contract: failure is data), and the down-transition is the only reachability event that touches fed state. Local units are not in this machine at all — they are never stale by construction, not by flag.

## 5. MERGE SPEC

### 5.1 Ingestion validation — `validate_peer_entry(entry) -> bool` (pure; T1)

An entry is valid iff: it is a dict; `entry.get('model_name')` is a non-empty `str`; every top-level value is a scalar (`str|int|float|bool`) or a dict whose values are all scalars (depth ≤ 2 — the H2 emitter invariant, now enforced at the trust boundary instead of asserted); no value is a list/None/deeper dict. Invalid entries are **dropped at ingestion** with `invalid_entries` incremented — a malformed peer degrades itself, never the document. (Peers run this same codebase, so in practice the count stays 0; the rule exists for version skew and for hostile input.)

### 5.2 `build_fleet_merge(local_entries, local_host, fed_rows) -> Dict` (pure; T1)

Inputs: the local `build_routing_entries` output (already model_name-sorted), `snapshot['host']`, and `fed_rows_unlocked()` output. Walk order: local, then fleet peers by name. A peer contributes iff `state == 'up' and fed_state == 'fresh'` (K4); every other fleet peer lands in `excluded` with `{name, state, fed_state, reason}`. Conflict scan over the walk: a `model_name` already emitted → entry dropped, `conflicts` gains `{'model_name': mn, 'kept_source': <host-or-name>, 'dropped_source': <name>}` (sources: `local_host` for local rows, peer name otherwise; the rule is uniform, so a local-local alias duplicate is caught too — first wins there as well). Returns `{'model_list': [...], 'contributors': [{'name': local_host, 'source': 'local'}, {'name': peer, 'fetched_at': ts}, ...], 'excluded': [...], 'conflicts': [...]}` — `model_list` entries are the ingested dicts **verbatim** (same objects deep-copied; no key added, none removed, none reordered beyond the per-contributor model_name sort).

### 5.3 `emit_fleet_yaml(meta, merge) -> str` — the generalized emitter (T1)

Header lines, frozen order and spelling:

```yaml
# generated-by: roundhouse@boltzmann (fleet)
# generated-at: 2026-08-14T12:00:00Z
# contributor: boltzmann (local)
# contributor: ampere (fetched 2026-08-14T11:59:30Z)
# excluded: cern (down, stale: connect)
# conflict: model_name 'boltzmann-qwen3.6-coding' also advertised by ampere — ampere's entry dropped, boltzmann's kept
model_list:
  - model_name: ...
```

`excluded` lines render `(<reachability>, <fed_state>[: <reason class before the first colon>])`; reason detail stays out of comments (server-controlled words only — the H2 rule that no untrusted data reaches a comment line HOLDS: names are argv-validated by the J3 regex, reason classes are our own literals, timestamps are ours). Entries: `_emit_entry(entry) -> List[str]` walks `model_name` first, then `litellm_params`, then `model_info`, then any remaining top-level keys sorted (version-skew tolerance, recon 6); sub-dicts walk in insertion order; **every key rendered bare only if it matches `[A-Za-z0-9_]+` else the entry was invalid at ingestion (assert); every value through `_yaml_scalar`** — the MVP5 quoting rule is the injection boundary and peer strings never bypass it. Empty merged list → `model_list: []` after the header, like the local emitter. The `.json` twin body: `{**meta_json, 'contributors': ..., 'excluded': ..., 'conflicts': ..., 'model_list': ...}` where `meta_json = {'generated_by': 'roundhouse@<host> (fleet)', 'generated_at': <ISO Z>}` — no warm_hook key (K5).

## 6. SURFACES SPEC

### 6.1 `GET /api/fleet` (frozen shape; unauthenticated; POST → 405)

```json
{"host": "boltzmann", "mode": "actuate", "generated_at": 1755160000.0,
 "fetch": {"timeout_seconds": 4.0, "max_bytes": 4194304, "cadence_seconds": 60},
 "units": [
   {"unit": "qwen3.6-coding.service", "rung": "READY", "port": 8085, "alias": "qwen3.6-coding",
    "enabled": true, "on_demand": false, "retired": false, "strategy_note": null,
    "badges": [], "source": "boltzmann", "stale": false},
   {"unit": "qwen3.6-coding.service", "rung": "READY", "port": 8085, "alias": "qwen3.6-coding",
    "enabled": true, "on_demand": false, "retired": false, "strategy_note": null,
    "badges": [], "source": "ampere", "stale": false}
 ],
 "peers": [
   {"name": "ampere", "kind": "roundhouse", "url": "https://ampere.fritz.box:8099",
    "state": "up", "mode": "read-only", "fed_state": "fresh", "stale": false, "reason": null,
    "fetched_at": 1755159990.0, "attempted_at": 1755159990.0,
    "unit_count": 1, "invalid_entries": 0}
 ]}
```

Local rows first (keep-list applied at serve time from `take_snapshot`), then each fleet peer's retained rows in peer-name order — **retained rows serve even when stale** (K4 retention; their `stale` flips true, `fetched_at` on the peer block dates them). Handler: `snap = self.server.take_snapshot()`, then `with watcher_lock: fed = pw.fed_rows_unlocked()` (two short holds; a between-holds update yields two internally-consistent views — harmless, stated), serialize outside. `unit_count = len(units)` per peer. No fleet peers declared → `units` = local only, `peers: []`, route always exists.

### 6.2 Route table additions (frozen)

| route | method | success | notes |
|---|---|---|---|
| `/api/fleet` | GET | 200 §6.1 | POST → 405; unauthenticated |
| `/api/routing-config/fleet` | GET | 200 `text/yaml; charset=utf-8` = `emit_fleet_yaml` | POST → 405 |
| `/api/routing-config/fleet.json` | GET | 200 `application/json; charset=utf-8` = §5.3 twin | POST → 405 |

`/api/routing-config` and `.json`: **byte-identical to MVP7 behavior, pinned by a test** (§10) — the handlers are not touched; the pin test runs the route with fed data populated and diffs against the unfederated body. `is_get_route` and the guard's `get_only` gain the three paths; `FROZEN_POST_ROUTES` stays 9 (recon 5).

### 6.3 Snapshot + `/api/peers` + SSE

`rows_unlocked` rows gain `kind`; fleet rows additionally gain `fed: {state, stale, reason, fetched_at, unit_count}` (the summary — never the unit rows; the snapshot stays bounded). One producer, both surfaces (recon 9). SSE `peer` fires on reachability transitions (unchanged table) AND fed transitions (§4.2 table); flat-lined peers of either kind stay silent. Mid-life clients need no replay: the `snapshot` event carries current rows (J7 logic carries).

### 6.4 UI (additive; textContent only)

`renderPeers` cell for `kind === 'roundhouse'`: `name + ' · ' + state + ' · ' + fed.unit_count + ' units'` plus `' · stale'` when `fed.stale` — muted class, no new colors. `#fleet-section` after the unit lists: heading `fleet (read-only)`, per peer a subheading `name (state, stale?)` and per unit one row `unit · rung` — no buttons, no handlers, no localStorage, no innerHTML; `renderFleetSection()` re-fetches `/api/fleet` on `snapshot` and `peer` events only. Existing static assertions re-run over the grown file.

### 6.5 MCP + docs

Per K8: registry row after `peer_status` — description: `Fleet roster across this host and its declared fleet peers: every unit tagged with its source host, plus per-peer mode, fetch time, and staleness. Reads only — peer units cannot be actuated from here.` `FROZEN_CATALOG` row `'fleet_roster': ('GET', '/api/fleet', False, (), False, False, _EMPTY_OBJ)`. Literals: counts 17→18, versions `'7.0'`→`'8.0'`, docs heading `17-Tool`→`18-Tool`, MCP.md read tool #9 with one example pair, boundary sentence: *the catalog is frozen at 18 as of MVP8; `fleet_roster` is a read — cross-host actuation does not exist (Milestone 9 will forward, each host still actuating only itself).* PEERS.md + ROUTING.md per K10.

## 7. THE HAZARD RULE — assertions + test matrix

Structural invariant first: **fed data is write-only into `PeerWatch.fed` and read-only out of `fed_rows_unlocked`** — the disjointness test populates fed with (a) a unit name that exists locally and (b) a novel name, then asserts `take_snapshot()['units']` names are unchanged and `watcher.units`/`engine.units` contain neither addition. Then the matrix (fixture: local units `local-a.service`, `both.service`; fed peer `bee` advertising `bee-only.service`, `both.service`):

| probe | peer-only name (`bee-only.service`) | shared name (`both.service`) | local-only (control) |
|---|---|---|---|
| GET `/api/units/<u>` | 404 | 200, the LOCAL unit's detail | 200 |
| POST `/edit`, `/rollout`, `/enablement` | 404 | acts on local; 2xx body `host` = local host | normal |
| POST `/api/switch` target | 404 | local target; 202 body `host` | normal |
| POST `/api/switch` stops member | 404 (per-stop check) | local stop actuated only | normal |
| POST `/api/warm` `unit=` | 404 `unknown_unit` | local unit resolved; response `host` | normal |
| POST `/api/warm` `logical=bee-<alias>` | 404 `unknown_alias` (only the LOCAL host prefix strips — a peer's namespaced model_name never resolves locally) | n/a | n/a |
| `engine.start_switch` direct | `ActuationError` | binds local | normal |
| `run_actuate` direct | `ActuationError` `not in selected units` | local unit only | normal |

Route legs run with fed data POPULATED (the adversarial condition — the 404 must hold *while the roster displays the name*). The drill re-proves the shared-name row end-to-end: acting on instance A's `both` leaves instance B's `both` untouched (B's own API asserts its rung unchanged). `rollout_public_record['host']` asserted on switch and rollout records, old-record fallback (dict without the key at serialization → key still injected).

## 8. GUARD SPEC (K9 — security-critical, exact)

### 8.1 NEW `test_fetch_confined` (in `TestWriteGuards`)

Over `ast.parse(roundhouse.py)` with the existing `_parents`/`_enclosing_func` machinery: **(a)** every `ast.Attribute` node with `attr in {'urlopen', 'build_opener', 'OpenerDirector', 'HTTPSHandler', 'HTTPHandler', 'HTTPConnection', 'HTTPSConnection'}` must have `_enclosing_func(node) in FETCH_CALLSITES = {'_fetch_peer'}` — with the single exemption that `HTTPRedirectHandler` (not in the set — it opens nothing) may appear in `_FleetNoRedirect`'s bases. **(b)** every Attribute `create_default_context` sits inside `_fetch_peer`, and exactly one such node exists. **(c)** ZERO occurrences anywhere in the file of Attributes `_create_unverified_context`, `CERT_NONE`, `SSLContext`, `set_ciphers`, and zero `ast.Assign`/`ast.AnnAssign` whose target is an Attribute named `check_hostname` or `verify_mode` — insecure TLS is unrepresentable, not discouraged. **(d)** within `_fetch_peer`'s subtree, no Attribute in the probe guard's send/recv set beyond `read` (fetching reads; it never writes application data past the opener). **(e)** the existing `test_outbound_connect_confined` (`PROBE_CALLSITES`) runs unmodified and stays green — urllib's internals are not in this AST, so the probe guard is untouched by construction.

### 8.2 Housekeeping + seeds

`get_only` gains the three §6.2 paths; `from_frozen` and `FROZEN_POST_ROUTES` unchanged (asserted by staying green). Write-verb, subprocess, file-write, snapshot-lock, arming, section-span guards: zero edits, green over the grown source (Section F's growth contains no write verbs, no subprocess, no file writes). **Seeded-violation acceptance** (seed → red → unseed at integration; red names in the commit message): **(s1)** `urllib.request.urlopen(peer_url)` added inside `serve_fleet` → 8.1(a) red; **(s2)** `ctx = ssl._create_unverified_context()` inside `_fetch_peer` → 8.1(c) red; **(s3)** a line in `apply_fetch_unlocked` writing a fed unit into `self.peers`... into the watcher: `watcher.units[row['unit']] = ...` cannot be seeded there (no watcher ref) — instead seed `serve_fleet` inserting a fed name into `snap['units']` → the §7 disjointness test red (proving the hazard test bites the merge-for-convenience shortcut, the likeliest real failure).

## 9. WORK BREAKDOWN — 2 tasks, sequential T1 → T2

Why 2: the seam is MVP7's — *mechanism* (parsing, client, state machine, merge, guards, all provable with injected seams) vs *surfaces* (routes, UI, MCP, docs, drill) — and both touch `roundhouse.py`/`test_server.py`, so sequential.

**T1 — Fleet-peer parsing + fetch + staleness + merge core + guards**
- **Writes:** imports; Section F growth complete (§2 list, §3, §4); Part 5 merge functions (§5); Section D (`--fleet-peer`, `validate_peer_sets`, merged D2 call, PeerWatch ctor `fleet=` kwarg); `tests/test_fleet.py` complete — URL-parse table (schemes, default ports, path/query/credential rejection, IPv6, dup names, caps, cross-flag dup), fed state machine row-for-row (§4.2 with injected clock + injected opener), classification table (each exception class → its prefix, HTTPError-before-URLError order, redirect → `http: 301`, oversize, non-JSON, non-dict), ingestion validation, keep-list, merge + conflict + excluded (§5.2), `emit_fleet_yaml` golden (2 contributors + 1 excluded + 1 conflict + a hostile peer string proving `_yaml_scalar` quotes it); `test_peers.py` legs (shared namespace, D2-for-fleet incl. self-port refusal + loopback-unmanaged allowed); `test_server.py`: §8 guards, fetch-outside-lock discipline (injected opener asserting `lock.acquire(blocking=False)` succeeds mid-fetch), §7 engine/run_actuate matrix rows + disjointness, hang-cost integration (accept-only listener as fleet peer; `/api/units` latency measured during the round, MVP7 style).
- **Must not touch:** `do_GET`/`do_POST`/handlers, `take_snapshot`, `index.html`, `roundhouse_mcp.py`, `test_mcp.py`, docs.
- **Frozen interfaces handed to T2:** `parse_fleet_peer_decls`/`validate_peer_sets` signatures; PeerWatch `fleet`, `fed`, `apply_fetch_unlocked`, `fed_rows_unlocked` + row shapes (§4.1, §6.3); `_fetch_peer` signature + error prefixes; `build_fleet_merge`/`emit_fleet_yaml` shapes (§5); the §6.1 JSON.
- **Self-test:** full discover green (pre-existing tests unmodified except the §2 permitted list); scratch-run two instances by hand and eyeball `fed_state` reaching `fresh`.

**T2 — Surfaces + UI + MCP + docs + drill**
- **Writes:** Section C (three handlers, route lines, `is_get_route`, action-body `host` key, `rollout_public_record` host); `index.html` §6.4; `roundhouse_mcp.py` §6.5 row + version; `test_mcp.py` (row, counts, version, docs assertions, a `tools/call` of `fleet_roster` against a stub); `test_server.py`: §6.1 frozen-shape route tests, the §7 route-level matrix, the `/api/routing-config` byte-identical pin (fed populated vs not → identical body), SSE fed-transition leg (exactly one event on never→fresh, zero on repeat success), statics; `docs/PEERS.md` + `ROUTING.md` + `MCP.md` per K10/K8; `scripts/fleet-drill.sh` per §10. Runs the seeded-violation acceptance (§8.2) once at integration.
- **May touch T1 code:** nothing but bug fixes with a failing test first.
- **Self-test:** full discover green; drill container leg green end-to-end.

**Shared-file ownership:** `roundhouse.py` — T1 owns F/Part 5/D, T2 owns C; `test_server.py` — T1 owns guards + lock/hang/engine-layer legs, T2 owns routes/SSE/pin/statics; nobody edits the other's classes or any E–J-era test beyond §2's permitted list.

## 10. TEST PLAN — mapped 1:1 to MVP8.md's acceptance checklist

`scripts/fleet-drill.sh`, container leg: instance A (:PA) and B (:PB), separate scratch unit dirs, fixture units with **disjoint aliases plus exactly one shared alias and one shared unit name** (recon 7 — same hostname is fine: disjoint aliases prove the clean merge, the shared alias produces the conflict leg, the shared unit name drives the hazard leg); A runs `--fleet-peer bee=http://127.0.0.1:PB --peer-interval 5`; fixtures are on-demand-marked so OFF units still route (H-series inclusion). Legs: roster aggregation; fragment merge vs B's own `/api/routing-config.json` (verbatim compare); conflict surfaced both places; local-route pin; hazard (404 peer-only, shared-name acts locally + `host` field, B untouched); kill B → entries leave the fragment + roster goes stale `connect:`/`down:` within 2 rounds; restart → fresh within 1; TLS leg (gated on `command -v openssl`): self-signed HTTPS listener declared as fleet peer → `up` + `never` + `tls:` reason, fragment excludes it, and `--help` output greps clean of `insecure|no-verify`; hang leg (accept-only listener) with the latency measurement; MCP `fleet_roster` via the real stdio subprocess. Live leg (operator-run, may remain open): boltzmann declares ampere; away → fleet view == local view; returned → models appear.

| criterion | proven by |
|---|---|
| `--fleet-peer` parses (path rejected), coexists, same watch, capped + batched | `test_fleet` parse table; `test_peers` shared-namespace/cap; drill startup |
| https verifies system store; untrusted cert → `up` + stale + TLS reason; **no bypass flag anywhere** | §4.2 table tests (injected opener raising `SSLCertVerificationError`); §8.1(c) guard; drill TLS leg + `--help` grep |
| `/api/fleet`: source, fetched_at, stale, per-peer mode; local never stale | §6.1 frozen-shape test; drill roster leg |
| `/api/routing-config` byte-identical, pinned | T2 pin test (fed populated vs not); drill diff leg |
| `/api/routing-config/fleet` merges verbatim, excludes non-up, namespaced, contributors named, model_name conflict surfaced | §5 merge/golden tests; drill verbatim-compare + conflict + kill legs |
| Peer units unactuatable at every layer; shared name acts locally and says so | §7 matrix (T1 engine/gateway rows, T2 route rows) + disjointness + seed s3 + drill hazard leg |
| Hanging/absent peer costs only its timeout; tick/API/slot unaffected, measured | K3 arithmetic + T1 hang-cost integration + drill hang leg |
| Fetches outside every lock; AST guard admits one named function reaching only declared URLs | lock-discipline test (injected opener); §8.1(a)(b); `_fetch_peer` signature (§3.3); seeds s1/s2 |
| Container drill: two instances, aggregate/merge/refuse/kill legs | `fleet-drill.sh` container leg (5 s cadence, exact-round assertions) |
| Live boltzmann + ampere (may remain open) | drill live leg, operator-run |
| Stdlib only, no build step, no German, no throughput figures | stdlib guard (`ssl`/`urllib` pass); review grep |

## 11. RISKS — top 3 mechanical-coder failure modes and the guards placed

1. **A fetch (or fed serialization) creeps under `watcher_lock` — Risk #1, now with an 8 s-per-peer payload.** The instinctive shapes: fetching inside the apply block "since we're already locked"; `serve_fleet` holding the lock across `json.dumps`; `fed_rows_unlocked` re-acquiring the non-reentrant lock (instant deadlock of every route). **Guards:** §3.4 writes the round out with the lock scoped to `apply_fetch_unlocked` only; the `_unlocked` naming convention makes misuse visible on sight; the injected-opener lock-discipline test proves the lock is free mid-fetch; the hang-cost integration turns a locked fetch into a red latency number.
2. **The insecure-TLS "fix".** The coder hits the drill's self-signed cert, watches the fetch fail, and reaches for `_create_unverified_context`, `check_hostname = False`, or a helpful `--fleet-insecure` flag — inverting the contract's sharpest sentence. **Guards:** the drill's TLS leg asserts the FAILURE (up + `tls:` + excluded) as the green outcome, so the "fix" breaks the drill; §8.1(c) makes the constructs unrepresentable; the `--help` grep catches the flag; K2's normative text states the doctrine (a federation that silently accepts any certificate is worse than none).
3. **Re-derivation or convenience-merging dressed as helpfulness.** Three sub-shapes: "normalizing" peer entries (rewriting api_base with the local advertise host, re-namespacing model_name — recomputing another host's truth, exactly what the contract forbids); merging fed units into `watcher.units` so `/api/fleet` and the UI come for free (the hazard rule dies silently — both hosts run `qwen3.6-coding.service`); widening the local `/api/routing-config` to the fleet "while in there" (changes a live consumer, hossenfelder). **Guards:** the drill's verbatim-compare diffs merged entries against the peer's own document byte-for-byte in field terms; the §7 disjointness test + seed s3 bite the merge shortcut; the byte-identical pin test bites the widening; §5.2 states verbatim as an invariant with the one permitted transformation (our YAML quoting) named.

**Out of scope (do not build, per contract):** cross-host actuation or forwarding (Milestone 9); placement/scheduling across hosts; a distributed operation slot; authenticated peer reads; writing anything to a peer; WoL; aggregating measurement databases; peer discovery; retry/backoff schedules beyond the round cadence; caching headers/ETags; fetching any route beyond `FLEET_PATHS`; a fleet UI beyond the read-only section; `do_DELETE`.

---

**Relay-worthy findings for the committer:** (1) The hazard layers already exist — `run_actuate`'s membership check and every handler's `watcher.units` lookup predate MVP8; the milestone's work there is adversarial proof (matrix + disjointness + seed s3), not construction, and the one genuinely new mutation is the `host` key on action responses. (2) The bare-TCP probe (J4) is what makes "up but stale on a bad cert" fall out mechanically — probe succeeds without TLS, fetch fails with `tls:`; nothing special-cased. (3) Two container instances share `os.uname()[1]`, so the drill proves clean merging with disjoint aliases and gets the conflict leg from one shared alias — do not "fix" the drill by faking hostnames. (4) `urllib` follows redirects by default and `HTTPError` subclasses `URLError`; `_FleetNoRedirect` and the §3.3 catch order are load-bearing, not style. (5) The local routing route is pinned byte-identical by test because hossenfelder pulls it live — the fleet view is a NEW route, and the pin is what keeps a helpful coder from widening the old one.
