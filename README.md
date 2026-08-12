# Roundhouse

Making the fleet's model list **(semi-)dynamic**, retiring hand-curation.

Roundhouse is the shed where the locomotives wait. This repo holds its design and,
from MVP1 onward, its code.

## Why

Inventory taken 2026-08-02: **five systems have overlapping responsibility** for
"which model, at what cost".

| system | what it holds |
|---|---|
| **`llm-proxy`** (hossenfelder, `/opt/llm-proxy.py`) | **the only real truth** — 265 catalog models (`[local] 4`, `[free] 15`, `[$] 246`), failover, admission gate, four-step cost regulator |
| pi-agents | hand copy: 13 providers / ~53 entries — duplicated *byte-identically* on `pica` and `deus`, and still listing switched-off backends |
| bullpen | hardwired ladder (`PY_GRIND_TIERS`) |
| Open WebUI | own config |
| opencode | own config |

Every consumer keeps its own hand-maintained copy, and they drift.

## The smallest step (still true, still first)

The gateway's `/v1/models` returns only `id`, `object`, `owned_by`, `reasoning`.
The proxy **already has** `name`, `ctx` (context_length) and `prompt_price` in
memory — `llm-proxy.py` lines 1081-1083 read `meta_info` and copy *only*
`reasoning`, discarding the rest.

Exposing those fields is **purely additive and breaks no consumer**. Without them
nothing downstream can decide by cost, so every further Roundhouse step depends on
it. **This happens in `llm-proxy`, not here.**

## The lever that appeared later

`bin/bullpen-evals` now measures which rung of the model ladder actually solved a
task (reads trajectory verdicts from mneme `/trajectory/*`). That data feeds back
nowhere today. So Roundhouse is no longer only "make the list dynamic" — it is
potentially a closed loop:

```
catalog  ->  selection  ->  measurement  ->  catalog re-rating
```

## Design questions, and where they now stand

1. **Who owns the truth** — gateway as catalog vs consumer as selector?
   → *Two truths, two owners, one one-way boundary.* See MVP1 §Q1.
2. **Push or Pull?**
   → *Push for process config, Pull for routing metadata.* See MVP1 §Q2.
3. **May the measurement loop influence the ladder?**
   → *The loop proposes, the operator disposes.* See MVP1 §Q3.

## Hard constraint

> **KEIN Paid-Offloading, nie.** No paid offloading, ever.

Structural, not advisory: MVP1 manages **local units only**, so it cannot offload.
Where catalog context is ever displayed, `[$]` entries are inert text — never an
action, never a fallback target. To be asserted in code, not just documented.

## Contents

| path | what |
|---|---|
| [`MVP1.md`](MVP1.md) | **the implementable spec** — Stellwerk v0.1, read-only |
| [`docs/design/stellwerk-design.html`](docs/design/stellwerk-design.html) | full design, 8 sections + 4 wireframes (open in a browser) |
| [`docs/fixtures/`](docs/fixtures/) | four real unit files as parser test fixtures |

## Status

- 2026-08-12 — design delivered (Stellwerk), MVP1 scoped. **No code yet.**
