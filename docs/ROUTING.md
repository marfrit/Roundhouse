# Roundhouse Warm-up Routing Configuration

## 1. What Roundhouse Generates

Roundhouse serves two unauthenticated endpoints for external systems to discover and warm up models:

- **`/api/routing-config`** (text/yaml)
  - Complete YAML document: header comment block + `model_list:` array
  - Header includes: `generated-by:`, `generated-at:`, `warm-hook:` (the cold-path entry point)
  - One entry per model: `model_name`, `litellm_params`, `model_info` (Roundhouse-standard shape)

- **`/api/routing-config.json`** (application/json)
  - JSON twin of the YAML: same entries, same null-omission policy, same metadata

Both are **live reads** — pull any time to get current fleet state. No authentication required. Generation incurs no file writes, no subprocess, no git operations.

### Inclusion Rules

A model appears in `model_list` when:

- **Hot always:** `rung ∈ {READY, BUSY}` — the unit is actively serving
- **Cold only if marked on-demand:** `on_demand == true` AND `rung ∈ {OFF, STARTING, LOADING}` — waiting for the warm hook
- **Never:** `rung ∈ {STANDBY, FAILED}` or `retired == true`

An unmarked unit that happens to be OFF does not appear, even if it is capable of serving. This enforces the contract: only units explicitly marked may be warm-started by external systems.

### Namespacing (`model_name`)

Each entry's `model_name` is `{host}-{logical_alias}`, e.g., `boltzmann-qwen3.6-coding`. The bare logical name is in `model_info.logical`. This prefix exists because units with the same alias can serve on different hosts (e.g., `qwen3.6-coding` on both boltzmann and ampere); without the namespace, the second host's fragment would overwrite the first's.

Example: a fragment from boltzmann carries `model_name: boltzmann-qwen3.6-coding`; one from ampere carries `model_name: ampere-qwen3.6-coding`. Both can coexist in an aggregated config.

## 2. Wiring LiteLLM (hossenfelder :4000)

LiteLLM on hossenfelder loads a static configuration file (never hot-reloaded). To use Roundhouse-discovered models:

1. **Fetch the fragment** (e.g., via systemd timer):
   ```bash
   curl -fsS http://boltzmann.fritz.box:8090/api/routing-config -o /etc/litellm/fragments/boltzmann.yaml
   ```

2. **Merge into the served config:**
   The fragment is a complete `model_list:` document. Merge by concatenating lists:
   ```python
   # pseudo-code
   merged = yaml.load(primary_config)
   fragment = yaml.load(boltzmann_fragment)
   merged['model_list'].extend(fragment['model_list'])
   serve(merged)
   ```

3. **Reload LiteLLM** (signal or API call, depends on LiteLLM setup).

### ⚠️ WARNING: Namespace Your Fragments

**Never point two hosts' fragments at the same bare `model_name` without a namespace prefix.** Example:

```yaml
# ❌ WRONG: boltzmann and ampere both send model_name: qwen3.6-coding
# The second fragment silently overwrites the first
- model_name: qwen3.6-coding
  litellm_params: { model: openai/qwen3.6-coding, api_base: http://boltzmann.fritz.box:8085/v1 }
- model_name: qwen3.6-coding
  litellm_params: { model: openai/qwen3.6-coding, api_base: http://ampere.fritz.box:8085/v1 }

# ✓ CORRECT: Roundhouse includes the host prefix automatically
- model_name: boltzmann-qwen3.6-coding
  model_info: { logical: qwen3.6-coding, host: boltzmann, ... }
- model_name: ampere-qwen3.6-coding
  model_info: { logical: qwen3.6-coding, host: ampere, ... }
```

The prefix is generated automatically; you do not add it. If you create a custom entry for another system, include its hostname.

## 3. Wiring the Bespoke llm-proxy

For the internal Roundhouse proxy (not LiteLLM):

1. **Fetch the JSON twin:**
   ```bash
   curl -fsS http://boltzmann.fritz.box:8090/api/routing-config.json -o /tmp/routes.json
   ```

2. **Map entries to endpoints:**
   ```python
   # For each model in model_list:
   for model in routes['model_list']:
       name = model['model_name']
       api_base = model['litellm_params']['api_base']  # e.g., http://boltzmann.fritz.box:8085/v1
       on_demand = model['model_info'].get('on_demand', False)
       rung = model['model_info'].get('rung')
       
       # Store mapping: name -> api_base
   ```

3. **Handle cold models:**
   If `on_demand == true` and `rung == 'OFF'`, the model is waiting for warm-up. Before forwarding a request:
   - POST `/api/warm` (see section 4 below) with the model's logical name
   - Wait for the model to reach READY
   - Then forward the request

## 4. The Warm Hook: Cold-path Recipe

The warm hook is documented in the routing-config header comment:

```
# warm-hook: POST http://boltzmann.fritz.box:8090/api/warm
```

### Request Body

```json
{
  "logical": "qwen3.6-coding"
}
```

or

```json
{
  "unit": "qwen3.6-coding.service"
}
```

Either `logical` or `unit`, not both. See section 4.5 of the MVP5 spec for the full error matrix.

### Response Codes

| Code | Status | Meaning |
|------|--------|---------|
| 200 | `already_warm` | Unit is already READY or becoming ready (rung ∈ STARTING/LOADING/READY) |
| 200 | `already_queued` | This unit is already pending warm (idempotent) |
| 202 | `{"rollout_id"}` | Warm-up started; monitor `/api/rollouts/{id}` for progress |
| 202 | `{"queued": true}` | Queued behind another operation; will fire when slot frees (≤ 3 s) |
| 404 | `unknown_unit` / `unknown_alias` | Unit/alias does not exist or is retired |
| 409 | `warm_queue_full` | Different warm already queued; retry after cancel |
| 422 | `not_on_demand` | Unit is not marked `# roundhouse: on-demand` — consent denied |
| 422 | `consent_unfittable` | Warming would require stopping unmarked (non-consenting) units |
| 422 | `ambiguous_alias` | Logical name matches multiple units (should not happen in practice) |

### Cold-path Algorithm

```python
# Pseudo-code for llm-proxy or external system

def call_model(model_name, request):
    # Check if cold
    model = routing_config[model_name]
    if model['model_info']['rung'] != 'OFF':
        # Already hot or warming; call directly
        return forward_to_api_base(model['litellm_params']['api_base'], request)
    
    if not model['model_info'].get('on_demand'):
        # Not marked; cannot warm
        return error_403('Model not available and not marked for warm-up')
    
    # Try to warm
    while True:
        warm_resp = POST /api/warm with logical=model_name
        
        if warm_resp.status == 200 and warm_resp['status'] == 'already_warm':
            # Now ready; call it
            break
        elif warm_resp.status == 202:
            # Warming started; poll the rollout or /api/warm until READY
            while True:
                state = GET /api/warm
                if state['pending']['rung'] == 'READY':  # or poll rollout
                    break
                sleep(0.1)
            break
        elif warm_resp.status == 202 and warm_resp.get('queued'):
            # Parked; poll until it fires (or GET /api/warm shows it started)
            while True:
                state = GET /api/warm
                if not state.get('pending'):  # Queue fired and cleared
                    break
                sleep(0.1)
            break
        elif warm_resp.status == 409 or warm_resp.status == 422:
            # Queue full, not marked, or unfittable: surface to caller
            return error_service_unavailable(f'Cannot warm {model_name}: {warm_resp}')
        else:
            return error_500(f'Unexpected warm response: {warm_resp}')
    
    # Now forward the original request
    return forward_to_api_base(model['litellm_params']['api_base'], request)
```

Do not spin on 409 `warm_queue_full`; surface it to the caller. Do not retry 422 errors; they are policy violations, not transient failures.

## 5. Token Provisioning

The warm hook requires bearer-token authentication. Roundhouse writes a single token file on the host:

- **Location:** `~/.config/roundhouse/token` (mode 0600; owned by the roundhouse user)
- **Content:** a single line, the bearer token
- **Usage:** include in the `Authorization: Bearer <token>` header

To provision the proxy:

1. Copy the token from the Roundhouse host to the proxy host's secret store:
   ```bash
   # On boltzmann (Roundhouse host)
   cat ~/.config/roundhouse/token  # e.g., "abc123xyz"
   
   # On hossenfelder (proxy host) — store in your system's secret mechanism
   # Examples:
   echo "abc123xyz" > /opt/llm-proxy/.token  # if llm-proxy reads from a file
   # Or populate environment var / k8s secret / vault / etc.
   ```

2. Include it in warm requests:
   ```bash
   curl -X POST \
     -H "Authorization: Bearer abc123xyz" \
     -H "Content-Type: application/json" \
     -d '{"logical": "qwen3.6-coding"}' \
     http://boltzmann.fritz.box:8090/api/warm
   ```

3. Optional: include `X-Roundhouse-Requester` header to label the warm source:
   ```bash
   -H "X-Roundhouse-Requester: llm-proxy"
   ```
   (Value is sanitized and logged; useful for debugging and audit trails.)

### Token Rotation

1. **Generate a new token** on the Roundhouse host.
2. **Replace** `~/.config/roundhouse/token` with the new value.
3. **Restart Roundhouse** (systemctl restart):
   ```bash
   systemctl --user restart roundhouse.service
   ```
4. **Update the proxy's secret** with the new token.
5. **Restart the proxy** or reload its config.

Old tokens become invalid after the restart; no gradual transition window.

## 6. Marking a Unit On-Demand

To make a unit eligible for warm-up, add a marker comment anywhere in its systemd unit file:

```ini
[Unit]
Description=Qwen 3.6 Coding (27B MoE)

[Service]
# roundhouse: on-demand
Type=simple
ExecStart=/usr/local/bin/llama-server --model /var/lib/models/qwen36-27b-a3b-coder.gguf --port 8085
```

**Important:** Roundhouse **parses units once at startup**. Adding or removing the marker requires:

```bash
systemctl --user restart roundhouse.service
```

The marker means both:
- **May be started autonomously** by warm requests (no human intervention needed)
- **May be stopped autonomously** to free memory for other models (symmetric consent)

The always-on trio (`llama-server`, `llama-embed`, `llama-task`) should **remain unmarked** — they are never stopped, and warm requests are forbidden for them.

### Alternative Syntaxes

Both of these are recognized (same substring mechanism as roundhouse's manage/ignore directives):

```
# roundhouse: on-demand
; roundhouse: on-demand
```

The marker can appear on any line, any position (though convention places it near the [Service] section for readability).

## 7. Operational Notes

### Queue Semantics

The warm queue has **depth 1**:
- While a warm is pending, all new warm requests for any unit go to the queue (not the slot)
- The parked warm fires within ≤ 3 seconds after the slot becomes free
- A restart clears the queue (in-memory only; no persistence)

### Slot Conflicts

If a human initiates a switch while a warm is queued:
- The human switch claims the slot immediately
- The queued warm remains parked and fires after the human switch completes
- No cancellation; queues are FIFO (human ops always get priority, but queue survives)

If a warm is queued and a human initiates a *different* warm (different target), use `POST /api/warm/cancel` first:

```bash
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  http://boltzmann.fritz.box:8090/api/warm/cancel
```

Response: `200 {"cancelled": true, "unit": "qwen3.6-coding.service"}` or `404 {"error": "no_pending"}`.

### Failed Warm

If a warm-up fails (preflight or consent check after queuing):
- The slot remains held (the queue does not auto-retry)
- A human must `POST /api/warm/cancel` or `POST /api/rollouts/<failed_id>/dismiss` to clear it
- Then the proxy can retry

### Advertised Host

By default, Roundhouse reports its hostname as seen by `os.uname()[1]`. On a container or VM, this may not resolve on the LAN:

```bash
# Inside container (hostname might be random):
$ hostname
e3f9a2c1b5d2

# Not resolvable from hossenfelder; warm-hook won't work.
```

Pass `--advertise-host` to override:

```bash
# In roundhouse.service ExecStart (or container run command):
ExecStart=/usr/bin/python3 roundhouse.py --serve --actuate --port 8090 --advertise-host boltzmann.fritz.box
```

This affects the `warm-hook` header and all URLs in `model_info.host`/`litellm_params.api_base`.

### Monitoring

- **Check routing-config:** `curl http://boltzmann.fritz.box:8090/api/routing-config`
- **Check pending warm:** `curl http://boltzmann.fritz.box:8090/api/warm`
- **Monitor warm execution:** Watch the UI stepper (`/api/events` SSE) or poll the rollout (`GET /api/rollouts/{id}`)

---

**Last Updated:** 2026-08-13 (MVP5 Specification)
