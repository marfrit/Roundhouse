# Roundhouse MCP Server — Claude Code Integration

The Roundhouse MCP server exposes the fleet as tools for agents: read tools for roster and feasibility, action tools for turntable, rollouts, boot strategy, and warm-up. Every action rides the existing gated HTTP routes; the MCP layer adds zero new actuation paths.

## Quick Start: Claude Code Registration

To use Roundhouse tools in Claude Code, register the MCP server in your Claude Code settings:

**File: `.mcp.json` (in your Claude Code config)**

```json
{
  "mcpServers": {
    "roundhouse": {
      "command": "python3",
      "args": [
        "/path/to/roundhouse_mcp.py",
        "--url",
        "http://boltzmann.fritz.box:8090"
      ],
      "env": {
        "ROUNDHOUSE_TOKEN": "paste-from-the-roundhouse-host"
      }
    }
  }
}
```

**Remarks:**
- `--url`: Roundhouse HTTP API endpoint (default: `http://127.0.0.1:8090`). Use a real hostname if behind a proxy or on a remote host.
- `ROUNDHOUSE_TOKEN`: Bearer token for authentication. See token provisioning below. Omit the whole `env` block for a read-only registration — every read tool works without a token (see [Read-Only Mode](#read-only-mode-live-cluster-integration)); action tools then answer `no_token` instead of reaching Roundhouse at all.

## Token Provisioning

Tokens are resolved in this order:
1. **Environment variable**: `ROUNDHOUSE_TOKEN` (highest priority)
2. **File**: `~/.config/roundhouse/token` (mode 600, never argv)
3. **None**: If neither is available, action tools report `no_token` error

**To provision a token:**

```bash
mkdir -p ~/.config/roundhouse
echo "your-bearer-token" > ~/.config/roundhouse/token
chmod 600 ~/.config/roundhouse/token
```

Or set the env var in Claude Code's `.mcp.json`:

```json
"env": {
  "ROUNDHOUSE_TOKEN": "your-bearer-token"
}
```

## The 16-Tool Catalog

### Read Tools (work in any mode: actuate or read-only)

#### 1. `fleet_status`
**Description:** Fleet roster summary: host, mode (actuate/read-only), memory gauge, every unit's rung/port/alias/enablement/on-demand flag, port conflicts, and the current operation.

**Input Schema:**
```json
{
  "type": "object",
  "properties": {},
  "additionalProperties": false
}
```

**Example Call:**
```json
{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "fleet_status", "arguments": {}}}
```

**Example Result (excerpt):**
```json
{
  "http_status": 200,
  "host": "boltzmann",
  "kernel": "6.1.75-npu-port",
  "mode": "actuate",
  "mem": {"total_bytes": 33554432000, "available_bytes": 26843545600},
  "self_port": 8091,
  "n_units": 22,
  "units": [
    {
      "unit": "qwen3.6-coding.service",
      "rung": "READY",
      "port": 8085,
      "alias": "qwen3.6-coding",
      "enabled": true,
      "on_demand": false,
      "retired": false,
      "badges": ["active"],
      "port_conflict": null
    }
  ]
}
```

#### 2. `unit_detail`
**Description:** Full detail for one unit: parameter profile, engine, wrapper, comments, warnings, gate, measured memory history.

**Input Schema:**
```json
{
  "type": "object",
  "properties": {"unit": {"type": "string"}},
  "required": ["unit"],
  "additionalProperties": false
}
```

**Example:** `{"unit": "qwen3.6-coding.service"}`

#### 3. `port_board`
**Description:** Port claim board: every claimed port with claimants, conflict class, and Roundhouse's own port.

**Example Result (excerpt — real captured shape: `ports` is a LIST; entries carry `claims` and `class`):**
```json
{
  "http_status": 200,
  "ports": [
    {
      "port": 8086,
      "claims": [
        {"unit": "llama-task.service", "enabled": true, "rung": "READY", "retired": false, "gate": null},
        {"unit": "llama-server-qwen35-npu.service", "enabled": false, "rung": "STANDBY", "retired": false,
         "gate": {"kind": "kernel", "wants": "6.1.75-npu-port"}}
      ],
      "class": "armed",
      "note": "harmless only while BOTH the disable and the kernel gate hold"
    }
  ],
  "self": {"port": 8090, "claims_by_units": []}
}
```
`class` is one of `active` (≥2 runtime-active claimants — red), `armed` (enabled or gate-blocked pair), `latent` (everything else).

#### 4. `deployments`
**Description:** Deployment records: artifact, engine, param profile, load strategy, roster state, memory, per unit.

#### 5. `routing_config`
**Description:** Live routing config (JSON): logical model entries with api_base, rung, on-demand flag, and the warm hook.

#### 6. `operation_status`
**Description:** Poll a rollout or switch by id: phase, detail, failure, rollback offer, stops, origin.

**Input Schema:**
```json
{
  "type": "object",
  "properties": {"rollout_id": {"type": "string"}},
  "required": ["rollout_id"],
  "additionalProperties": false
}
```

#### 7. `warm_state`
**Description:** Warm queue state: the pending parked request and the last disposition, or nulls.

---

### Action Tools (require `--actuate` + token; surface preflight refusals as structured results)

#### 8. `switch_preview`
**Description:** Preview a turntable switch: fit arithmetic, checks, suggested stops, and the confirm hash for switch_execute.

**Input Schema:**
```json
{
  "type": "object",
  "properties": {
    "target": {"type": "string"},
    "stops": {"type": "array", "items": {"type": "string"}}
  },
  "required": ["target"],
  "additionalProperties": false
}
```

**Example Call:** `{"target": "alt.service", "stops": ["main.service"]}`

#### 9. `switch_execute`
**Description:** Execute a previewed switch; requires the exact confirm from switch_preview (state changed since preview means re-preview).

**Input Schema:**
```json
{
  "type": "object",
  "properties": {
    "target": {"type": "string"},
    "stops": {"type": "array", "items": {"type": "string"}},
    "confirm": {"type": "string"}
  },
  "required": ["target", "confirm"],
  "additionalProperties": false
}
```

**Key:** `confirm` is **required** and **frozen** from the preview result. This enforces the two-step preview-then-execute pattern.

#### 10. `edit_preview`
**Description:** Preview parameter edits to a unit file: planned edits, unified diff, preflight, and the confirm hash for edit_rollout.

**Input Schema:**
```json
{
  "type": "object",
  "properties": {
    "unit": {"type": "string"},
    "edits": {"type": "object", "additionalProperties": {"type": "string"}, "minProperties": 1}
  },
  "required": ["unit", "edits"],
  "additionalProperties": false
}
```

#### 11. `edit_rollout`
**Description:** Apply previewed edits as a rollout; requires the exact confirm from edit_preview; returns a rollout_id to poll.

**Input Schema:**
```json
{
  "type": "object",
  "properties": {
    "unit": {"type": "string"},
    "edits": {"type": "object", "additionalProperties": {"type": "string"}, "minProperties": 1},
    "confirm": {"type": "string"}
  },
  "required": ["unit", "edits", "confirm"],
  "additionalProperties": false
}
```

#### 12. `set_boot`
**Description:** Enable or disable a unit's on-boot strategy (the checkbox); refuses on enable-collision with claimants listed.

**Input Schema:**
```json
{
  "type": "object",
  "properties": {
    "unit": {"type": "string"},
    "enabled": {"type": "boolean"}
  },
  "required": ["unit", "enabled"],
  "additionalProperties": false
}
```

#### 13. `warm`
**Description:** Request warm-up of an on-demand unit by logical alias or unit name (exactly one); may start a consented switch, park, or refuse with fit arithmetic.

**Input Schema:**
```json
{
  "type": "object",
  "properties": {
    "logical": {"type": "string"},
    "unit": {"type": "string"},
    "requester": {"type": "string"}
  },
  "additionalProperties": false,
  "oneOf": [{"required": ["logical"]}, {"required": ["unit"]}]
}
```

**Note:** `requester` defaults to `mcp-<client name from initialize>`.

#### 14. `warm_cancel`
**Description:** Cancel the parked warm request, if any.

**Input Schema:**
```json
{
  "type": "object",
  "properties": {},
  "additionalProperties": false
}
```

#### 15. `operation_rollback`
**Description:** Roll back a failed operation that is offering restore.

**Input Schema:**
```json
{
  "type": "object",
  "properties": {"rollout_id": {"type": "string"}},
  "required": ["rollout_id"],
  "additionalProperties": false
}
```

#### 16. `operation_dismiss`
**Description:** Dismiss a failed operation's restore offer, releasing the slot without restoring.

**Input Schema:**
```json
{
  "type": "object",
  "properties": {"rollout_id": {"type": "string"}},
  "required": ["rollout_id"],
  "additionalProperties": false
}
```

---

## Refusal Reasoning: The Port Collision Example

When an action hits a preflight guard, Roundhouse returns a **structured refusal** with full arithmetic, never a crash. The agent reads the refusal and reasons about what to do next.

**Scenario:** Try to enable `llama-server-gemma4.service` (disabled) when `llama-task.service` (enabled) already claims port `:8086`.

**Tool Call:**
```json
{
  "jsonrpc": "2.0", "id": 1, "method": "tools/call",
  "params": {"name": "set_boot", "arguments": {"unit": "llama-server-gemma4.service", "enabled": true}}
}
```

**Refusal Result (isError: false):**
```json
{
  "http_status": 422,
  "error": "enable_collision",
  "port": 8086,
  "claimants": [
    {"unit": "llama-task.service", "alias": "task-qwen2.5-3b", "port": 8086,
     "rung": "READY", "enabled": true, "gate": null}
  ],
  "detail": "port 8086 is already a boot claim of: llama-task.service (enabled, READY)"
}
```

**Agent's Reasoning:**
1. The error is `enable_collision`, not a crash.
2. The port `:8086` is claimed by two units: one active, one gated (kernelbound).
3. Options: disable the active claimant, or use a different port.
4. The agent can now call `set_boot` with the active unit to disable it, or try a different target.

This pattern applies to all refusals: **every structured refusal (`422`, `409`, `403`, `401`) contains the full state and reasoning, not just an error string.**

---

## Boundaries and Guarantees

**Agents are operators, not loops.** The MCP server:
- Acts **only when an agent calls a tool**; no autonomous reconciler.
- Enforces **preview/confirm two-step** for switch/edit paths; no tool collapses them into one call.
- Respects all existing guards: RETIRED lockout, consent fence, operation slot, enable-collision interlock.
- **Never pre-empts or retries** around a refusal; the agent decides what to do next.
- Grants nothing the token doesn't already grant.

**Read tools work against any mode** (actuate or read-only), with or without a token. **Action tools** (`switch_execute`, `edit_rollout`, `set_boot`, `warm`, `warm_cancel`, `operation_rollback`, `operation_dismiss`) require `--actuate` and a token. In read-only mode, action tools return a structured refusal with `isError: false`, never an exception — `read_only_mode` (403) when a token is presented, `no_token` when none is provisioned (see [Read-Only Mode](#read-only-mode-live-cluster-integration)).

---

## Read-Only Mode: Live Cluster Integration

To query Roundhouse against a live (read-only) instance without actuate privileges, register a read-only variant in `.mcp.json`:

```json
{
  "mcpServers": {
    "roundhouse-live": {
      "command": "python3",
      "args": [
        "/path/to/roundhouse_mcp.py",
        "--url",
        "http://boltzmann.fritz.box:8091"
      ],
      "env": {
        "ROUNDHOUSE_TOKEN": "any-non-empty-value"
      }
    }
  }
}
```

**Read tools work with or without a token:** `fleet_status`, `unit_detail`, `port_board`, `deployments`, `routing_config`, `operation_status`, `warm_state`. Every GET route is unauthenticated by design (MVP2 E8), so a token-less registration is a fully functional read-only client — the `env` block above can be dropped entirely.

**Action tools refuse — but read which refusal you get.** The token check happens in the MCP server, *before* any HTTP request, so the two cases are distinguishable and both arrive with `isError: false`:

| registration | `set_boot` against a read-only Roundhouse | why |
|---|---|---|
| no token provisioned | `{"http_status": null, "error": "no_token", "hint": "..."}` | the wrapper never leaves the process; no HTTP happens |
| any token provisioned | `{"http_status": 403, "error": "read_only_mode", "detail": "launch with --actuate to enable rollouts"}` | Roundhouse's E8 gate checks armed-ness *before* it validates the bearer, so even a wrong token yields the 403 |

```json
{
  "http_status": 403,
  "error": "read_only_mode",
  "detail": "launch with --actuate to enable rollouts"
}
```

Either way the fleet is untouched. If you want the live instance to *say* `read_only_mode` rather than `no_token` — e.g. to prove the server-side gate rather than the client-side one — provision any non-empty token value; it is never accepted, only presented.

This allows agents to safely integrate with live hosts without risk of unintended mutations.

---

## Implementation Notes

- **Stdlib only:** `roundhouse_mcp.py` uses only Python standard library (json, sys, os, argparse, urllib, http.client).
- **No writes:** The MCP server is a pure HTTP client; it makes no subprocess calls, file writes, or systemd state changes.
- **Framing:** JSON-RPC 2.0 over stdio; one message per line.
- **URL:** Defaults to `http://127.0.0.1:8090`; use `--url` to override.
- **Token resolution:** Env var → file → none (in that order).

---

## Example: Multi-Step Workflow

```
# Agent session: Claude Code with roundhouse MCP registered

1. Initialize the MCP connection
   → roundhouse-mcp/initialize negotiates protocol version

2. Read current fleet state
   → fleet_status/fleet_status shows all units, port conflicts, memory

3. Plan a switch: main → alt
   → switch_preview/switch_preview returns fit checks and confirm hash

4. Execute if fit is ok
   → switch_execute/switch_execute with the exact confirm from step 3
   → Returns rollout_id

5. Poll until done
   → operation_status/operation_status(rollout_id) repeats until phase=done

6. If operation fails and offers rollback
   → operation_rollback/operation_rollback(rollout_id) to restore

7. Adjust boot strategy
   → set_boot/set_boot(unit, enabled) for individual on-boot flags

8. Warm up an on-demand model
   → warm/warm(unit=...) to park a consented switch
   → warm_state/warm_state to check what's waiting
   → warm_cancel/warm_cancel to cancel the parked request
```

Every step's result includes `http_status` and complete structured data (or refusal), allowing the agent to reason about what happened and what to try next.

---

## Verifying a Registration: the MCP drill

`mvp1/scripts/mcp-drill.sh` drives `roundhouse_mcp.py` as a real subprocess over real
pipes and asserts every step's tool result. One MCP process holds the whole session,
because framing drift and the `initialize`-derived requester tag only show up across a
multi-message session. The drill adds no actuation of its own: everything it changes,
it changes by calling an MCP tool, which calls a gated HTTP route.

```bash
# read leg only — safe against any instance, no token needed
mvp1/scripts/mcp-drill.sh --url http://boltzmann.fritz.box:8090

# full container drill: read + action + read-only legs
mvp1/scripts/mcp-drill.sh \
  --url http://boltzmann.fritz.box:8090 \
  --token "$(incus exec roundhouse-test -- su -l roundhouse -c 'cat ~/.config/roundhouse/token')" \
  --actions \
  --target llama-server-fake-b.service \
  --stops qwen3.6-coding.service \
  --warm-unit llama-server-fake-b.service \
  --unmarked llama-server-gemma4-q4km.service \
  --read-only-url http://boltzmann.fritz.box:8095
```

Two fixture rules the drill cannot check for you:

- `--unmarked` must name a unit that is **not** marked on-demand **and is currently
  inactive**. The warm route answers `already_warm` before it ever reaches the consent
  fence, so pointing it at a running unit proves nothing.
- `--warm-unit` must be marked on-demand and inactive. The drill fires it while the
  switch still holds the operation slot, so the request parks (202 `queued`) instead of
  starting a second switch — that park is what `warm_state` and `warm_cancel` then act on.

## Error Taxonomy (the isError line)

| situation | isError | result content |
|---|---|---|
| HTTP 2xx and 4xx (including 401/403/422 refusals) | `false` | the response body as JSON with `http_status` injected — refusals are DATA for the agent to reason about |
| HTTP 5xx | `true` | the body (or `{"http_status", "raw"}` for non-JSON) |
| transport failure (connection refused, timeout, DNS) | `true` | `{"error": "roundhouse_unreachable", "url": "...", "detail": "..."}` |
| no token + action tool | `false` | `{"http_status": null, "error": "no_token", "hint": "..."}` — decided client-side, no HTTP request is made |

