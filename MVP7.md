# MVP7 — a configurable listen list, and the peer watch

**Two sensing-layer changes, no new power.** Roundhouse learns *where to listen*
(a list of addresses instead of a hardcoded wildcard) and *who else is out there*
(a once-a-minute reachability watch over declared peer hosts). Neither feature
actuates anything, on this host or any other.

## Part 1 — the listen list

1. `--bind ADDR` is repeatable and also accepts a comma-separated list;
   `--port` stays single. Default is exactly today's behavior — `0.0.0.0` — so
   existing installs and the packaged unit are unaffected.
2. One listener per address, all sharing one watcher, one engine, one event bus,
   one operation slot. IPv6 literals are supported (`::1`, `::`), address family
   detected per address.
3. **Startup is loud and all-or-nothing.** If any declared address cannot be
   bound, Roundhouse reports *every* failing address with its errno and exits
   non-zero — it does not silently serve on the subset that worked. A half-bound
   server is the kind of thing an operator discovers a week later from a phone
   that cannot reach it.
4. This is what makes a reverse proxy honest: `--bind 127.0.0.1` puts Roundhouse
   behind caddy with no second, unencrypted door standing open beside it. The
   package keeps the wildcard default; changing it is an operator edit.

## Part 2 — the peer watch

**The fleet has hosts that come and go** — `ampere` is a laptop that travels,
`dirac` answers only sometimes. Roundhouse should say which of them are present
right now, and say nothing more than it actually knows.

1. Peers are declared on the command line: `--peer NAME=HOST:PORT`, repeatable
   (e.g. `--peer ampere=ampere.fritz.box:8099 --peer dirac=dirac.fritz.box:22`).
   **There is no config file, deliberately** — Roundhouse holds no configuration
   of its own; its own unit file is its configuration surface, exactly as the
   managed units are theirs.
2. **Probe: a TCP connect, 2 s timeout, once every 60 s**, re-resolving the name
   every time (a roaming laptop returns on a different address; DNS answers for
   both these hosts even while they are away, so name resolution proves nothing).
   No ICMP (privileges), no HTTP (a peer's port may be an inference server, and
   Roundhouse does not knock on those).
3. **Hysteresis, asymmetric on purpose:** `up` on the first successful connect,
   `down` only after **two consecutive** failures. A single dropped packet must
   not flip a roaming host's state — and a returning host should appear promptly.
4. States are `up`, `down`, and `unknown` (never probed yet — the honest state
   for the first minute), each with `since` and the last transition time.
5. **What it means, stated in the UI and the API:** *reachable*, not *healthy*
   and not *serving*. A TCP connect proves something is listening on that port;
   it proves nothing about the fleet behind it. MVP7 does not fetch a peer's
   roster — cross-host aggregation stays out of scope.
6. Surfaces: `GET /api/peers` (unauthenticated read, like every read route), a
   `peers` key in the snapshot, an SSE `peer` event **on transition only** (a
   host that is simply absent must not generate traffic every minute), a compact
   peer strip in the UI, and one new MCP read tool, `peer_status` — the frozen
   catalog goes from 16 to 17 tools, amended by this spec.
7. The watch is sensing only: no placement input, no warm input, no actuation,
   no effect on the operation slot. It cannot fail a rollout or a switch.

## Acceptance criteria

- [ ] `--bind` accepts repeats and comma lists, IPv4 and IPv6; default unchanged
      (`0.0.0.0`); `--bind 127.0.0.1` is provably unreachable from another host
      while still serving locally.
- [ ] Multiple addresses serve one shared state: a rollout started through one
      listener is visible through the other, and the operation slot is shared
      (a second operation via the other address answers 409).
- [ ] An unbindable address (in-use port, bogus address) fails startup with a
      non-zero exit that names **every** failing address, not just the first;
      nothing is left listening.
- [ ] Peers declared per `--peer NAME=HOST:PORT`; a malformed declaration fails
      startup with the offending text.
- [ ] Probe behavior: `up` on first success; `down` only after two consecutive
      failures; `unknown` until first probe completes; re-resolution every probe
      (proven with a name whose address changes between probes).
- [ ] A peer that is absent for an hour produces exactly the transitions it
      earned — no SSE traffic while its state is unchanged.
- [ ] `/api/peers`, the snapshot key, and the MCP `peer_status` tool agree
      field-for-field; the UI strip renders `up`/`down`/`unknown` neutrally
      (no new red sharers) and labels them *reachable*, never *healthy*.
- [ ] The watch cannot actuate: with peers configured and flapping, no
      `systemctl`/`git`/file-write call is made on their account (three-leg proof
      pattern), and `engine.current` is untouched.
- [ ] Peer probing never targets a managed unit's port on the local host
      (asserted in code, not only in prose).
- [ ] Container drill: two peers, one reachable and one not; kill and restore the
      reachable one and observe both transitions with correct hysteresis timing.
- [ ] Live boltzmann (operator, may remain open): declare `ampere` and `dirac`,
      observe the states over a few minutes.
- [ ] Runs without a build step; stdlib only; no German; no throughput figures.

## Out of scope (MVP7)

Fetching or aggregating a peer's roster/units/deployments; any placement or warm
decision informed by peer state; acting on a peer (starting, stopping, waking);
Wake-on-LAN; TLS/reverse-proxy configuration (Roundhouse binds, caddy is the
operator's); per-peer credentials; ICMP; more than one probe protocol; peers as
MCP action targets; a config file of any kind.
