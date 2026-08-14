#!/usr/bin/env python3
"""Roundhouse MCP server — stdio JSON-RPC wrapper over the HTTP API. Pure client:
no subprocess, no writes, no state; every action rides the existing gated routes."""

import sys
import os
import json
import argparse
import urllib.parse
import http.client


# ===== SECTION 1: HTTP CLIENT (the only socket owner; one construction site) =====

def call_roundhouse(method, path, body=None, token=None, requester=None) -> tuple:
    """Make an HTTP call to Roundhouse.

    Returns (status_code, response_body).
    status_code is None on transport failure; response_body is dict (parsed JSON) or str (raw text).
    On transport failure, response_body is the exception string.
    """
    try:
        # Parse URL (already validated in main)
        parsed = urllib.parse.urlsplit(state['url'])
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)

        # Create connection
        conn = http.client.HTTPConnection(host, port, timeout=5)
        try:
            conn.connect()
            # Raise timeout to 30s after connect
            if conn.sock:
                conn.sock.settimeout(30.0)

            # Build headers
            headers = {}
            if method == 'POST':
                headers['Content-Type'] = 'application/json'

            if token:
                headers['Authorization'] = f'Bearer {token}'

            if requester:
                headers['X-Roundhouse-Requester'] = requester

            # Make request
            body_bytes = json.dumps(body).encode('utf-8') if body else None
            conn.request(method, path, body_bytes, headers)

            # Get response
            response = conn.getresponse()
            status = response.status
            response_body = response.read()

            # Try to decode as JSON
            try:
                return (status, json.loads(response_body))
            except json.JSONDecodeError:
                # Return as raw text
                return (status, response_body.decode('utf-8', errors='replace'))
        finally:
            conn.close()
    except (ConnectionRefusedError, http.client.HTTPException, OSError, TimeoutError) as e:
        return (None, str(e))


# ===== SECTION 2: RESULT SHAPING (I2 doctrine; fleet_status is the one shaper) =====

def shape_result(status, payload) -> tuple:
    """Shape HTTP response into result dict and error flag.

    Returns (result_dict, is_error_bool).
    """
    if status is None:
        # Transport failure
        return (
            {
                'error': 'roundhouse_unreachable',
                'url': state['url'],
                'detail': payload
            },
            True
        )

    # Build base result. `http_status` is seeded first so it reads at the top of the
    # pretty-printed block, and re-written after the body is merged in because I2 says
    # the INJECTION wins on key collision (no route emits the key today; the rule is
    # documented for the one that someday might).
    result = {'http_status': status}

    if isinstance(payload, dict):
        # JSON response
        result.update(payload)
    else:
        # Raw text response (recon 2: error_404/error_405 are text/plain)
        result['raw'] = payload if payload is not None else ''

    result['http_status'] = status

    # Determine error flag
    is_error = status >= 500

    return (result, is_error)


def shape_fleet_status(snapshot) -> dict:
    """Shape fleet_status snapshot per I5 keep-list.

    Keeps: http_status, host, kernel, now, mode, mem, sources, self_port, self_unit, n_units, units, operation
    Drops from units: description, detail, roster, since, start_ts_mono, stale, sensed_at,
                      active_state, sub_state, n_restarts, port_source, model_file, quant_hint, ctx, mem, gate
    Keeps in units: unit, rung, port, alias, enabled, on_demand, retired, strategy_note, badges
    Port_conflict only when non-null.
    """
    result = {
        k: snapshot[k]
        for k in ['http_status', 'host', 'kernel', 'now', 'mode', 'mem', 'sources', 'self_port', 'self_unit']
        if k in snapshot
    }

    # Keep-list for unit fields
    keep_unit_fields = {
        'unit', 'rung', 'port', 'alias', 'enabled', 'on_demand', 'retired', 'strategy_note', 'badges'
    }

    # Process units
    shaped_units = []
    for unit in snapshot.get('units', []):
        shaped_unit = {k: v for k, v in unit.items() if k in keep_unit_fields}
        # Include port_conflict only if non-null
        if unit.get('port_conflict'):
            shaped_unit['port_conflict'] = unit['port_conflict']
        shaped_units.append(shaped_unit)

    result['n_units'] = len(shaped_units)
    result['units'] = shaped_units

    # I5: `operation` is the snapshot's `rollout` value verbatim (already the compact
    # rollout_public_record, or null). take_snapshot emits the key as `rollout`; reading
    # a key named `operation` here made every fleet_status report "no operation running".
    result['operation'] = snapshot.get('rollout')

    return result


# ===== SECTION 3: TOOL REGISTRY (single source of truth; data, not code) =====

TOOLS = {
    'fleet_status': {
        'description': 'Fleet roster summary: host, mode (actuate/read-only), memory gauge, every unit\'s rung/port/alias/enablement/on-demand flag, port conflicts, and the current operation.',
        'schema': {
            'type': 'object',
            'properties': {},
            'additionalProperties': False
        },
        'method': 'GET',
        'path': '/api/units',
        'action': False,
        'body_args': (),
        'shaper': shape_fleet_status,
        'send_requester': False,
    },
    'unit_detail': {
        'description': 'Full detail for one unit: parameter profile, engine, wrapper, comments, warnings, gate, measured memory history.',
        'schema': {
            'type': 'object',
            'properties': {
                'unit': {'type': 'string'}
            },
            'required': ['unit'],
            'additionalProperties': False
        },
        'method': 'GET',
        'path': '/api/units/{unit}',
        'action': False,
        'body_args': (),
        'shaper': None,
        'send_requester': False,
    },
    'port_board': {
        'description': 'Port claim board: every claimed port with claimants, conflict class, and Roundhouse\'s own port.',
        'schema': {
            'type': 'object',
            'properties': {},
            'additionalProperties': False
        },
        'method': 'GET',
        'path': '/api/ports',
        'action': False,
        'body_args': (),
        'shaper': None,
        'send_requester': False,
    },
    'deployments': {
        'description': 'Deployment records: artifact, engine, param profile, load strategy, roster state, memory, per unit.',
        'schema': {
            'type': 'object',
            'properties': {},
            'additionalProperties': False
        },
        'method': 'GET',
        'path': '/api/deployments',
        'action': False,
        'body_args': (),
        'shaper': None,
        'send_requester': False,
    },
    'routing_config': {
        'description': 'Live routing config (JSON): logical model entries with api_base, rung, on-demand flag, and the warm hook.',
        'schema': {
            'type': 'object',
            'properties': {},
            'additionalProperties': False
        },
        'method': 'GET',
        'path': '/api/routing-config.json',
        'action': False,
        'body_args': (),
        'shaper': None,
        'send_requester': False,
    },
    'operation_status': {
        'description': 'Poll a rollout or switch by id: phase, detail, failure, rollback offer, stops, origin.',
        'schema': {
            'type': 'object',
            'properties': {
                'rollout_id': {'type': 'string'}
            },
            'required': ['rollout_id'],
            'additionalProperties': False
        },
        'method': 'GET',
        'path': '/api/rollouts/{rollout_id}',
        'action': False,
        'body_args': (),
        'shaper': None,
        'send_requester': False,
    },
    'warm_state': {
        'description': 'Warm queue state: the pending parked request and the last disposition, or nulls.',
        'schema': {
            'type': 'object',
            'properties': {},
            'additionalProperties': False
        },
        'method': 'GET',
        'path': '/api/warm',
        'action': False,
        'body_args': (),
        'shaper': None,
        'send_requester': False,
    },
    'switch_preview': {
        'description': 'Preview a turntable switch: fit arithmetic, checks, suggested stops, and the confirm hash for switch_execute.',
        'schema': {
            'type': 'object',
            'properties': {
                'target': {'type': 'string'},
                'stops': {
                    'type': 'array',
                    'items': {'type': 'string'}
                }
            },
            'required': ['target'],
            'additionalProperties': False
        },
        'method': 'POST',
        'path': '/api/switch/preview',
        'action': True,
        'body_args': ('target', 'stops'),
        'shaper': None,
        'send_requester': False,
    },
    'switch_execute': {
        'description': 'Execute a previewed switch; requires the exact confirm from switch_preview (state changed since preview means re-preview).',
        'schema': {
            'type': 'object',
            'properties': {
                'target': {'type': 'string'},
                'stops': {
                    'type': 'array',
                    'items': {'type': 'string'}
                },
                'confirm': {'type': 'string'}
            },
            'required': ['target', 'confirm'],
            'additionalProperties': False
        },
        'method': 'POST',
        'path': '/api/switch',
        'action': True,
        'body_args': ('target', 'stops', 'confirm'),
        'shaper': None,
        'send_requester': False,
    },
    'edit_preview': {
        'description': 'Preview parameter edits to a unit file: planned edits, unified diff, preflight, and the confirm hash for edit_rollout.',
        'schema': {
            'type': 'object',
            'properties': {
                'unit': {'type': 'string'},
                'edits': {
                    'type': 'object',
                    'additionalProperties': {'type': 'string'},
                    'minProperties': 1
                }
            },
            'required': ['unit', 'edits'],
            'additionalProperties': False
        },
        'method': 'POST',
        'path': '/api/units/{unit}/edit',
        'action': True,
        'body_args': ('edits',),
        'shaper': None,
        'send_requester': False,
    },
    'edit_rollout': {
        'description': 'Apply previewed edits as a rollout; requires the exact confirm from edit_preview; returns a rollout_id to poll.',
        'schema': {
            'type': 'object',
            'properties': {
                'unit': {'type': 'string'},
                'edits': {
                    'type': 'object',
                    'additionalProperties': {'type': 'string'},
                    'minProperties': 1
                },
                'confirm': {'type': 'string'}
            },
            'required': ['unit', 'edits', 'confirm'],
            'additionalProperties': False
        },
        'method': 'POST',
        'path': '/api/units/{unit}/rollout',
        'action': True,
        'body_args': ('edits', 'confirm'),
        'shaper': None,
        'send_requester': False,
    },
    'set_boot': {
        'description': 'Enable or disable a unit\'s on-boot strategy (the checkbox); refuses on enable-collision with claimants listed.',
        'schema': {
            'type': 'object',
            'properties': {
                'unit': {'type': 'string'},
                'enabled': {'type': 'boolean'}
            },
            'required': ['unit', 'enabled'],
            'additionalProperties': False
        },
        'method': 'POST',
        'path': '/api/units/{unit}/enablement',
        'action': True,
        'body_args': ('enabled',),
        'shaper': None,
        'send_requester': False,
    },
    'warm': {
        'description': 'Request warm-up of an on-demand unit by logical alias or unit name (exactly one); may start a consented switch, park, or refuse with fit arithmetic.',
        'schema': {
            'type': 'object',
            'properties': {
                'logical': {'type': 'string'},
                'unit': {'type': 'string'},
                'requester': {'type': 'string'}
            },
            'additionalProperties': False,
            'oneOf': [
                {'required': ['logical']},
                {'required': ['unit']}
            ]
        },
        'method': 'POST',
        'path': '/api/warm',
        'action': True,
        'body_args': ('logical', 'unit'),
        'shaper': None,
        'send_requester': True,
    },
    'warm_cancel': {
        'description': 'Cancel the parked warm request, if any.',
        'schema': {
            'type': 'object',
            'properties': {},
            'additionalProperties': False
        },
        'method': 'POST',
        'path': '/api/warm/cancel',
        'action': True,
        'body_args': (),
        'shaper': None,
        'send_requester': False,
    },
    'operation_rollback': {
        'description': 'Roll back a failed operation that is offering restore.',
        'schema': {
            'type': 'object',
            'properties': {
                'rollout_id': {'type': 'string'}
            },
            'required': ['rollout_id'],
            'additionalProperties': False
        },
        'method': 'POST',
        'path': '/api/rollouts/{rollout_id}/rollback',
        'action': True,
        'body_args': (),
        'shaper': None,
        'send_requester': False,
    },
    'operation_dismiss': {
        'description': 'Dismiss a failed operation\'s restore offer, releasing the slot without restoring.',
        'schema': {
            'type': 'object',
            'properties': {
                'rollout_id': {'type': 'string'}
            },
            'required': ['rollout_id'],
            'additionalProperties': False
        },
        'method': 'POST',
        'path': '/api/rollouts/{rollout_id}/dismiss',
        'action': True,
        'body_args': (),
        'shaper': None,
        'send_requester': False,
    },
    'peer_status': {
        'description': 'Peer reachability watch: declared peers with up/down/unknown state, since, last probe, and last error — reachable means a TCP connect succeeded, not healthy.',
        'schema': {
            'type': 'object',
            'properties': {},
            'additionalProperties': False
        },
        'method': 'GET',
        'path': '/api/peers',
        'action': False,
        'body_args': (),
        'shaper': None,
        'send_requester': False,
    },
    'fleet_roster': {
        'description': 'Fleet roster across this host and its declared fleet peers: every unit tagged with its source host, plus per-peer mode, fetch time, and staleness. Reads only — peer units cannot be actuated from here.',
        'schema': {
            'type': 'object',
            'properties': {},
            'additionalProperties': False
        },
        'method': 'GET',
        'path': '/api/fleet',
        'action': False,
        'body_args': (),
        'shaper': None,
        'send_requester': False,
    },
}


def validate_args(schema, args) -> str or None:
    """Validate arguments against a schema.

    Returns error message string or None if valid.
    Checks: type, required, additionalProperties: false, minProperties.
    """
    if not isinstance(args, dict):
        return 'arguments must be an object'

    # Check for additionalProperties: false
    if schema.get('additionalProperties') is False:
        allowed_keys = set(schema.get('properties', {}).keys())
        for key in args.keys():
            if key not in allowed_keys:
                return f'unknown property: {key}'

    # Check required fields
    for req_key in schema.get('required', []):
        if req_key not in args:
            return f'missing required property: {req_key}'

    # Check minProperties
    if 'minProperties' in schema:
        if len(args) < schema['minProperties']:
            return f'expected at least {schema["minProperties"]} properties'

    # Check types
    properties = schema.get('properties', {})
    for key, value in args.items():
        if key not in properties:
            continue
        prop_schema = properties[key]
        expected_type = prop_schema.get('type')

        if expected_type == 'string':
            if not isinstance(value, str):
                return f'property {key}: expected string, got {type(value).__name__}'
        elif expected_type == 'boolean':
            if not isinstance(value, bool):
                return f'property {key}: expected boolean, got {type(value).__name__}'
        elif expected_type == 'object':
            if not isinstance(value, dict):
                return f'property {key}: expected object, got {type(value).__name__}'
            # Check additionalProperties on nested object
            if prop_schema.get('additionalProperties') is not False:
                # Type is open, just check minProperties if present
                if 'minProperties' in prop_schema:
                    if len(value) < prop_schema['minProperties']:
                        return f'property {key}: expected at least {prop_schema["minProperties"]} properties'
            else:
                # Closed schema
                for nested_key in value.keys():
                    if nested_key not in prop_schema.get('properties', {}):
                        return f'property {key}: unknown nested property {nested_key}'
            # Check nested minProperties
            if 'minProperties' in prop_schema:
                if len(value) < prop_schema['minProperties']:
                    return f'property {key}: expected at least {prop_schema["minProperties"]} properties'
        elif expected_type == 'array':
            if not isinstance(value, list):
                return f'property {key}: expected array, got {type(value).__name__}'
            # Check array items type
            items_schema = prop_schema.get('items', {})
            items_type = items_schema.get('type')
            if items_type:
                for i, item in enumerate(value):
                    if items_type == 'string':
                        if not isinstance(item, str):
                            return f'property {key}[{i}]: expected string, got {type(item).__name__}'
                    elif items_type == 'boolean':
                        if not isinstance(item, bool):
                            return f'property {key}[{i}]: expected boolean, got {type(item).__name__}'

    return None


# ===== SECTION 4: JSON-RPC LOOP (I11 framing; initialize/tools list+call/ping) =====

# Global connection state
state = {'url': '', 'client_name': 'unknown'}

SUPPORTED_VERSIONS = ('2025-06-18', '2025-03-26', '2024-11-05')


def resolve_token(args_token=None):
    """Resolve the bearer token from env or file.

    Returns the token string or None if not found.
    """
    # Check env first
    env_token = os.environ.get('ROUNDHOUSE_TOKEN')
    if env_token:
        return env_token

    # Try file
    token_path = os.path.expanduser('~/.config/roundhouse/token')
    try:
        with open(token_path, 'r') as f:
            token = f.read().strip()
            if token:
                return token
    except (FileNotFoundError, IOError):
        pass

    return None


def sanitize_requester(name):
    """Sanitize requester name per H7 charset: [A-Za-z0-9._@ -], truncate 64."""
    if not name:
        return ''
    # Keep only allowed characters
    allowed = set('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._@ -')
    sanitized = ''.join(c for c in name if c in allowed)
    return sanitized[:64]


def handle_message(msg, conn_state) -> dict or None:
    """Handle one JSON-RPC message.

    Returns response dict or None (for notifications).
    conn_state is the shared state dict with 'url' and 'client_name'.
    """
    global state
    state = conn_state

    # Validate JSON-RPC structure. I11: the batch check comes FIRST — a top-level array
    # is well-formed JSON, so answering it -32700 "parse error" would misreport why it
    # was refused (and the check was unreachable below the isinstance(dict) guard).
    if isinstance(msg, list):
        return {
            'jsonrpc': '2.0',
            'id': None,
            'error': {
                'code': -32600,
                'message': 'batch not supported'
            }
        }

    if not isinstance(msg, dict):
        return {
            'jsonrpc': '2.0',
            'id': None,
            'error': {
                'code': -32700,
                'message': 'parse error'
            }
        }

    msg_id = msg.get('id')
    method = msg.get('method')
    # JSON-RPC 2.0: a request without an id is a NOTIFICATION and must never be
    # answered — for KNOWN methods too, not just the notifications/* namespace
    # (MVP6 review F6: known-method notifications were answered with id: null).
    if 'id' not in msg:
        # Still honor side effects that matter (none today: initialize without
        # an id is not something real MCP clients send; state capture is skipped).
        return None
    # `params: null` is legal JSON-RPC; `.get('params', {})` returns None for it, and
    # every downstream membership test would then raise -> -32603. I11 says tolerate.
    params = msg.get('params') or {}

    try:
        # Handle initialize
        if method == 'initialize':
            # Capture client name
            if 'clientInfo' in params and 'name' in params['clientInfo']:
                state['client_name'] = params['clientInfo']['name']

            # Negotiate protocol version
            requested_version = params.get('protocolVersion')
            negotiated_version = requested_version if requested_version in SUPPORTED_VERSIONS else '2024-11-05'

            return {
                'jsonrpc': '2.0',
                'id': msg_id,
                'result': {
                    'protocolVersion': negotiated_version,
                    'capabilities': {'tools': {}},
                    'serverInfo': {
                        'name': 'roundhouse-mcp',
                        'version': '8.0'
                    }
                }
            }

        # Handle notifications/initialized
        elif method == 'notifications/initialized':
            return None  # No response for notifications

        # Handle ping
        elif method == 'ping':
            return {
                'jsonrpc': '2.0',
                'id': msg_id,
                'result': {}
            }

        # Handle tools/list
        elif method == 'tools/list':
            tools_list = [
                {
                    'name': name,
                    'description': row['description'],
                    'inputSchema': row['schema']
                }
                for name, row in TOOLS.items()
            ]
            return {
                'jsonrpc': '2.0',
                'id': msg_id,
                'result': {
                    'tools': tools_list
                }
            }

        # Handle tools/call
        elif method == 'tools/call':
            tool_name = params.get('name')
            arguments = params.get('arguments', {})

            # Check tool exists
            if tool_name not in TOOLS:
                return {
                    'jsonrpc': '2.0',
                    'id': msg_id,
                    'error': {
                        'code': -32602,
                        'message': f'unknown tool: {tool_name}'
                    }
                }

            tool_row = TOOLS[tool_name]

            # Validate arguments
            validation_error = validate_args(tool_row['schema'], arguments)
            if validation_error:
                return {
                    'jsonrpc': '2.0',
                    'id': msg_id,
                    'error': {
                        'code': -32602,
                        'message': validation_error
                    }
                }

            # Check token for action tools
            if tool_row['action']:
                token = resolve_token()
                if not token:
                    result = {
                        'http_status': None,
                        'error': 'no_token',
                        'hint': 'write the bearer token to ~/.config/roundhouse/token or set ROUNDHOUSE_TOKEN'
                    }
                    return {
                        'jsonrpc': '2.0',
                        'id': msg_id,
                        'result': {
                            'content': [{'type': 'text', 'text': json.dumps(result, indent=2, ensure_ascii=False)}],
                            'isError': False
                        }
                    }
            else:
                token = None

            # Build requester if needed
            requester = None
            if tool_row['send_requester']:
                # Use explicit requester argument if provided, else use default
                explicit_requester = arguments.get('requester')
                if explicit_requester:
                    requester = sanitize_requester(explicit_requester)
                else:
                    # Default: mcp-<client_name>
                    requester = f'mcp-{sanitize_requester(state["client_name"])}'

            # Build path with substitutions
            path = tool_row['path']
            if '{unit}' in path and 'unit' in arguments:
                path = path.replace('{unit}', urllib.parse.quote(arguments['unit'], safe=''))
            if '{rollout_id}' in path and 'rollout_id' in arguments:
                path = path.replace('{rollout_id}', urllib.parse.quote(arguments['rollout_id'], safe=''))

            # Build body
            body = None
            if tool_row['body_args']:
                body = {k: arguments[k] for k in tool_row['body_args'] if k in arguments}

            # Make HTTP call
            http_status, http_body = call_roundhouse(
                tool_row['method'],
                path,
                body=body,
                token=token,
                requester=requester
            )

            # Shape result — I2/I8 live in shape_result and nowhere else. This used to
            # be an inline second copy of that logic, so the taxonomy was proven by
            # TestShaping against a function the server never called.
            result_dict, is_error = shape_result(http_status, http_body)

            # Apply shaper if present. ONLY on a 200: the I5 keep-list describes the
            # /api/units snapshot, and running it over a refusal body or over I8's
            # {error, url, detail} unreachable result drops every key it does not know
            # — the agent would read a cheerful empty fleet instead of "server down".
            if tool_row['shaper'] and http_status == 200:
                result_dict = tool_row['shaper'](result_dict)

            # Pretty-print result as JSON string
            result_text = json.dumps(result_dict, indent=2, ensure_ascii=False)

            return {
                'jsonrpc': '2.0',
                'id': msg_id,
                'result': {
                    'content': [{'type': 'text', 'text': result_text}],
                    'isError': is_error
                }
            }

        # Unknown method
        else:
            if msg_id is not None:
                return {
                    'jsonrpc': '2.0',
                    'id': msg_id,
                    'error': {
                        'code': -32601,
                        'message': 'method not found'
                    }
                }
            else:
                # Notification - don't respond
                return None

    except Exception as e:
        # Catch-all for bug paths
        return {
            'jsonrpc': '2.0',
            'id': msg_id,
            'error': {
                'code': -32603,
                'message': f'internal error: {type(e).__name__}'
            }
        }


def main() -> int:
    """Main entry point: argparse, stdin loop, JSON-RPC framing."""
    parser = argparse.ArgumentParser(description='Roundhouse MCP server')
    parser.add_argument('--url', default='http://127.0.0.1:8090', help='Roundhouse URL')
    args = parser.parse_args()

    # Validate URL
    parsed = urllib.parse.urlsplit(args.url)
    if parsed.scheme != 'http':
        sys.stderr.write(f'error: unsupported scheme {parsed.scheme}; only http is supported\n')
        return 2

    state['url'] = args.url

    # Main loop: read stdin, process JSON-RPC messages, write responses to stdout
    while True:
        line = sys.stdin.readline()
        if not line:
            # EOF
            break

        line = line.rstrip('\n\r')

        # Skip blank lines
        if not line:
            continue

        # Parse JSON
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            # Parse error
            response = {
                'jsonrpc': '2.0',
                'id': None,
                'error': {
                    'code': -32700,
                    'message': 'parse error'
                }
            }
            sys.stdout.write(json.dumps(response, separators=(',', ':')) + '\n')
            sys.stdout.flush()
            continue

        # Handle message
        response = handle_message(msg, state)

        # Only send response if not a notification
        if response is not None:
            sys.stdout.write(json.dumps(response, separators=(',', ':')) + '\n')
            sys.stdout.flush()

    return 0


if __name__ == '__main__':
    sys.exit(main())
