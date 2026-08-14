# MVP8 — federation: the fleet roster and the merged routing config (reads only)

**One turntable per host, one view across all of them.** Roundhouse learns to ask
its peers what they are running and to merge the answers into a fleet roster and a
fleet-wide routing fragment. **Every action stays local.** No host actuates another
in this milestone; forwarding is Milestone 9, and it will still be each host
actuating only itself.

## Why this shape

The pieces were already leaning here. MVP5 namespaces routing entries
`<host>-<alias>` precisely because `qwen3.6-coding` exists on boltzmann *and*
ampere. MVP7's peer watch already knows who is present. What is missing is the
one honest step between them: ask a peer that is up what it has.

**Fragments are merged, never re-derived.** A peer authors its own routing entries
with its own advertise host, its own measured memory, its own liveness. The
aggregator concatenates; it does not recompute another host's truth.

## Fleet peers vs reachability peers

- `--peer NAME=HOST:PORT` (MVP7) is unchanged: a bare TCP connect, zero bytes,
  for hosts that are not Roundhouse instances (dirac's sshd, a switch, anything).
  **The watcher still does not knock on inference ports.**
- `--fleet-peer NAME=URL` is new: the peer IS a Roundhouse instance and may be
  asked, over HTTP(S), for its roster and its routing fragment. A URL, not a
  host:port, because a peer may sit behind TLS (boltzmann already does).
  - `https://` verifies against the **system trust store** (the fleet CA package
    puts it there). There is **no flag to disable verification** — a federation
    that silently accepts any certificate is worse than no federation.
  - A fleet peer is also reachability-watched, so `up`/`down` hysteresis and the
    transition-only SSE rule apply unchanged.

## What is fetched, and when

1. On each peer round (the existing 60 s cadence, `--peer-interval`), for each
   fleet peer that is `up`: `GET <url>/api/units` and `GET <url>/api/routing-config.json`,
   short timeout, **outside every lock**, sequential, never blocking the local
   3 s tick. A peer that hangs must cost only its own timeout.
2. Nothing else is fetched. No detail routes, no operations, no MCP-to-MCP.
3. Failure is data: a fetch that errors marks that peer's federated data `stale`
   with the reason; it does not change the peer's reachability state, and it never
   raises into the local loop.

## Surfaces

- **`GET /api/fleet`** (new, unauthenticated read): the aggregate roster — this
  host plus every fleet peer, each unit tagged with its `source` host, plus
  `fetched_at`, `stale`, and the peer's own `mode`. Local units are always first
  and never stale.
- **`GET /api/routing-config`** — **unchanged, local-only.** hossenfelder already
  pulls this; silently widening it to the fleet would change a live consumer's
  behaviour without asking. Stable contract, stable meaning.
- **`GET /api/routing-config/fleet`** (new, YAML) and **`.json`** twin: the merged
  fragment — local entries plus each `up` fleet peer's entries verbatim. A peer
  that is not `up` contributes **nothing**: a router must never be handed a
  backend on an absent host. The header comments name every contributing host and
  the moment each was fetched.
- **UI**: the peer strip gains a unit count per fleet peer and a `stale` marker;
  a fleet section lists peers' units read-only, visibly separated from local ones.
- **MCP**: one new read tool, `fleet_roster` (catalog 17 → 18).

## The hazard this milestone introduces, and the rule for it

The merged roster contains unit names that are **not local** — and both hosts run
`qwen3.6-coding.service`. Every action path must therefore key strictly on local
units, so that an action naming a peer's unit answers 404 rather than acting on a
local unit that happens to share the name. This is the milestone's sharpest test,
not a footnote: it is asserted at the route, in the engine, and in `run_actuate`.

## Acceptance criteria

- [ ] `--fleet-peer NAME=URL` parses (http/https, host, optional port, path
      rejected), coexists with `--peer`, appears in the same reachability watch,
      and is capped and batched-error-checked like `--peer`.
- [ ] `https://` peers verify against the system trust store; a peer with an
      untrusted certificate is `up` (TCP) but its federated data is `stale` with
      the TLS error surfaced — and there is no flag anywhere to skip verification.
- [ ] `/api/fleet` aggregates local + peers with `source`, `fetched_at`, `stale`,
      and per-peer `mode`; local units are never stale.
- [ ] `/api/routing-config` is byte-identical to MVP7 behaviour (local only) —
      pinned by a test, because a live consumer depends on it.
- [ ] `/api/routing-config/fleet` merges verbatim peer fragments, excludes peers
      that are not `up`, namespaces by host, and names contributors in the header.
      Two hosts advertising the same `model_name` is a surfaced conflict, not a
      silent overwrite.
- [ ] **Peer units are unactuatable at every layer**: `POST` to any action route
      naming a peer-only unit → 404; a name that exists on BOTH hosts acts on the
      local one and says so; `run_actuate` still refuses anything not in the local
      selected set.
- [ ] A hanging or absent fleet peer costs only its own timeout: the 3 s tick, the
      API and the operation slot are unaffected (measured, as MVP7 measured it).
- [ ] Fetches happen outside every lock; the AST guard extends to admit the
      federated HTTP client in exactly one named function reaching only declared
      fleet-peer URLs.
- [ ] Container drill: two Roundhouse instances, one federating the other —
      roster aggregates, fleet fragment merges, peer's units refuse actuation,
      peer killed → its entries leave the fleet fragment and go stale in the roster.
- [ ] Live boltzmann (operator, may remain open): declare ampere as a fleet peer;
      with ampere away the fleet view equals the local view; when it returns its
      models appear.
- [ ] Stdlib only, no build step, no German, no throughput figures.

## Out of scope (MVP8)

Cross-host actuation of any kind (Milestone 9: forwarding to the peer's own gated
API, still each host actuating itself); placement or scheduling decisions across
hosts; a distributed operation slot; authenticated peer reads; writing anything to
a peer; waking a peer (WoL); aggregating peers' measurement databases; peer-to-peer
discovery (peers are declared, as ever, in Roundhouse's own unit file).
