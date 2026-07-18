# Roundhouse — a fleet manager for heterogeneous LLM inference

**Cross-model, cross-host, cross-engine control plane for a fleet of always-on inference boxes that can't all serve every model at once.** Roundhouse decides what runs where, brings models up and down on demand, and hands a single clean API to your agents.

> Status: design stage, defensive publication. This document stakes out the design before it is built. The novel part, which no existing tool covers, is the combination: an engine-agnostic driver contract, desired-state reconciliation, and thermal-aware placement, for heterogeneous fleets running arbitrary (including bespoke) inference engines. Dated and public on purpose.

## The metaphor

A railway roundhouse uses a turntable to rotate heavy locomotives onto a limited set of stalls. You can't park every locomotive on a track at once, so the turntable swaps them in and out as needed.

- **the turntable** — the placement/scheduling engine: decides which model becomes resident on which host, and what gets evicted to make room.
- **stalls / tracks** — a host's resident capacity (VRAM / RAM slots). Finite. The whole problem is that you have more models than stalls.
- **the roster** — the thermal availability model: which models are `hot` (resident), `warm` (kept-alive), `cold` (swap needed), or `unavailable`. This is the sensing layer.

## The problem

A real fleet is heterogeneous and oversubscribed:

- Many always-on hosts, some capable and some modest, but collectively they cannot serve every model side-by-side.
- Models are bound to engines: DeepSeek V4 to [ds4/DwarfStar](https://github.com/antirez/ds4), GGUF quants to llama.cpp, safetensors/AWQ to vLLM. You can't just run any model anywhere.
- Constant churn: switching models, switching engines, and changing parameters (context length, KV-cache size, cache type q8/q4), today by hand, per host.

Existing tools each solve a slice:

| Tool | Solves | Misses |
|---|---|---|
| **LiteLLM** | Unified API, routing, fallback across running endpoints | No process lifecycle; doesn't bring models up |
| **llama-swap** | Per-host on-demand load/unload of *any* OpenAI-compatible engine | Single host; no cross-host placement |
| **GPUStack** | Cross-host scheduling + gateway across a cluster | Fixed backend set — a bespoke engine (ds4) isn't a first-class citizen |
| **KServe + ModelMesh / Ray Serve** | Placement + eviction across a cluster | Heavy (K8s); assumes containerized standard runtimes |

Roundhouse combines what those tools offer separately: GPUStack's cross-host placement, llama-swap's indifference to the engine, and a reconciliation loop on top. Bespoke engines become first-class, and the fleet runs from declared desired state rather than by hand.

## Architecture: separate sensing, actuation, and routing

Three layers with clean ownership. Roundhouse owns placement; it generates config for the others and actuates them rather than reimplementing them.

```
                 ┌──────────────────────────────────────────┐
   agents /      │            LiteLLM  (routing)            │   ← consumers bind to
   consumers ───▶│  LogicalModel name → healthy Deployments │     LOGICAL model names
                 └───────────────▲──────────────────────────┘
                                 │ generated config
                 ┌───────────────┴──────────────────────────┐
                 │        ROUNDHOUSE  (the turntable)        │
                 │  desired-state controller + placement +   │
                 │  thermal roster + engine drivers          │
                 └───┬───────────────┬───────────────┬───────┘
                     │ actuate        │ actuate       │ actuate
                 ┌───▼────┐      ┌────▼───┐       ┌───▼────┐
                 │llama-  │      │llama-  │       │ ds4 /  │   ← per-host lifecycle
                 │swap @A │      │swap @B │       │ custom │     (start/stop/preload)
                 └────────┘      └────────┘       └────────┘
                  host A          host B           host C  (stalls: VRAM/RAM)
```

- **Actuation (per host):** [llama-swap](https://github.com/mostlygeek/llama-swap) where it fits. It spawns any command that exposes an OpenAI-compatible endpoint, so ds4 and friends slot in. A host's own engine driver can actuate directly where llama-swap doesn't apply.
- **Routing (fleet):** [LiteLLM](https://github.com/BerriAI/litellm). Roundhouse generates its config (`LogicalModel → deployments`, failover, thermal-weighted routing). Consumers bind to logical names only.
- **The turntable (Roundhouse):** desired-state controller, placement/feasibility solver, thermal roster, and the engine-driver registry.

## The entity spine

Most of the ambiguity in "manage models across hosts" comes from treating a model as one thing. It is actually a chain:

```
Model            DeepSeek-V4                        abstract identity
  └─ Artifact    V4 · Q4_K_M · GGUF · sha256:…      a specific quant/format file (+ revision pin)
       └─ RepoItem      downloaded, 24 GB           presence in the local repository
            └─ HostArtifact   on host-A             distributed to a specific box
                 └─ Deployment = Artifact + Engine + ParamProfile + Host + LoadStrategy
                      • desired state   (what you asked for)
                      • observed state  (what's actually true)
LogicalModel     "deepseek-v4"  →  [Deployment A, Deployment B]   what the proxy & agents see
```

What this buys you:
- Engine choices follow from the Artifact format, not the abstract model. The UI offers only engines that can load what you actually downloaded.
- A ParamProfile is first-class. `v4-32k-q8` and `v4-8k-q4` are two Deployments over one on-disk Artifact, not a mutable field you edit in place.
- Consumers bind to a LogicalModel, not to a physical Deployment, so Roundhouse can rebalance, move, or re-quantize underneath them without touching a single agent's config.

## Model lifecycle (states, including the transient ones)

Repository / distribution axis:

```
not-downloaded → downloading(%) → downloaded → distributing → on-host
                      │ fail                        │ fail
                      ▼                             ▼
                   failed                        failed
```

Deployment runtime axis (the **roster**):

```
configured → loading → hot ⇄ warm → cold        load-strategy: on-boot | manual | on-demand
                │ fail          (kept-alive)
                ▼
            load-failed
unavailable  (host down)
```

Operations, each at the right layer of the spine:
- **Change load strategy** — edit the Deployment (on-boot / manual / on-demand).
- **Unload** — free the stall; the Deployment stays configured and goes `cold`. (memory only)
- **Evict** — remove the HostArtifact from the inference host. (host disk)
- **Purge** — delete the RepoItem from the local repository. (repo disk; refcounted, so it blocks or cascades if any Deployment still needs it)

## The five decisions that make it a manager, not a dashboard

1. **Desired-state reconciliation.** The UI expresses intent; a controller continuously drives `observed → desired` and surfaces drift. Host reboots, engine crashes, and OOMs get handled by the loop, not by you. The UI shows both states (converged / reconciling / failed). Without it you've built a remote control, not a manager.
2. **The engine-driver contract**, the keystone and the novel part. Each engine implements: supported artifact formats and hardware, a parameter schema (so the params UI is generated, not hardcoded), a memory estimator, a launch command, a health check, and lifecycle verbs (start / stop / preload / unload). This is what lets ds4 or any future engine be first-class, which GPUStack's fixed backend set can't do.
3. **Logical vs physical model.** The proxy-facing `LogicalModel` is an alias over a set of Deployments. Routing policy lives here (in generated LiteLLM config); placement lives below.
4. **Feasibility computed before placement.** `weights + KV-cache(context × cache-type) + engine overhead` has to fit a host's free stall space, as a prediction rather than an OOM discovered at load. It's circular, since params feed the estimate and the estimate feeds host-suitability, so the placement UI is a live constraint filter that greys out hosts that can't fit these params. The estimator lives in the engine driver.
5. **Thermal state and on-demand load.** "Serving" isn't binary once you're oversubscribed. Model the runtime state as the roster (hot/warm/cold), and decide the policy: a request for a cold Deployment either triggers an autoscale-to-warm (with a queue) or gets rejected. This is the bridge from manual load strategies to a real scheduler, and it's what llama-swap actuates.

## The frontend (ETL / swim-lane)

- **Model repository** panel: browse a catalog; each Artifact shows its stage (not-downloaded → downloaded → on-host) with progress on the transient steps.
- **Serving swim lane**: drag an Artifact in, Roundhouse offers only feasible (host, engine) pairs, an engine-driven parameter form follows, you pick a load strategy, and the Deployment materializes while the controller reconciles it.
- **Host boxes** (bottom rail): each host shows live CPU / GPU / VRAM / storage, its installed engines, and its occupied vs free stalls.
- **Wiring**: Deployments roll up into LogicalModels; agents and consumers connect to LogicalModels and to the proxy; the proxy connects to every consumer. Destructive actions (evict / purge) are confirmation-gated.

## What's genuinely novel here (the prior-art hook)

Against the field: an engine-agnostic driver contract, desired-state reconciliation, and thermal-aware placement, for heterogeneous fleets running arbitrary or bespoke engines. GPUStack has placement but a closed backend set. llama-swap has engine-agnosticism but a single host. LiteLLM has routing but no lifecycle. KServe/ModelMesh has placement and eviction but is K8s-heavy and runtime-opinionated. None of them fuse all three. Roundhouse does, and it treats `(model, engine, param-profile)` as the schedulable unit.

## Milestones

1. **Driver + roster (read-only):** the engine-driver interface plus a thermal roster that senses hot/warm/cold across hosts. No actuation yet; this proves the sensing model. Bootstraps from the availability service already running alongside LiteLLM.
2. **Actuation:** wire in llama-swap (and a direct ds4 driver) so Roundhouse can bring one Deployment up or down on one host.
3. **Feasibility + placement:** a memory estimator per driver, and the constraint-filtered "drag to a feasible host" flow.
4. **Reconciliation loop:** the desired/observed controller, which survives host reboot and process crash and reloads on-boot Deployments.
5. **Proxy generation + on-demand:** emit LiteLLM config from LogicalModels, and implement request-triggered autoscale-to-warm.

## License / contributing

Do whatever you want with this design. Built as the control plane for a personal heterogeneous inference fleet, and shared as prior art. Issues and better ideas welcome.

---

*Named for the railway roundhouse: heavy locomotives, too few tracks, a turntable that rotates them into the stalls that fit. Swap `locomotive` for `model` and `stall` for `VRAM`, and the rest explains itself.*
