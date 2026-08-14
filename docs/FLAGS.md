# Roundhouse CLI Flags — Operator Reference

Roundhouse configuration via command-line arguments (no config file, by design).

## Network Listen Configuration: `--bind`

**Syntax:** `--bind ADDR [--bind ADDR ...]` or `--bind ADDR,ADDR,...`

Specifies which local addresses to listen on. **Repeatable and accepts comma-separated lists.** Default is `0.0.0.0` (all interfaces).

### Details

- **One listener per address**, all sharing one watcher, one engine, one event bus, one operation slot
- **IPv4 and IPv6 supported**: `0.0.0.0`, `127.0.0.1`, `::1`, `::`
- **Address family detected per address**: bind correctly selects IPv4 or IPv6 socket
- **Default unchanged**: if `--bind` is not given, Roundhouse binds `0.0.0.0` (backward compatible)
- **Startup all-or-nothing**: if any declared address cannot be bound, Roundhouse reports *every* failing address with its errno and exits non-zero; nothing is left listening

### Use Case: Reverse Proxy

`--bind 127.0.0.1` puts Roundhouse behind a reverse proxy (e.g., Caddy) with no second, unencrypted door standing open beside it:

```bash
# Roundhouse listens only on localhost
./roundhouse --bind 127.0.0.1 --port 8090

# Caddy proxies from the public internet to Roundhouse
# caddy.conf or Caddyfile: reverse_proxy http://127.0.0.1:8090
```

### Examples

```bash
# Default: listen on all interfaces
./roundhouse

# Single address
./roundhouse --bind 127.0.0.1

# Multiple addresses (repeatable)
./roundhouse --bind 127.0.0.1 --bind 192.168.1.5

# Multiple addresses (comma-separated)
./roundhouse --bind 127.0.0.1,::1

# IPv6 only
./roundhouse --bind ::
```

### Packaged Unit Default

The systemd unit file distributed with Roundhouse keeps the wildcard default (`0.0.0.0`). To restrict to localhost or a specific interface, edit the unit or override via environment:

```bash
# Override in /etc/systemd/system/roundhouse.service.d/override.conf
[Service]
Environment="ROUNDHOUSE_BIND=127.0.0.1"
ExecStart=
ExecStart=/usr/bin/roundhouse --bind $ROUNDHOUSE_BIND --port 8090 --select all
```

Then reload and restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart roundhouse
```

---

## Peer Reachability Watch: `--peer`

**Syntax:** `--peer NAME=HOST:PORT [--peer NAME=HOST:PORT ...]`

Declares fleet peers to watch via TCP connect probes. No action is taken on peer state; this is sensing only.

### Details

- **Repeatable**: declare as many peers as needed
- **Format**: `NAME=HOST:PORT` where `HOST` is a DNS name or IP, `PORT` is numeric
- **Probe frequency**: once every 60 seconds (hardcoded, not configurable; override available via `--peer-interval` for testing)
- **Probe behavior**: TCP connect with 2 s timeout, re-resolves `HOST` every probe (so roaming laptops return on a different address)
- **Hysteresis**: `up` on first success; `down` only after two consecutive failures; `unknown` until first probe completes
- **No HTTP**: probes are TCP only; port is an inference server port, health endpoint, SSH, etc. — Roundhouse does not knock
- **No ICMP**: requires no special privileges
- **Meaning**: *reachable*, not *healthy* or *serving*. A TCP connect proves listening; it proves nothing about the fleet behind it

### Surfaces

- **HTTP API**: `GET /api/peers` (unauthenticated) returns peers array with state, transition times, and failure counters
- **SSE events**: `peer` event published *on transition only* (no per-minute chatter if state is unchanged)
- **MCP tool**: `peer_status` (read-only) exposes the same data
- **UI strip**: compact display of peer names and states (non-interactive; labels say "reachable", never "healthy")

### Examples

```bash
# Declare two peers
./roundhouse --peer ampere=ampere.fritz.box:8099 --peer dirac=dirac.fritz.box:22

# Declare one peer (e.g., for a laptop that roams)
./roundhouse --peer remote-gpu=gpu.example.com:8090

# No peers (default; peer watch is dormant)
./roundhouse
```

### Peer State Transitions

```
unknown --[first probe finishes: success]--> up
unknown --[first probe finishes: failure]--> unknown (waits for next probe)
unknown --[two consecutive failures]--> down
up --[one failure]--> up (single packet loss is tolerated)
up --[two consecutive failures]--> down
down --[one success]--> up (returns promptly when available)
```

### Testing Peer Drill

The `scripts/peer-drill.sh` container test verifies peer watching works correctly:

```bash
cd mvp1
bash scripts/peer-drill.sh
# PASS: peer1 initial state is 'up'
# PASS: peer2 initial state is 'down'
# PASS: peer1 transitioned to 'down'
# PASS: peer1 returned to 'up'
# Summary: PASS: 4
```

---

## Port Configuration: `--port`

**Syntax:** `--port PORT`

Specifies the TCP port Roundhouse listens on. Default is 8090. All listeners (declared via `--bind`) share this port.

**Example:**

```bash
./roundhouse --bind 127.0.0.1 --port 9999
# Listens on http://127.0.0.1:9999
```

---

## Unit Selection: `--select`

**Syntax:** `--select all` or `--select GLOB [--select GLOB ...]`

Which systemd units to manage. `all` includes every unit named `*-llama.service` or `*-llamafile.service`.

---

## Actuation Enablement: `--actuate`

**Syntax:** `--actuate`

Enables edit, rollout, and switch operations. Without this flag, Roundhouse runs in read-only mode.

---

## Environment Variables

- `ROUNDHOUSE_TOKEN`: Bearer token for authenticated endpoints (when `--actuate` is enabled)
- `ROUNDHOUSE_BIND`: Override listen address (used in systemd override configs)

