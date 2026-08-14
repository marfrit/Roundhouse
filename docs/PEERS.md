# Where Roundhouse listens, and who it watches

Two operator-facing knobs, one story: `--bind` says which local addresses Roundhouse
answers on, `--peer` says which other hosts it watches. Neither actuates anything.
There is no config file, deliberately — Roundhouse holds no configuration of its own;
its unit file is its configuration surface, exactly as the managed units are theirs.

---

## The listen list: `--bind`

**Syntax:** `--bind ADDR [--bind ADDR ...]` and/or `--bind ADDR,ADDR,...`

Repeatable *and* comma-separated; `--port` stays single. Default is exactly today's
behavior — `0.0.0.0` — so existing installs and the packaged unit are unaffected.

- **One listener per address**, all sharing one watcher, one engine, one event bus,
  one operation slot. A rollout started through one door is visible through the other,
  and a second operation via the other address answers `409` — there is one slot, not
  one per listener.
- **Literal IP addresses only.** A hostname is refused:
  `'boltzmann.fritz.box': not a literal IP address — bind to the address, not the name`.
  Binding a name resolves once and silently pins whatever DNS said at boot, which is
  the lying-prone behavior this milestone exists to remove.
- **IPv4 and IPv6**, family detected per address. IPv6 may be written bare (`::1`) or
  bracketed (`[::1]`); with `--port` separate there is no colon ambiguity. `::` is
  bound `IPV6_V6ONLY=1`, so `--bind 0.0.0.0,::` means "everything, both families"
  without the v6 wildcard silently shadowing the v4 one.
- **Configuration errors, refused with a reason:** a duplicate address after
  canonicalization (`::1` and `0:0:0:0:0:0:0:1` are the same address), and a
  same-family wildcard beside a specific address of that family
  (`'0.0.0.0' already covers '127.0.0.1' — bind one or the other`). The wildcard
  already covers the specific; half the operator's intent would be silently redundant.
- **Startup is loud and all-or-nothing.** If any declared address cannot be bound,
  Roundhouse reports *every* failing address with its errno and exits non-zero:

  ```
  cannot bind 127.0.0.1:8090: [Errno 98] Address already in use
  cannot bind [::1]:8090: [Errno 98] Address already in use
  ```

  Nothing is left listening — sockets that did bind are closed before the exit — and
  **no `listening` line is printed at all** unless every bind succeeded. A half-bound
  server is the kind of thing an operator discovers a week later from a phone that
  cannot reach it.

### The caddy pattern

`--bind 127.0.0.1` puts Roundhouse behind a reverse proxy with no second, unencrypted
door standing open beside it:

```ini
# ~/.config/systemd/user/roundhouse.service
ExecStart=/usr/bin/python3 /path/to/mvp1/roundhouse.py --serve --bind 127.0.0.1 --port 8090
```

```caddyfile
roundhouse.example.org {
    reverse_proxy 127.0.0.1:8090
}
```

Caddy terminates TLS on :443; Roundhouse is reachable on loopback only. The package
ships the wildcard default — restricting the bind is an operator edit to `ExecStart`.
`--advertise-host` is independent of all of this: it names how *others* reach this
host, not where Roundhouse listens.

---

## The peer watch: `--peer`

**Syntax:** `--peer NAME=HOST:PORT`, repeatable (no comma lists — a declaration already
contains `=` and `:`, and commas would only invite quoting bugs).

```bash
roundhouse.py --serve \
  --peer ampere=ampere.fritz.box:8099 \
  --peer dirac=dirac.fritz.box:22
```

- **NAME:** `^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$`; duplicates are a configuration error.
- **HOST:** a DNS name or an IP literal; IPv6 literals must be bracketed
  (`[fe80::1]:22`) because the port is attached here.
- **PORT:** 1–65535.
- **Cap: 8 peers.** Worst case one sequential round is 8 × 2 s = 16 s, comfortably
  inside the 60 s cadence with DNS latency on top.
- A malformed declaration fails startup, echoing the offending text verbatim, and
  **every** offending declaration is listed before the single exit.

### The probe

A **TCP connect, 2 s timeout, once every 60 s**, re-resolving the name every round.

- **Why TCP and not ICMP:** ICMP needs privileges Roundhouse does not want.
- **Why TCP and not HTTP:** a peer's port may be an inference server, and Roundhouse
  does not knock on those. Nothing is written and nothing is read — the socket is
  opened and closed, which is asserted in the test suite, not merely promised.
- **Why re-resolve every round:** the Fritz!Box answers DNS for `ampere` and `dirac`
  even while they are away, so name resolution proves nothing; and a roaming laptop
  comes back on a different address. Re-resolution is `create_connection`'s own
  `getaddrinfo` on every call, so it is guaranteed rather than remembered.
- **Success** means something accepted the connect. **Failure** is any exception —
  DNS failure, timeout, refusal, unreachable. A refused connect is a failure, not a
  soft state: *reachable* means something is listening, and refusal proves nothing is.

`--peer-interval SECONDS` (default 60, minimum 1) exists so a drill can prove
hysteresis timing in seconds instead of minutes. The 2 s timeout is a constant.

### Hysteresis (asymmetric on purpose)

`up` on the first successful connect; `down` only after **two consecutive** failures.
A single dropped packet must not flip a roaming host's state, and a returning host
should appear promptly.

| state | probe result | new state | consecutive_failures | transition event? |
|---|---|---|---|---|
| unknown | success | **up** | 0 | YES (unknown→up) |
| unknown | failure (cf becomes 1) | unknown | 1 | no |
| unknown | failure (cf becomes 2) | **down** | 2 | YES (unknown→down) |
| up | success | up | 0 | no |
| up | failure (cf becomes 1) | up | 1 | no |
| up | failure (cf becomes 2) | **down** | 2 | YES (up→down) |
| down | failure | down | cf+1 | no |
| down | success | **up** | 0 | YES (down→up) |

There are no other transitions. A host that is absent for an hour earns exactly one
`→down` event and then silence.

`since` is the time the current state was entered — it doubles as the last-transition
time, one field with both meanings. `last_probe` is the completion time of the most
recent probe (`null` before the first). `last_error` is the failure string from the
most recent failed probe, cleared to `null` on success. `unknown` is the honest state
until the first probe completes; the first round runs at startup, so it lasts seconds,
not a minute.

### What *reachable* means, and what it does not

> A TCP connect proves something is listening on that port; it proves nothing about
> the fleet behind it.

*Reachable*, not *healthy* and not *serving*. The UI says it, the API says it in the
frozen `means` string, and the MCP tool description says it. MVP7 does not fetch a
peer's roster — cross-host aggregation is out of scope, as is any action on a peer.

### Peers are other hosts (the D2 rule)

A peer declaration that names **this host** on a **managed unit's port** is refused at
startup:

```
peer 'self' targets 127.0.0.1:8085 — port 8085 is managed unit qwen3.6-coding.service's
port (or roundhouse's own) on this host; peers are other hosts
```

The rule is host-**and**-port, never port-only. `qwen3.6-coding` serves :8085 on
boltzmann *and* on ampere, so `--peer ampere=ampere.fritz.box:8085` is entirely
legitimate and starts fine. "This host" covers `localhost`, the kernel nodename and
its short form, `--advertise-host`, any specific `--bind` address, any loopback
literal (all of `127.0.0.0/8` and `::1`), the wildcards, and the best-effort
`getaddrinfo` answers for the nodename. A local address on an *unmanaged* port is
allowed — that is how a drill points a fake peer at an ephemeral listener.

Residual, documented: a DNS name that later resolves to this host is out of the threat
model. The zone is operator-controlled, and sensing stays sensing — there is no
runtime re-resolution vetting.

---

## Surfaces

| surface | shape |
|---|---|
| `GET /api/peers` | `{"peers": [...], "probe": {"method": "tcp-connect", "timeout_seconds": 2.0, "cadence_seconds": 60}, "means": "..."}` — unauthenticated, like every read route; POST answers 405 |
| snapshot | a `peers` key carrying the same rows, field for field (it is the same function) |
| SSE | a `peer` event **on transition only**, carrying the row plus `prev_state`. A host that is simply absent generates no traffic. Clients need no replay: every `snapshot` event already carries current `peers` |
| UI | a compact, non-interactive strip labelled `peers (reachable)`, one `name · state` cell per peer, rendered in neutral colours — no red, no amber; these are other people's hosts and down is information, not an alarm |
| MCP | read tool `peer_status` (#17 of 18), a passthrough of `/api/peers`. Peers are never MCP action targets |

A row:

```json
{"name": "ampere", "host": "ampere.fritz.box", "port": 8099, "state": "up",
 "since": 1755100000.0, "last_probe": 1755100060.0,
 "consecutive_failures": 0, "last_error": null}
```

With no peers declared the route still exists and answers the same envelope with an
empty list.

## The watch cannot actuate

No placement input, no warm input, no `systemctl`, no `git`, no file write, no contact
with the operation slot. It cannot fail a rollout or a switch. The one outbound socket
in the whole codebase lives in `_probe_peer`, which takes no host/port parameters at
all — it looks the endpoint up in the startup-validated declaration table, so probing
anything undeclared is unrepresentable rather than merely discouraged.

## Packaged unit

`mvp1/roundhouse.service` ships the wildcard default and no peers. Binding and peers
are operator edits to `ExecStart`, consistent with "its own unit file is its
configuration surface":

```ini
ExecStart=/usr/bin/python3 /home/roundhouse/roundhouse/mvp1/roundhouse.py --serve \
    --bind 127.0.0.1 --port 8090 \
    --peer ampere=ampere.fritz.box:8099 --peer dirac=dirac.fritz.box:22
```

## Drill

`mvp1/scripts/peer-drill.sh` runs the container leg end to end: two peers (one
reachable, one not), a kill and a restore, with the exact round at which each
transition is allowed to happen asserted rather than eyeballed. It also covers the D2
refusal, the legitimate same-port peer, the bind-failure refusal, and the multi-listener
shared-slot 409. `--live` prints the operator checklist for the boltzmann run.

---

## Federation: `--fleet-peer`

A `--peer` watches whether something is listening; a `--fleet-peer` watches a
**Roundhouse instance** and, once it answers, asks it what it is running.
`--fleet-peer NAME=URL` is not a variant of `--peer` — it is reachability-watched
exactly like a TCP peer (same hysteresis, same `up`/`down` transitions, same SSE
events) and, in addition, is periodically fetched over HTTP(S) for its own
roster and its own routing fragment. `--peer` still never knocks on an inference
port; `--fleet-peer` is the one declaration that is allowed to, because the peer
named there is Roundhouse itself.

### Declaring a fleet peer

**Syntax:** `--fleet-peer NAME=URL`, repeatable.

- **NAME:** the same `^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$` rule as `--peer`, and
  the **same namespace** — a name cannot be declared with both flags in the same
  invocation. Roundhouse refuses at startup:
  ```
  'ampere' is declared as both --peer and --fleet-peer — one watch row per name;
  a fleet peer is reachability-watched already
  ```
  A fleet peer needs no separate `--peer` entry; declaring it once is enough to
  be both reachability-watched and fetched.
- **URL:** scheme `http` or `https`, a host (DNS name or IP literal, IPv6
  bracketed), and an optional port (default 80 for `http`, 443 for `https`). A
  path, query, fragment, or embedded credentials are refused — a fleet peer is
  an instance root, not an endpoint:
  ```
  malformed --fleet-peer 'ampere=https://ampere.fritz.box:8099/api/units': URL
  must not carry a path, query, fragment, or credentials — declare the instance
  root (scheme://host[:port])
  ```
- **Caps:** at most 4 fleet peers (`too many fleet peers (5 > 4): two fetches per
  peer per round must finish well inside the 60 s cadence`), inside a combined
  total of 8 peers shared with `--peer` (`too many peers (9 > 8): 6 TCP + 3
  fleet; combined probe round must finish well inside 60 s cadence`) — both caps
  exist because every fleet peer costs two fetches per round on top of its probe.
  As with `--peer`, every malformed declaration is listed before the single exit;
  nothing is applied partially.
- The D2 rule (above) applies identically to a fleet peer's derived host:port —
  a fleet peer cannot target this instance's own bind address or its own API
  port.

Packaged-unit example — this replaces the reachability-only `--peer
ampere=ampere.fritz.box:8099` line shown earlier, once ampere is confirmed to be
a Roundhouse instance rather than just something worth watching:

```ini
ExecStart=/usr/bin/python3 /home/roundhouse/roundhouse/mvp1/roundhouse.py --serve \
    --bind 127.0.0.1 --port 8090 \
    --peer dirac=dirac.fritz.box:22 \
    --fleet-peer ampere=https://ampere.fritz.box:8099
```

### What is fetched, and when

On the existing peer round (`--peer-interval`, default 60 s), for each fleet
peer that is currently `up`, Roundhouse fetches exactly two documents and
nothing else:

- `GET <url>/api/units`
- `GET <url>/api/routing-config.json` — only if the first request succeeded

Both requests happen **sequentially**, on the peer-watch thread, **outside every
lock** — the same discipline the reachability probe already follows. Each
request is bounded by a 4-second timeout, and the response read is capped at
4 MiB; going over the cap is a fetch failure (`body: oversized (> 4194304
bytes)`), not an unbounded read. Redirects are never followed: a `3xx` response
is recorded as data, not chased — `http: 301`. A peer that hangs or is absent
costs only its own timeout; the 3-second local tick, the HTTP API, and the
operation slot run on other threads and are unaffected.

Nothing else is ever fetched — no detail routes, no operations endpoints, no
MCP-to-MCP calls. A failed fetch never changes the peer's reachability state and
never raises into the local loop; it is recorded as federated staleness, below.

### The trust requirement

`https://` fleet peers are verified with `ssl.create_default_context()` against
the **system trust store** — the same store a fleet CA package installs its
certificate into. There is **no flag anywhere to disable verification**, no
environment override, and no way to hand `_fetch_peer` an alternate context; the
construction lives in exactly one function in the whole codebase. A federation
that silently accepted any certificate would be worse than no federation at all.

One consequence follows directly from how reachability is measured: the
`up`/`down` probe is a bare TCP connect (above) that never touches TLS. A peer
with an untrusted certificate is therefore **`up`** — something answered the
port — while its federated data never becomes `fresh`, carrying a `tls:`
reason. That combination is expected behavior, not a bug: reachability and
trustworthiness are different questions, and Roundhouse answers them
separately.

### Federated state: `never` / `fresh` / `stale`

Each fleet peer's federated data is one of three states:

| state | meaning |
|---|---|
| `never` | no fetch has ever succeeded for this peer (a failed attempt may still set `reason`) |
| `fresh` | the most recent fetch succeeded for **both** `/api/units` and `/api/routing-config.json`, in the same round |
| `stale` | the peer had fresh data at some point, and either the last fetch failed or the peer's reachability left `up` |

Failure is always classified into one of six frozen reason prefixes:

| prefix | failure class |
|---|---|
| `tls:` | any `ssl.SSLError`, including certificate verification failure |
| `http:` | a non-200 response, including an unfollowed `3xx` |
| `timeout:` | the fetch did not complete within the timeout |
| `connect:` | DNS failure, connection refused, or an unreachable host |
| `body:` | an oversized response, a non-JSON body, or the wrong document shape |
| `down:` | the peer's own reachability probe failed (written at the `up`→`down` transition) |

Three surfaces show this state, at increasing detail: `GET /api/peers` rows
carry a `kind` (`tcp` or `roundhouse`) and, for fleet peers, a `fed` summary
(`state`, `stale`, `reason`, `fetched_at`, `unit_count`); `GET /api/fleet`
carries the full per-peer block (state, mode, both timestamps, unit count, and
invalid-entry count); and the UI peer strip renders `name · state · N units`,
with a trailing ` · stale` marker whenever the fed state is not `fresh`.

### Retention

The roster (`GET /api/fleet`) keeps a peer's last-known units and marks them
`stale: true` once the fed state stops being `fresh` — an operator can still see
what a now-absent peer was last running. The fleet routing fragment
(`GET /api/routing-config/fleet`) does not retain anything: a peer contributes
to it only while it is both `up` and `fresh`, so a peer that goes stale or down
disappears from the fragment entirely. See `docs/ROUTING.md` for the consumer
side of that distinction.

### Why `/api/routing-config` did not change

`/api/routing-config` and `/api/routing-config.json` remain local-only and
byte-identical to their pre-federation behavior — pinned by a test. hossenfelder
already pulls this route live; silently widening it to include peer data would
change a running consumer's behavior without asking. The fleet-wide merge lives
at its own URL instead: `GET /api/routing-config/fleet` (YAML) and
`GET /api/routing-config/fleet.json` (JSON) — see `docs/ROUTING.md`.

### Drill

`mvp1/scripts/fleet-drill.sh` is the federation counterpart to
`peer-drill.sh`: two Roundhouse instances, one declaring the other as a
`--fleet-peer`, proving roster aggregation, fragment merge, the conflict case,
a killed peer leaving the fragment and going stale in the roster, and a TLS leg
against a self-signed certificate (gated on `openssl` being available) that
proves the untrusted-cert row above end to end — `up`, `never`/`stale`, `tls:`
reason, excluded from the fragment.
