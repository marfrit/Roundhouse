# MVP9 — roaming: optional binds and multi-endpoint peers

**A laptop is not a server with a bad attitude; it is a host with more than one
identity and no obligation to hold any of them.** Ampere is `ampere.fritz.box`
(192.168.88.168) on the sofa and `ampere.vpn` (`fd96:cafe:cafe::1000`, plus
10.170.x) at the work desk, and never both. Today Roundhouse cannot express that:
`--bind` is resolved once and is all-or-nothing, so a laptop can bind loopback
(useless to the fleet) or a home address (refuses to start away from home).

MVP9 makes both sides of federation roam. Two small, symmetric features.

## Part 1 — optional binds (the serving side)

1. `--bind-optional ADDR|NAME`, repeatable and comma-separated. Meaning: *bind
   this when it exists here; carry on when it does not.*
2. `--bind` keeps its MVP7 contract exactly — mandatory, all-or-nothing, loud.
   A host that must be reachable still fails loudly; a laptop uses the optional
   form. **Loopback should remain a mandatory bind**, so there is always exactly
   one address that cannot vanish.
3. **The bind attempt is the presence probe.** `EADDRNOTAVAIL` means the address
   is not on this machine right now; anything else (in use, permission) is a
   real error and is reported per-address without killing the process.
4. A **bind watch** re-evaluates every 30 s (`--bind-retry`, its own cadence, not
   the peer round — a laptop changing desks should be reachable in half a minute,
   not after a minute): addresses that appeared get a listener, addresses that
   vanished get their listener closed. Names are re-resolved every cycle, because
   that is the point.
5. Never `0.0.0.0`/`::` as an optional bind — a wildcard cannot be absent, and
   accepting it would silently reintroduce the exposure the loopback default
   exists to prevent. Refuse it at startup.
6. Listener state is visible: the snapshot gains `listeners: [{addr, port, kind:
   mandatory|optional, state: bound|absent, since, last_error}]`, and the UI shows
   it. "Why is the fleet view empty" must be answerable by looking.

## Part 2 — multi-endpoint fleet peers (the federating side)

1. `--fleet-peer NAME=URL[,URL...]` — an ordered candidate list for **one** peer,
   e.g. `--fleet-peer ampere=http://ampere.fritz.box:8099,http://[fd96:cafe:cafe::1000]:8099`.
2. The reachability probe walks candidates in order and stops at the first that
   answers; that candidate becomes `endpoint_in_use` and is what the fetch uses.
   A peer is `up` if **any** candidate answers; the MVP7 hysteresis (up on first
   success, down after two consecutive failures) is unchanged and applies to the
   peer, not to individual candidates.
3. When the winning candidate stops answering, the next round re-walks the list
   from the top — a laptop that moved from sofa to desk changes endpoint without
   changing identity. Endpoint changes are visible (`endpoint_in_use`,
   `endpoint_changed_at`) and emit the existing `peer` SSE event.
4. Every candidate is subject to the existing rules, unchanged: the D2
   host-and-port refusal, TLS verification with no bypass, no redirects, the
   4 s/4 MiB limits, and the per-round budget — the cap arithmetic must account
   for candidates, not just peers.
5. Candidate lists are ordered by the operator, and the order is honoured. No
   scoring, no learning, no reordering: the sofa is first because you said so.

## What this does not become

Not a VPN manager, not a discovery protocol, not mDNS. Roundhouse does not bring
a network up, does not wake a peer, and does not guess an identity it was not
given. Peers and binds are declared, as ever, in Roundhouse's own unit file.

## Acceptance criteria

- [ ] `--bind-optional` parses (repeats, commas, names, IPv4/IPv6, wildcard
      refused), coexists with `--bind`, and an absent address does **not** fail
      startup while a mandatory one still does.
- [ ] Address appears → a listener appears within one bind-retry cycle and serves;
      address disappears → its listener is closed and the rest keep serving. Proven
      by adding and removing a real address, not by mocking.
- [ ] A permission or in-use error on an optional bind is reported per-address and
      the process continues; `EADDRNOTAVAIL` is treated as absence, not error.
- [ ] `listeners` appears in the snapshot and the UI with per-address state; the
      wildcard refusal and the mandatory-loopback recommendation are documented.
- [ ] `--fleet-peer` accepts a candidate list; the probe walks in order; the first
      answering candidate is `endpoint_in_use` and is the one fetched from.
- [ ] Peer stays `up` while any candidate answers; hysteresis unchanged; moving
      from candidate 1 to candidate 2 emits one `peer` event and does not flap the
      peer's own state.
- [ ] D2, TLS-verification, no-redirect and the size/time limits apply to **every**
      candidate; the round budget accounts for candidates and is pinned.
- [ ] Container drill: a Roundhouse with one optional bind on a scratch address —
      add it, remove it, add it again; and a fleet peer with two candidates where
      the first is dead, proving the second is used and reported.
- [ ] Live: ampere serves on loopback plus whichever of its identities exists, and
      boltzmann federates it as one peer with two candidates — verified at home,
      and verified again by the operator from the work desk (that leg may remain
      open at push).
- [ ] Stdlib only, no build step, no German, no throughput figures.

## Out of scope (MVP9)

Managing, starting or detecting VPNs as such; waking peers; mDNS/DNS-SD or any
discovery; scoring or reordering candidates; per-candidate credentials; binding
by interface name rather than address; IPv6 privacy-address churn handling beyond
"it is there or it is not"; cross-host actuation (still Milestone 10).
