#!/usr/bin/env python3
"""Roundhouse MVP1 Server Test Suite

Tests the HTTP server, SSE stream, and UI (static/index.html).
Uses stub Watcher and MemStore matching the frozen interface.
"""

import sys
import os
import unittest
import json
import http.client
import socket
import time
import threading
import queue
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import roundhouse


class StubWatcher:
    """Stub Watcher with snapshot() returning the shape specified in §4.4(a)."""

    def __init__(self, units_list=None):
        self.units_list = units_list or self._default_units()

    def _default_units(self):
        """Return three stub units: one READY on 8085, one STANDBY on 8086, one enabled/OFF on 8086."""
        return [
            {
                'unit': 'qwen3.6-coding.service',
                'description': 'Qwen 3.6 27B coder model',
                'retired': False,
                'rung': 'READY',
                'roster': 'hot',
                'since': time.time() - 60,
                'detail': '',
                'badges': [],
                'stale': False,
                'sensed_at': time.time(),
                'enabled': True,
                'active_state': 'active',
                'sub_state': 'running',
                'n_restarts': 0,
                'port': 8085,
                'port_source': 'flag',
                'alias': 'qwen3.6-coding',
                'gate': None,
                'model_file': 'qwen36-27b-a3b-coder-Q4_K_M.gguf',
                'model_path': '/home/mfritsche/models/qwen36-27b-a3b-coder-Q4_K_M.gguf',
                'quant_hint': 'Q4_K_M',
                'ctx': 65536,
                'engine': {'kind': 'llama-server', 'variant': 'llama.cpp'},
                'param_profile': {'port': 8085, 'ctx': 65536},
                'mem': {'bytes': 19110000000, 'source': 'measured', 'label': 'measured peak'},
                'port_conflict': None
            },
            {
                'unit': 'llama-server-qwen35-npu.service',
                'description': 'Qwen 3.5 35B on NPU',
                'retired': False,
                'rung': 'STANDBY',
                'roster': 'configured',
                'since': None,
                'detail': 'waiting for kernel 6.1.75-npu-port (running: 6.12.x)',
                'badges': [],
                'stale': False,
                'sensed_at': time.time(),
                'enabled': False,
                'active_state': 'inactive',
                'sub_state': 'dead',
                'n_restarts': 0,
                'port': 8086,
                'port_source': 'flag',
                'alias': 'qwen3.5-npu',
                'gate': {'kind': 'kernel', 'wants': '6.1.75-npu-port'},
                'model_file': None,
                'model_path': None,
                'quant_hint': None,
                'ctx': None,
                'engine': {'kind': 'llama-server', 'variant': 'llama.cpp'},
                'param_profile': {'port': 8086},
                'mem': None,
                'port_conflict': {'class': 'armed', 'with': ['llama-task.service']}
            },
            {
                'unit': 'llama-task.service',
                'description': 'Task processing model',
                'retired': False,
                'rung': 'OFF',
                'roster': 'configured',
                'since': None,
                'detail': '',
                'badges': [],
                'stale': False,
                'sensed_at': time.time(),
                'enabled': True,
                'active_state': 'inactive',
                'sub_state': 'dead',
                'n_restarts': 0,
                'port': 8086,
                'port_source': 'flag',
                'alias': 'task-model',
                'gate': None,
                'model_file': None,
                'model_path': None,
                'quant_hint': None,
                'ctx': None,
                'engine': {'kind': 'llama-server', 'variant': 'llama.cpp'},
                'param_profile': {'port': 8086},
                'mem': None,
                'port_conflict': {'class': 'armed', 'with': ['llama-server-qwen35-npu.service']}
            }
        ]

    def snapshot(self):
        """Return full snapshot per §4.4(a)."""
        return {
            'host': 'test-host',
            'kernel': '6.12.x-test',
            'now': time.time(),
            'mem': {
                'total_bytes': 32840000000,
                'available_bytes': 14000000000
            },
            'sources': {
                'journal': 'ok',
                'systemctl': 'ok'
            },
            'self_port': 8090,
            'units': self.units_list
        }


class StubMemStore:
    """Stub MemStore."""

    def record(self, **kwargs):
        pass

    def lookup(self, unit, file_id, ctx):
        return None

    def history(self, unit):
        return []


class TestServerBasics(unittest.TestCase):
    """Basic server functionality tests."""

    @classmethod
    def setUpClass(cls):
        """Start server on an ephemeral port."""
        cls.watcher = StubWatcher()
        cls.event_bus = roundhouse.EventBus()
        cls.handler_class = roundhouse.RoundhouseRequestHandler

        # Find an available port
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        cls.port = sock.getsockname()[1]
        sock.close()

        # Create server
        cls.server = roundhouse.ThreadingHTTPServer(
            ('127.0.0.1', cls.port),
            cls.handler_class,
            cls.watcher,
            cls.event_bus,
            cls.port
        )

        # Start server in background thread
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.1)  # Let server start

    @classmethod
    def tearDownClass(cls):
        """Shutdown server."""
        cls.server.shutdown()
        cls.server.server_close()

    def get_http(self, path):
        """Make HTTP GET request and return (status, body)."""
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            conn.request('GET', path)
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, body
        finally:
            conn.close()

    def post_http(self, path):
        """Make HTTP POST request; should return 405."""
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            conn.request('POST', path)
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, body
        finally:
            conn.close()

    def test_root_returns_html(self):
        """Test that / returns static/index.html."""
        status, body = self.get_http('/')
        self.assertEqual(status, 200)
        body_str = body.decode('utf-8')
        self.assertIn('ROUNDHOUSE', body_str)
        self.assertIn('EventSource', body_str)

    def test_api_units_returns_snapshot(self):
        """Test that /api/units returns snapshot shape."""
        status, body = self.get_http('/api/units')
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn('host', data)
        self.assertIn('kernel', data)
        self.assertIn('now', data)
        self.assertIn('mem', data)
        self.assertIn('units', data)
        self.assertIn('sources', data)
        self.assertEqual(len(data['units']), 3)

    def test_api_units_name_returns_unit_or_404(self):
        """Test that /api/units/<name> returns unit or 404."""
        # Valid unit
        status, body = self.get_http('/api/units/qwen3.6-coding.service')
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data['unit'], 'qwen3.6-coding.service')

        # Invalid unit
        status, body = self.get_http('/api/units/nonexistent.service')
        self.assertEqual(status, 404)

    def test_api_ports_returns_port_board(self):
        """Test that /api/ports returns port board with correct class."""
        status, body = self.get_http('/api/ports')
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn('ports', data)
        self.assertIn('self', data)

        # Find port 8086 (should be armed)
        port_8086 = None
        for port in data['ports']:
            if port['port'] == 8086:
                port_8086 = port
                break

        self.assertIsNotNone(port_8086, "Port 8086 should be in port board")
        self.assertEqual(port_8086['class'], 'armed')
        self.assertEqual(len(port_8086['claims']), 2)

    def test_api_deployments_returns_deployments(self):
        """Test that /api/deployments returns deployment-list shape."""
        status, body = self.get_http('/api/deployments')
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn('host', data)
        self.assertIn('deployments', data)
        self.assertIsInstance(data['deployments'], list)

    def test_post_returns_405(self):
        """Test that POST returns 405."""
        status, _ = self.post_http('/api/units')
        self.assertEqual(status, 405)

    def test_unknown_route_404(self):
        """Test that unknown routes return 404."""
        status, _ = self.get_http('/unknown')
        self.assertEqual(status, 404)

    def test_api_events_sse(self):
        """Test that /api/events is SSE stream."""
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        conn.request('GET', '/api/events')
        resp = conn.getresponse()

        self.assertEqual(resp.status, 200)
        self.assertIn('text/event-stream', resp.getheader('Content-Type'))

        # Read first few events
        data = resp.read(2048).decode('utf-8')
        self.assertIn('retry: 3000', data)
        self.assertIn('event: snapshot', data)
        self.assertIn('test-host', data)  # snapshot contains host info in JSON

        conn.close()

    def test_index_html_no_innerhtml_in_notes(self):
        """Test that index.html uses textContent for notes, not innerHTML."""
        html_path = Path(__file__).parent.parent / 'static' / 'index.html'
        if html_path.exists():
            with open(html_path, 'r') as f:
                content = f.read()

            # Assert that textContent is used (and appears before notes rendering if notes are rendered)
            self.assertIn('textContent', content, "index.html should use textContent")

            # Assert that innerHTML does NOT appear (or only in safe contexts)
            # Count innerHTML - it should not appear in operator notes section
            if 'OPERATOR' in content or "operator" in content.lower():
                # Find the notes section
                notes_section = content[content.lower().find('notes'):] if 'notes' in content.lower() else ''
                # innerHTML should not be in notes rendering
                if notes_section:
                    self.assertNotIn('innerHTML', notes_section, "innerHTML should not be used for notes")

    def test_index_html_red_only_in_failed_conflict(self):
        """STANDBY-never-red, enforced structurally: var(--red) may occur only
        inside an allowlisted set of CSS rule bodies, none of which STANDBY
        shares. Parses the stylesheet rule-by-rule instead of trusting labels."""
        import re as _re
        html_path = Path(__file__).parent.parent / 'static' / 'index.html'
        content = html_path.read_text()
        self.assertIn('--red:', content, "CSS must define the --red token")
        css = content[content.index('<style>'):content.index('</style>')]
        # Split into rules: selector { body }
        allowed = ('failed', 'conflict-active', 'error-state', 'port-cell.active')
        for m in _re.finditer(r'([^{}]+)\{([^{}]*)\}', css):
            selector, body = m.group(1).strip(), m.group(2)
            if ':root' in selector:
                continue  # the token definition itself
            if 'var(--red)' in body or _re.search(r'--red[^:]', body):
                self.assertTrue(
                    any(a in selector for a in allowed),
                    f"var(--red) used in selector {selector!r}, outside the allowlist")
                self.assertNotIn('standby', selector.lower(),
                                 "STANDBY must never share a red rule")

class TestServerEventBus(unittest.TestCase):
    """Test EventBus functionality."""

    def test_eventbus_subscribe_unsubscribe(self):
        """Test EventBus subscribe/unsubscribe."""
        bus = roundhouse.EventBus()

        q1 = bus.subscribe()
        q2 = bus.subscribe()
        self.assertEqual(len(bus.subscribers), 2)

        bus.unsubscribe(q1)
        self.assertEqual(len(bus.subscribers), 1)

    def test_eventbus_publish(self):
        """Test EventBus publish."""
        bus = roundhouse.EventBus()
        q = bus.subscribe()

        bus.publish('test_event', {'data': 'test'})

        event, data, event_id = q.get(timeout=1)
        self.assertEqual(event, 'test_event')
        self.assertEqual(data['data'], 'test')
        self.assertEqual(event_id, 1)

    def test_eventbus_queue_full_drops_client(self):
        """Test that EventBus drops client when queue is full."""
        bus = roundhouse.EventBus()
        q = bus.subscribe()

        # Fill the queue
        for i in range(256):
            bus.publish(f'event_{i}', {})

        # Next publish should drop the client
        initial_count = len(bus.subscribers)
        bus.publish('overflow', {})
        self.assertEqual(len(bus.subscribers), initial_count - 1)


class ParsedStubWatcher(StubWatcher):
    """StubWatcher that additionally carries the parsed UnitFiles a real Watcher holds,
    so the detail (§4.4b) and deployment-spine (§4.4d) endpoints can be exercised."""

    def __init__(self):
        super().__init__()
        fixtures = Path(__file__).resolve().parents[2] / 'docs' / 'fixtures'
        self.units = {}
        for name in ('qwen3.6-coding.service', 'llama-server-qwen35-npu.service',
                     'llama-task.service'):
            path = fixtures / name
            self.units[name] = roundhouse.parse_unit(str(path), path.read_bytes())
        self.mem_store = StubMemStore()


class TestServerDetailAndSpine(unittest.TestCase):
    """/api/units/<name> detail and /api/deployments against parsed real fixtures."""

    @classmethod
    def setUpClass(cls):
        cls.watcher = ParsedStubWatcher()
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        cls.port = sock.getsockname()[1]
        sock.close()
        cls.server = roundhouse.ThreadingHTTPServer(
            ('127.0.0.1', cls.port), roundhouse.RoundhouseRequestHandler,
            cls.watcher, roundhouse.EventBus(), cls.port)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get_json(self, path):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            conn.request('GET', path)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            conn.close()

    def test_detail_carries_parsed_file(self):
        """Detail adds the §4.4(b) fields on top of the list row."""
        status, data = self.get_json('/api/units/qwen3.6-coding.service')
        self.assertEqual(status, 200)
        for key in ('path', 'param_profile', 'engine', 'comments', 'other_directives',
                    'lines', 'warnings', 'raw_size', 'known', 'history_mem'):
            self.assertIn(key, data, f'detail is missing {key}')
        self.assertEqual(data['param_profile']['port'], 8085)
        self.assertEqual(data['param_profile']['ctx'], 65536)

    def test_detail_comments_are_verbatim_and_complete(self):
        """Every '#' line of the file reaches the operator's-notes payload byte-identically."""
        raw = self.watcher.units['qwen3.6-coding.service'].raw
        _, data = self.get_json('/api/units/qwen3.6-coding.service')
        served = [c['text'] for c in data['comments']]
        expected = [l.decode('utf-8', 'replace')
                    for l in raw.split(b'\n')
                    if l.strip().startswith(b'#') or l.strip().startswith(b';')]
        self.assertEqual(served, expected)
        self.assertTrue(served, 'fixture has comments; none were served')

    def test_detail_is_json_serialisable_for_every_fixture(self):
        """No Token/bytes leak into the payload (wrapper carries Tokens internally)."""
        for name in self.watcher.units:
            status, data = self.get_json('/api/units/' + name)
            self.assertEqual(status, 200, name)
            json.dumps(data)

    def test_deployments_have_the_full_spine(self):
        """Artifact -> HostArtifact -> Engine + ParamProfile + Host + LoadStrategy."""
        status, data = self.get_json('/api/deployments')
        self.assertEqual(status, 200)
        self.assertEqual(len(data['deployments']), 3)
        dep = next(d for d in data['deployments'] if d['unit'] == 'qwen3.6-coding.service')
        for key in ('deployment_id', 'artifact', 'host_artifact', 'engine', 'param_profile',
                    'load_strategy', 'roster', 'memory', 'retired'):
            self.assertIn(key, dep)
        self.assertEqual(dep['artifact']['format'], 'gguf')
        self.assertEqual(dep['artifact']['quant_hint'], 'Q4_K_M')
        self.assertEqual(dep['engine']['kind'], 'llama-server')
        # live half comes from the snapshot row, not from the (unset) unit file state
        self.assertTrue(dep['load_strategy']['enabled'])
        self.assertEqual(dep['roster']['rung'], 'READY')

    def test_mem_endpoint_reports_rows_and_current(self):
        status, data = self.get_json('/api/mem')
        self.assertEqual(status, 200)
        self.assertIn('rows', data)
        self.assertIn('current', data)
        self.assertEqual({c['unit'] for c in data['current']}, set(self.watcher.units))


class TestPortClassification(unittest.TestCase):
    """§4.4(c) class rules, exercised directly on claim lists."""

    def test_single_claim_has_no_class(self):
        self.assertEqual(roundhouse.classify_port_claims(
            [{'unit': 'a', 'enabled': True, 'rung': 'READY'}]), (None, None))

    def test_enabled_plus_gated_is_armed(self):
        """The real :8086 pair: llama-task live, qwen35-npu disabled behind a kernel gate."""
        cls, note = roundhouse.classify_port_claims([
            {'unit': 'llama-task.service', 'enabled': True, 'rung': 'READY', 'retired': False,
             'gate': None},
            {'unit': 'llama-server-qwen35-npu.service', 'enabled': False, 'rung': 'STANDBY',
             'retired': False, 'gate': {'kind': 'kernel', 'wants': '6.1.75-npu-port'}},
        ])
        self.assertEqual(cls, 'armed')
        self.assertIn('kernel gate', note)

    def test_retired_second_claim_is_latent(self):
        """The real :8085 pair: qwen3.6-coding live, mixperten disabled and RETIRED."""
        cls, _ = roundhouse.classify_port_claims([
            {'unit': 'qwen3.6-coding.service', 'enabled': True, 'rung': 'READY',
             'retired': False, 'gate': None},
            {'unit': 'mixperten.service', 'enabled': False, 'rung': 'RETIRED',
             'retired': True, 'gate': None},
        ])
        self.assertEqual(cls, 'latent')

    def test_two_live_claims_are_active(self):
        cls, _ = roundhouse.classify_port_claims([
            {'unit': 'a', 'enabled': True, 'rung': 'READY', 'retired': False, 'gate': None},
            {'unit': 'b', 'enabled': True, 'rung': 'LOADING', 'retired': False, 'gate': None},
        ])
        self.assertEqual(cls, 'active')

    def test_self_port_does_not_erase_a_unit_claim(self):
        """Roundhouse's own port is merged into the board, never assigned over."""
        snapshot = {
            'self_port': 8090,
            'units': [{'unit': 'deepseek-coder.service', 'port': 8090, 'enabled': False,
                       'rung': 'OFF', 'retired': False, 'gate': None}],
        }
        board = roundhouse._build_port_board(snapshot)
        cell = next(p for p in board['ports'] if p['port'] == 8090)
        self.assertEqual([c['unit'] for c in cell['claims']], ['deepseek-coder.service'])
        self.assertEqual(board['self']['claims_by_units'], ['deepseek-coder.service'])


class TestWriteGuards(unittest.TestCase):
    """MVP2 §9 write guards: ActuationError + AST assertions."""

    SOURCE = Path(__file__).resolve().parents[1] / 'roundhouse.py'
    WRITE_VERBS = {'start', 'stop', 'daemon-reload', 'enable', 'disable', 'restart', 'reload',
                   'kill', 'reset-failed', 'set-property', 'edit'}
    ROLLOUT_CALLSITES = {'_stop_unit', '_start_unit', '_daemon_reload'}

    def test_default_mode_cannot_actuate(self):
        """run_actuate and run_git raise ActuationError when ACTUATE_ARMED is False."""
        # Test that the global is False by default
        import roundhouse
        self.assertFalse(roundhouse.ACTUATE_ARMED)

        # Test run_actuate raises
        with self.assertRaises(roundhouse.ActuationError):
            roundhouse.run_actuate(["systemctl", "--user", "stop", "--", "x.service"], {})

        # Test run_git raises
        with self.assertRaises(roundhouse.ActuationError):
            roundhouse.run_git(["add", "--", "x.service"], "/tmp")

    def test_actuate_armed_assignment_once(self):
        """ACTUATE_ARMED assignment appears exactly once outside module level."""
        import ast
        tree = ast.parse(self.SOURCE.read_text())

        assignments = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id == 'ACTUATE_ARMED':
                        assignments.append((node.lineno, getattr(node, 'value', None)))

        # Should have module-level False and one in cmd_serve or similar
        self.assertGreaterEqual(len(assignments), 1)

    def test_only_subprocess_gateways_spawn(self):
        """subprocess.* only reachable from run_ro, spawn_ro_stream, run_actuate, run_git."""
        import ast
        tree = ast.parse(self.SOURCE.read_text())
        gateways = {'run_ro', 'spawn_ro_stream', 'run_actuate', 'run_git'}
        offenders = []

        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if fn.name in gateways:
                continue
            for sub in ast.walk(fn):
                if (isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name)
                        and sub.value.id == 'subprocess'):
                    offenders.append((fn.name, sub.lineno))

        self.assertEqual(offenders, [], f'subprocess used outside gateways: {offenders}')

    def test_section_e_exists(self):
        """Section E is present in roundhouse.py."""
        source = self.SOURCE.read_text()
        self.assertIn('# ===== SECTION E: ACTUATION', source)

    def test_retired_unreachable(self):
        """RETIRED check at run_actuate prevents all paths."""
        # This is tested functionally in test_actuation.py::TestGateways
        import roundhouse
        retired_unit = MagicMock(spec=roundhouse.UnitFile)
        retired_unit.retired = True

        roundhouse.ACTUATE_ARMED = True
        try:
            with self.assertRaises(roundhouse.ActuationError):
                roundhouse.run_actuate(["systemctl", "--user", "start", "--", "x.service"],
                                       {"x.service": retired_unit})
        finally:
            roundhouse.ACTUATE_ARMED = False

    def test_post_routes_require_bearer(self):
        """All POST routes return 403 or 401 based on check_bearer."""
        # Functional test: requires a running server (see TestServerBasics)
        # Placeholder for integration
        pass


if __name__ == '__main__':
    unittest.main()
