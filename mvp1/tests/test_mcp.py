"""Test suite for roundhouse_mcp.py (T1 half).

Tests cover: framing, registry, schema validation, result shaping, AST guards, stdlib-only.
"""

import unittest
import json
import sys
import os
import ast
import tempfile
import shutil
import http.server
import http.client
import socket
import subprocess
import threading
import time
import copy
import queue as _queue
from pathlib import Path
from unittest.mock import MagicMock, patch
from io import StringIO

# Add mvp1 to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import roundhouse_mcp
import roundhouse

# Constants
_GIB = 1024 * 1024 * 1024
_MCP_PATH = str(Path(__file__).resolve().parent.parent / 'roundhouse_mcp.py')


class McpPipeClient:
    """The MCP server as a REAL SUBPROCESS over real pipes (SPEC Risk #1 guard).

    Every read has a deadline: a framing mistake (Content-Length headers, a
    pretty-printed envelope, a stray print to stdout) deadlocks the pipe, and the
    deadline is what turns that silence into a red instead of a hung suite.
    """

    def __init__(self, url, token=None, client_name='pipe-client'):
        env = dict(os.environ)
        env.pop('ROUNDHOUSE_TOKEN', None)
        if token is not None:
            env['ROUNDHOUSE_TOKEN'] = token
        # HOME is redirected so a stray ~/.config/roundhouse/token on the build box
        # can never silently satisfy a test that means to run token-less.
        env['HOME'] = tempfile.mkdtemp(prefix='mcp-pipe-home-')
        self._home = env['HOME']
        self.proc = subprocess.Popen(
            [sys.executable, _MCP_PATH, '--url', url],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env)
        self._lines = _queue.Queue()
        self._stderr = []
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        self._errreader = threading.Thread(target=self._pump_err, daemon=True)
        self._errreader.start()
        self.client_name = client_name

    def _pump(self):
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _pump_err(self):
        for line in self.proc.stderr:
            self._stderr.append(line)

    def send(self, msg):
        self.proc.stdin.write(json.dumps(msg, separators=(',', ':')) + '\n')
        self.proc.stdin.flush()

    def recv(self, timeout=20.0):
        try:
            line = self._lines.get(timeout=timeout)
        except _queue.Empty:
            raise AssertionError(
                'no JSON-RPC line from the MCP subprocess within %.0fs '
                '(framing deadlock?); stderr so far: %r' % (timeout, self.drain_stderr()))
        if line is None:
            raise AssertionError('MCP subprocess closed stdout; stderr: %r' % self.drain_stderr())
        assert line.endswith('\n'), 'response was not newline-terminated'
        assert '\r' not in line, 'response carried a CR'
        return json.loads(line)

    def request(self, method, params=None, msg_id=1, timeout=20.0):
        self.send({'jsonrpc': '2.0', 'id': msg_id, 'method': method, 'params': params or {}})
        return self.recv(timeout=timeout)

    def initialize(self):
        resp = self.request('initialize', {
            'protocolVersion': '2025-06-18',
            'capabilities': {},
            'clientInfo': {'name': self.client_name, 'version': '1.0'},
        }, msg_id=0)
        self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        return resp['result']

    def call_tool(self, name, arguments=None, msg_id=1, timeout=20.0):
        """Returns (parsed tool-result JSON, isError)."""
        resp = self.request('tools/call',
                            {'name': name, 'arguments': arguments or {}},
                            msg_id=msg_id, timeout=timeout)
        if 'error' in resp:
            raise AssertionError('tools/call returned a JSON-RPC error: %r' % resp['error'])
        result = resp['result']
        return json.loads(result['content'][0]['text']), result['isError']

    def drain_stderr(self):
        return ''.join(self._stderr)

    def close(self):
        for stream in (self.proc.stdin,):
            try:
                stream.close()
            except Exception:
                pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self._reader.join(timeout=5)
        self._errreader.join(timeout=5)
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except Exception:
                pass
        shutil.rmtree(self._home, ignore_errors=True)


class TestFraming(unittest.TestCase):
    """Test JSON-RPC framing per I11."""

    def test_parse_error_returns_minus_32700(self):
        """Malformed JSON line -> -32700 parse error with id:null."""
        response = roundhouse_mcp.handle_message('not json', {})
        self.assertEqual(response['error']['code'], -32700)
        self.assertIsNone(response['id'])

    def test_unknown_method_with_id_returns_minus_32601(self):
        """Unknown method with id -> -32601 method not found."""
        msg = {'jsonrpc': '2.0', 'id': 1, 'method': 'unknown_method'}
        response = roundhouse_mcp.handle_message(msg, {})
        self.assertEqual(response['error']['code'], -32601)
        self.assertEqual(response['id'], 1)

    def test_unknown_notification_no_response(self):
        """Unknown notification (no id) -> None (no response)."""
        msg = {'jsonrpc': '2.0', 'method': 'unknown_method'}
        response = roundhouse_mcp.handle_message(msg, {})
        self.assertIsNone(response)

    def test_batch_array_returns_minus_32600(self):
        """I11: a top-level JSON array (batch) -> -32600 'batch not supported', id null."""
        msg = [{'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}]
        response = roundhouse_mcp.handle_message(msg, {})
        self.assertEqual(response['error']['code'], -32600)
        self.assertEqual(response['error']['message'], 'batch not supported')
        self.assertIsNone(response['id'])

    def test_null_params_tolerated(self):
        """I11/§3: params absent or explicitly null must not become -32603."""
        for params in (None, {}):
            msg = {'jsonrpc': '2.0', 'id': 4, 'method': 'initialize'}
            if params is not None:
                msg['params'] = params
            else:
                msg['params'] = None
            response = roundhouse_mcp.handle_message(msg, {'url': 'http://127.0.0.1:1',
                                                           'client_name': 'unknown'})
            self.assertNotIn('error', response, f'params={params!r} produced an error')
            self.assertEqual(response['result']['serverInfo']['name'], 'roundhouse-mcp')

        # tools/list with params: null likewise
        msg = {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/list', 'params': None}
        response = roundhouse_mcp.handle_message(msg, {'url': 'http://127.0.0.1:1',
                                                       'client_name': 'unknown'})
        self.assertEqual(len(response['result']['tools']), 16)

    def test_ping_returns_empty_result(self):
        """ping method -> empty result dict."""
        msg = {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}
        response = roundhouse_mcp.handle_message(msg, {'url': 'http://localhost', 'client_name': 'test'})
        self.assertEqual(response['result'], {})
        self.assertEqual(response['id'], 1)

    def test_initialize_returns_protocol_version(self):
        """initialize -> protocolVersion negotiation."""
        msg = {
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'initialize',
            'params': {
                'protocolVersion': '2025-06-18',
                'capabilities': {},
                'clientInfo': {'name': 'test-client', 'version': '1.0'}
            }
        }
        response = roundhouse_mcp.handle_message(msg, {'url': 'http://localhost', 'client_name': 'unknown'})
        self.assertEqual(response['result']['protocolVersion'], '2025-06-18')
        self.assertEqual(response['result']['serverInfo']['name'], 'roundhouse-mcp')
        self.assertEqual(response['result']['serverInfo']['version'], '6.0')
        self.assertIn('tools', response['result']['capabilities'])

    def test_initialize_unsupported_version_downgrades(self):
        """initialize with unsupported version -> downgrades to 2024-11-05."""
        msg = {
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'initialize',
            'params': {
                'protocolVersion': '1999-01-01',
                'capabilities': {},
            }
        }
        response = roundhouse_mcp.handle_message(msg, {'url': 'http://localhost', 'client_name': 'unknown'})
        self.assertEqual(response['result']['protocolVersion'], '2024-11-05')

    def test_initialize_captures_client_name(self):
        """initialize captures clientInfo.name."""
        state = {'url': 'http://localhost', 'client_name': 'unknown'}
        msg = {
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'initialize',
            'params': {
                'protocolVersion': '2025-06-18',
                'clientInfo': {'name': 'claude-code'}
            }
        }
        roundhouse_mcp.handle_message(msg, state)
        self.assertEqual(state['client_name'], 'claude-code')

    def test_response_is_single_line(self):
        """All serialized responses are single line (no embedded newlines)."""
        msg = {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}
        response = roundhouse_mcp.handle_message(msg, {'url': 'http://localhost', 'client_name': 'test'})
        serialized = json.dumps(response, separators=(',', ':'))
        self.assertNotIn('\n', serialized)
        self.assertNotIn('\r', serialized)


class TestRegistry(unittest.TestCase):
    """Test tool registry structure and frozen list."""

    def test_exactly_16_tools(self):
        """Registry has exactly 16 tools."""
        self.assertEqual(len(roundhouse_mcp.TOOLS), 16)

    def test_frozen_names_in_order(self):
        """Tool names match the frozen list."""
        expected_names = [
            'fleet_status', 'unit_detail', 'port_board', 'deployments',
            'routing_config', 'operation_status', 'warm_state',
            'switch_preview', 'switch_execute', 'edit_preview', 'edit_rollout',
            'set_boot', 'warm', 'warm_cancel', 'operation_rollback', 'operation_dismiss'
        ]
        actual_names = list(roundhouse_mcp.TOOLS.keys())
        self.assertEqual(actual_names, expected_names)

    def test_each_tool_has_required_fields(self):
        """Each tool row has all required fields."""
        required_fields = {
            'description', 'schema', 'method', 'path', 'action',
            'body_args', 'shaper', 'send_requester'
        }
        for name, row in roundhouse_mcp.TOOLS.items():
            with self.subTest(tool=name):
                self.assertTrue(required_fields.issubset(row.keys()), f"{name} missing fields")

    def test_all_schemas_have_additional_properties_false(self):
        """All schemas have additionalProperties: false at top level."""
        for name, row in roundhouse_mcp.TOOLS.items():
            with self.subTest(tool=name):
                schema = row['schema']
                self.assertFalse(schema.get('additionalProperties'), f"{name} allows additional properties")

    def test_exactly_one_shaper_fleet_status(self):
        """Only fleet_status has a shaper."""
        shapers = [name for name, row in roundhouse_mcp.TOOLS.items() if row['shaper'] is not None]
        self.assertEqual(shapers, ['fleet_status'])

    def test_exactly_one_send_requester_warm(self):
        """Only warm has send_requester=True."""
        send_requesters = [name for name, row in roundhouse_mcp.TOOLS.items() if row['send_requester']]
        self.assertEqual(send_requesters, ['warm'])

    def test_preview_execute_pairs_exist(self):
        """switch_preview/switch_execute and edit_preview/edit_rollout pairs exist."""
        names = set(roundhouse_mcp.TOOLS.keys())
        self.assertIn('switch_preview', names)
        self.assertIn('switch_execute', names)
        self.assertIn('edit_preview', names)
        self.assertIn('edit_rollout', names)
        # Verify no orphaned execute/rollout without preview
        for name in names:
            if name.endswith('_execute') or name.endswith('_rollout'):
                preview_name = name.replace('_execute', '_preview').replace('_rollout', '_preview')
                self.assertIn(preview_name, names, f"{name} has no preview counterpart")

    def test_get_routes_have_action_false(self):
        """All GET routes have action=False."""
        get_routes = ['fleet_status', 'unit_detail', 'port_board', 'deployments',
                      'routing_config', 'operation_status', 'warm_state']
        for name in get_routes:
            with self.subTest(tool=name):
                self.assertFalse(roundhouse_mcp.TOOLS[name]['action'])

    def test_post_routes_have_action_true(self):
        """All POST routes have action=True."""
        post_routes = ['switch_preview', 'switch_execute', 'edit_preview', 'edit_rollout',
                       'set_boot', 'warm', 'warm_cancel', 'operation_rollback', 'operation_dismiss']
        for name in post_routes:
            with self.subTest(tool=name):
                self.assertTrue(roundhouse_mcp.TOOLS[name]['action'])


class TestSchemaValidation(unittest.TestCase):
    """Test schema validation via validate_args."""

    def test_validate_accepts_valid_input(self):
        """Valid input passes validation."""
        schema = {
            'type': 'object',
            'properties': {'unit': {'type': 'string'}},
            'required': ['unit'],
            'additionalProperties': False
        }
        error = roundhouse_mcp.validate_args(schema, {'unit': 'test.service'})
        self.assertIsNone(error)

    def test_extra_property_rejected(self):
        """Extra properties rejected when additionalProperties: false."""
        schema = {
            'type': 'object',
            'properties': {'unit': {'type': 'string'}},
            'additionalProperties': False
        }
        error = roundhouse_mcp.validate_args(schema, {'unit': 'test', 'extra': 'value'})
        self.assertIn('unknown property', error)

    def test_missing_required_field(self):
        """Missing required field triggers error."""
        schema = {
            'type': 'object',
            'properties': {'unit': {'type': 'string'}},
            'required': ['unit'],
            'additionalProperties': False
        }
        error = roundhouse_mcp.validate_args(schema, {})
        self.assertIn('missing required property', error)

    def test_wrong_type_string(self):
        """Wrong type (not string) for string property."""
        schema = {
            'type': 'object',
            'properties': {'unit': {'type': 'string'}},
            'additionalProperties': False
        }
        error = roundhouse_mcp.validate_args(schema, {'unit': 123})
        self.assertIn('expected string', error)

    def test_wrong_type_boolean(self):
        """Wrong type for boolean property."""
        schema = {
            'type': 'object',
            'properties': {'enabled': {'type': 'boolean'}},
            'additionalProperties': False
        }
        error = roundhouse_mcp.validate_args(schema, {'enabled': 1})
        self.assertIn('expected boolean', error)

    def test_array_type_check(self):
        """Array type validation."""
        schema = {
            'type': 'object',
            'properties': {'stops': {'type': 'array', 'items': {'type': 'string'}}},
            'additionalProperties': False
        }
        # Valid
        error = roundhouse_mcp.validate_args(schema, {'stops': ['a', 'b']})
        self.assertIsNone(error)
        # Invalid type
        error = roundhouse_mcp.validate_args(schema, {'stops': 'not-array'})
        self.assertIn('expected array', error)

    def test_array_items_type_check(self):
        """Array items type validation."""
        schema = {
            'type': 'object',
            'properties': {'stops': {'type': 'array', 'items': {'type': 'string'}}},
            'additionalProperties': False
        }
        error = roundhouse_mcp.validate_args(schema, {'stops': ['a', 123]})
        self.assertIn('expected string', error)

    def test_object_property_validation(self):
        """Object property validation."""
        schema = {
            'type': 'object',
            'properties': {'edits': {'type': 'object', 'additionalProperties': {'type': 'string'}}},
            'additionalProperties': False
        }
        # Valid
        error = roundhouse_mcp.validate_args(schema, {'edits': {'key': 'value'}})
        self.assertIsNone(error)
        # Invalid type
        error = roundhouse_mcp.validate_args(schema, {'edits': 'not-object'})
        self.assertIn('expected object', error)

    def test_min_properties_validation(self):
        """minProperties validation."""
        schema = {
            'type': 'object',
            'properties': {'edits': {'type': 'object', 'additionalProperties': {'type': 'string'}, 'minProperties': 1}},
            'additionalProperties': False
        }
        # Valid
        error = roundhouse_mcp.validate_args(schema, {'edits': {'a': 'b'}})
        self.assertIsNone(error)
        # Invalid
        error = roundhouse_mcp.validate_args(schema, {'edits': {}})
        self.assertIn('expected at least 1 properties', error)

    def test_tools_call_with_invalid_args_returns_32602(self):
        """tools/call with invalid args returns -32602."""
        msg = {
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'tools/call',
            'params': {
                'name': 'unit_detail',
                'arguments': {'extra_key': 'value'}  # Invalid
            }
        }
        response = roundhouse_mcp.handle_message(msg, {'url': 'http://localhost', 'client_name': 'test'})
        self.assertEqual(response['error']['code'], -32602)


class TestShaping(unittest.TestCase):
    """Test result shaping."""

    def test_shape_result_injects_http_status(self):
        """HTTP status is injected into result."""
        status, payload = 200, {'key': 'value'}
        result, is_error = roundhouse_mcp.shape_result(status, payload)
        self.assertEqual(result['http_status'], 200)

    def test_shape_result_4xx_is_not_error(self):
        """HTTP 4xx responses are not errors (isError: false)."""
        status, payload = 404, {'error': 'not_found'}
        result, is_error = roundhouse_mcp.shape_result(status, payload)
        self.assertFalse(is_error)

    def test_shape_result_5xx_is_error(self):
        """HTTP 5xx responses are errors (isError: true)."""
        status, payload = 500, {'error': 'internal'}
        result, is_error = roundhouse_mcp.shape_result(status, payload)
        self.assertTrue(is_error)

    def test_shape_result_transport_failure_is_error(self):
        """Transport failure (status=None) is error."""
        status, payload = None, 'connection refused'
        result, is_error = roundhouse_mcp.shape_result(status, payload)
        self.assertTrue(is_error)
        self.assertEqual(result['error'], 'roundhouse_unreachable')

    def test_shape_result_text_response(self):
        """Non-JSON response becomes 'raw' field."""
        status, payload = 404, '404 Not Found'
        result, is_error = roundhouse_mcp.shape_result(status, payload)
        self.assertEqual(result['raw'], '404 Not Found')

    def test_shape_result_injection_wins_on_key_collision(self):
        """I2: the injected http_status wins over a body key of the same name."""
        result, _is_error = roundhouse_mcp.shape_result(200, {'http_status': 'from-body', 'k': 1})
        self.assertEqual(result['http_status'], 200)

    def _call_with_canned_http(self, tool, arguments, status, payload):
        """Drive tools/call through handle_message with call_roundhouse canned."""
        calls = []

        def fake_call(method, path, body=None, token=None, requester=None):
            calls.append((method, path, body, token, requester))
            return (status, payload)

        os.environ['ROUNDHOUSE_TOKEN'] = 'canned-token'
        try:
            with patch.object(roundhouse_mcp, 'call_roundhouse', fake_call):
                response = roundhouse_mcp.handle_message(
                    {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                     'params': {'name': tool, 'arguments': arguments}},
                    {'url': 'http://127.0.0.1:9', 'client_name': 'unit-test'})
        finally:
            del os.environ['ROUNDHOUSE_TOKEN']
        text = response['result']['content'][0]['text']
        return json.loads(text), response['result']['isError'], calls

    def test_tools_call_injection_wins_on_key_collision(self):
        """I2 on the LIVE path: tools/call must shape through the same rule as shape_result."""
        result, is_error, _calls = self._call_with_canned_http(
            'port_board', {}, 200, {'http_status': 'from-body', 'ports': []})
        self.assertEqual(result['http_status'], 200)
        self.assertFalse(is_error)

    def test_tools_call_empty_text_body_still_carries_raw(self):
        """I2: a non-JSON body becomes {'http_status': N, 'raw': ...} even when empty."""
        result, is_error, _calls = self._call_with_canned_http(
            'port_board', {}, 404, '')
        self.assertEqual(result, {'http_status': 404, 'raw': ''})
        self.assertFalse(is_error)

    def test_fleet_status_surfaces_snapshot_rollout_as_operation(self):
        """I5: `operation` is the snapshot's `rollout` value verbatim (not a key named
        `operation`, which take_snapshot never emits)."""
        record = {'rollout_id': 'sw-1-1', 'kind': 'switch', 'phase': 'stopping',
                  'unit': 'a.service', 'detail': 'stopping b.service (1/1)'}
        snapshot = {
            'host': 'h', 'kernel': '6.1', 'now': 1.0, 'mode': 'actuate',
            'mem': {}, 'sources': {}, 'self_port': 8090, 'self_unit': {},
            'units': [], 'rollout': record,
        }
        result, _is_error, _calls = self._call_with_canned_http(
            'fleet_status', {}, 200, snapshot)
        self.assertEqual(result['operation'], record,
                         'fleet_status dropped the running operation')
        self.assertEqual(result['n_units'], 0)
        self.assertNotIn('rollout', result)

    def test_one_http_call_per_tool_call_even_on_409(self):
        """Risk #3, the stub-count leg: a 409 `preview_stale` is answered ONCE. No
        auto-re-preview, no retry — the wrapper has no state slot to loop with."""
        result, is_error, calls = self._call_with_canned_http(
            'switch_execute', {'target': 'a.service', 'confirm': 'stale'},
            409, {'error': 'preview_stale', 'detail': 'fleet state changed'})
        self.assertEqual(len(calls), 1, f'{len(calls)} HTTP calls for one tools/call')
        self.assertEqual(calls[0][0], 'POST')
        self.assertEqual(calls[0][1], '/api/switch')
        self.assertFalse(is_error)
        self.assertEqual(result['error'], 'preview_stale')

    def test_confirm_is_passed_through_verbatim(self):
        """I4: the wrapper is confirm-opaque — it forwards the string, never derives it."""
        _result, _is_error, calls = self._call_with_canned_http(
            'switch_execute',
            {'target': 'a.service', 'stops': ['b.service'], 'confirm': 'CONFIRM-FROM-PREVIEW'},
            202, {'rollout_id': 'sw-1'})
        body = calls[0][2]
        self.assertEqual(body, {'target': 'a.service', 'stops': ['b.service'],
                                'confirm': 'CONFIRM-FROM-PREVIEW'})

    def test_fleet_status_shaper_does_not_eat_a_transport_failure(self):
        """I8 beats I5: an unreachable Roundhouse must still answer
        {error, url, detail}. Running the I5 keep-list over that dict dropped all
        three keys and handed the agent a cheerful empty fleet instead."""
        result, is_error, _calls = self._call_with_canned_http(
            'fleet_status', {}, None, '[Errno 111] Connection refused')
        self.assertTrue(is_error)
        self.assertEqual(result['error'], 'roundhouse_unreachable')
        self.assertEqual(result['url'], 'http://127.0.0.1:9')
        self.assertIn('refused', result['detail'])
        self.assertNotIn('n_units', result)

    def test_fleet_status_shaper_does_not_eat_an_error_body(self):
        """The keep-list is for the /api/units SNAPSHOT. A 5xx (or any refusal) body
        must pass through whole — shaping it would drop `error` and `detail`."""
        result, is_error, _calls = self._call_with_canned_http(
            'fleet_status', {}, 500, {'error': 'boom', 'detail': 'watcher died'})
        self.assertTrue(is_error)
        self.assertEqual(result, {'http_status': 500, 'error': 'boom', 'detail': 'watcher died'})

    def test_shape_fleet_status_keep_list(self):
        """shape_fleet_status keeps only specified fields."""
        snapshot = {
            'http_status': 200,
            'host': 'boltzmann',
            'kernel': '6.1',
            'now': 1234567890.0,
            'mode': 'actuate',
            'mem': {'total_bytes': 1000, 'available_bytes': 500},
            'sources': {},
            'self_port': 8090,
            'self_unit': 'roundhouse.service',
            'units': [
                {
                    'unit': 'llama.service',
                    'rung': 1,
                    'port': 8080,
                    'alias': 'llama',
                    'enabled': True,
                    'on_demand': False,
                    'retired': False,
                    'strategy_note': None,
                    'badges': [],
                    'dropped_field': 'should not appear',
                    'port_conflict': None
                }
            ],
            # take_snapshot emits the operation record under the key `rollout` (I5).
            'rollout': None,
        }
        result = roundhouse_mcp.shape_fleet_status(snapshot)
        # Check kept fields
        self.assertEqual(result['host'], 'boltzmann')
        self.assertEqual(result['mode'], 'actuate')
        self.assertEqual(result['n_units'], 1)
        # Check dropped field doesn't appear
        self.assertNotIn('dropped_field', result['units'][0])
        # Check port_conflict not present when None
        self.assertNotIn('port_conflict', result['units'][0])

    def test_shape_fleet_status_includes_port_conflict_when_set(self):
        """shape_fleet_status includes port_conflict only when non-null."""
        snapshot = {
            'http_status': 200,
            'host': 'test',
            'kernel': '6.1',
            'now': 1234567890.0,
            'mode': 'actuate',
            'mem': {},
            'sources': {},
            'self_port': 8090,
            'self_unit': 'test.service',
            'units': [
                {
                    'unit': 'a.service',
                    'rung': 1,
                    'port': 8080,
                    'alias': 'a',
                    'enabled': True,
                    'on_demand': False,
                    'retired': False,
                    'strategy_note': None,
                    'badges': [],
                    'port_conflict': {'port': 8080, 'claimants': ['b.service']}
                }
            ],
            # take_snapshot emits the operation record under the key `rollout` (I5).
            'rollout': None,
        }
        result = roundhouse_mcp.shape_fleet_status(snapshot)
        self.assertIn('port_conflict', result['units'][0])
        self.assertEqual(result['units'][0]['port_conflict']['claimants'], ['b.service'])


S = {'type': 'string'}
_EMPTY_OBJ = {'type': 'object', 'properties': {}, 'additionalProperties': False}
_EDITS = {'type': 'object', 'additionalProperties': S, 'minProperties': 1}

# MVP6-SPEC §4 transcribed as data: name -> (method, path, action, body_args,
# send_requester, has_shaper, schema). Any drift between the registry and the frozen
# catalog is a red here — this is the §4/§5 audit as a mechanical check, not a reading.
FROZEN_CATALOG = {
    'fleet_status': ('GET', '/api/units', False, (), False, True, _EMPTY_OBJ),
    'unit_detail': ('GET', '/api/units/{unit}', False, (), False, False,
                    {'type': 'object', 'properties': {'unit': S},
                     'required': ['unit'], 'additionalProperties': False}),
    'port_board': ('GET', '/api/ports', False, (), False, False, _EMPTY_OBJ),
    'deployments': ('GET', '/api/deployments', False, (), False, False, _EMPTY_OBJ),
    'routing_config': ('GET', '/api/routing-config.json', False, (), False, False, _EMPTY_OBJ),
    'operation_status': ('GET', '/api/rollouts/{rollout_id}', False, (), False, False,
                         {'type': 'object', 'properties': {'rollout_id': S},
                          'required': ['rollout_id'], 'additionalProperties': False}),
    'warm_state': ('GET', '/api/warm', False, (), False, False, _EMPTY_OBJ),
    'switch_preview': ('POST', '/api/switch/preview', True, ('target', 'stops'), False, False,
                       {'type': 'object',
                        'properties': {'target': S, 'stops': {'type': 'array', 'items': S}},
                        'required': ['target'], 'additionalProperties': False}),
    'switch_execute': ('POST', '/api/switch', True, ('target', 'stops', 'confirm'), False, False,
                       {'type': 'object',
                        'properties': {'target': S, 'stops': {'type': 'array', 'items': S},
                                       'confirm': S},
                        'required': ['target', 'confirm'], 'additionalProperties': False}),
    'edit_preview': ('POST', '/api/units/{unit}/edit', True, ('edits',), False, False,
                     {'type': 'object', 'properties': {'unit': S, 'edits': _EDITS},
                      'required': ['unit', 'edits'], 'additionalProperties': False}),
    'edit_rollout': ('POST', '/api/units/{unit}/rollout', True, ('edits', 'confirm'), False, False,
                     {'type': 'object',
                      'properties': {'unit': S, 'edits': _EDITS, 'confirm': S},
                      'required': ['unit', 'edits', 'confirm'], 'additionalProperties': False}),
    'set_boot': ('POST', '/api/units/{unit}/enablement', True, ('enabled',), False, False,
                 {'type': 'object',
                  'properties': {'unit': S, 'enabled': {'type': 'boolean'}},
                  'required': ['unit', 'enabled'], 'additionalProperties': False}),
    'warm': ('POST', '/api/warm', True, ('logical', 'unit'), True, False,
             {'type': 'object',
              'properties': {'logical': S, 'unit': S, 'requester': S},
              'additionalProperties': False,
              'oneOf': [{'required': ['logical']}, {'required': ['unit']}]}),
    'warm_cancel': ('POST', '/api/warm/cancel', True, (), False, False, _EMPTY_OBJ),
    'operation_rollback': ('POST', '/api/rollouts/{rollout_id}/rollback', True, (), False, False,
                           {'type': 'object', 'properties': {'rollout_id': S},
                            'required': ['rollout_id'], 'additionalProperties': False}),
    'operation_dismiss': ('POST', '/api/rollouts/{rollout_id}/dismiss', True, (), False, False,
                          {'type': 'object', 'properties': {'rollout_id': S},
                           'required': ['rollout_id'], 'additionalProperties': False}),
}


class TestFrozenCatalog(unittest.TestCase):
    """MVP6-SPEC §4/§5 audit: the registry IS the frozen catalog, row for row."""

    def test_catalog_names_and_order(self):
        self.assertEqual(list(roundhouse_mcp.TOOLS.keys()), list(FROZEN_CATALOG.keys()))

    def test_every_row_matches_the_frozen_table(self):
        for name, (method, path, action, body_args,
                   send_requester, has_shaper, schema) in FROZEN_CATALOG.items():
            with self.subTest(tool=name):
                row = roundhouse_mcp.TOOLS[name]
                self.assertEqual(row['method'], method)
                self.assertEqual(row['path'], path)
                self.assertEqual(row['action'], action)
                self.assertEqual(tuple(row['body_args']), body_args)
                self.assertEqual(row['send_requester'], send_requester)
                self.assertEqual(row['shaper'] is not None, has_shaper)
                self.assertEqual(row['schema'], schema)

    def test_body_args_are_declared_properties(self):
        """A body_arg the schema does not declare can never be sent (dead wiring)."""
        for name, row in roundhouse_mcp.TOOLS.items():
            with self.subTest(tool=name):
                props = set(row['schema'].get('properties', {}))
                self.assertTrue(set(row['body_args']).issubset(props))

    def test_unit_and_rollout_id_ride_the_path_never_the_body(self):
        """§4: `unit` rides the path, never the body; same for rollout_id."""
        for name, row in roundhouse_mcp.TOOLS.items():
            with self.subTest(tool=name):
                for key in ('unit', 'rollout_id'):
                    if '{%s}' % key in row['path']:
                        self.assertNotIn(key, row['body_args'])
        # `warm` is the one tool where `unit` is a BODY arg (there is no {unit} in its path)
        self.assertIn('unit', roundhouse_mcp.TOOLS['warm']['body_args'])
        self.assertNotIn('{unit}', roundhouse_mcp.TOOLS['warm']['path'])

    def test_requester_is_never_a_body_arg(self):
        """recon 4: X-Roundhouse-Requester rides the header on /api/warm only."""
        for name, row in roundhouse_mcp.TOOLS.items():
            self.assertNotIn('requester', row['body_args'], name)

    def test_no_action_tool_is_a_read_route(self):
        """§5: GET rows carry no token (all GET routes are unauthenticated by design)."""
        for name, row in roundhouse_mcp.TOOLS.items():
            with self.subTest(tool=name):
                self.assertEqual(row['action'], row['method'] == 'POST')


class TestZeroWriteGuard(unittest.TestCase):
    """Test AST guards for I9 compliance."""

    def test_no_subprocess_imports(self):
        """subprocess is not imported."""
        with open(Path(__file__).resolve().parent.parent / 'roundhouse_mcp.py', 'r') as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(alias.name, 'subprocess')
            elif isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, 'subprocess')

    def test_allowed_imports_only(self):
        """Only allowed imports appear."""
        allowed = {'sys', 'os', 'json', 'argparse', 'urllib.parse', 'http.client'}
        with open(Path(__file__).resolve().parent.parent / 'roundhouse_mcp.py', 'r') as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name, allowed, f"Disallowed import: {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                self.assertIn(node.module, allowed, f"Disallowed import: {node.module}")

    def test_no_os_destructive_calls(self):
        """I9(b) in full: no destructive/process/write-capable os attributes.

        MVP6 review F1: the original set covered filesystem mutation only —
        os.system/os.popen/os.exec*/os.spawn*/os.open/os.fdopen all slipped
        every guard (os is on the import allowlist and the open-counter only
        sees bare open() Names)."""
        forbidden_attrs = {'remove', 'rename', 'unlink', 'mkdir', 'makedirs', 'rmdir',
                           'chmod', 'replace', 'system', 'popen', 'open', 'fdopen'}
        forbidden_prefixes = ('exec', 'spawn')
        with open(Path(__file__).resolve().parent.parent / 'roundhouse_mcp.py', 'r') as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                if isinstance(node.value, ast.Name) and node.value.id == 'os':
                    self.assertNotIn(node.attr, forbidden_attrs, f"Forbidden os.{node.attr}")
                    self.assertFalse(node.attr.startswith(forbidden_prefixes),
                                     f"Forbidden os.{node.attr}")

    def test_only_one_open_call(self):
        """Only one open() call (token file read)."""
        with open(Path(__file__).resolve().parent.parent / 'roundhouse_mcp.py', 'r') as f:
            tree = ast.parse(f.read())
        opens = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == 'open']
        self.assertEqual(len(opens), 1, f"Expected 1 open call, found {len(opens)}")

    def test_open_has_read_mode_only(self):
        """The single open() call has mode='r' or no mode."""
        with open(Path(__file__).resolve().parent.parent / 'roundhouse_mcp.py', 'r') as f:
            tree = ast.parse(f.read())
        opens = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == 'open']
        if opens:
            call = opens[0]
            # Check mode argument (can be keyword or positional arg 2)
            mode_arg = None
            for keyword in call.keywords:
                if keyword.arg == 'mode':
                    if isinstance(keyword.value, ast.Constant):
                        mode_arg = keyword.value.value
            # If no keyword, check positional args
            if mode_arg is None and len(call.args) > 1:
                if isinstance(call.args[1], ast.Constant):
                    mode_arg = call.args[1].value
            # Mode should be 'r' or absent
            if mode_arg is not None:
                self.assertIn(mode_arg, {'r'}, f"open() mode must be 'r', got {mode_arg}")

    def test_http_connection_only_in_call_roundhouse(self):
        """I9 leg (e): EXACTLY ONE HTTPConnection construction site, and it is inside
        `call_roundhouse`.

        The previous version of this guard collected the call nodes, computed
        `is_inside`, and then asserted nothing at all — a stray HTTPConnection seeded
        anywhere in the file passed it silently. Every branch below ends in an assert.
        """
        with open(Path(__file__).resolve().parent.parent / 'roundhouse_mcp.py', 'r') as f:
            tree = ast.parse(f.read())

        def is_http_connection(func):
            # http.client.HTTPConnection(...)  /  client.HTTPConnection(...)
            if isinstance(func, ast.Attribute) and func.attr in (
                    'HTTPConnection', 'HTTPSConnection'):
                return True
            # a bare HTTPConnection(...) after `from http.client import HTTPConnection`
            if isinstance(func, ast.Name) and func.id in (
                    'HTTPConnection', 'HTTPSConnection'):
                return True
            return False

        # Map every node to the name of its enclosing top-level function ('<module>'
        # for anything outside one), so the check is a lookup, not a nested walk.
        enclosing = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(node):
                    enclosing.setdefault(child, node.name)

        sites = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and is_http_connection(n.func)]
        where = sorted(enclosing.get(n, '<module>') for n in sites)

        self.assertEqual(len(sites), 1,
                         f'expected exactly one HTTPConnection site, found {len(sites)} '
                         f'in {where}')
        self.assertEqual(where, ['call_roundhouse'],
                         f'HTTPConnection constructed outside call_roundhouse: {where}')

        # ...and its host/port derive from the parsed --url, not from a literal.
        site = sites[0]
        self.assertTrue(site.args, 'HTTPConnection called with no host argument')
        self.assertIsInstance(site.args[0], ast.Name,
                              'HTTPConnection host must come from a parsed variable')
        self.assertEqual(site.args[0].id, 'host')


class TestStdlibImports(unittest.TestCase):
    """Test that only stdlib is imported."""

    def test_stdlib_only_imports(self):
        """Only Python stdlib modules are imported."""
        stdlib_modules = {'sys', 'os', 'json', 'argparse', 'urllib', 'urllib.parse', 'http', 'http.client'}
        with open(Path(__file__).resolve().parent.parent / 'roundhouse_mcp.py', 'r') as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    # Check if module is in stdlib
                    base = alias.name.split('.')[0]
                    self.assertIn(base, {'sys', 'os', 'json', 'argparse', 'urllib', 'http'},
                                 f"Non-stdlib import: {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                base = node.module.split('.')[0] if node.module else ''
                self.assertIn(base, {'sys', 'os', 'json', 'argparse', 'urllib', 'http'},
                             f"Non-stdlib import: {node.module}")


class TestTokenSource(unittest.TestCase):
    """Test token resolution per I6."""

    def test_env_token_wins(self):
        """Environment variable token wins over file."""
        with patch.dict(os.environ, {'ROUNDHOUSE_TOKEN': 'env-token'}):
            with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
                f.write('file-token')
                f.flush()
                token_path = f.name
            try:
                with patch('roundhouse_mcp.os.path.expanduser') as mock_expand:
                    mock_expand.return_value = token_path
                    token = roundhouse_mcp.resolve_token()
                    self.assertEqual(token, 'env-token')
            finally:
                os.unlink(token_path)

    def test_file_token_fallback(self):
        """File token is used when env is absent."""
        with patch.dict(os.environ, {}, clear=True):
            with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
                f.write('file-token\n')
                f.flush()
                token_path = f.name
            try:
                with patch('roundhouse_mcp.os.path.expanduser') as mock_expand:
                    mock_expand.return_value = token_path
                    token = roundhouse_mcp.resolve_token()
                    self.assertEqual(token, 'file-token')
            finally:
                os.unlink(token_path)

    def test_no_token_returns_none(self):
        """No token available returns None."""
        with patch.dict(os.environ, {}, clear=True):
            with patch('roundhouse_mcp.os.path.expanduser') as mock_expand:
                mock_expand.return_value = '/nonexistent/path'
                token = roundhouse_mcp.resolve_token()
                self.assertIsNone(token)

    def test_no_token_action_refuses_before_any_http_request(self):
        """§7.8 / §3 handling order: step 3 (no token) fires BEFORE step 4 (HTTP).

        This is also the live-row gotcha: against a read-only Roundhouse a TOKEN-LESS
        registration answers `no_token`, never `read_only_mode` — the 403 lives on the
        far side of an HTTP call that never happens. Both are structured refusals with
        isError: false; docs/MCP.md must not promise the 403 without a token.
        """
        calls = []

        def must_not_be_called(*a, **k):
            calls.append(a)
            raise AssertionError('an HTTP request was made without a token')

        with patch.dict(os.environ, {}, clear=True), \
             patch.object(roundhouse_mcp, 'call_roundhouse', must_not_be_called), \
             patch.object(roundhouse_mcp.os.path, 'expanduser',
                          lambda p: '/nonexistent/roundhouse/token'):
            response = roundhouse_mcp.handle_message(
                {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                 'params': {'name': 'set_boot',
                            'arguments': {'unit': 'a.service', 'enabled': True}}},
                {'url': 'http://127.0.0.1:8090', 'client_name': 'x'})
        result = json.loads(response['result']['content'][0]['text'])
        self.assertEqual(calls, [])
        self.assertFalse(response['result']['isError'])
        self.assertEqual(result['error'], 'no_token')
        self.assertIsNone(result['http_status'])
        self.assertIn('ROUNDHOUSE_TOKEN', result['hint'])

    def test_read_tools_need_no_token(self):
        """recon 7: every GET route is unauthenticated, so a token-less registration is
        a fully functional read-only client (the live acceptance row's whole premise)."""
        seen = []

        def fake_call(method, path, body=None, token=None, requester=None):
            seen.append((path, token))
            return (200, {'units': []})

        with patch.dict(os.environ, {}, clear=True), \
             patch.object(roundhouse_mcp, 'call_roundhouse', fake_call), \
             patch.object(roundhouse_mcp.os.path, 'expanduser',
                          lambda p: '/nonexistent/roundhouse/token'):
            for name, row in roundhouse_mcp.TOOLS.items():
                if row['action']:
                    continue
                args = {}
                if '{unit}' in row['path']:
                    args['unit'] = 'a.service'
                if '{rollout_id}' in row['path']:
                    args['rollout_id'] = 'r-1'
                response = roundhouse_mcp.handle_message(
                    {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                     'params': {'name': name, 'arguments': args}},
                    {'url': 'http://127.0.0.1:8090', 'client_name': 'x'})
                self.assertFalse(response['result']['isError'], name)
        self.assertEqual(len(seen), 7)
        self.assertTrue(all(token is None for _p, token in seen),
                        'a read tool attached a bearer token')

    def test_sanitize_requester(self):
        """Requester name is sanitized per H7 charset."""
        # Keep only [A-Za-z0-9._@ -]
        self.assertEqual(roundhouse_mcp.sanitize_requester('claude-code'), 'claude-code')
        self.assertEqual(roundhouse_mcp.sanitize_requester('claude:code'), 'claudecode')  # : is removed
        self.assertEqual(roundhouse_mcp.sanitize_requester('claude_code.test'), 'claude_code.test')
        # Truncate to 64
        long_name = 'a' * 100
        self.assertEqual(len(roundhouse_mcp.sanitize_requester(long_name)), 64)


# ===== MVP6 T2: fidelity + session =====


class TestRefusalFidelity(unittest.TestCase):
    """Refusal fidelity: 422/409/403/401 responses match between direct HTTP and MCP tool.

    Tests that each refusal class surfaces the COMPLETE response body in the tool result,
    field-for-field equal to a direct HTTP call, plus injected http_status.
    isError is asserted false for refusals (they are structured returns, not crashes).

    MVP6 review F7: every test here targets a PRE-actuation refusal, but if a
    refusal ever regresses, the request would fall through to real systemctl on
    the build box before the assertion reds. setUp arms forbidding gateways so
    a regression fails loudly instead of actuating.
    """

    HOST = 'testhost'
    ADVERTISE = 'advertise.example'

    def setUp(self):
        import roundhouse as _rh
        self._gw_patches = [
            unittest.mock.patch.object(
                _rh, 'run_actuate',
                side_effect=AssertionError('refusal regressed: run_actuate reached')),
            unittest.mock.patch.object(
                _rh, 'run_git',
                side_effect=AssertionError('refusal regressed: run_git reached')),
            unittest.mock.patch.object(
                _rh, '_atomic_write',
                side_effect=AssertionError('refusal regressed: _atomic_write reached')),
        ]
        for p in self._gw_patches:
            p.start()
            self.addCleanup(p.stop)

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.mkdtemp()

        # Build units with port collision for testing
        cls.units = {}
        for uname, alias, port, on_demand, retired, enabled in (
            ('twin1.service', 'twin', 9110, False, False, True),
            ('twin2.service', 'twin', 9110, False, False, False),
            ('on-demand.service', 'on-demand', 9111, True, False, False),
            ('plain.service', None, 9112, False, False, False),
        ):
            u = _mvp5_unit(uname, alias, port, on_demand=on_demand, retired=retired, enabled=enabled)
            cls.units[u.name] = u

        cls.watcher = MagicMock(spec=roundhouse.Watcher)
        cls.watcher.lock = threading.Lock()
        cls.watcher.units = cls.units
        cls.watcher.mem_store = None
        cls.watcher._cgroup_cache = {}
        cls.watcher.snapshot.side_effect = lambda: copy.deepcopy(cls.snap)

        cls.event_bus = roundhouse.EventBus()

        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        cls.port = sock.getsockname()[1]
        sock.close()

        cls.snap = cls._default_snapshot()

        roundhouse.ACTUATE_ARMED = True
        roundhouse.TOKEN = 'test-token'

        cls.server = roundhouse.ThreadingHTTPServer(
            ('127.0.0.1', cls.port),
            roundhouse.RoundhouseRequestHandler,
            cls.watcher,
            cls.event_bus,
            cls.port,
            watcher_lock=cls.watcher.lock,
            advertise_host=cls.ADVERTISE,
        )
        cls.engine = roundhouse.RolloutEngine(
            cls.watcher, cls.units, cls.temp_dir, cls.port,
            cls.event_bus, cls.watcher.lock)
        cls.server.rollout_engine = cls.engine

        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.temp_dir, ignore_errors=True)
        roundhouse.ACTUATE_ARMED = False
        roundhouse.TOKEN = None

    @classmethod
    def _default_snapshot(cls):
        def row(unit, rung, port, alias, on_demand, retired=False, enabled=False):
            return {
                'unit': unit, 'description': '', 'retired': retired, 'rung': rung,
                'roster': 'cold', 'since': 0, 'start_ts_mono': '1000',
                'detail': '', 'badges': [], 'stale': False, 'sensed_at': 0,
                'enabled': enabled, 'active_state': 'inactive', 'sub_state': 'dead',
                'n_restarts': 0, 'port': port, 'port_source': 'flag', 'alias': alias,
                'on_demand': on_demand, 'gate': None, 'model_file': '', 'quant_hint': None,
                'ctx': None, 'mem': {},
                'port_conflict': None, 'strategy_note': None,
            }

        return {
            'host': cls.HOST,
            'kernel': '6.1.0-test',
            'now': 1000.0,
            'mem': {'total_bytes': 256 * _GIB, 'available_bytes': 200 * _GIB},
            'sources': {'journal': 'ok', 'systemctl': 'ok'},
            'self_port': cls.port,
            'self_unit': {'unit': 'roundhouse.service',
                          'unit_file_state': 'enabled', 'enabled': True},
            'units': [
                row('twin1.service', 'READY', 9110, 'twin', False, enabled=True),
                row('twin2.service', 'OFF', 9110, 'twin', False, enabled=False),
                row('on-demand.service', 'OFF', 9111, 'on-demand', True, enabled=False),
                row('plain.service', 'OFF', 9112, None, False, enabled=False),
            ],
        }

    def setUp(self):
        type(self).snap = self._default_snapshot()
        roundhouse.ACTUATE_ARMED = True
        roundhouse.TOKEN = 'test-token'
        self.engine.current = None
        self.engine.rollouts = {}
        self.engine.counter = 0

    def _request(self, method, path, data=None, headers=None, token='test-token'):
        """Make direct HTTP request."""
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            req_headers = dict(headers or {})
            if token is not None and 'Authorization' not in req_headers:
                req_headers['Authorization'] = f'Bearer {token}'
            if method == 'POST' and data is not None:
                req_headers['Content-Type'] = 'application/json'
            body = json.dumps(data).encode('utf-8') if data is not None else None
            conn.request(method, path, body, req_headers)
            resp = conn.getresponse()
            resp_body = resp.read()
            try:
                return (resp.status, json.loads(resp_body))
            except json.JSONDecodeError:
                return (resp.status, resp_body.decode('utf-8', errors='replace'))
        finally:
            conn.close()

    def _call_mcp_tool(self, tool_name, arguments, token='test-token'):
        """Call an MCP tool via in-process handle_message, with token."""
        # Set env for token resolution
        old_token = os.environ.get('ROUNDHOUSE_TOKEN')
        os.environ['ROUNDHOUSE_TOKEN'] = token
        try:
            state = {
                'url': f'http://127.0.0.1:{self.port}',
                'client_name': 'test-client'
            }
            msg = {
                'jsonrpc': '2.0',
                'id': 1,
                'method': 'tools/call',
                'params': {
                    'name': tool_name,
                    'arguments': arguments
                }
            }
            response = roundhouse_mcp.handle_message(msg, state)
            # Parse the tool result text
            if response.get('result') and response['result'].get('content'):
                text = response['result']['content'][0]['text']
                return json.loads(text), response['result'].get('isError', False)
            return None, response['result'].get('isError', False)
        finally:
            if old_token is not None:
                os.environ['ROUNDHOUSE_TOKEN'] = old_token
            elif 'ROUNDHOUSE_TOKEN' in os.environ:
                del os.environ['ROUNDHOUSE_TOKEN']

    def _assert_fidelity(self, mcp_result, is_error, http_status, http_body):
        """The acceptance rule: tool-result JSON minus http_status == the HTTP body,
        field for field, and a refusal is never an error."""
        self.assertFalse(is_error, 'a refusal below 500 must carry isError: false')
        self.assertEqual(mcp_result['http_status'], http_status)
        stripped = {k: v for k, v in mcp_result.items() if k != 'http_status'}
        self.assertEqual(stripped, http_body)

    def test_consent_unfittable_422_fidelity(self):
        """422 consent_unfittable through a REAL MCP SUBPROCESS: the full warm
        arithmetic (estimate, headroom, shortfall, consenting, excluded_unmarked)
        survives field-for-field. This is the SPEC Risk #1 pipe leg."""
        # Squeeze memory so the plan cannot fit, and leave twin1 (READY, UNMARKED)
        # standing so `excluded_unmarked` is non-empty — the claimant reasoning an
        # agent needs in order to explain why nothing could be freed.
        snap = self._default_snapshot()
        snap['mem']['available_bytes'] = 64 * 1024 * 1024
        type(self).snap = snap

        http_status, http_body = self._request(
            'POST', '/api/warm', data={'unit': 'on-demand.service'})
        self.assertEqual(http_status, 422)
        self.assertEqual(http_body['error'], 'consent_unfittable')
        self.assertTrue(http_body['excluded_unmarked'],
                        'fixture must leave an unmarked active unit to be excluded')
        self.assertEqual([r['unit'] for r in http_body['excluded_unmarked']], ['twin1.service'])

        client = McpPipeClient(f'http://127.0.0.1:{self.port}', token='test-token')
        try:
            client.initialize()
            mcp_result, is_error = client.call_tool('warm', {'unit': 'on-demand.service'})
        finally:
            client.close()

        self._assert_fidelity(mcp_result, is_error, 422, http_body)
        # Named explicitly so a "helpful" summarising wrapper cannot pass by accident.
        for field in ('estimate_bytes', 'estimate_source', 'mem_available_bytes',
                      'headroom_bytes', 'freed_by', 'shortfall_bytes',
                      'consenting', 'excluded_unmarked', 'detail', 'unit'):
            self.assertIn(field, mcp_result, field)
            self.assertEqual(mcp_result[field], http_body[field], field)

    def test_not_on_demand_422_fidelity(self):
        """422 not_on_demand: warming an unmarked unit is a refusal, not an error."""
        http_status, http_body = self._request(
            'POST', '/api/warm', data={'unit': 'plain.service'})
        self.assertEqual(http_status, 422)
        self.assertEqual(http_body['error'], 'not_on_demand')

        mcp_result, is_error = self._call_mcp_tool('warm', {'unit': 'plain.service'})
        self._assert_fidelity(mcp_result, is_error, 422, http_body)
        self.assertEqual(mcp_result['unit'], 'plain.service')
        self.assertIn('on-demand', mcp_result['detail'])

    def test_operation_in_progress_409_fidelity(self):
        """409 operation_in_progress: the slot holder's id and kind reach the agent."""
        held = {
            'rollout_id': 'sw-fixture-hold', 'kind': 'switch', 'unit': 'twin1.service',
            'target': 'twin1.service', 'stops': [], 'stopped': [], 'target_started': False,
            'phase': 'stopping', 'detail': 'stopping twin1.service (1/1)',
            'failure': None, 'rollback': None, 'restored': False,
            'started_at': 1.0, 'updated_at': 1.0, 'origin': 'human', 'requester': None,
        }
        self.engine.current = held
        self.engine.rollouts[held['rollout_id']] = held
        self.assertFalse(roundhouse._slot_free(held))

        args = {'target': 'on-demand.service', 'stops': [], 'confirm': 'stale-confirm'}
        http_status, http_body = self._request('POST', '/api/switch', data=args)
        self.assertEqual(http_status, 409)
        self.assertEqual(http_body['error'], 'operation_in_progress')

        mcp_result, is_error = self._call_mcp_tool('switch_execute', args)
        self._assert_fidelity(mcp_result, is_error, 409, http_body)
        self.assertEqual(mcp_result['rollout_id'], 'sw-fixture-hold')
        self.assertEqual(mcp_result['kind'], 'switch')

    def test_enable_collision_422_fidelity(self):
        """422 enable_collision: MCP result matches direct HTTP call."""
        # twin2 can't be enabled; twin1 already is
        http_status, http_body = self._request(
            'POST',
            '/api/units/twin2.service/enablement',
            data={'enabled': True}
        )
        self.assertEqual(http_status, 422)
        self.assertIn('error', http_body)
        self.assertEqual(http_body['error'], 'enable_collision')
        self.assertIn('claimants', http_body)

        # Call via MCP
        mcp_result, is_error = self._call_mcp_tool(
            'set_boot',
            {'unit': 'twin2.service', 'enabled': True}
        )
        self._assert_fidelity(mcp_result, is_error, 422, http_body)
        self.assertEqual(mcp_result['error'], 'enable_collision')
        self.assertTrue(mcp_result['claimants'], 'claimants must reach the agent')

    def test_read_only_mode_403_fidelity(self):
        """403 read_only_mode: MCP result matches direct HTTP call."""
        # Test with read-only mode (no token/armament)
        roundhouse.ACTUATE_ARMED = False
        try:
            # Direct HTTP call to an action endpoint with no actuate armed
            http_status, http_body = self._request(
                'POST',
                '/api/units/plain.service/enablement',
                data={'enabled': True},
                token='test-token'
            )
            self.assertEqual(http_status, 403)
            self.assertIn('error', http_body)
            self.assertEqual(http_body['error'], 'read_only_mode')

            # Call via MCP (MCP should pass through the 403 structured)
            mcp_result, is_error = self._call_mcp_tool(
                'set_boot',
                {'unit': 'plain.service', 'enabled': True},
                token='test-token'
            )
            self._assert_fidelity(mcp_result, is_error, 403, http_body)
            self.assertEqual(mcp_result['error'], 'read_only_mode')
        finally:
            roundhouse.ACTUATE_ARMED = True

    def test_bad_token_401_fidelity(self):
        """401 bad_token: MCP result matches direct HTTP call."""
        bad_token = 'bad-token-123'
        # Direct HTTP call with bad token
        http_status, http_body = self._request(
            'POST',
            '/api/units/plain.service/enablement',
            data={'enabled': True},
            token=bad_token
        )
        self.assertEqual(http_status, 401)

        # Call via MCP with bad token (set via env)
        os.environ['ROUNDHOUSE_TOKEN'] = bad_token
        try:
            state = {
                'url': f'http://127.0.0.1:{self.port}',
                'client_name': 'test-client'
            }
            msg = {
                'jsonrpc': '2.0',
                'id': 1,
                'method': 'tools/call',
                'params': {
                    'name': 'set_boot',
                    'arguments': {'unit': 'plain.service', 'enabled': True}
                }
            }
            response = roundhouse_mcp.handle_message(msg, state)
            text = response['result']['content'][0]['text']
            mcp_result = json.loads(text)
            is_error = response['result'].get('isError', False)

            self._assert_fidelity(mcp_result, is_error, 401, http_body)
        finally:
            if 'ROUNDHOUSE_TOKEN' in os.environ:
                del os.environ['ROUNDHOUSE_TOKEN']


class TestScriptedSession(unittest.TestCase):
    """MVP6.md's scripted-session acceptance row, end to end over REAL PIPES.

    initialize -> fleet_status -> unit_detail -> switch_preview (tick a stop) ->
    switch_execute with the confirm FROM the preview -> operation_status polled to
    done -> a deliberately failing op -> operation_rollback -> restored ->
    a second failing op -> operation_dismiss -> set_boot both ways ->
    stale confirm -> 409 preview_stale -> warm -> warm_state -> warm_cancel.

    The fleet is scripted from the lifecycle verbs (the recorder gateway mutates the
    roster) rather than canned: the confirmed-OFF gate and the watch freshness gate
    are timing rules and only mean something against a roster that changes when — and
    only when — something was actually stopped or started.
    """

    HOST = 'testhost'
    ADVERTISE = 'advertise.example'

    # unit -> (alias, port, on_demand, rung, enabled)
    FLEET = (
        ('main.service', 'main', 9121, False, 'READY', True),
        ('alt.service', 'alt', 9122, False, 'OFF', False),
        ('spare.service', 'spare', 9123, True, 'OFF', False),
        ('extra.service', 'extra', 9124, False, 'OFF', False),
    )

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.mkdtemp()

        cls.units = {}
        for uname, alias, port, on_demand, _rung, enabled in cls.FLEET:
            u = _mvp5_unit(uname, alias, port, on_demand=on_demand, enabled=enabled)
            cls.units[u.name] = u

        cls.watcher = MagicMock(spec=roundhouse.Watcher)
        cls.watcher.lock = threading.Lock()
        cls.watcher.units = cls.units
        cls.watcher.mem_store = None
        cls.watcher._cgroup_cache = {}
        cls.watcher.snapshot.side_effect = lambda: cls._live_snapshot()

        cls.event_bus = roundhouse.EventBus()

        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        cls.port = sock.getsockname()[1]
        sock.close()

        roundhouse.ACTUATE_ARMED = True
        roundhouse.TOKEN = 'test-token'

        cls.server = roundhouse.ThreadingHTTPServer(
            ('127.0.0.1', cls.port),
            roundhouse.RoundhouseRequestHandler,
            cls.watcher,
            cls.event_bus,
            cls.port,
            watcher_lock=cls.watcher.lock,
            advertise_host=cls.ADVERTISE,
        )
        cls.engine = roundhouse.RolloutEngine(
            cls.watcher, cls.units, cls.temp_dir, cls.port,
            cls.event_bus, cls.watcher.lock)
        cls.server.rollout_engine = cls.engine

        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.temp_dir, ignore_errors=True)
        roundhouse.ACTUATE_ARMED = False
        roundhouse.TOKEN = None

    # --- the scripted roster ---------------------------------------------------

    @classmethod
    def _reset_rows(cls):
        cls.rows = {}
        for uname, alias, port, on_demand, rung, enabled in cls.FLEET:
            cls.rows[uname] = {
                'unit': uname, 'description': '', 'retired': False, 'rung': rung,
                'roster': 'cold', 'since': 100.0 if rung == 'READY' else 0,
                'start_ts_mono': str(port), 'detail': '', 'badges': [], 'stale': False,
                'enabled': enabled, 'active_state': 'inactive', 'sub_state': 'dead',
                'n_restarts': 0, 'port': port, 'port_source': 'flag', 'alias': alias,
                'on_demand': on_demand, 'gate': None, 'model_file': '', 'quant_hint': None,
                'ctx': None, 'mem': {}, 'port_conflict': None, 'strategy_note': None,
            }
        cls.unit_file_state = {u: ('enabled' if e else 'disabled')
                               for u, _a, _p, _o, _r, e in cls.FLEET}
        cls.start_plan = {}     # unit -> 'ready' (default) | 'failed'
        cls.stop_plan = {}      # unit -> 'off' (default) | 'failed'

    @classmethod
    def _live_snapshot(cls):
        """Like Watcher.snapshot(): lock-free, and `sensed_at` is always NOW — the
        confirm-off gate demands a sample sensed after the stop command returned."""
        now = time.time()
        return {
            'host': cls.HOST, 'kernel': '6.1.0-test', 'now': now,
            'mem': {'total_bytes': 256 * _GIB, 'available_bytes': 200 * _GIB},
            'sources': {'journal': 'ok', 'systemctl': 'ok'},
            'self_port': cls.port,
            'self_unit': {'unit': 'roundhouse.service',
                          'unit_file_state': 'enabled', 'enabled': True},
            'units': [dict(row, sensed_at=now) for row in cls.rows.values()],
        }

    def setUp(self):
        type(self)._reset_rows()
        roundhouse.ACTUATE_ARMED = True
        roundhouse.TOKEN = 'test-token'
        self.engine.current = None
        self.engine.rollouts = {}
        self.engine.counter = 0
        self.engine.pending_warm = None
        self.engine.last_warm = None
        self.engine.warm_seq = 0
        self.actuate_calls = []

        patches = [
            patch.object(roundhouse, 'run_actuate', self._stub_actuate),
            patch.object(roundhouse, 'run_ro', self._stub_run_ro),
            # The fixtures name model files that do not exist, so the estimator would
            # fall back to its 9 GiB default and the fit check would depend on the build
            # box's free RAM. Memory arithmetic is TestSwitchPreflight's subject.
            patch.object(roundhouse, '_estimate_start_bytes',
                         lambda unit, profile, store: (64 * 1024 * 1024, 'test-injected')),
            # A switch touches neither the git gateway nor the file writer; if the MCP
            # layer ever grew its own actuation path, these would fire.
            patch.object(roundhouse, 'run_git', self._forbidden('run_git')),
            patch.object(roundhouse, '_atomic_write', self._forbidden('_atomic_write')),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _forbidden(name):
        def boom(*a, **k):
            raise AssertionError(f'{name} must never be called in this session: {a}')
        return boom

    def _stub_actuate(self, argv, units, timeout=90):
        """The ONLY actuation gateway: records the verb and moves the scripted roster."""
        self.actuate_calls.append(list(argv))
        verb, name = argv[2], argv[-1]
        cls = type(self)
        row = cls.rows.get(name)
        if verb == 'stop' and row is not None:
            row['rung'] = 'FAILED' if cls.stop_plan.get(name) == 'failed' else 'OFF'
        elif verb == 'start' and row is not None:
            row['since'] = time.time()
            row['start_ts_mono'] = str(int(time.time() * 1e6))
            row['rung'] = 'FAILED' if cls.start_plan.get(name) == 'failed' else 'READY'
        elif verb in ('enable', 'disable') and row is not None:
            cls.unit_file_state[name] = 'enabled' if verb == 'enable' else 'disabled'
            row['enabled'] = (verb == 'enable')
        return ''

    def _stub_run_ro(self, argv, timeout=None, **kwargs):
        """handle_enablement's read-back (`systemctl show -p UnitFileState`)."""
        name = argv[-1]
        return f"UnitFileState={type(self).unit_file_state.get(name, 'disabled')}\n"

    # --- helpers ---------------------------------------------------------------

    def _client(self, token='test-token', client_name='pipe-client'):
        client = McpPipeClient(f'http://127.0.0.1:{self.port}', token=token,
                               client_name=client_name)
        self.addCleanup(client.close)
        return client

    def _poll_operation(self, client, rollout_id, terminals, timeout=30.0):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last, is_error = client.call_tool('operation_status', {'rollout_id': rollout_id})
            self.assertFalse(is_error)
            self.assertEqual(last['http_status'], 200)
            if last.get('phase') in terminals:
                return last
            time.sleep(0.2)
        self.fail(f'operation {rollout_id} never settled; last: {last!r}')

    def _preview_and_execute(self, client, target, stops):
        """The two-step: preview, then execute with the confirm FROM the preview."""
        preview, is_error = client.call_tool(
            'switch_preview', {'target': target, 'stops': stops})
        self.assertFalse(is_error)
        self.assertEqual(preview['http_status'], 200,
                         f'preview refused: {preview!r}')
        self.assertTrue(preview.get('confirm'), 'preview carried no confirm')
        self.assertTrue(all(c['ok'] for c in preview['checks']))
        ticked = [c for c in preview['stop_candidates'] if c['ticked']]
        self.assertEqual([c['unit'] for c in ticked], stops)

        execute, is_error = client.call_tool(
            'switch_execute',
            {'target': target, 'stops': stops, 'confirm': preview['confirm']})
        self.assertFalse(is_error)
        self.assertEqual(execute['http_status'], 202, f'execute refused: {execute!r}')
        self.assertTrue(execute.get('rollout_id'))
        return preview, execute

    # --- the session -----------------------------------------------------------

    def test_full_session_flow(self):
        client = self._client()

        # 1. initialize
        init = client.initialize()
        self.assertEqual(init['serverInfo']['name'], 'roundhouse-mcp')
        self.assertEqual(init['protocolVersion'], '2025-06-18')

        # 2. fleet_status: armed, four units, no operation
        fleet, is_error = client.call_tool('fleet_status')
        self.assertFalse(is_error)
        self.assertEqual(fleet['http_status'], 200)
        self.assertEqual(fleet['host'], self.HOST)
        self.assertEqual(fleet['mode'], 'actuate')
        self.assertEqual(fleet['n_units'], 4)
        self.assertIsNone(fleet['operation'])
        self.assertEqual({u['unit'] for u in fleet['units']},
                         {u for u, *_ in self.FLEET})

        # 3. unit_detail
        detail, is_error = client.call_tool('unit_detail', {'unit': 'main.service'})
        self.assertFalse(is_error)
        self.assertEqual(detail['http_status'], 200)
        self.assertEqual(detail['unit'], 'main.service')

        # 4-5. switch_preview (tick main) -> switch_execute with THAT confirm
        preview, execute = self._preview_and_execute(client, 'alt.service', ['main.service'])
        first_id = execute['rollout_id']

        # 6. operation_status polled to done
        settled = self._poll_operation(client, first_id, ('done', 'failed', 'restored'))
        self.assertEqual(settled['phase'], 'done', settled.get('detail'))
        self.assertEqual(settled['kind'], 'switch')
        self.assertEqual(settled['stopped'], ['main.service'])
        self.assertEqual(self.rows['alt.service']['rung'], 'READY')
        self.assertEqual(self.rows['main.service']['rung'], 'OFF')

        # 7. the operation is visible on fleet_status (I5 `operation` passthrough)
        fleet, _ = client.call_tool('fleet_status')
        self.assertIsNotNone(fleet['operation'], 'fleet_status lost the operation record')
        self.assertEqual(fleet['operation']['rollout_id'], first_id)
        self.assertEqual(fleet['operation']['phase'], 'done')

        # 8-10. a deliberately failing op -> rollback offered
        type(self).start_plan['main.service'] = 'failed'
        _preview2, execute2 = self._preview_and_execute(client, 'main.service', ['alt.service'])
        failed_id = execute2['rollout_id']
        failed = self._poll_operation(client, failed_id, ('failed', 'done', 'restored'))
        self.assertEqual(failed['phase'], 'failed')
        self.assertEqual(failed['failure']['reason'], 'unit_failed')
        self.assertTrue(failed['rollback']['offered'],
                        'a failed switch that stopped a unit must offer restore')

        # 11-12. operation_rollback -> restored
        rb, is_error = client.call_tool('operation_rollback', {'rollout_id': failed_id})
        self.assertFalse(is_error)
        self.assertEqual(rb['http_status'], 202)
        self.assertEqual(rb['rollout_id'], failed_id)
        restored = self._poll_operation(client, failed_id, ('restored', 'restore_failed'))
        self.assertEqual(restored['phase'], 'restored', restored.get('detail'))
        self.assertEqual(self.rows['alt.service']['rung'], 'READY',
                         'the stopped unit was not put back')

        # 13-16. a second failing op -> operation_dismiss releases the slot
        type(self).start_plan['extra.service'] = 'failed'
        _preview3, execute3 = self._preview_and_execute(client, 'extra.service', ['alt.service'])
        dismiss_id = execute3['rollout_id']
        failed3 = self._poll_operation(client, dismiss_id, ('failed', 'done', 'restored'))
        self.assertEqual(failed3['phase'], 'failed')
        self.assertTrue(failed3['rollback']['offered'])

        dm, is_error = client.call_tool('operation_dismiss', {'rollout_id': dismiss_id})
        self.assertFalse(is_error)
        self.assertEqual(dm['http_status'], 200)
        self.assertTrue(dm['ok'])
        after = self._poll_operation(client, dismiss_id, ('failed',))
        self.assertFalse(after['rollback']['offered'])
        self.assertTrue(roundhouse._slot_free(self.engine.current),
                        'dismiss must release the operation slot')

        # 17. set_boot both ways
        on, is_error = client.call_tool('set_boot', {'unit': 'alt.service', 'enabled': True})
        self.assertFalse(is_error)
        self.assertEqual(on['http_status'], 200)
        self.assertTrue(on['enabled'])
        self.assertFalse(on['was'])
        self.assertTrue(on['changed'])

        off, is_error = client.call_tool('set_boot', {'unit': 'alt.service', 'enabled': False})
        self.assertEqual(off['http_status'], 200)
        self.assertFalse(off['enabled'])
        self.assertTrue(off['was'])
        self.assertTrue(off['changed'])
        self.assertIn(['systemctl', '--user', 'enable', '--', 'alt.service'],
                      self.actuate_calls)
        self.assertIn(['systemctl', '--user', 'disable', '--', 'alt.service'],
                      self.actuate_calls)

        # 18. two-step discipline: a fabricated confirm is 409 preview_stale, and the
        #     wrapper neither re-previews nor retries (I4).
        stale, is_error = client.call_tool(
            'switch_execute',
            {'target': 'spare.service', 'stops': [], 'confirm': 'deadbeef' * 8})
        self.assertFalse(is_error)
        self.assertEqual(stale['http_status'], 409)
        self.assertEqual(stale['error'], 'preview_stale')
        self.assertEqual(self.rows['spare.service']['rung'], 'OFF',
                         'a refused execute must not have actuated anything')

        # 19. warm -> warm_state -> warm_cancel, with the slot held so the request parks
        held = {'rollout_id': 'sw-held', 'kind': 'switch', 'phase': 'stopping',
                'unit': 'main.service', 'detail': 'held for the warm leg',
                'failure': None, 'rollback': None, 'restored': False,
                'stopped': [], 'target_started': False,
                'started_at': time.time(), 'updated_at': time.time(),
                'origin': 'human', 'requester': None}
        self.engine.current = held
        self.engine.rollouts[held['rollout_id']] = held

        warm, is_error = client.call_tool('warm', {'unit': 'spare.service'})
        self.assertFalse(is_error)
        self.assertEqual(warm['http_status'], 202, warm)
        self.assertTrue(warm['queued'])
        self.assertEqual(warm['unit'], 'spare.service')

        state, is_error = client.call_tool('warm_state')
        self.assertFalse(is_error)
        self.assertEqual(state['http_status'], 200)
        self.assertIsNotNone(state['pending'])
        self.assertEqual(state['pending']['unit'], 'spare.service')
        # I6: the requester default is `mcp-<clientInfo.name>` (H7's charset has no ':')
        self.assertEqual(state['pending']['requester'], 'mcp-pipe-client')

        cancel, is_error = client.call_tool('warm_cancel')
        self.assertFalse(is_error)
        self.assertEqual(cancel['http_status'], 200)
        self.assertTrue(cancel['cancelled'])
        self.assertEqual(cancel['unit'], 'spare.service')

        state, _ = client.call_tool('warm_state')
        self.assertIsNone(state['pending'])
        self.assertEqual(state['last']['disposition'], 'cancelled')

        self.engine.current = None

        # 20. every actuation went through the one gateway, and only with the four verbs
        self.assertTrue(self.actuate_calls)
        for argv in self.actuate_calls:
            self.assertEqual(argv[:2], ['systemctl', '--user'])
            self.assertIn(argv[2], ('stop', 'start', 'enable', 'disable'))

    def test_explicit_requester_argument_overrides_the_default(self):
        """§4: `warm`'s optional `requester` replaces the I6 default (both sanitized)."""
        held = {'rollout_id': 'sw-held-2', 'kind': 'switch', 'phase': 'stopping',
                'unit': 'main.service', 'detail': 'held', 'failure': None,
                'rollback': None, 'restored': False, 'stopped': [],
                'target_started': False, 'started_at': time.time(),
                'updated_at': time.time(), 'origin': 'human', 'requester': None}
        self.engine.current = held
        self.engine.rollouts[held['rollout_id']] = held

        client = self._client()
        client.initialize()
        warm, is_error = client.call_tool(
            'warm', {'unit': 'spare.service', 'requester': 'bullpen:coder/1'})
        self.assertFalse(is_error)
        self.assertEqual(warm['http_status'], 202)

        state, _ = client.call_tool('warm_state')
        # ':' and '/' are outside the H7 charset and are dropped on both sides.
        self.assertEqual(state['pending']['requester'], 'bullpencoder1')
        self.engine.current = None

    def test_read_only_mode_structured_refusal(self):
        """Read tools work against a read-only Roundhouse; every action tool answers a
        structured `read_only_mode` refusal with isError: false (never an exception)."""
        roundhouse.ACTUATE_ARMED = False
        client = self._client()
        client.initialize()
        try:
            read_calls = {
                'fleet_status': {},
                'unit_detail': {'unit': 'main.service'},
                'port_board': {},
                'deployments': {},
                'routing_config': {},
                'warm_state': {},
            }
            for tool, args in read_calls.items():
                with self.subTest(tool=tool):
                    result, is_error = client.call_tool(tool, args)
                    self.assertFalse(is_error)
                    self.assertEqual(result['http_status'], 200, result)

            # operation_status is a read tool too, but wants an id: an unknown one is a
            # structured 404, still not an error.
            result, is_error = client.call_tool('operation_status', {'rollout_id': 'nope'})
            self.assertFalse(is_error)
            self.assertEqual(result['http_status'], 404)

            action_calls = {
                'switch_preview': {'target': 'alt.service'},
                'switch_execute': {'target': 'alt.service', 'confirm': 'x'},
                'edit_preview': {'unit': 'alt.service', 'edits': {'ctx': '4096'}},
                'edit_rollout': {'unit': 'alt.service', 'edits': {'ctx': '4096'},
                                 'confirm': 'x'},
                'set_boot': {'unit': 'alt.service', 'enabled': True},
                'warm': {'unit': 'spare.service'},
                'warm_cancel': {},
                'operation_rollback': {'rollout_id': 'nope'},
                'operation_dismiss': {'rollout_id': 'nope'},
            }
            for tool, args in action_calls.items():
                with self.subTest(tool=tool):
                    result, is_error = client.call_tool(tool, args)
                    self.assertFalse(is_error, f'{tool} raised instead of refusing')
                    self.assertEqual(result['http_status'], 403, result)
                    self.assertEqual(result['error'], 'read_only_mode')
            self.assertEqual(self.actuate_calls, [],
                             'a read-only instance actuated something')
        finally:
            roundhouse.ACTUATE_ARMED = True



class TestTwoStepDiscipline(unittest.TestCase):
    """Static assertion: no tool both computes a confirm and executes.

    Verify that switch_execute and edit_rollout require confirm in schema.
    Verify that roundhouse_mcp.py contains no 'compute_switch_confirm' or 'compute_confirm'.
    """

    def test_switch_execute_requires_confirm(self):
        """switch_execute schema requires confirm."""
        schema = roundhouse_mcp.TOOLS['switch_execute']['schema']
        self.assertIn('confirm', schema.get('required', []),
                     "switch_execute must require confirm in schema")

    def test_edit_rollout_requires_confirm(self):
        """edit_rollout schema requires confirm."""
        schema = roundhouse_mcp.TOOLS['edit_rollout']['schema']
        self.assertIn('confirm', schema.get('required', []),
                     "edit_rollout must require confirm in schema")

    def test_no_compute_confirm_in_mcp_file(self):
        """roundhouse_mcp.py contains no compute_switch_confirm or compute_confirm functions."""
        mcp_file = Path(__file__).resolve().parent.parent / 'roundhouse_mcp.py'
        content = mcp_file.read_text()
        self.assertNotIn('compute_switch_confirm', content,
                        "roundhouse_mcp.py must not contain compute_switch_confirm")
        self.assertNotIn('compute_confirm', content,
                        "roundhouse_mcp.py must not contain compute_confirm")


def _mvp5_unit(unit_name: str, alias: str or None, port: int, on_demand: bool = False,
               retired: bool = False, enabled: bool = False):
    """Build a parsed UnitFile for testing (similar to test_actuation.py)."""
    desc = '[RETIRED 2026-01-01] retired fixture' if retired else f'fixture {unit_name}'
    marker = '# roundhouse: on-demand\n' if on_demand else ''
    alias_arg = f' --alias {alias}' if alias else ''
    text = (
        '[Unit]\n'
        f'Description={desc}\n'
        '\n'
        '[Service]\n'
        f'{marker}'
        f'ExecStart=/usr/bin/llama-server -m /nonexistent/{unit_name}.gguf{alias_arg}'
        f' --host 0.0.0.0 --port {port}\n'
        '\n'
        '[Install]\n'
        'WantedBy=default.target\n'
    )
    parsed = roundhouse.parse_unit(f'/tmp/mvp5-units/{unit_name}', text.encode('utf-8'))
    # Parse doesn't set enabled, so do it manually
    parsed.unit_file_state = 'enabled' if enabled else 'disabled'
    return parsed


if __name__ == '__main__':
    unittest.main()
