# MVP5 — proxy generation, and on-demand warm-up with symmetric consent

**Milestone 5 connects Roundhouse to the routing layer, in both directions.** Outward:
Roundhouse *generates* routing config from its deployments — the proxy pulls it, nobody
hand-maintains a model list again. Inward: a request for a cold model may ask Roundhouse
to warm it — the first sanctioned autonomous actuation, fenced by an explicit opt-in.

## Fleet reality this contract binds to

- The routing layer lives on hossenfelder (LiteLLM on :4000, plus the bespoke llm-proxy).
  **Pull, not push** (per the MVP1 reconciliation ruling): Roundhouse *serves* generated
  config; consumers fetch it. Roundhouse never writes to a remote host, ever.
- Roundhouse remains **boltzmann-scoped**. The generated fragment covers boltzmann's
  deployments only; fleet-level merging of fragments from future per-host drivers is not
  this milestone. Alias collisions across hosts are real today (`qwen3.6-coding` serves
  on both boltzmann and ampere :8085) — generated entries are therefore **namespaced by
  host** (`boltzmann-<alias>`) with the bare alias recorded as the LogicalModel.
- Units stay the configuration surface, full stop (MVP4 owner decision). The on-demand
  opt-in is a unit-file marker comment, not a database row.

## Part 1 — config generation (read-only, pull-based)

1. `GET /api/routing-config` returns a **LiteLLM `model_list` fragment (YAML)** derived
   from the live snapshot + parsed units: one entry per selected, non-RETIRED deployment —
   `model_name: boltzmann-<alias>`, `api_base: http://boltzmann.fritz.box:<port>/v1`,
   plus Roundhouse metadata per entry: the LogicalModel (bare alias), rung at generation
   time, load strategy, `on_demand: true|false`, measured peak + load seconds when known
   (labelled sources, as everywhere). A JSON twin at `GET /api/routing-config.json` for
   non-LiteLLM consumers (the llm-proxy/models-json ecosystem).
2. **Inclusion policy:** hot units (READY/BUSY) always; cold units only if marked
   on-demand (a router can't use a cold model that nothing will warm). STANDBY (gated)
   and FAILED units are never emitted. RETIRED never, at any layer.
3. Generation is a pure read: no file writes, no subprocess beyond the existing sensing.
   It is an unauthenticated GET like every read route — it exposes nothing the snapshot
   doesn't already.
4. The fragment carries a generation header comment: source (`roundhouse@boltzmann`),
   timestamp, and the warm-hook URL (Part 2) so a proxy operator wires the cold path
   from the same document they pulled.

## Part 2 — on-demand warm-up (the fenced exception)

1. **The marker:** a unit opts in with `# roundhouse: on-demand` in its file. Parsing it
   is Section A work (same override-comment mechanism as `# roundhouse: manage`).
   **Symmetric consent, the milestone's safety spine:** the marker means *both* "a warm
   request may auto-start me" *and* "a warm request may auto-stop me to make room."
   Units without the marker — the always-on trio above all — can NEVER be started or
   stopped by a warm request. No exceptions, no override parameter.
2. `POST /api/warm {"logical": "<bare alias>" | "unit": "<unit>"}` — behind the same
   `--actuate` + bearer gate (the proxy or an agent holds a token like any operator).
   Resolution: alias → the boltzmann deployment carrying it; ambiguity → 422.
3. Semantics: target already READY/BUSY → 200 `already_warm` (idempotent, no-op).
   Target not on-demand-marked → 422 `not_on_demand` (the consent fence). Otherwise
   Roundhouse plans a **turntable switch, reusing the MVP3 engine end-to-end** —
   preflights, sequential stops, watch-to-READY, restore-on-failure, the single
   operation slot — with one difference: stop selection is automatic, and **may only
   pick from on-demand-marked units** (greedy by residency, the F7 rule, restricted to
   consenting units). If it cannot fit within consenting stops → 422 with the full
   machine-readable arithmetic (the caller learns *why*, per the Milestone 6 doctrine).
4. **Queue policy (README: "with a queue, or rejected — decide"): queue depth exactly 1.**
   Slot busy → the warm request parks as the pending warm (202 `queued`); a second
   distinct warm while one is parked → 409 `warm_queue_full`; a duplicate of the parked
   target → 200 `already_queued`. The parked warm runs when the slot frees, re-running
   its preflight from a fresh snapshot. A human operation always outranks the queue: it
   takes the slot normally; the parked warm waits.
5. Every warm-triggered switch is a normal operation record (`kind: switch`, plus
   `origin: warm` and the requester) — visible in the stepper, the SSE stream, and
   `GET /api/rollouts/<id>` exactly like a human switch. Nothing autonomous is invisible.

## What this deliberately does not breach

The reconciliation boundary stands: nothing restarts crashed units, nothing edits
parameters, nothing enables/disables, nothing acts without either a human click or an
authenticated warm request for a consenting unit. The warm path cannot touch unmarked
units in either direction. `--actuate` off → the warm route 403s like every mutation.

## Acceptance criteria

- [ ] `/api/routing-config` (YAML) and `.json` twin: golden-file test over the container
      fleet — hot units present, cold+on-demand present, cold+unmarked absent,
      STANDBY/FAILED/RETIRED absent, host-namespaced names, metadata + sources, the
      warm-hook header comment.
- [ ] Marker parsing: `# roundhouse: on-demand` detected (Section A), surfaced in
      snapshot rows and deployment records; unmarked units report `on_demand: false`.
- [ ] Consent fence, both directions, at preflight AND engine: warm for an unmarked
      target → 422 `not_on_demand`; a warm plan may not stop any unmarked unit even
      when that leaves it unfittable (422 with arithmetic naming only consenting
      candidates).
- [ ] Container: full warm drill — fake A (marked) READY, fake B (marked) cold; warm B →
      auto-switch stops A, B reaches READY; record carries `origin: warm`; warm B again
      → 200 `already_warm`.
- [ ] Container: queue drill — long human switch running; warm parks (202 queued);
      second warm → 409; duplicate → 200 `already_queued`; parked warm executes after
      the slot frees and re-preflights.
- [ ] Container: fence drill — warm a marked target whose only fit requires stopping the
      unmarked llama-task fake → 422, llama-task untouched.
- [ ] 403 unarmed / 401 bad token on `/api/warm`; generation routes stay unauthenticated
      reads; zero writes in generation (three-leg pattern).
- [ ] A hossenfelder wiring note ships as `docs/ROUTING.md`: how the proxy pulls the
      fragment and calls the warm hook — documentation only; touching hossenfelder is
      out of scope for this repo's agents.
- [ ] Live boltzmann (operator drill, may remain open): pull the fragment, verify the 3
      hot entries + any marked cold ones; no live warm until the operator marks a unit.
- [ ] Runs without a build step; stdlib only (YAML emitted by hand-rolled serializer —
      no pyyaml); no German; no throughput figures.

## Out of scope (MVP5)

Multi-host aggregation; touching hossenfelder (docs only); LiteLLM process management;
autoscale-to-COLD (nothing is ever auto-stopped except to make room under consent);
crash-restart reconciliation; queue depth > 1; priorities; TTL/idle eviction; per-model
tokens; the MCP interface (Milestone 6).
