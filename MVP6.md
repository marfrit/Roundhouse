# MVP6 — the MCP interface: agents as first-class operators

**Milestone 6 gives agents the same hands the UI gives humans — and not one finger
more.** An MCP server exposes the fleet as tools: read tools for the roster and
feasibility, action tools for the turntable, rollouts, boot strategy, and warm-up.
Every action rides the existing gated HTTP routes; the MCP layer adds **zero new
actuation paths** to Roundhouse itself.

## Shape

- **One new file, `mvp1/roundhouse_mcp.py`** — a stdlib-only MCP server speaking
  JSON-RPC 2.0 over **stdio** (`initialize`, `tools/list`, `tools/call`; no SDK, no
  pip). It is a pure HTTP client of a running Roundhouse: no subprocess, no file
  writes, no direct systemd/git access — provably, by the same AST-guard style as
  everything else. `roundhouse.py` is not modified except where a route's response
  needs a machine-readable field it lacks (none known; any such change goes through
  the normal spec discipline).
- Connection config: `--url` (default `http://127.0.0.1:8090`), token from
  `~/.config/roundhouse/token` or `ROUNDHOUSE_TOKEN` env — never argv. Read-only
  Roundhouse → action tools report the 403 as a structured refusal, not a crash.

## The tool surface

Read (work against any mode):
| tool | wraps |
|---|---|
| `fleet_status` | `/api/units` — roster summary: rungs, ports, enablement, drift notes, on-demand flags, memory gauge |
| `unit_detail` | `/api/units/<name>` — params, operator notes, gate, measured history |
| `port_board` | `/api/ports` |
| `deployments` | `/api/deployments` |
| `routing_config` | `/api/routing-config.json` |
| `operation_status` | `/api/rollouts/<id>` + snapshot `rollout` — poll a running operation |
| `warm_state` | `GET /api/warm` |

Action (require `--actuate` + token; every one surfaces preflight refusals as
structured results with the full arithmetic/claimants — an agent must be able to
reason about *why*, per the Milestone 6 doctrine in the README):
| tool | wraps |
|---|---|
| `switch_preview` / `switch_execute` | `/api/switch/preview` + `/api/switch` — two tools, confirm passed through so the agent deliberates between preview and act |
| `edit_preview` / `edit_rollout` | `/api/units/<name>/edit` + `/rollout` — parameter rollouts with the diff in the preview result |
| `set_boot` | `/api/units/<name>/enablement` — the checkbox |
| `warm` / `warm_cancel` | `/api/warm` + `/cancel` — requester defaults to `mcp:<client name from initialize>` |
| `operation_rollback` / `operation_dismiss` | `/api/rollouts/<id>/rollback` + `/dismiss` |

## Boundaries that carry over unchanged

The MCP server grants nothing the token doesn't already grant. Preview/confirm
stays two-step — no tool collapses preview-and-execute into one call. RETIRED
lockout, consent fence, the operation slot, the enable-collision interlock: all
enforced server-side as today; the MCP layer never pre-empts or retries around a
refusal. No autonomous loop lives here — the MCP server acts only when a connected
agent calls a tool.

## Acceptance criteria

- [ ] `tools/list` returns all tools above with JSON-Schema input schemas; names and
      schemas frozen in the spec.
- [ ] Scripted MCP client session (stdio, in the container): initialize →
      fleet_status → switch_preview (tick a stop) → switch_execute → operation_status
      polled to done → operation_rollback of a failed op → set_boot both ways →
      warm → warm_state → warm_cancel. Every step's tool result asserted.
- [ ] Refusal fidelity: a 422 (collision/consent/preflight) surfaces the COMPLETE
      response body in the tool result (claimants, arithmetic, excluded_unmarked) —
      asserted field-for-field against a direct HTTP call.
- [ ] Read tools work against a read-only Roundhouse; action tools return a
      structured `read_only_mode` refusal there (not an exception).
- [ ] Zero-write proof for the MCP file: AST test — no subprocess, no file opens for
      write, no socket use beyond the HTTP client to the configured URL.
- [ ] Two-step discipline: the spec's frozen tool list contains no single-call
      mutate-without-preview tool for switch/edit paths (set_boot and warm are
      single-call by design — their preflights are server-side and atomic).
- [ ] `docs/MCP.md`: registration for Claude Code (`.mcp.json` stdio example),
      token provisioning, tool catalog with example calls/results, the refusal-
      reasoning pattern, and the boundary statement (agents = operators, no loop).
- [ ] Container drill green end-to-end; live boltzmann row (operator, may remain
      open): register the MCP server against the read-only live instance and run the
      read tools from a real agent session.
- [ ] Stdlib only, no build step, no German, no throughput figures; `roundhouse.py`
      diff empty or spec-disciplined.

## Out of scope (MVP6)

HTTP/SSE MCP transports (stdio only); MCP resources/prompts (tools only);
multi-host; any new Roundhouse actuation surface; agent identity beyond the
requester tag; rate limiting; the autonomous reconciler (still never).
