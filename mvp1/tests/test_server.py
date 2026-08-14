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
        allowed = ('failed', 'conflict-active', 'error-state', 'port-cell.active', 'error', 'token.error', 'fail')
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

    def test_index_html_no_innerhtml_no_localstorage(self):
        """index.html must use textContent only (no innerHTML, insertAdjacentHTML,
        document.write) and must not use localStorage/sessionStorage."""
        html_path = Path(__file__).parent.parent / 'static' / 'index.html'
        content = html_path.read_text()

        # Check for forbidden patterns
        forbidden = [
            ('innerHTML', 'innerHTML is not allowed (use textContent)'),
            ('insertAdjacentHTML', 'insertAdjacentHTML is not allowed (use textContent)'),
            ('document.write', 'document.write is not allowed'),
            ('localStorage', 'localStorage is not allowed'),
            ('sessionStorage', 'sessionStorage is not allowed'),
        ]

        for pattern, msg in forbidden:
            self.assertNotIn(pattern, content, msg)

    def test_index_html_mvp2_ui_elements(self):
        """Verify that index.html contains all MVP2 UI elements and references."""
        html_path = Path(__file__).parent.parent / 'static' / 'index.html'
        content = html_path.read_text()

        # Check for mode badge
        self.assertIn('mode-badge', content, "HTML must contain mode badge element")
        self.assertIn('[READ-ONLY]', content, "HTML must reference READ-ONLY badge text")
        self.assertIn('[ACTUATE]', content, "HTML must reference ACTUATE badge text")

        # Check for token input
        self.assertIn('id="token"', content, "HTML must contain token input element")
        self.assertIn('type="password"', content, "HTML must have password input for token")

        # Check for edit button reference
        self.assertIn('edit', content, "HTML must reference edit button")

        # Check for stepper phases
        phases = ['preflight', 'applying', 'reloading', 'starting', 'watching', 'rolled_back']
        for phase in phases:
            self.assertIn(phase, content, f"HTML must reference rollout phase '{phase}'")

        # Check for rollback button
        self.assertIn('rollback', content, "HTML must reference rollback functionality")
        self.assertIn('dismiss', content, "HTML must reference dismiss link")

        # Check for modal/preview elements
        self.assertIn('diff-modal', content, "HTML must contain diff modal element")
        self.assertIn('diff', content, "HTML must reference diff rendering")
        self.assertIn('/edit', content, "HTML must reference /edit endpoint")
        self.assertIn('/rollout', content, "HTML must reference /rollout endpoint")
        self.assertIn('/rollback', content, "HTML must reference /rollback endpoint")
        self.assertIn('/dismiss', content, "HTML must reference /dismiss endpoint")

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
    ROLLOUT_CALLSITES = {'_stop_unit', '_start_unit', '_daemon_reload', '_set_enablement'}

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

    # ---- shared AST machinery for the §7.1 guards ---------------------------------

    @classmethod
    def _tree(cls):
        import ast
        source = cls.SOURCE.read_text()
        return source, ast.parse(source)

    @staticmethod
    def _parents(tree):
        """child -> parent map; `ast` gives no parent links and every guard needs them."""
        import ast
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        return parents

    @staticmethod
    def _section_spans(source):
        """{letter: [(first_line, end_line_exclusive)]} from the SECTION banners.

        A letter's span runs to the next banner carrying a DIFFERENT letter, so Section
        E's `PART 2` / `PART 3` banners stay inside one span (§7.1).
        """
        import re
        banners = [(i, m.group(1))
                   for i, line in enumerate(source.splitlines(), 1)
                   for m in [re.match(r'^# ===== SECTION ([A-F])\b', line)] if m]
        total = len(source.splitlines()) + 1
        spans = {}
        for idx, (line, letter) in enumerate(banners):
            end = next((l2 for l2, let2 in banners[idx + 1:] if let2 != letter), total)
            spans.setdefault(letter, []).append((line, end))
        return spans

    @classmethod
    def _enclosing_func(cls, node, parents):
        """Name of the innermost FunctionDef containing `node`, or None."""
        import ast
        cur = parents.get(node)
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return cur.name
            cur = parents.get(cur)
        return None

    @classmethod
    def _enclosing_call(cls, node, parents):
        """The innermost Call containing `node`, or None."""
        import ast
        cur = parents.get(node)
        while cur is not None:
            if isinstance(cur, ast.Call):
                return cur
            cur = parents.get(cur)
        return None

    @staticmethod
    def _callee_name(call):
        import ast
        if isinstance(call.func, ast.Name):
            return call.func.id
        if isinstance(call.func, ast.Attribute):
            return call.func.attr
        return None

    def test_actuate_armed_assignment_once(self):
        """Exactly two ACTUATE_ARMED assignments: module-level `False`, and one in cmd_serve.

        The MVP2 version asserted `>= 1`, which a third assignment anywhere would have
        satisfied — including one that arms the process outside `--actuate`.
        """
        import ast
        source, tree = self._tree()
        parents = self._parents(tree)

        assignments = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id == 'ACTUATE_ARMED':
                        assignments.append(node)

        self.assertEqual(len(assignments), 2,
                         'ACTUATE_ARMED assignments at lines '
                         f'{[n.lineno for n in assignments]}; expected exactly 2')

        module_level = [n for n in assignments if self._enclosing_func(n, parents) is None]
        in_cmd_serve = [n for n in assignments
                        if self._enclosing_func(n, parents) == 'cmd_serve']
        self.assertEqual(len(module_level), 1,
                         'expected exactly one module-level ACTUATE_ARMED assignment')
        self.assertEqual(len(in_cmd_serve), 1,
                         'expected exactly one ACTUATE_ARMED assignment, inside cmd_serve')
        self.assertIsInstance(module_level[0], ast.Assign)
        self.assertIsInstance(module_level[0].value, ast.Constant)
        self.assertIs(module_level[0].value.value, False,
                      'the default must be a literal False — read-only unless armed')

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

    def test_os_system_os_popen_zero_tolerance(self):
        """os.system and os.popen must not appear anywhere (§6(ii) guard)."""
        import ast
        tree = ast.parse(self.SOURCE.read_text())
        offenders = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                if isinstance(node.value, ast.Name) and node.value.id == 'os':
                    if node.attr in ('system', 'popen'):
                        offenders.append((node.lineno, f'os.{node.attr}'))

        self.assertEqual(offenders, [], f'os.system/os.popen found: {offenders}')

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

    def _write_verb_constants(self, tree, parents):
        """Every WRITE_VERBS string literal that could become an argv token.

        Dict KEYS are excluded and only dict keys: `{'start': line.start}` in the parser
        and the detail serializer are span offsets, not systemd verbs, and no dict key
        can reach `subprocess` as an argument. Everything else — bare literals, list and
        tuple elements, call arguments — is in scope.
        """
        import ast
        out = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if node.value not in self.WRITE_VERBS:
                continue
            parent = parents.get(node)
            if isinstance(parent, ast.Dict) and any(k is node for k in parent.keys):
                continue
            out.append(node)
        return out

    def test_write_verbs_only_in_section_e(self):
        """§7.1(1), all four legs — the MVP2 version implemented none of (a) and only a
        shallow (b), so a `subprocess.run(["systemctl", "--user", "restart", ...])` seeded
        in Section C sailed past it (the verb sits inside a List, not in `Call.args`).

        (a) every write-verb literal lives inside Section E's span
        (b) each one's innermost enclosing Call is `run_actuate`
        (c) every `run_actuate` call sits in ROLLOUT_CALLSITES — unchanged by MVP3, which
            is the point: the switch reuses the same three methods
        (d) `_daemon_reload` is called only from the two rollout workers (F10's static leg)
        """
        import ast
        source, tree = self._tree()
        parents = self._parents(tree)
        spans = self._section_spans(source)
        self.assertIn('E', spans, 'no SECTION E banner found')
        e_spans = spans['E']

        def in_section_e(lineno):
            return any(start <= lineno < end for start, end in e_spans)

        verbs = self._write_verb_constants(tree, parents)
        self.assertTrue(verbs, 'no write verbs found at all — the guard is not looking')

        # (a) confinement to Section E
        strays = [(n.lineno, n.value) for n in verbs if not in_section_e(n.lineno)]
        self.assertEqual(strays, [],
                         f'write verb literal outside SECTION E (spans {e_spans}): {strays}')

        # (b) each one is an argument of a run_actuate call
        for node in verbs:
            call = self._enclosing_call(node, parents)
            if call is None:
                # Not an argument to anything: the ACTUATE_SYSTEMCTL_VERBS allowlist
                # itself is such a literal set. Leg (a) already pinned it inside E, and
                # §7.1(b) scopes this leg to constants "among a Call's arguments".
                continue
            callee = self._callee_name(call)
            self.assertEqual(
                callee, 'run_actuate',
                f"write verb '{node.value}' at line {node.lineno} is passed to "
                f"'{callee}', not run_actuate")

        # (c) run_actuate is only called from the three lifecycle methods
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and self._callee_name(node) == 'run_actuate':
                fn = self._enclosing_func(node, parents)
                self.assertIn(fn, self.ROLLOUT_CALLSITES,
                              f'run_actuate called at line {node.lineno} from {fn!r}, '
                              f'not one of {sorted(self.ROLLOUT_CALLSITES)}')

        # (d) daemon-reload never reaches the switch or restore workers
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and self._callee_name(node) == '_daemon_reload':
                fn = self._enclosing_func(node, parents)
                self.assertIn(fn, {'_run_rollout', '_run_rollback'},
                              f'_daemon_reload called at line {node.lineno} from {fn!r} — '
                              'a switch is lifecycle verbs only (F10)')

    def test_snapshot_calls_locked(self):
        """Every snapshot() call must be inside a with-block or inside locked_snapshot.

        Regression guard (§7.1 item 3): proves the presence of with-blocks around
        snapshot() calls, not lock identity — a wrong-lock with-block would pass
        this guard. Unlocked calls would deadlock the worker threads (Risk 2).
        """
        import ast
        source, tree = self._tree()
        parents = self._parents(tree)

        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and self._callee_name(node) == 'snapshot':
                # Check if inside a with block
                in_with = False
                in_locked_snapshot = False

                # Check if inside locked_snapshot function
                fn = self._enclosing_func(node, parents)
                if fn == 'locked_snapshot':
                    in_locked_snapshot = True
                else:
                    # Check if inside a with block
                    cur = parents.get(node)
                    while cur is not None:
                        if isinstance(cur, ast.With):
                            in_with = True
                            break
                        cur = parents.get(cur)

                if not (in_with or in_locked_snapshot):
                    violations.append((node.lineno, fn))

        self.assertEqual(violations, [],
                        f'snapshot() called unlocked at: {violations}')

    def test_post_route_table_frozen_and_complete(self):
        """POST route table is complete and matches FROZEN_POST_ROUTES."""
        import ast

        # Behavioral test: boot unarmed server, POST to each frozen route, all 403
        import tempfile
        import shutil
        import socket
        import threading
        import time
        from unittest.mock import MagicMock

        temp_dir = tempfile.mkdtemp()
        try:
            watcher = MagicMock(spec=roundhouse.Watcher)
            watcher.snapshot.return_value = {
                'host': 'test', 'kernel': '6.1', 'now': time.time(),
                'mem': {}, 'units': [], 'sources': {}
            }
            watcher.units = {}

            event_bus = roundhouse.EventBus()

            # Find an available port
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
            sock.close()

            # Create server without arming
            roundhouse.ACTUATE_ARMED = False
            server = roundhouse.ThreadingHTTPServer(
                ('127.0.0.1', port),
                roundhouse.RoundhouseRequestHandler,
                watcher,
                event_bus,
                port
            )

            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            time.sleep(0.1)

            try:
                # Instantiate each frozen route with dummy parameters
                routes_to_test = [
                    '/api/units/dummy.service/edit',
                    '/api/units/dummy.service/rollout',
                    '/api/units/dummy.service/enablement',
                    '/api/rollouts/ro-1-1/rollback',
                    '/api/rollouts/ro-1-1/dismiss',
                    '/api/switch/preview',
                    '/api/switch',
                ]

                import http.client
                for route in routes_to_test:
                    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                    try:
                        conn.request('POST', route, b'{}', {'Content-Type': 'application/json'})
                        resp = conn.getresponse()
                        # All should be 403 unarmed or 404 for missing unit
                        self.assertIn(resp.status, [403, 404], f"Route {route} returned {resp.status}")
                    finally:
                        conn.close()

            finally:
                server.shutdown()
                server.server_close()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        # Structural test 1: FROZEN_POST_ROUTES exists and contains the right paths
        self.assertTrue(hasattr(roundhouse, 'FROZEN_POST_ROUTES'))
        self.assertIsInstance(roundhouse.FROZEN_POST_ROUTES, (tuple, list))
        expected_routes = {
            "/api/units/<name>/edit", "/api/units/<name>/rollout",
            "/api/rollouts/<id>/rollback", "/api/rollouts/<id>/dismiss",
            "/api/switch/preview", "/api/switch", "/api/units/<name>/enablement",
            "/api/warm", "/api/warm/cancel",
        }
        self.assertEqual(set(roundhouse.FROZEN_POST_ROUTES), expected_routes)

        # Structural test 2 (§7.1(2)) — the leg that makes the table load-bearing.
        # Every path literal inside do_POST must be derivable from the frozen table (plus
        # the GET-only paths do_POST must recognise to answer 405 instead of 404). Adding
        # a dispatch branch without touching FROZEN_POST_ROUTES fails HERE; asserting the
        # constant against itself, as the MVP2 version did, never could.
        import ast
        _, tree = self._tree()
        do_post = next((n for n in ast.walk(tree)
                        if isinstance(n, ast.FunctionDef) and n.name == 'do_POST'), None)
        self.assertIsNotNone(do_post, 'do_POST not found')

        found = {c.value for c in ast.walk(do_post)
                 if isinstance(c, ast.Constant) and isinstance(c.value, str)
                 and c.value.startswith('/')}
        # Fragments the nine frozen routes decompose into when matched by prefix/suffix
        from_frozen = {'/api/units/', '/edit', '/rollout', '/enablement',
                       '/api/rollouts/', '/rollback', '/dismiss',
                       '/api/switch/preview', '/api/switch',
                       '/api/warm', '/api/warm/cancel'}
        # GET-only paths do_POST recognises purely to answer 405 (§4 status doctrine)
        get_only = {'/', '/api/units', '/api/ports', '/api/deployments',
                    '/api/mem', '/api/events', '/api/routing-config',
                    '/api/routing-config.json', '/api/fleet', '/api/routing-config/fleet',
                    '/api/routing-config/fleet.json', '/api/warm', '/api/peers'}
        allowed = from_frozen | get_only

        self.assertEqual(
            found - allowed, set(),
            'do_POST dispatches path literals absent from FROZEN_POST_ROUTES: '
            f'{sorted(found - allowed)} — add the route to the table, or remove it')
        self.assertEqual(
            from_frozen - found, set(),
            'FROZEN_POST_ROUTES names routes do_POST no longer dispatches: '
            f'{sorted(from_frozen - found)}')

    def test_file_writes_confined(self):
        """File writes are confined to _atomic_write() function."""
        import ast

        source_text = self.SOURCE.read_text()
        tree = ast.parse(source_text)

        write_funcs = {'_atomic_write'}

        for node in ast.walk(tree):
            # Check for open() with write modes
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id == 'open':
                    # Check mode argument
                    mode_str = None
                    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                        mode_str = node.args[1].value
                    elif any(kw.arg == 'mode' and isinstance(kw.value, ast.Constant) for kw in node.keywords):
                        for kw in node.keywords:
                            if kw.arg == 'mode' and isinstance(kw.value, ast.Constant):
                                mode_str = kw.value.value

                    if mode_str and any(c in mode_str for c in ['w', 'a', 'x', '+']):
                        # Find enclosing function
                        found = False
                        for fn in ast.walk(tree):
                            if isinstance(fn, ast.FunctionDef):
                                if any(n is node for n in ast.walk(fn)):
                                    if fn.name in write_funcs:
                                        found = True
                                        break
                        if not found:
                            self.fail(f"open() with write mode found outside {write_funcs}")

                # Check for os.replace, os.rename, write_text, write_bytes
                if isinstance(node.func, ast.Attribute):
                    attr = node.func.attr
                    if attr in {'replace', 'rename', 'write_text', 'write_bytes', 'open'}:
                        # Check if it's os.X or X.write_text or Path.open
                        if isinstance(node.func.value, ast.Name) and node.func.value.id == 'os':
                            if attr in {'replace', 'rename'}:
                                # Find enclosing function
                                found = False
                                for fn in ast.walk(tree):
                                    if isinstance(fn, ast.FunctionDef):
                                        if any(n is node for n in ast.walk(fn)):
                                            if fn.name in write_funcs:
                                                found = True
                                                break
                                if not found:
                                    self.fail(f"os.{attr} found outside {write_funcs}")
                        elif attr in {'write_text', 'write_bytes'}:
                            # Find enclosing function
                            found = False
                            for fn in ast.walk(tree):
                                if isinstance(fn, ast.FunctionDef):
                                    if any(n is node for n in ast.walk(fn)):
                                        if fn.name in write_funcs:
                                            found = True
                                            break
                            if not found:
                                self.fail(f".{attr} found outside {write_funcs}")
                        elif attr == 'open':
                            # Path.open with write mode check (§6(ii))
                            # Check first positional arg or mode= kwarg for w/a/x/+
                            mode_str = None
                            if len(node.args) >= 1 and isinstance(node.args[0], ast.Constant):
                                mode_str = node.args[0].value
                            elif any(kw.arg == 'mode' and isinstance(kw.value, ast.Constant) for kw in node.keywords):
                                for kw in node.keywords:
                                    if kw.arg == 'mode' and isinstance(kw.value, ast.Constant):
                                        mode_str = kw.value.value

                            if mode_str and any(c in mode_str for c in ['w', 'a', 'x', '+']):
                                # Find enclosing function
                                found = False
                                for fn in ast.walk(tree):
                                    if isinstance(fn, ast.FunctionDef):
                                        if any(n is node for n in ast.walk(fn)):
                                            if fn.name in write_funcs:
                                                found = True
                                                break
                                if not found:
                                    self.fail(f"Path.open with write mode found outside {write_funcs}")

    def test_listener_construction_confined(self):
        """§6.1 guard: ThreadingHTTPServer construction only in _make_listener (§L8).

        Every ast.Call whose func resolves to ThreadingHTTPServer has _enclosing_func
        in SERVER_CALLSITES = {'_make_listener'}; exactly ONE such call exists
        (the class definition is not a Call and needs no exemption).
        """
        import ast
        source, tree = self._tree()
        parents = self._parents(tree)

        SERVER_CALLSITES = {'_make_listener'}
        server_call_count = 0
        violations = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # Check if this is a ThreadingHTTPServer(...) call
                if isinstance(node.func, ast.Name) and node.func.id == 'ThreadingHTTPServer':
                    server_call_count += 1
                    func_name = self._enclosing_func(node, parents)
                    if func_name not in SERVER_CALLSITES:
                        violations.append((node.lineno, 'ThreadingHTTPServer', func_name))

        self.assertEqual(violations, [],
                        f"ThreadingHTTPServer construction outside {SERVER_CALLSITES}: {violations}")

        self.assertEqual(server_call_count, 1,
                        f"Found {server_call_count} ThreadingHTTPServer(...) calls; "
                        "expected exactly 1 (in _make_listener)")

    def test_outbound_connect_confined(self):
        """§6.1 guard evolved: socket.create_connection and socket.socket() only in PROBE_CALLSITES.

        Five legs (§L8):
        (a) socket.create_connection and socket.socket() must be in PROBE_CALLSITES
        (b) Exactly one create_connection exists (in _probe_peer's default argument)
        (c) No send/recv/makefile within _probe_peer (connect-and-close only)
        (d) No connect-attrs inside _presence_probe (bind-only)
        (e) No bind attrs inside _probe_peer (connect-only)
        """
        import ast
        source, tree = self._tree()
        parents = self._parents(tree)

        PROBE_CALLSITES = {'_probe_peer', '_presence_probe'}
        CONNECT_ATTRS = {'create_connection', 'connect', 'connect_ex'}
        FORBIDDEN_SOCKET_METHODS = {'send', 'sendall', 'sendto', 'recv', 'recv_into', 'makefile', 'sendfile'}
        # sqlite3.connect opens a FILE, not a socket; it is the one benign `connect`
        # attribute in the tree and is named here rather than left to a value-shape
        # coincidence. socket.gethostname is outside the guarded set entirely (recon 7).
        BENIGN_CONNECT_OWNERS = {'sqlite3'}

        violations_callsite = []
        create_connection_count = 0
        forbidden_in_probe_peer = []
        connect_attrs_in_presence = []
        bind_attrs_in_probe_peer = []

        for node in ast.walk(tree):
            # (a) every connect-shaped attribute, whoever owns it, must sit in a probe
            # callsite — an obj.connect((h, p)) on a socket handed in from elsewhere
            # is exactly the erosion this guard exists to catch.
            if isinstance(node, ast.Attribute) and node.attr in CONNECT_ATTRS:
                owner = node.value.id if isinstance(node.value, ast.Name) else None
                if owner in BENIGN_CONNECT_OWNERS:
                    continue
                func_name = self._enclosing_func(node, parents)
                if func_name not in PROBE_CALLSITES:
                    violations_callsite.append(
                        (node.lineno, f'{owner or "<expr>"}.{node.attr}', func_name))
                if node.attr == 'create_connection':
                    create_connection_count += 1

            # Check for socket.socket(...) calls
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr == 'socket':
                    if isinstance(node.func.value, ast.Name) and node.func.value.id == 'socket':
                        func_name = self._enclosing_func(node, parents)
                        if func_name not in PROBE_CALLSITES:
                            violations_callsite.append((node.lineno, 'socket.socket', func_name))

            # (c) Check for forbidden socket methods within _probe_peer
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_SOCKET_METHODS:
                func_name = self._enclosing_func(node, parents)
                if func_name == '_probe_peer':
                    forbidden_in_probe_peer.append((node.lineno, node.attr))

            # (d) Check for connect-attrs inside _presence_probe (should have none)
            if isinstance(node, ast.Attribute) and node.attr in CONNECT_ATTRS:
                func_name = self._enclosing_func(node, parents)
                if func_name == '_presence_probe':
                    connect_attrs_in_presence.append((node.lineno, node.attr))

            # (e) Check for bind attrs inside _probe_peer (should have none)
            if isinstance(node, ast.Attribute) and node.attr == 'bind':
                func_name = self._enclosing_func(node, parents)
                if func_name == '_probe_peer':
                    bind_attrs_in_probe_peer.append((node.lineno, node.attr))

        # Assertions
        self.assertEqual(violations_callsite, [],
                        f"Socket module methods outside PROBE_CALLSITES={PROBE_CALLSITES}: {violations_callsite}")

        # (b) EXACTLY one create_connection: the default argument in _probe_peer. Not
        # "at most" — zero would mean the probe stopped being a probe, and a guard
        # that is happy with a missing mechanism is not guarding anything.
        self.assertEqual(create_connection_count, 1,
                        f"Found {create_connection_count} socket.create_connection nodes; "
                        "expected exactly 1 (the _probe_peer default argument)")

        self.assertEqual(forbidden_in_probe_peer, [],
                        f"Forbidden socket methods in _probe_peer: {forbidden_in_probe_peer} — "
                        "connect-and-close means no send/recv")

        self.assertEqual(connect_attrs_in_presence, [],
                        f"Connect-attrs in _presence_probe: {connect_attrs_in_presence} — "
                        "_presence_probe binds, never connects")

        self.assertEqual(bind_attrs_in_probe_peer, [],
                        f"Bind attrs in _probe_peer: {bind_attrs_in_probe_peer} — "
                        "_probe_peer connects, never binds")

    # ---- MVP8 §8.1: the federated HTTP client is confined and cannot be weakened ----

    FETCH_CALLSITES = {'_fetch_peer'}
    OPENING_ATTRS = {'urlopen', 'build_opener', 'OpenerDirector', 'HTTPSHandler',
                     'HTTPHandler', 'HTTPConnection', 'HTTPSConnection'}

    def test_fetch_confined(self):
        """§8.1(a): every connection-opening urllib/http.client symbol sits in
        FETCH_CALLSITES = {'_fetch_peer'}.

        _FleetNoRedirect subclasses HTTPRedirectHandler, which opens nothing and is
        not in the set — the class body is the one place a handler name may appear
        outside the client.
        """
        import ast
        source, tree = self._tree()
        parents = self._parents(tree)

        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in self.OPENING_ATTRS:
                fn = self._enclosing_func(node, parents)
                if fn not in self.FETCH_CALLSITES:
                    violations.append((node.lineno, node.attr, fn))

        self.assertEqual(violations, [],
                         f"connection-opening symbols outside {self.FETCH_CALLSITES}: {violations}")

    def test_default_context_is_the_only_tls_construction(self):
        """§8.1(b): ssl.create_default_context appears EXACTLY once, inside _fetch_peer.

        Exactly one, not at most one: zero would mean the client stopped verifying,
        and a guard content with a missing mechanism guards nothing.
        """
        import ast
        source, tree = self._tree()
        parents = self._parents(tree)

        sites = [(node.lineno, self._enclosing_func(node, parents))
                 for node in ast.walk(tree)
                 if isinstance(node, ast.Attribute) and node.attr == 'create_default_context']

        self.assertEqual(len(sites), 1, f"expected exactly one create_default_context, got {sites}")
        self.assertEqual(sites[0][1], '_fetch_peer', f"create_default_context outside _fetch_peer: {sites}")

    def test_insecure_tls_is_unrepresentable(self):
        """§8.1(c)/K2: the constructs that would disable verification are ABSENT.

        Not "insecure only outside the client" — absent, full stop. A federation that
        silently accepts any certificate is worse than no federation (contract), so
        the bypass must not exist to be reached for when a drill's self-signed cert
        fails (Risk 2 — the likeliest wrong 'fix' in this milestone).
        """
        import ast
        source, tree = self._tree()

        FORBIDDEN_ATTRS = {'_create_unverified_context', 'CERT_NONE', 'SSLContext', 'set_ciphers'}
        FORBIDDEN_TARGETS = {'check_hostname', 'verify_mode'}

        found_attrs, found_assigns = [], []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS:
                found_attrs.append((node.lineno, node.attr))
            if isinstance(node, ast.Name) and node.id in FORBIDDEN_ATTRS:
                found_attrs.append((node.lineno, node.id))
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            for t in targets:
                if isinstance(t, ast.Attribute) and t.attr in FORBIDDEN_TARGETS:
                    found_assigns.append((node.lineno, t.attr))

        self.assertEqual(found_attrs, [], f"insecure-TLS construct present: {found_attrs}")
        self.assertEqual(found_assigns, [], f"TLS verification assignment present: {found_assigns}")

        # And no flag can reach it either: argparse must not learn the word.
        for word in ('insecure', 'no-verify', 'noverify', 'skip-verify'):
            self.assertNotIn(word, source.lower(),
                             f"the source mentions {word!r} — there is no bypass flag (K2)")

    def test_fetch_peer_only_reads(self):
        """§8.1(d): inside _fetch_peer, no send-shaped socket verb beyond read()."""
        import ast
        source, tree = self._tree()
        parents = self._parents(tree)

        SEND_ATTRS = {'send', 'sendall', 'sendto', 'write', 'makefile', 'sendfile'}
        offenders = [(node.lineno, node.attr) for node in ast.walk(tree)
                     if isinstance(node, ast.Attribute) and node.attr in SEND_ATTRS
                     and self._enclosing_func(node, parents) == '_fetch_peer']
        self.assertEqual(offenders, [], f"_fetch_peer writes application data: {offenders}")

    def test_fetch_peer_takes_no_url(self):
        """§3.3/K9: the base comes from the startup-frozen fleet table and the path
        from FLEET_PATHS — an arbitrary URL is unrepresentable at the signature."""
        import ast
        import inspect
        source, tree = self._tree()

        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == '_fetch_peer')
        arg_names = [a.arg for a in fn.args.args]
        self.assertEqual(arg_names, ['peer_watch', 'name', 'path', 'opener', 'index'])
        self.assertNotIn('url', arg_names)

        body_src = ast.get_source_segment(source, fn)
        self.assertIn('assert path in FLEET_PATHS', body_src)
        self.assertIn('peer_watch.fleet[name]', body_src)
        self.assertEqual(roundhouse.FLEET_PATHS, ('/api/units', '/api/routing-config.json'))


class TestMultiListener(unittest.TestCase):
    """T1 integration: multi-listener lifecycle per §7.4."""

    def test_multi_listener_shared_state(self):
        """Two listeners on different addresses share watcher/engine/bus state (§7.4 multi-listener)."""
        import tempfile
        import shutil
        import threading
        import time
        from unittest.mock import MagicMock

        temp_dir = tempfile.mkdtemp()
        try:
            watcher = MagicMock(spec=roundhouse.Watcher)
            watcher.snapshot.return_value = {
                'host': 'test', 'kernel': '6.1', 'now': time.time(),
                'mem': {}, 'units': [], 'sources': {},
                'rollout': None
            }
            watcher.units = {}

            event_bus = roundhouse.EventBus()

            # Try to bind two servers on different loopback addresses
            # (127.0.0.1 and ::1 if available)
            servers = []
            try:
                sock_v4 = socket.socket()
                sock_v4.bind(('127.0.0.1', 0))
                port_v4 = sock_v4.getsockname()[1]
                sock_v4.close()

                sock_v6 = socket.socket(socket.AF_INET6)
                sock_v6.bind(('::1', 0))
                port_v6 = sock_v6.getsockname()[1]
                sock_v6.close()

                ipv6_available = True
            except OSError:
                ipv6_available = False
                port_v4 = None

            if not ipv6_available:
                self.skipTest("IPv6 not available")

            # Find same free port for both
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
            common_port = sock.getsockname()[1]
            sock.close()

            roundhouse.ACTUATE_ARMED = False
            # J1: every listener is constructed with the SAME watcher, bus, engine and
            # lock. Handing each server its own lock would still pass a both-doors-answer
            # test while destroying the property the test claims to prove.
            shared_lock = threading.Lock()
            engine = roundhouse.RolloutEngine(watcher, {}, temp_dir, common_port, event_bus, shared_lock)

            # Create two servers on different addresses, same port
            try:
                server1 = roundhouse.ThreadingHTTPServer(
                    ('127.0.0.1', common_port),
                    roundhouse.RoundhouseRequestHandler,
                    watcher,
                    event_bus,
                    common_port,
                    watcher_lock=shared_lock,
                    rollout_engine=engine,
                    address_family=socket.AF_INET
                )
                servers.append(server1)

                server2 = roundhouse.ThreadingHTTPServer(
                    ('::1', common_port),
                    roundhouse.RoundhouseRequestHandler,
                    watcher,
                    event_bus,
                    common_port,
                    watcher_lock=shared_lock,
                    rollout_engine=engine,
                    address_family=socket.AF_INET6
                )
                servers.append(server2)

                self.assertIs(server1.rollout_engine, server2.rollout_engine)
                self.assertIs(server1.watcher_lock, server2.watcher_lock)
                self.assertIs(server1.watcher, server2.watcher)
                self.assertIs(server1.event_bus, server2.event_bus)

                # Start both
                for srv in servers:
                    threading.Thread(target=srv.serve_forever, daemon=True).start()
                    time.sleep(0.05)

                # Verify both respond
                import http.client
                for addr in ['127.0.0.1', '::1']:
                    conn = http.client.HTTPConnection(addr, common_port, timeout=2)
                    try:
                        conn.request('GET', '/')
                        resp = conn.getresponse()
                        self.assertIn(resp.status, [200, 404], f"Address {addr} responded with {resp.status}")
                    except Exception as e:
                        self.fail(f"Could not connect to {addr}:{common_port}: {e}")
                    finally:
                        conn.close()

            finally:
                for srv in servers:
                    srv.shutdown()
                    srv.server_close()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_bind_failure_all_or_nothing(self):
        """Bind failure closes every successful bind, and the failure is REAL.

        MVP7 review M1: the first version of this test never called listen() on
        the blocking socket, and with SO_REUSEADDR on both sides Linux let the
        server bind and listen anyway — `failures` stayed empty, every assertion
        below was skipped, and two live listeners leaked into the rest of the
        suite. A test that cannot fail proves nothing; this one asserts that the
        collision actually happened before it checks the cleanup.
        """
        sock_block = socket.socket()
        # NO SO_REUSEADDR here, and listen() so the port is genuinely taken:
        # that combination is what makes the second bind raise EADDRINUSE.
        sock_block.bind(('127.0.0.1', 0))
        sock_block.listen(1)
        blocked_port = sock_block.getsockname()[1]

        servers = []
        failures = []
        try:
            for addr, family in (('::1', socket.AF_INET6), ('127.0.0.1', socket.AF_INET)):
                try:
                    srv = roundhouse.ThreadingHTTPServer(
                        (addr, blocked_port),
                        roundhouse.RoundhouseRequestHandler,
                        MagicMock(spec=roundhouse.Watcher),
                        roundhouse.EventBus(),
                        blocked_port,
                        address_family=family
                    )
                    servers.append(srv)
                except OSError as e:
                    display_addr = f'[{addr}]' if family == socket.AF_INET6 else addr
                    failures.append(
                        f"cannot bind {display_addr}:{blocked_port}: [Errno {e.errno}] {e.strerror}")

            # THE assertion the old version lacked: the conflict must be real.
            self.assertTrue(failures, "127.0.0.1 bind should have failed on an occupied port")
            self.assertIn('127.0.0.1', failures[0])
            self.assertIn('Errno', failures[0])

            # All-or-nothing: everything that did bind gets closed.
            for srv in servers:
                srv.server_close()
            servers = []

            probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            try:
                probe.bind(('::1', blocked_port))
            except OSError:
                self.fail("a successful listener was not closed after the failed bind")
            finally:
                probe.close()
        finally:
            for srv in servers:
                srv.server_close()
            sock_block.close()

    def test_real_socket_hysteresis(self):
        """Real-socket hysteresis: up on first success, down after two failures (§7.4)."""
        # Create an ephemeral listener
        server_sock = socket.socket()
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind(('127.0.0.1', 0))
        peer_port = server_sock.getsockname()[1]
        server_sock.listen(1)

        lock = threading.Lock()
        event_bus = MagicMock()
        peer_watch = roundhouse.PeerWatch(
            {'test_peer': ('127.0.0.1', peer_port)},
            lock,
            event_bus
        )

        try:
            # Round 1: server listening → up
            roundhouse.peer_watch_round(peer_watch)
            with lock:
                state1 = peer_watch.peers['test_peer']['state']
            self.assertEqual(state1, 'up', 'After first successful probe, should be up')

            # Close the server socket (refuse new connections)
            server_sock.close()

            # Round 2: server down but only 1st failure → still up (cf=1)
            roundhouse.peer_watch_round(peer_watch)
            with lock:
                state2 = peer_watch.peers['test_peer']['state']
                cf2 = peer_watch.peers['test_peer']['consecutive_failures']
            self.assertEqual(state2, 'up', 'After 1 failure, still up (hysteresis)')
            self.assertEqual(cf2, 1)

            # Round 3: 2nd failure → down
            roundhouse.peer_watch_round(peer_watch)
            with lock:
                state3 = peer_watch.peers['test_peer']['state']
                cf3 = peer_watch.peers['test_peer']['consecutive_failures']
            self.assertEqual(state3, 'down', 'After 2 consecutive failures, should be down')
            self.assertEqual(cf3, 2)

            # Rebind the server (on same port, SO_REUSEADDR)
            server_sock2 = socket.socket()
            server_sock2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_sock2.bind(('127.0.0.1', peer_port))
            server_sock2.listen(1)

            # Round 4: server back up → back to up
            roundhouse.peer_watch_round(peer_watch)
            with lock:
                state4 = peer_watch.peers['test_peer']['state']
                cf4 = peer_watch.peers['test_peer']['consecutive_failures']
            self.assertEqual(state4, 'up', 'After success from down, should be up')
            self.assertEqual(cf4, 0)

            server_sock2.close()
        except Exception as e:
            if not server_sock._closed:
                server_sock.close()
            raise


class TestMobileStatic(unittest.TestCase):
    """Static UI tests for mobile layout and F12 switch feature."""

    @classmethod
    def setUpClass(cls):
        """Load index.html once for all tests."""
        cls.HTML_PATH = Path(__file__).parent.parent / 'static' / 'index.html'
        cls.HTML_CONTENT = cls.HTML_PATH.read_text()

    def test_mobile_media_block_exists(self):
        """@media (max-width: 700px) block exists in CSS."""
        self.assertIn('@media (max-width: 700px)', self.HTML_CONTENT,
                      '700px mobile media query not found in CSS')

    def test_min_height_44px_for_touch_targets(self):
        """min-height: 44px present for frozen touch-target selectors in media query."""
        # Extract the @media (max-width: 700px) block, handling nested braces
        import re
        # Find the @media block and count braces to extract the full content
        start_match = re.search(r'@media\s*\(\s*max-width:\s*700px\s*\)\s*\{', self.HTML_CONTENT, re.DOTALL)
        self.assertIsNotNone(start_match, '700px media block not found')

        start_pos = start_match.end()
        # Find the closing brace for this media block
        brace_count = 1
        end_pos = start_pos
        while end_pos < len(self.HTML_CONTENT) and brace_count > 0:
            if self.HTML_CONTENT[end_pos] == '{':
                brace_count += 1
            elif self.HTML_CONTENT[end_pos] == '}':
                brace_count -= 1
            end_pos += 1

        media_content = self.HTML_CONTENT[start_pos:end_pos-1]

        frozen_selectors = {'.unit-row', 'button', '.off-section-toggle', '.stop-tick-row'}
        # Check that min-height: 44px is declared somewhere for these selectors
        # (may be grouped or individual selectors)
        self.assertIn('min-height: 44px', media_content,
                      'min-height: 44px rule not found in mobile media query')

        # Verify that all frozen selectors are mentioned somewhere in the media block
        for selector in frozen_selectors:
            self.assertIn(selector, media_content,
                          f'Selector {selector} not found in mobile media query')

    def test_no_anchor_href_empty(self):
        """No <a href="#"> links (converted to buttons)."""
        self.assertNotIn('<a href="#"', self.HTML_CONTENT,
                         'Found <a href="#" — should be converted to <button>')

    def test_onclick_only_on_buttons(self):
        """onclick= attribute only appears on <button> tags."""
        import re
        # Find all onclick attributes
        onclick_pattern = r'<(\w+)[^>]*onclick='
        matches = re.findall(onclick_pattern, self.HTML_CONTENT)
        self.assertFalse([m for m in matches if m != 'button'],
                         f'onclick= found on non-button elements: {set(matches) - {"button"}}')

    def test_overlay_open_in_css_and_script(self):
        """overlay-open class defined in CSS and used in JavaScript."""
        self.assertIn('body.overlay-open', self.HTML_CONTENT,
                      'body.overlay-open CSS rule not found')
        self.assertIn('.add(\'overlay-open\')', self.HTML_CONTENT,
                      'overlay-open not added in script')
        self.assertIn('.remove(\'overlay-open\')', self.HTML_CONTENT,
                      'overlay-open not removed in script')

    def test_switch_modal_present(self):
        """Switch preview modal (#switch-modal) exists with required elements."""
        self.assertIn('id="switch-modal"', self.HTML_CONTENT,
                      '#switch-modal not found')
        self.assertIn('stop-tick-row', self.HTML_CONTENT,
                      '.stop-tick-row class reference not found')
        self.assertIn('id="port-board-body"', self.HTML_CONTENT,
                      '#port-board-body not found')

    def test_switch_to_this_string_present(self):
        """'switch to this' button string exists."""
        self.assertIn('switch to this', self.HTML_CONTENT,
                      "'switch to this' button string not found")

    def test_gate_notice_condition_fixed(self):
        """Gate notice condition uses unit.rung === 'STANDBY' (not gate.kind)."""
        self.assertIn("unit.rung === 'STANDBY'", self.HTML_CONTENT,
                      "Fixed gate-notice condition (unit.rung === 'STANDBY') not found")
        # Negative check: old broken condition should not exist
        self.assertNotIn("gate.kind === 'STANDBY'", self.HTML_CONTENT,
                         "Old broken gate-notice condition (gate.kind === 'STANDBY') still present")

    def test_refreshOperation_function_present(self):
        """refreshOperation(id) function exists and is called on SSE rollout events."""
        self.assertIn('function refreshOperation(id)', self.HTML_CONTENT,
                      'refreshOperation(id) function not found')
        self.assertIn('refreshOperation(data.rollout_id)', self.HTML_CONTENT,
                      'refreshOperation not called on SSE rollout event')

    def test_no_innerHTML_usage(self):
        """innerHTML not used anywhere (textContent-only rule)."""
        self.assertNotIn('.innerHTML', self.HTML_CONTENT,
                         '.innerHTML found — use textContent only')

    def test_no_local_storage_usage(self):
        """localStorage and sessionStorage not used."""
        self.assertNotIn('localStorage', self.HTML_CONTENT,
                         'localStorage found — no client-side storage')
        self.assertNotIn('sessionStorage', self.HTML_CONTENT,
                         'sessionStorage found — no client-side storage')

    def test_no_arithmetic_on_estimate_bytes(self):
        """No byte arithmetic logic in JavaScript; only display formatting allowed.

        Constraint: estimate_bytes / freed_bytes may be divided by 1e9 for display (GB conversion),
        but must not be added/subtracted with each other or used in decision logic.
        This is an honest check: division is for rendering, not computation.
        The string '+ freed' in display concatenation is acceptable.
        """
        import re
        # Look for improper arithmetic patterns on the byte variables themselves
        # (string concatenation with '+ freed' in display strings is OK)
        bad_patterns = [
            r'estimate_bytes\s*[\*+\-]',  # multiplication, addition, subtraction on variable
            r'freed_bytes\s*[\*+\-]',     # multiplication, addition, subtraction on variable
        ]
        for pattern in bad_patterns:
            matches = re.findall(pattern, self.HTML_CONTENT)
            self.assertFalse(matches,
                             f'Found improper byte arithmetic: {pattern} — only server does arithmetic')

    def test_api_routes_string_constants_present(self):
        """All required API route strings present in HTML."""
        required_routes = [
            '/api/switch/preview',
            '/api/switch',
        ]
        for route in required_routes:
            self.assertIn(f"'{route}'", self.HTML_CONTENT,
                          f"API route string {route} not found in HTML")

    def test_phase_strings_present(self):
        """Required switch phase strings present in stepper logic."""
        phases = ['stopping', 'restoring', 'restored']
        for phase in phases:
            self.assertIn(f"'{phase}'", self.HTML_CONTENT,
                          f"Phase string '{phase}' not found in HTML")

    def test_enablement_listener_present(self):
        """SSE listener for 'enablement' event is present."""
        self.assertIn("addEventListener('enablement'", self.HTML_CONTENT,
                      "enablement SSE listener not found")

    def test_enable_toggle_checkbox_present(self):
        """Enable-toggle checkbox (.enable-toggle class) is present in renderUnitList."""
        self.assertIn("label.className = 'enable-toggle'", self.HTML_CONTENT,
                      'enable-toggle label creation not found in JavaScript')
        self.assertIn('.enable-toggle', self.HTML_CONTENT,
                      '.enable-toggle CSS styling not found in HTML')

    def test_enable_toggle_in_44px_rule(self):
        """The .enable-toggle and #token are in the 44px min-height rule."""
        self.assertIn('.enable-toggle', self.HTML_CONTENT)
        self.assertIn('#token', self.HTML_CONTENT)
        # Find the min-height: 44px rule
        self.assertIn('min-height: 44px', self.HTML_CONTENT,
                      'min-height: 44px rule not found')
        # Verify both are in the selector list (may be in the same rule or separate)
        import re
        # Look for a rule containing min-height: 44px and check if the selectors include both
        # by looking for the media query block where .enable-toggle CSS is defined
        self.assertIn('.enable-toggle input[type="checkbox"]', self.HTML_CONTENT,
                      '.enable-toggle input checkbox sizing not found in media query')

    def test_stop_propagation_for_enable_toggle(self):
        """stopPropagation is present for enable-toggle click handler."""
        self.assertIn('stopPropagation', self.HTML_CONTENT,
                      'stopPropagation not found in HTML')

    def test_toast_div_present(self):
        """Toast notification div (#toast) is present."""
        self.assertIn('id="toast"', self.HTML_CONTENT,
                      '#toast div element not found')

    def test_no_gb_strings(self):
        """All 'GB' strings are changed to 'GiB'."""
        # Should not find ' GB ' (with spaces), only 'GiB'
        import re
        gb_pattern = r'\s+GB\s+'
        matches = re.findall(gb_pattern, self.HTML_CONTENT)
        self.assertFalse(matches,
                         f"Found ' GB ' strings (should be 'GiB'): {matches}")

    def test_failure_phase_in_stepper(self):
        """Stepper honesty: failure.phase or equivalent marker present."""
        self.assertIn('rollout.failure', self.HTML_CONTENT,
                      'failure object handling not found in stepper logic')
        self.assertIn('failure.phase', self.HTML_CONTENT,
                      'failure.phase marker not found in stepper')

    def test_boot_status_in_port_board(self):
        """Port board self cell includes 'boot:' status indicator."""
        # The boot status is built dynamically with concatenation
        boot_present = ("' · boot: '" in self.HTML_CONTENT) or ("'boot: '" in self.HTML_CONTENT)
        self.assertTrue(boot_present,
                        "boot: status string not found in port board self cell")

    def test_load_strategy_in_detail_pane(self):
        """Detail pane includes Load strategy row and note line."""
        self.assertIn('on-boot (enabled)', self.HTML_CONTENT,
                      "Load strategy 'on-boot (enabled)' text not found")
        self.assertIn('manual (disabled)', self.HTML_CONTENT,
                      "Load strategy 'manual (disabled)' text not found")


# ===== MVP7 §7.4 legs 4-5, 7 — the surfaces of the peer watch (T2's owed suite) =====

MEANS = ('reachable, not healthy: a TCP connect proves something is listening '
         'on that port and nothing more')


class _PeersRouteHarness(unittest.TestCase):
    """A live server whose PeerWatch is driven by hand (no sockets, injected clock)."""

    PEERS = {'ampere': ('ampere.fritz.box', 8099), 'dirac': ('dirac.fritz.box', 22)}
    PEER_INTERVAL = 60

    @classmethod
    def setUpClass(cls):
        cls.watcher = StubWatcher()
        cls.event_bus = roundhouse.EventBus()
        cls.watcher_lock = threading.Lock()
        cls.clock = [1_755_100_000.0]

        cls.peer_watch = (roundhouse.PeerWatch(cls.PEERS, cls.watcher_lock, cls.event_bus,
                                               now=lambda: cls.clock[0])
                          if cls.PEERS else None)

        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        cls.port = sock.getsockname()[1]
        sock.close()

        cls.server = roundhouse.ThreadingHTTPServer(
            ('127.0.0.1', cls.port), roundhouse.RoundhouseRequestHandler,
            cls.watcher, cls.event_bus, cls.port,
            watcher_lock=cls.watcher_lock, peer_watch=cls.peer_watch,
            peer_interval=cls.PEER_INTERVAL)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, method, path, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            conn.request(method, path, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def drive(self, name, ok, error=None):
        """One probe result applied the way peer_watch_round applies it."""
        with self.watcher_lock:
            payload = self.peer_watch.apply_result_unlocked(name, ok, error, self.clock[0])
            if payload:
                self.event_bus.publish('peer', payload)
        return payload


class TestPeersRoute(_PeersRouteHarness):
    """§5.1 / §7.4: GET /api/peers — shape, unauthenticated, POST 405."""

    def test_envelope_is_frozen(self):
        """Exactly three keys; the probe block and the `means` string are verbatim."""
        status, body = self.request('GET', '/api/peers')
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(set(data.keys()), {'peers', 'probe', 'means'})
        self.assertEqual(data['probe'], {'method': 'tcp-connect',
                                         'timeout_seconds': 2.0,
                                         'cadence_seconds': self.PEER_INTERVAL})
        self.assertEqual(data['means'], MEANS)

    def test_means_never_claims_health(self):
        """Contract Part 2.5: the wire says reachable and denies healthy."""
        _s, body = self.request('GET', '/api/peers')
        means = json.loads(body)['means']
        self.assertIn('reachable', means)
        self.assertIn('not healthy', means)

    def test_rows_are_the_frozen_shape_sorted_by_name(self):
        status, body = self.request('GET', '/api/peers')
        rows = json.loads(body)['peers']
        self.assertEqual([r['name'] for r in rows], ['ampere', 'dirac'])
        for row in rows:
            self.assertEqual(set(row.keys()),
                             {'name', 'host', 'port', 'state', 'since',
                              'last_probe', 'consecutive_failures', 'last_error', 'kind',
                              'endpoint_in_use', 'endpoint_changed_at'})
        self.assertEqual(rows[0]['host'], 'ampere.fritz.box')
        self.assertEqual(rows[0]['port'], 8099)

    def test_declared_peers_actually_reach_the_wire(self):
        """The regression that made the whole feature invisible: /api/peers served an
        empty list while peers were declared and probed."""
        self.drive('ampere', True)
        try:
            _s, body = self.request('GET', '/api/peers')
            rows = json.loads(body)['peers']
            self.assertEqual(len(rows), 2, 'declared peers missing from /api/peers')
            ampere = next(r for r in rows if r['name'] == 'ampere')
            self.assertEqual(ampere['state'], 'up')
        finally:
            self.peer_watch.peers['ampere'].update(
                {'state': 'unknown', 'consecutive_failures': 0,
                 'last_probe': None, 'last_error': None})

    def test_unauthenticated_like_every_read(self):
        """No Authorization header, and a bogus bearer, both answer 200."""
        for headers in ({}, {'Authorization': 'Bearer not-a-real-token'}):
            status, _body = self.request('GET', '/api/peers', headers=headers)
            self.assertEqual(status, 200)

    def test_post_is_405(self):
        status, _body = self.request('POST', '/api/peers')
        self.assertEqual(status, 405)

    def test_snapshot_key_agrees_field_for_field(self):
        """§5.2: the snapshot `peers` key and the route serve the same rows."""
        self.drive('dirac', False, 'TimeoutError: timed out')
        _s, body = self.request('GET', '/api/peers')
        route_rows = json.loads(body)['peers']
        snap_rows = self.server.take_snapshot()['peers']
        self.assertEqual(snap_rows, route_rows)

    def test_reading_peers_writes_nothing(self):
        """Three-leg proof: the read route touches no write gateway and no subprocess."""
        from unittest.mock import patch
        recorder = []

        def boom(*a, **k):
            raise AssertionError('write gateway called from /api/peers')

        def record(*a, **k):
            recorder.append(a)
            raise AssertionError('subprocess spawned from /api/peers')

        with patch('roundhouse._atomic_write', side_effect=boom), \
             patch('roundhouse.run_git', side_effect=boom), \
             patch('roundhouse.run_actuate', side_effect=boom), \
             patch('roundhouse.subprocess.Popen', side_effect=record), \
             patch('roundhouse.subprocess.run', side_effect=record):
            status, _body = self.request('GET', '/api/peers')
        self.assertEqual(status, 200)
        self.assertEqual(recorder, [])


class TestPeersRouteWithoutPeers(_PeersRouteHarness):
    """Graceful degradation: the route always exists, even with nothing declared."""

    PEERS = {}
    PEER_INTERVAL = 5

    def test_empty_envelope_is_the_same_envelope(self):
        status, body = self.request('GET', '/api/peers')
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data['peers'], [])
        self.assertEqual(set(data.keys()), {'peers', 'probe', 'means'})
        self.assertEqual(data['means'], MEANS)

    def test_cadence_reflects_the_configured_interval(self):
        _s, body = self.request('GET', '/api/peers')
        self.assertEqual(json.loads(body)['probe']['cadence_seconds'], 5)

    def test_snapshot_key_present_and_empty(self):
        snap = self.server.take_snapshot()
        self.assertIn('peers', snap)
        self.assertEqual(snap['peers'], [])


class TestPeersFlapCannotActuate(_PeersRouteHarness):
    """Contract Part 2.7 / §7.4 leg 5: a full up→down→up flap actuates nothing."""

    def test_flap_touches_no_gateway_and_no_operation_slot(self):
        from unittest.mock import patch
        engine = MagicMock()
        engine.current = None
        self.server.rollout_engine = engine
        recorder = []

        def boom(*a, **k):
            raise AssertionError('write gateway called from the peer watch')

        def record(*a, **k):
            recorder.append(a)
            raise AssertionError('subprocess spawned from the peer watch')

        peer_watch = roundhouse.PeerWatch({'p': ('h', 1)}, self.watcher_lock, self.event_bus)
        results = [(True, None), (False, 'e1'), (False, 'e2'), (True, None), (True, None)]
        seq = iter(results)

        try:
            with patch('roundhouse._atomic_write', side_effect=boom), \
                 patch('roundhouse.run_git', side_effect=boom), \
                 patch('roundhouse.run_actuate', side_effect=boom), \
                 patch('roundhouse.subprocess.Popen', side_effect=record), \
                 patch('roundhouse.subprocess.run', side_effect=record), \
                 patch.object(roundhouse, '_probe_peer', lambda pw, n, index=0, connect=None: next(seq)):
                for _ in results:
                    roundhouse.peer_watch_round(peer_watch)
            self.assertEqual(recorder, [])
            self.assertEqual(peer_watch.peers['p']['state'], 'up')
            self.assertIsNone(engine.current)
            self.assertEqual(engine.mock_calls, [])
        finally:
            self.server.rollout_engine = None


class TestPeerSSE(_PeersRouteHarness):
    """§5.3: the `peer` event is a transition delta on a snapshot the client has."""

    PEERS = {'solo': ('solo.example', 4242)}

    def _open_stream(self):
        """A raw socket, not http.client: proving SILENCE needs a read that can time
        out and be retried, which a buffered response object refuses to do."""
        sock = socket.create_connection(('127.0.0.1', self.port), timeout=5)
        sock.sendall(b'GET /api/events HTTP/1.1\r\nHost: 127.0.0.1\r\n'
                     b'Accept: text/event-stream\r\n\r\n')
        header = b''
        while b'\r\n\r\n' not in header:
            header += sock.recv(1)
        self.assertIn(b'200 OK', header.split(b'\r\n')[0])
        self.assertIn(b'text/event-stream', header)
        return sock, None

    def _read_events(self, sock, resp, count, timeout=4.0):
        """Read up to `count` SSE frames (id/event/data), skipping heartbeats.

        Returns early on `timeout` — a quiet stream is the assertion in the
        transition-only test, so silence must be observable, not a hang.
        """
        import select
        events, buf, deadline = [], b'', time.time() + timeout
        while len(events) < count and time.time() < deadline:
            readable, _, _ = select.select([sock], [], [], 0.2)
            if not readable:
                continue
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            if buf.endswith(b'\n\n'):
                frame = buf.decode('utf-8')
                buf = b''
                name = data = None
                for line in frame.strip().split('\n'):
                    if line.startswith('event: '):
                        name = line[7:]
                    elif line.startswith('data: '):
                        data = json.loads(line[6:])
                if name:
                    events.append((name, data))
        return events

    def test_initial_snapshot_carries_peers_so_no_replay_is_needed(self):
        sock, resp = self._open_stream()
        try:
            events = self._read_events(sock, resp, 1)
            self.assertEqual(events[0][0], 'snapshot')
            self.assertIn('peers', events[0][1])
            self.assertEqual([r['name'] for r in events[0][1]['peers']], ['solo'])
        finally:
            sock.close()

    def test_transition_emits_exactly_one_event_and_stability_emits_none(self):
        sock, resp = self._open_stream()
        try:
            self._read_events(sock, resp, 1)                 # the initial snapshot

            self.drive('solo', True)                         # unknown→up: one event
            events = self._read_events(sock, resp, 1)
            self.assertEqual(len(events), 1)
            name, payload = events[0]
            self.assertEqual(name, 'peer')
            self.assertEqual(payload['prev_state'], 'unknown')
            self.assertEqual(payload['state'], 'up')
            self.assertEqual(payload['name'], 'solo')
            self.assertEqual(set(payload.keys()),
                             {'name', 'host', 'port', 'state', 'since', 'last_probe',
                              'consecutive_failures', 'last_error', 'prev_state', 'kind',
                              'endpoint_in_use', 'endpoint_changed_at'})

            for _ in range(4):                               # four stable rounds
                self.drive('solo', True)
            self.drive('solo', False, 'TimeoutError: timed out')   # cf=1, still up

            quiet = self._read_events(sock, resp, 1, timeout=1.5)
            self.assertEqual([e for e in quiet if e[0] == 'peer'], [],
                             'a stable peer generated SSE traffic')
        finally:
            sock.close()


class TestPeerStripStatic(unittest.TestCase):
    """§5.4 / §7.4 leg 7: the UI strip is neutral, labelled reachable, non-interactive."""

    @classmethod
    def setUpClass(cls):
        cls.html = (Path(__file__).parent.parent / 'static' / 'index.html').read_text()
        cls.css = cls.html[cls.html.index('<style>'):cls.html.index('</style>')]
        cls.peer_css = '\n'.join(
            m.group(0) for m in __import__('re').finditer(r'([^{}]+)\{([^{}]*)\}', cls.css)
            if 'peer' in m.group(1))
        start = cls.html.index('function renderPeers')
        cls.renderer = cls.html[start:cls.html.index('\n        function ', start + 10)]
        cls.markup = cls.html[cls.html.index('<div class="peer-strip"'):]
        cls.markup = cls.markup[:cls.markup.index('</div>', cls.markup.index('peer-items'))]

    def test_strip_element_present(self):
        self.assertIn('id="peer-strip"', self.html)

    def test_strip_says_reachable(self):
        self.assertIn('reachable', self.markup + self.renderer)

    def test_strip_never_says_healthy(self):
        """index.html says `healthy` elsewhere legitimately; the peer surface must not."""
        for scope, text in (('markup', self.markup), ('renderer', self.renderer),
                            ('css', self.peer_css)):
            with self.subTest(scope=scope):
                self.assertNotIn('healthy', text.lower())

    def test_strip_uses_no_alarm_colors(self):
        """No red, no amber, no green — these are other people's hosts (§5.4).

        Hard-coded rgba() literals count: they are the same pixels as var(--red)
        with the token guard walked around.
        """
        import re as _re
        self.assertNotIn('var(--red)', self.peer_css)
        self.assertNotIn('var(--amber)', self.peer_css)
        self.assertNotIn('var(--green)', self.peer_css)
        literals = _re.findall(r'#[0-9a-fA-F]{3,8}|rgba?\([^)]*\)', self.peer_css)
        self.assertEqual(literals, [],
                         f'peer strip must use theme tokens only, found {literals}')

    def test_strip_is_non_interactive(self):
        """No buttons, no click handlers — so the frozen 44 px touch-target set stands."""
        for forbidden in ('<button', 'onclick', 'addEventListener(\'click\''):
            self.assertNotIn(forbidden, self.markup)
        self.assertNotIn('<button', self.renderer)
        self.assertNotIn('onclick', self.renderer)

    def test_renderer_reads_the_snapshot_key(self):
        """The strip must be populated by the snapshot, not only by transition deltas —
        a peer that never flaps while the page is open is still a peer."""
        self.assertIn('state.peers', self.renderer)
        self.assertIn('renderPeers', self.html[self.html.index("addEventListener('snapshot'"):
                                               self.html.index('function renderHeader')])


class TestFleetSectionStatic(unittest.TestCase):
    """MVP8 §6.4: the strip grows a unit count and a stale marker; a read-only
    fleet section lists peers' units, visibly separated and non-interactive."""

    @classmethod
    def setUpClass(cls):
        cls.html = (Path(__file__).parent.parent / 'static' / 'index.html').read_text()
        cls.css = cls.html[cls.html.index('<style>'):cls.html.index('</style>')]
        import re as _re
        cls.fleet_css = '\n'.join(
            m.group(0) for m in _re.finditer(r'([^{}]+)\{([^{}]*)\}', cls.css)
            if 'fleet' in m.group(1))
        start = cls.html.index('function renderFleetSection')
        cls.renderer = cls.html[start:cls.html.index('\n        function ', start + 10)]
        start_p = cls.html.index('function renderPeers')
        cls.peer_renderer = cls.html[start_p:cls.html.index('\n        function ', start_p + 10)]
        cls.markup = cls.html[cls.html.index('<div class="fleet-section"'):]
        cls.markup = cls.markup[:cls.markup.index('</div>', cls.markup.index('fleet-peers'))]

    # ---- the strip amendment -------------------------------------------------
    def test_strip_shows_unit_count_for_a_fleet_peer(self):
        self.assertIn("peer.kind === 'roundhouse'", self.peer_renderer)
        self.assertIn('unit_count', self.peer_renderer)
        self.assertIn("' units'", self.peer_renderer)

    def test_strip_shows_a_stale_marker(self):
        self.assertIn('fed.stale', self.peer_renderer)
        self.assertIn("' · stale'", self.peer_renderer)

    def test_strip_stays_textcontent_only(self):
        self.assertNotIn('innerHTML', self.peer_renderer)

    # ---- the fleet section ---------------------------------------------------
    def test_section_present_and_labelled_read_only(self):
        self.assertIn('id="fleet-section"', self.html)
        self.assertIn('fleet (read-only)', self.markup)

    def test_section_is_hidden_until_a_fleet_peer_exists(self):
        self.assertIn('style="display: none;"', self.markup)
        self.assertIn("section.style.display = 'none'", self.renderer)

    def test_section_is_non_interactive(self):
        for forbidden in ('<button', 'onclick', "addEventListener('click'"):
            self.assertNotIn(forbidden, self.markup)
            self.assertNotIn(forbidden, self.renderer)
        self.assertNotIn('innerHTML', self.renderer)
        self.assertNotIn('localStorage', self.renderer)

    def test_section_uses_no_alarm_colors(self):
        import re as _re
        for token in ('var(--red)', 'var(--amber)', 'var(--green)'):
            self.assertNotIn(token, self.fleet_css)
        literals = _re.findall(r'#[0-9a-fA-F]{3,8}|rgba?\([^)]*\)', self.fleet_css)
        self.assertEqual(literals, [], f'fleet section must use theme tokens only: {literals}')

    def test_section_reads_the_fleet_route(self):
        self.assertIn("fetch('/api/fleet')", self.renderer)
        self.assertIn('u.source === peer.name', self.renderer)

    def test_section_refreshes_on_snapshot_and_peer_events_only(self):
        """Transition-only peer events bound the fetch rate (§6.4)."""
        peer_handler = self.html[self.html.index("addEventListener('peer'"):
                                 self.html.index('eventSource.onerror')]
        self.assertIn('renderFleetSection', peer_handler)
        render_fn = self.html[self.html.index('function render()'):
                              self.html.index('function renderHeader')]
        self.assertIn('renderFleetSection', render_fn)
        # No timer anywhere in the renderer: the SSE stream is the clock.
        self.assertNotIn('setInterval', self.renderer)

    def test_the_frozen_44px_touch_target_list_did_not_grow(self):
        """§6.4/K8: no 44 px amendment — the fleet rows are text, not targets."""
        self.assertIn('.unit-row, button, .off-section-toggle, .stop-tick-row, '
                      '#token, .enable-toggle {\n                min-height: 44px;',
                      self.html)
        self.assertNotIn('fleet', self.html[self.html.index('min-height: 44px') - 200:
                                            self.html.index('min-height: 44px')])


class TestBindFailureIsLoud(unittest.TestCase):
    """§3.3 / contract acceptance: all-or-nothing startup, and it does not lie first."""

    def _run_serve(self, extra_args, port, timeout=30):
        """Run a real `--serve` to completion. A run that does NOT exit is itself the
        failure: every case here must be a startup refusal."""
        import subprocess
        repo = Path(__file__).resolve().parents[2]
        cmd = [sys.executable, str(repo / 'mvp1' / 'roundhouse.py'), '--serve',
               '--unit-dir', str(repo / 'docs' / 'fixtures'), '--no-db',
               '--port', str(port)] + extra_args
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            self.fail(f'startup did not refuse; it kept serving: {" ".join(cmd)}\n'
                      f'stdout={e.stdout!r}')

    def test_no_listening_line_is_printed_when_a_later_bind_fails(self):
        """The message must not claim a door is open that startup then closes.

        Behavior was already correct (exit 1, nothing left listening); the stdout
        line lied about it, which is exactly the failure mode an operator finds a
        week later from a phone that cannot reach the box.
        """
        blocker = socket.socket()
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(('127.0.0.1', 0))
        blocked_port = blocker.getsockname()[1]
        blocker.listen(1)

        try:
            try:
                probe = socket.socket(socket.AF_INET6)
                probe.bind(('::1', 0))
                probe.close()
            except OSError:
                self.skipTest('IPv6 not available')

            # ::1 binds first and succeeds; 127.0.0.1 is occupied and fails.
            result = self._run_serve(['--bind', '::1,127.0.0.1'], blocked_port)

            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(f'cannot bind 127.0.0.1:{blocked_port}', result.stderr)
            self.assertIn('Errno 98', result.stderr)
            self.assertNotIn('listening', result.stdout.lower(),
                             'printed a listening line for a door it then closed')

            # Nothing left listening on the address that DID bind.
            leftover = socket.socket(socket.AF_INET6)
            leftover.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            try:
                leftover.bind(('::1', blocked_port))
            except OSError as e:
                self.fail(f'::1:{blocked_port} still held after the refusal: {e}')
            finally:
                leftover.close()
        finally:
            blocker.close()

    def test_every_failing_address_is_named(self):
        """Not just the first: an operator must see the whole truth in one go."""
        blocker4 = socket.socket()
        blocker4.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker4.bind(('127.0.0.1', 0))
        port = blocker4.getsockname()[1]
        blocker4.listen(1)
        blocker6 = socket.socket(socket.AF_INET6)
        blocker6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        try:
            blocker6.bind(('::1', port))
            blocker6.listen(1)
        except OSError:
            blocker4.close()
            blocker6.close()
            self.skipTest('IPv6 not available')

        try:
            result = self._run_serve(['--bind', '127.0.0.1,::1'], port)
            self.assertEqual(result.returncode, 1)
            self.assertIn(f'cannot bind 127.0.0.1:{port}', result.stderr)
            self.assertIn(f'cannot bind [::1]:{port}', result.stderr)
            self.assertNotIn('listening', result.stdout.lower())
        finally:
            blocker4.close()
            blocker6.close()

    def test_peer_interval_floor_refuses_at_startup(self):
        """J6: min 1 — a zero cadence would spin the watch thread."""
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        free_port = sock.getsockname()[1]
        sock.close()

        result = self._run_serve(['--bind', '127.0.0.1', '--peer', 'x=192.0.2.1:22',
                                  '--peer-interval', '0'], free_port)
        self.assertEqual(result.returncode, 1)
        self.assertIn('peer-interval', result.stderr)
        self.assertNotIn('listening', result.stdout.lower())

    def test_d2_collision_refuses_at_startup_naming_both_sides(self):
        """§6.3: a peer that names this host on a managed unit's port is a refusal."""
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        free_port = sock.getsockname()[1]
        sock.close()

        result = self._run_serve(['--bind', '127.0.0.1',
                                  '--peer', 'self=127.0.0.1:8085'], free_port)
        self.assertEqual(result.returncode, 1)
        self.assertIn("peer 'self' targets 127.0.0.1:8085", result.stderr)
        self.assertIn('qwen3.6-coding', result.stderr)
        self.assertNotIn('listening', result.stdout.lower())

    def test_listening_lines_reach_the_journal_while_running(self):
        """Announce, then serve — not announce-when-you-eventually-exit.

        stdout is block-buffered on a pipe, so under systemd the startup lines sat
        in the process buffer and `systemctl status` showed nothing at all. The
        first thing an operator checks after editing --bind is which doors opened.
        """
        import select
        import subprocess
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        free_port = sock.getsockname()[1]
        sock.close()

        repo = Path(__file__).resolve().parents[2]
        proc = subprocess.Popen(
            [sys.executable, str(repo / 'mvp1' / 'roundhouse.py'), '--serve',
             '--unit-dir', str(repo / 'docs' / 'fixtures'), '--no-db',
             '--port', str(free_port), '--bind', '127.0.0.1'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            line, deadline = '', time.time() + 15
            while time.time() < deadline and not line:
                if proc.poll() is not None:
                    self.fail('process exited before announcing')
                readable, _, _ = select.select([proc.stdout], [], [], 0.5)
                if readable:
                    line = proc.stdout.readline()
            self.assertIn(f'Roundhouse listening on http://127.0.0.1:{free_port}', line,
                          'the announcement never left the stdout buffer')
        finally:
            proc.terminate()
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()

    def test_an_ipv6_listener_actually_serves(self):
        """The contract's IPv6 row, end to end through a real process.

        Binding ::1 succeeded and then the lifecycle loop unpacked
        `srv.server_address` as a 2-tuple — an AF_INET6 sockaddr is a 4-tuple
        (host, port, flowinfo, scope_id), so every IPv6 start died with
        `too many values to unpack` after printing that it was listening.
        Constructing the socket is not the same as serving on it.
        """
        import subprocess
        try:
            probe = socket.socket(socket.AF_INET6)
            probe.bind(('::1', 0))
            free_port = probe.getsockname()[1]
            probe.close()
        except OSError:
            self.skipTest('IPv6 not available')

        repo = Path(__file__).resolve().parents[2]
        proc = subprocess.Popen(
            [sys.executable, str(repo / 'mvp1' / 'roundhouse.py'), '--serve',
             '--unit-dir', str(repo / 'docs' / 'fixtures'), '--no-db',
             '--port', str(free_port), '--bind', '::1'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline, served = time.time() + 20, False
            while time.time() < deadline and proc.poll() is None:
                try:
                    conn = http.client.HTTPConnection('::1', free_port, timeout=1)
                    conn.request('GET', '/api/units')
                    self.assertEqual(conn.getresponse().status, 200)
                    conn.close()
                    served = True
                    break
                except (OSError, http.client.HTTPException):
                    time.sleep(0.2)
            self.assertIsNone(proc.poll(), 'the process died instead of serving on ::1')
            self.assertTrue(served, 'nothing answered on [::1] after a successful bind')
        finally:
            proc.terminate()
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()

    def test_same_port_on_another_host_is_allowed(self):
        """recon 10: the rule is host-AND-port, so ampere:8085 must still start."""
        import subprocess
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        free_port = sock.getsockname()[1]
        sock.close()

        repo = Path(__file__).resolve().parents[2]
        proc = subprocess.Popen(
            [sys.executable, str(repo / 'mvp1' / 'roundhouse.py'), '--serve',
             '--unit-dir', str(repo / 'docs' / 'fixtures'), '--no-db',
             '--port', str(free_port), '--bind', '127.0.0.1',
             '--peer', 'ampere=ampere.fritz.box:8085', '--peer-interval', '3600'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.time() + 15
            listening = False
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    conn = http.client.HTTPConnection('127.0.0.1', free_port, timeout=1)
                    conn.request('GET', '/api/peers')
                    resp = conn.getresponse()
                    rows = json.loads(resp.read())['peers']
                    conn.close()
                    listening = True
                    self.assertEqual([r['name'] for r in rows], ['ampere'])
                    break
                except (OSError, http.client.HTTPException):
                    time.sleep(0.2)
            self.assertIsNone(proc.poll(), 'startup refused a legitimate same-port peer')
            self.assertTrue(listening)
        finally:
            proc.terminate()
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()


# ===== MVP8 §7 — THE HAZARD MATRIX ============================================
#
# The merged roster contains unit names that are not local, and both hosts run
# qwen3.6-coding.service. Every action path must key strictly on LOCAL units:
#   (a) a peer-only name answers 404 and nothing is actuated,
#   (b) a name that exists on BOTH hosts binds the LOCAL unit and the 2xx body
#       says which host did it,
#   (c) run_actuate still refuses anything outside the local selected set.
# Every route leg runs with fed data POPULATED — the adversarial condition: the
# 404 must hold while the roster is displaying the very name being refused.

import shutil
import subprocess
import tempfile
from unittest.mock import patch

PEER_ONLY = 'bee-only.service'
SHARED = 'both.service'          # exists on BOTH hosts — the collision IS the test
LOCAL_ONLY = 'local-a.service'


class _HazardHarness(unittest.TestCase):
    """A live server with two local units and a fleet peer advertising one of them."""

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.mkdtemp()
        fixtures = Path(__file__).resolve().parents[2] / 'docs' / 'fixtures'

        # Real unit files on disk, renamed to the matrix's names.
        cls.units = {}
        for name, src in ((SHARED, 'qwen3.6-coding.service'),
                          (LOCAL_ONLY, 'llama-server-gemma4.service')):
            dst = Path(cls.temp_dir) / name
            raw = (fixtures / src).read_bytes()
            if name == SHARED:
                raw = raw.replace(b'[Unit]', b'# roundhouse: on-demand\n[Unit]', 1)
            dst.write_bytes(raw)
            cls.units[name] = roundhouse.parse_unit(str(dst), dst.read_bytes())

        # The unit dir is a git repo with both files committed: preflight's git check
        # is orthogonal to the hazard question, and a 422 there would hide the answer.
        env = {**os.environ, 'GIT_AUTHOR_NAME': 'drill', 'GIT_AUTHOR_EMAIL': 'd@x',
               'GIT_COMMITTER_NAME': 'drill', 'GIT_COMMITTER_EMAIL': 'd@x'}
        for argv in (['git', 'init', '-q'], ['git', 'add', '--', SHARED, LOCAL_ONLY],
                     ['git', 'commit', '-qm', 'fixtures']):
            subprocess.run(argv, cwd=cls.temp_dir, env=env, check=True,
                           capture_output=True)

        cls.watcher = MagicMock(spec=roundhouse.Watcher)
        cls.watcher.lock = threading.Lock()
        cls.watcher.units = dict(cls.units)
        cls.watcher.mem_store = None
        cls.watcher._cgroup_cache = {}
        cls.watcher.snapshot.return_value = {
            'host': 'test', 'kernel': '6.1', 'now': time.time(),
            'mem': {'total_bytes': 32 * 1024 ** 3, 'available_bytes': 24 * 1024 ** 3},
            'sources': {}, 'self_port': 8090,
            'self_unit': {'unit': 'roundhouse.service', 'unit_file_state': 'enabled',
                          'enabled': True},
            'units': [
                {'unit': SHARED, 'rung': 'OFF', 'retired': False, 'enabled': False,
                 'port': 8085, 'alias': 'both', 'on_demand': True, 'badges': [],
                 'strategy_note': None, 'start_ts_mono': '1234',
                 'mem': {'bytes': 200 * 1024 ** 2, 'source': 'measured',
                         'label': 'measured peak'},
                 'port_conflict': None, 'stale': False},
                {'unit': LOCAL_ONLY, 'rung': 'READY', 'retired': False, 'enabled': True,
                 'port': 8093, 'alias': 'local-a', 'on_demand': False, 'badges': [],
                 'strategy_note': None, 'start_ts_mono': '5678',
                 'mem': {'bytes': 100 * 1024 ** 2, 'source': 'measured',
                         'label': 'measured peak'},
                 'port_conflict': None, 'stale': False},
            ],
        }

        cls.event_bus = roundhouse.EventBus()
        cls.watcher_lock = threading.Lock()
        cls.clock = [1_755_100_000.0]

        # The peer: a fleet peer whose roster carries BOTH a novel unit name and one
        # this host also runs. Populated through the real ingestion path.
        cls.peer_watch = roundhouse.PeerWatch(
            {'bee': ('127.0.0.1', 9)}, cls.watcher_lock, cls.event_bus,
            fleet={'bee': 'http://127.0.0.1:9'}, now=lambda: cls.clock[0])
        with cls.watcher_lock:
            cls.peer_watch.apply_result_unlocked('bee', True, None, cls.clock[0])
            cls.peer_watch.apply_fetch_unlocked('bee', True, {
                'mode': 'read-only',
                'units': [
                    {'unit': PEER_ONLY, 'rung': 'READY', 'port': 9001,
                     'alias': 'bee-only', 'enabled': True, 'on_demand': False,
                     'retired': False, 'strategy_note': None, 'badges': []},
                    {'unit': SHARED, 'rung': 'READY', 'port': 9999,
                     'alias': 'both', 'enabled': True, 'on_demand': False,
                     'retired': False, 'strategy_note': None, 'badges': []},
                ]}, {'model_list': [
                    {'model_name': 'bee-bee-only',
                     'litellm_params': {'model': 'openai/bee-only',
                                        'api_base': 'http://bee:9001/v1'}},
                    {'model_name': 'bee-both',
                     'litellm_params': {'model': 'openai/both',
                                        'api_base': 'http://bee:9999/v1'}},
                ]}, None, cls.clock[0])

        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        cls.port = sock.getsockname()[1]
        sock.close()

        cls.engine = MagicMock()
        cls.engine.current = None
        cls.engine.rollouts = {}
        cls.engine.pending_warm = None
        cls.engine.last_warm = None
        cls.engine.units = dict(cls.units)

        roundhouse.ACTUATE_ARMED = True
        roundhouse.TOKEN = 'test-token'
        cls.server = roundhouse.ThreadingHTTPServer(
            ('127.0.0.1', cls.port), roundhouse.RoundhouseRequestHandler,
            cls.watcher, cls.event_bus, cls.port,
            watcher_lock=cls.watcher_lock, rollout_engine=cls.engine,
            peer_watch=cls.peer_watch, peer_interval=5)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.temp_dir, ignore_errors=True)
        roundhouse.ACTUATE_ARMED = False
        roundhouse.TOKEN = None

    def setUp(self):
        self.engine.reset_mock()
        self.engine.current = None
        self.engine.pending_warm = None
        self.engine.rollouts = {}

    # ---- transport -----------------------------------------------------------
    def get(self, path):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            conn.request('GET', path)
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    def post(self, path, data=None, token='test-token'):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            body = json.dumps(data if data is not None else {}).encode('utf-8')
            headers = {'Content-Type': 'application/json'}
            if token:
                headers['Authorization'] = f'Bearer {token}'
            conn.request('POST', path, body, headers)
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    def measured(self):
        """A measured peak for both fixture units.

        The memory FIT check is orthogonal to the hazard question, and on a test box
        the fixtures' model files do not exist, so the estimator falls back to its 9
        GiB default and every (b) leg would 422 before it could say which host acted.
        _estimate_start_bytes is the one seam all three preflights share; the fit
        arithmetic, the port check, the retired check and the git check all stay real.
        """
        return patch.object(roundhouse, '_estimate_start_bytes',
                            lambda unit, profile, store: (200 * 1024 ** 2, 'measured peak row'))

    def assert_local_host(self, body):
        """(b): the 2xx body names the host that acted."""
        payload = json.loads(body)
        self.assertIn('host', payload, f'no host key in {payload}')
        self.assertEqual(payload['host'], os.uname()[1])
        return payload


class TestHazardRosterShowsWhatItRefuses(_HazardHarness):
    """The adversarial precondition: the peer's names ARE on the roster."""

    def test_roster_carries_both_peer_names(self):
        status, body = self.get('/api/fleet')
        self.assertEqual(status, 200)
        names = [(u['unit'], u['source']) for u in json.loads(body)['units']]
        self.assertIn((PEER_ONLY, 'bee'), names)
        self.assertIn((SHARED, 'bee'), names)
        self.assertIn((SHARED, 'test'), names)

    def test_fed_data_is_disjoint_from_every_actuation_table(self):
        """§7 structural invariant: fed data is write-only into PeerWatch.fed.

        This is the test that bites the merge-for-convenience shortcut (seed s3):
        pulling peer rows into watcher.units would make /api/fleet and the UI free
        and kill the hazard rule silently.
        """
        self.assertNotIn(PEER_ONLY, self.watcher.units)
        self.assertNotIn(PEER_ONLY, self.engine.units)
        snap = self.server.take_snapshot()
        self.assertEqual([r['unit'] for r in snap['units']], [SHARED, LOCAL_ONLY])
        for row in snap['units']:
            self.assertNotEqual(row.get('source'), 'bee')

        # ... and the roster route may not smuggle one in either: every row it calls
        # LOCAL must be a unit this host actually owns. This is the leg that bites the
        # convenience shortcut of appending fed rows to the snapshot so /api/fleet and
        # the UI come for free.
        status, body = self.get('/api/fleet')
        self.assertEqual(status, 200)
        doc = json.loads(body)
        local_rows = [u['unit'] for u in doc['units'] if u['source'] == doc['host']]
        self.assertEqual(sorted(local_rows), sorted(self.watcher.units),
                         f'a non-local unit is tagged as local: {local_rows}')

        # And the local roster route is unchanged by any of it.
        status, body = self.get('/api/units')
        self.assertEqual(sorted(u['unit'] for u in json.loads(body)['units']),
                         sorted(self.watcher.units))

    def test_local_units_route_is_untouched_by_federation(self):
        status, body = self.get('/api/units')
        self.assertEqual(status, 200)
        names = [u['unit'] for u in json.loads(body)['units']]
        self.assertNotIn(PEER_ONLY, names)


class TestHazardUnitDetail(_HazardHarness):
    """Row 1: GET /api/units/<u>."""

    def test_peer_only_name_is_404(self):
        status, _ = self.get(f'/api/units/{PEER_ONLY}')
        self.assertEqual(status, 404)

    def test_shared_name_resolves_the_local_unit(self):
        status, body = self.get(f'/api/units/{SHARED}')
        self.assertEqual(status, 200)
        detail = json.loads(body)
        # The peer advertises this same unit on port 9999; the local one is 8085.
        self.assertEqual(detail['unit'], SHARED)
        self.assertEqual(detail['port'], 8085)
        self.assertTrue(detail['path'].startswith(self.temp_dir))

    def test_local_only_control(self):
        status, _ = self.get(f'/api/units/{LOCAL_ONLY}')
        self.assertEqual(status, 200)


class TestHazardEditPreview(_HazardHarness):
    """Row 2a: POST /api/units/<u>/edit."""

    EDITS = {'edits': {'ctx': '8192'}}

    def test_peer_only_name_is_404_and_nothing_is_planned(self):
        with patch.object(roundhouse, 'plan_edits') as planner:
            status, body = self.post(f'/api/units/{PEER_ONLY}/edit', self.EDITS)
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)['error'], 'not_found')
        planner.assert_not_called()

    def test_shared_name_previews_the_local_unit_and_says_which_host(self):
        with self.measured():
            status, body = self.post(f'/api/units/{SHARED}/edit', self.EDITS)
        self.assertEqual(status, 200, body)
        payload = self.assert_local_host(body)
        self.assertEqual(payload['unit'], SHARED)

    def test_local_only_control(self):
        with self.measured():
            status, body = self.post(f'/api/units/{LOCAL_ONLY}/edit', self.EDITS)
        self.assertEqual(status, 200, body)
        self.assert_local_host(body)


class TestHazardRollout(_HazardHarness):
    """Row 2b: POST /api/units/<u>/rollout — the actuating twin of the preview."""

    def _confirm(self, unit_name):
        unit = self.units[unit_name]
        edits = roundhouse.plan_edits(unit, {'ctx': '8192'})
        return roundhouse.compute_confirm(unit_name, unit.raw, edits)

    def test_peer_only_name_is_404_and_no_rollout_starts(self):
        status, _ = self.post(f'/api/units/{PEER_ONLY}/rollout',
                              {'edits': {'ctx': '8192'}, 'confirm': 'whatever'})
        self.assertEqual(status, 404)
        self.engine.start_rollout.assert_not_called()

    def test_shared_name_rolls_out_the_local_unit_and_says_which_host(self):
        self.engine.start_rollout.return_value = {'rollout_id': 'ro-1-1'}
        status, body = self.post(f'/api/units/{SHARED}/rollout',
                                 {'edits': {'ctx': '8192'},
                                  'confirm': self._confirm(SHARED)})
        self.assertEqual(status, 202, body)
        self.assert_local_host(body)
        self.assertEqual(self.engine.start_rollout.call_args[0][0], SHARED)

    def test_local_only_control(self):
        self.engine.start_rollout.return_value = {'rollout_id': 'ro-1-2'}
        status, body = self.post(f'/api/units/{LOCAL_ONLY}/rollout',
                                 {'edits': {'ctx': '8192'},
                                  'confirm': self._confirm(LOCAL_ONLY)})
        self.assertEqual(status, 202, body)
        self.assert_local_host(body)


class TestHazardEnablement(_HazardHarness):
    """Row 2c: POST /api/units/<u>/enablement."""

    def test_peer_only_name_is_404_and_nothing_is_set(self):
        status, _ = self.post(f'/api/units/{PEER_ONLY}/enablement', {'enabled': True})
        self.assertEqual(status, 404)
        self.engine._set_enablement.assert_not_called()

    def test_shared_name_toggles_the_local_unit_and_says_which_host(self):
        self.watcher.apply_unit_file_state = MagicMock()
        status, body = self.post(f'/api/units/{SHARED}/enablement', {'enabled': True})
        self.assertEqual(status, 200, body)
        payload = self.assert_local_host(body)
        self.assertEqual(payload['unit'], SHARED)
        self.assertEqual(self.engine._set_enablement.call_args[0][0], SHARED)

    def test_local_only_control(self):
        self.watcher.apply_unit_file_state = MagicMock()
        status, body = self.post(f'/api/units/{LOCAL_ONLY}/enablement', {'enabled': False})
        self.assertEqual(status, 200, body)
        self.assert_local_host(body)


class TestHazardSwitchPreview(_HazardHarness):
    """Row 3: POST /api/switch/preview — target AND every stops member."""

    def test_peer_only_target_is_404(self):
        status, _ = self.post('/api/switch/preview', {'target': PEER_ONLY})
        self.assertEqual(status, 404)

    def test_peer_only_stop_member_is_404(self):
        """The per-stop check: a legitimate local target does not launder a peer stop."""
        status, _ = self.post('/api/switch/preview',
                              {'target': SHARED, 'stops': [PEER_ONLY]})
        self.assertEqual(status, 404)

    def test_shared_target_previews_locally_and_says_which_host(self):
        with self.measured():
            status, body = self.post('/api/switch/preview', {'target': SHARED})
        self.assertEqual(status, 200, body)
        payload = self.assert_local_host(body)
        # The peer advertises `both.service` on 9999; the preview bound the local 8085.
        self.assertEqual(payload['target']['unit'], SHARED)
        self.assertEqual(payload['target']['port'], 8085)

    def test_local_only_control(self):
        with self.measured():
            status, body = self.post('/api/switch/preview',
                                     {'target': SHARED, 'stops': [LOCAL_ONLY]})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)['stop_candidates'][0]['unit'], LOCAL_ONLY)


class TestHazardSwitchExecute(_HazardHarness):
    """Row 4: POST /api/switch."""

    def _confirm(self, target, stops):
        fingerprint = roundhouse.fleet_fingerprint(self.watcher.snapshot.return_value)
        return roundhouse.compute_switch_confirm(target, stops, fingerprint)

    def test_peer_only_target_is_404_and_no_switch_starts(self):
        status, _ = self.post('/api/switch', {'target': PEER_ONLY, 'confirm': 'x'})
        self.assertEqual(status, 404)
        self.engine.start_switch.assert_not_called()

    def test_peer_only_stop_member_is_404_and_no_switch_starts(self):
        status, _ = self.post('/api/switch',
                              {'target': SHARED, 'stops': [PEER_ONLY], 'confirm': 'x'})
        self.assertEqual(status, 404)
        self.engine.start_switch.assert_not_called()

    def test_shared_target_switches_the_local_unit_and_says_which_host(self):
        self.engine.start_switch.return_value = {'rollout_id': 'sw-1-1'}
        with self.measured():
            status, body = self.post('/api/switch',
                                     {'target': SHARED, 'stops': [],
                                      'confirm': self._confirm(SHARED, [])})
        self.assertEqual(status, 202, body)
        self.assert_local_host(body)
        self.assertEqual(self.engine.start_switch.call_args[0][0], SHARED)

    def test_local_only_control(self):
        self.engine.start_switch.return_value = {'rollout_id': 'sw-1-2'}
        with self.measured():
            status, body = self.post('/api/switch',
                                     {'target': SHARED, 'stops': [LOCAL_ONLY],
                                      'confirm': self._confirm(SHARED, [LOCAL_ONLY])})
        self.assertEqual(status, 202, body)
        self.assertEqual(list(self.engine.start_switch.call_args[0][1]), [LOCAL_ONLY])


class TestHazardWarm(_HazardHarness):
    """Rows 5-6: POST /api/warm by unit= and by logical=."""

    def test_peer_only_unit_is_404_unknown_unit(self):
        status, body = self.post('/api/warm', {'unit': PEER_ONLY})
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)['error'], 'unknown_unit')
        self.assertIsNone(self.engine.pending_warm)

    def test_peer_namespaced_logical_is_404_unknown_alias(self):
        """Only the LOCAL host prefix strips: a peer's namespaced model_name
        (`bee-both`, straight out of the merged fragment) never resolves here."""
        status, body = self.post('/api/warm', {'logical': 'bee-both'})
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)['error'], 'unknown_alias')
        self.assertIsNone(self.engine.pending_warm)

    def test_shared_unit_resolves_locally_and_says_which_host(self):
        self.engine.start_switch.return_value = {'rollout_id': 'sw-warm-1'}
        with self.measured():
            status, body = self.post('/api/warm', {'unit': SHARED})
        self.assertIn(status, (200, 202), body)
        self.assert_local_host(body)
        # It fired a switch at the LOCAL both.service, not the peer's.
        self.assertEqual(self.engine.start_switch.call_args[0][0], SHARED)

    def test_local_only_control_is_already_warm(self):
        status, body = self.post('/api/warm', {'unit': LOCAL_ONLY})
        self.assertEqual(status, 200, body)
        payload = self.assert_local_host(body)
        self.assertEqual(payload['status'], 'already_warm')


class TestHazardWarmCancel(_HazardHarness):
    """Row 7: POST /api/warm/cancel — no unit name crosses the wire, so the hazard
    is only "which host answered"; the queue it cancels is local by construction."""

    def test_cancel_says_which_host(self):
        self.engine.pending_warm = {'unit': SHARED, 'requester': 'test'}
        status, body = self.post('/api/warm/cancel')
        self.assertEqual(status, 200, body)
        payload = self.assert_local_host(body)
        self.assertEqual(payload['unit'], SHARED)

    def test_empty_queue_is_404(self):
        self.engine.pending_warm = None
        status, _ = self.post('/api/warm/cancel')
        self.assertEqual(status, 404)


class TestHazardRollbackDismiss(_HazardHarness):
    """Rows 8-9: POST /api/rollouts/<id>/rollback and /dismiss.

    These key on a rollout id, and a rollout id is minted by the host that ran it —
    a peer's record is simply not in this host's table, which is the 404. The `host`
    key on the record is what tells an operator whose rollout they are looking at.
    """

    RECORD = {'rollout_id': 'ro-9-9', 'kind': 'rollout', 'unit': SHARED,
              'phase': 'failed', 'detail': '', 'edits': [], 'was_active': True,
              'commit': None, 'restored': False, 'failure': 'verify',
              'rollback': {'offered': True}, 'started_at': 1.0, 'updated_at': 2.0}

    def test_unknown_rollout_id_is_404_on_both_routes(self):
        for path in ('/api/rollouts/ro-from-bee/rollback', '/api/rollouts/ro-from-bee/dismiss'):
            status, _ = self.post(path)
            self.assertEqual(status, 404, path)
        self.engine.rollback.assert_not_called()

    def test_rollback_of_a_local_record_says_which_host(self):
        self.engine.rollouts = {'ro-9-9': dict(self.RECORD)}
        self.engine.rollback.return_value = 'ro-9-9'
        status, body = self.post('/api/rollouts/ro-9-9/rollback')
        self.assertEqual(status, 202, body)
        self.assert_local_host(body)

    def test_dismiss_of_a_local_record_says_which_host(self):
        self.engine.rollouts = {'ro-9-9': dict(self.RECORD)}
        status, body = self.post('/api/rollouts/ro-9-9/dismiss')
        self.assertEqual(status, 200, body)
        self.assert_local_host(body)

    def test_public_record_carries_the_host_at_serialization(self):
        """K7: injected in rollout_public_record itself, so the snapshot and the SSE
        stream carry it too — and records already in memory need no migration."""
        record = roundhouse.rollout_public_record(dict(self.RECORD))
        self.assertEqual(record['host'], os.uname()[1])
        self.engine.rollouts = {'ro-9-9': dict(self.RECORD)}
        status, body = self.get('/api/rollouts/ro-9-9')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['host'], os.uname()[1])


class TestHazardLayer3RunActuate(unittest.TestCase):
    """(c): run_actuate refuses anything outside the local selected set — layer 3,
    below every route and every engine, where a peer name has no standing at all."""

    def setUp(self):
        fixtures = Path(__file__).resolve().parents[2] / 'docs' / 'fixtures'
        self.temp_dir = tempfile.mkdtemp()
        dst = Path(self.temp_dir) / SHARED
        dst.write_bytes((fixtures / 'qwen3.6-coding.service').read_bytes())
        self.local = {SHARED: roundhouse.parse_unit(str(dst), dst.read_bytes())}

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        roundhouse.ACTUATE_ARMED = False

    def test_peer_only_unit_is_refused_even_when_armed(self):
        roundhouse.ACTUATE_ARMED = True
        calls = []
        with patch.object(roundhouse.subprocess, 'run',
                          lambda *a, **k: calls.append(a) or MagicMock(returncode=0)):
            with self.assertRaises(roundhouse.ActuationError) as ctx:
                roundhouse.run_actuate(
                    ['systemctl', '--user', 'stop', '--', PEER_ONLY], self.local)
        self.assertIn('not in selected units', str(ctx.exception))
        self.assertEqual(calls, [], 'a subprocess ran for a peer unit')

    def test_shared_name_is_accepted_as_the_local_unit(self):
        roundhouse.ACTUATE_ARMED = True
        seen = []

        def fake_run(argv, **kw):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, '', '')

        with patch.object(roundhouse.subprocess, 'run', fake_run):
            roundhouse.run_actuate(['systemctl', '--user', 'stop', '--', SHARED], self.local)
        self.assertEqual(seen, [['systemctl', '--user', 'stop', '--', SHARED]])


class TestHazardLayer2Engine(unittest.TestCase):
    """Layer 2: the engine's own lookup misses for a peer name (§7 row 7)."""

    def setUp(self):
        fixtures = Path(__file__).resolve().parents[2] / 'docs' / 'fixtures'
        self.temp_dir = tempfile.mkdtemp()
        units = {}
        for name, src in ((SHARED, 'qwen3.6-coding.service'),
                          (LOCAL_ONLY, 'llama-server-gemma4.service')):
            dst = Path(self.temp_dir) / name
            dst.write_bytes((fixtures / src).read_bytes())
            units[name] = roundhouse.parse_unit(str(dst), dst.read_bytes())
        watcher = MagicMock()
        watcher.units = units
        watcher.mem_store = None
        watcher._cgroup_cache = {}
        watcher.lock = threading.Lock()
        watcher.snapshot.return_value = {'host': 'test', 'units': [
            {'unit': SHARED, 'rung': 'OFF', 'retired': False, 'enabled': False,
             'since': 1.0, 'badges': [], 'port': 8085, 'mem': {}, 'start_ts_mono': '1'},
            {'unit': LOCAL_ONLY, 'rung': 'READY', 'retired': False, 'enabled': True,
             'since': 1.0, 'badges': [], 'port': 8093, 'mem': {}, 'start_ts_mono': '2'},
        ], 'mem': {}, 'sources': {}}
        self.engine = roundhouse.RolloutEngine(watcher, units, self.temp_dir, 8090,
                                               roundhouse.EventBus(), threading.Lock())

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_start_switch_refuses_a_peer_only_target(self):
        with self.assertRaises(roundhouse.ActuationError):
            self.engine.start_switch(PEER_ONLY, [], 'confirm')

    def test_start_switch_refuses_a_peer_only_stop(self):
        with self.assertRaises(roundhouse.ActuationError):
            self.engine.start_switch(SHARED, [PEER_ONLY], 'confirm')

    def test_start_rollout_refuses_a_peer_only_unit(self):
        with self.assertRaises(roundhouse.ActuationError):
            self.engine.start_rollout(PEER_ONLY, [], 'confirm')


# ===== MVP8 §6 — the fleet surfaces, and the promise to the local consumer =====


class _FleetSurfaceHarness(unittest.TestCase):
    """A server with one fleet peer whose federated data is populated by hand.

    The peer's fragment carries a key this version does not know (`rpm`) and an
    entry namespaced by the peer's own host — verbatim means both survive.
    """

    PEER_ENTRIES = [
        {'model_name': 'ampere-qwen3.6-coding',
         'litellm_params': {'model': 'openai/qwen3.6-coding',
                            'api_base': 'http://ampere.fritz.box:8085/v1',
                            'api_key': 'none', 'rpm': 240},
         'model_info': {'unit': 'qwen3.6-coding.service', 'logical': 'qwen3.6-coding',
                        'host': 'ampere', 'rung': 'READY', 'accelerator': 'npu'}},
    ]
    PEER_UNITS = [
        {'unit': 'qwen3.6-coding.service', 'rung': 'READY', 'port': 8085,
         'alias': 'qwen3.6-coding', 'enabled': True, 'on_demand': False,
         'retired': False, 'strategy_note': None, 'badges': [],
         'sensed_at': 1.0, 'description': 'peer-only key that must be dropped'},
    ]

    @classmethod
    def setUpClass(cls):
        cls.watcher = StubWatcher()
        cls.event_bus = roundhouse.EventBus()
        cls.watcher_lock = threading.Lock()
        cls.clock = [1_755_100_000.0]
        cls.peer_watch = roundhouse.PeerWatch(
            {'ampere': ('ampere.fritz.box', 8099)}, cls.watcher_lock, cls.event_bus,
            fleet={'ampere': 'https://ampere.fritz.box:8099'}, now=lambda: cls.clock[0])

        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        cls.port = sock.getsockname()[1]
        sock.close()
        cls.server = roundhouse.ThreadingHTTPServer(
            ('127.0.0.1', cls.port), roundhouse.RoundhouseRequestHandler,
            cls.watcher, cls.event_bus, cls.port,
            watcher_lock=cls.watcher_lock, peer_watch=cls.peer_watch,
            advertise_host='boltzmann', peer_interval=60)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def feed(self, state='fresh', up=True):
        """Put the peer into (reachability, fed) = (up|down, fresh|stale|never)."""
        with self.watcher_lock:
            pw = self.peer_watch
            pw.peers['ampere'].update({'state': 'up' if up else 'down',
                                       'consecutive_failures': 0})
            if state == 'never':
                pw.fed['ampere'].update({'state': 'never', 'units': [], 'entries': [],
                                         'mode': None, 'fetched_at': None,
                                         'reason': 'tls: SSLCertVerificationError'})
                return
            pw.apply_fetch_unlocked(
                'ampere', True,
                {'mode': 'read-only', 'units': [dict(u) for u in self.PEER_UNITS]},
                {'model_list': [dict(e) for e in self.PEER_ENTRIES]}, None,
                1_755_159_990.0)
            if state == 'stale':
                pw.fed['ampere']['state'] = 'stale'
                pw.fed['ampere']['reason'] = 'down: peer unreachable (tcp probe failed)'

    def clear(self):
        with self.watcher_lock:
            self.peer_watch.fed['ampere'].update(
                {'state': 'never', 'units': [], 'entries': [], 'mode': None,
                 'invalid_entries': 0, 'fetched_at': None, 'attempted_at': None,
                 'reason': None})
            self.peer_watch.peers['ampere']['state'] = 'unknown'

    def get(self, path):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            conn.request('GET', path)
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    def post(self, path):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            conn.request('POST', path, b'{}', {'Content-Type': 'application/json'})
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()


class TestRoutingConfigIsPinned(_FleetSurfaceHarness):
    """MVP8 acceptance: /api/routing-config is byte-identical to MVP7 behaviour.

    hossenfelder pulls this route live. Widening it to the fleet would change a
    running consumer's behaviour without asking — so the pin runs the route WITH
    federated data populated and diffs it against the unfederated body.
    """

    def _frozen_clock_body(self, path):
        """Both bodies under one frozen clock, so 'byte-identical' means bytes."""
        class _FrozenDatetime(roundhouse.datetime):
            @classmethod
            def now(cls, tz=None):
                return roundhouse.datetime(2026, 8, 14, 12, 0, 0, tzinfo=tz)

        with patch.object(roundhouse, 'datetime', _FrozenDatetime):
            status, body = self.get(path)
        self.assertEqual(status, 200)
        return body

    def test_yaml_is_byte_identical_with_and_without_federation(self):
        self.clear()
        without = self._frozen_clock_body('/api/routing-config')
        self.feed()
        with_fed = self._frozen_clock_body('/api/routing-config')
        self.assertEqual(with_fed, without)

    def test_json_is_byte_identical_with_and_without_federation(self):
        self.clear()
        without = self._frozen_clock_body('/api/routing-config.json')
        self.feed()
        with_fed = self._frozen_clock_body('/api/routing-config.json')
        self.assertEqual(with_fed, without)

    def test_no_peer_entry_and_no_fleet_header_while_federated(self):
        self.feed()
        _, body = self.get('/api/routing-config')
        text = body.decode()
        self.assertNotIn('ampere', text)
        self.assertNotIn('contributor:', text)
        self.assertNotIn('excluded:', text)
        self.assertNotIn('conflict:', text)
        self.assertIn('# warm-hook:', text)          # the local header, unchanged
        _, jbody = self.get('/api/routing-config.json')
        data = json.loads(jbody)
        self.assertEqual(set(data), {'generated_by', 'generated_at', 'warm_hook', 'model_list'})
        self.assertTrue(all('ampere' not in e['model_name'] for e in data['model_list']))

    def test_the_fleet_route_is_where_the_peers_live(self):
        """The other half of the promise: the fleet view exists, at its own URL."""
        self.feed()
        _, body = self.get('/api/routing-config/fleet')
        text = body.decode()
        self.assertIn('ampere-qwen3.6-coding', text)
        self.assertIn('# contributor: ampere (fetched ', text)
        _, jbody = self.get('/api/routing-config/fleet.json')
        data = json.loads(jbody)
        self.assertIn('ampere-qwen3.6-coding', [e['model_name'] for e in data['model_list']])


class TestFleetFragment(_FleetSurfaceHarness):
    """§5: the merged fragment — verbatim, contributor-named, absent hosts excluded."""

    def test_peer_entry_is_merged_verbatim(self):
        self.feed()
        _, body = self.get('/api/routing-config/fleet.json')
        merged = next(e for e in json.loads(body)['model_list']
                      if e['model_name'] == 'ampere-qwen3.6-coding')
        self.assertEqual(merged, self.PEER_ENTRIES[0],
                         'the peer entry was reshaped — merging is concatenation')

    def test_unknown_keys_reach_the_yaml_too(self):
        self.feed()
        _, body = self.get('/api/routing-config/fleet')
        self.assertIn('rpm: 240', body.decode())
        self.assertIn('accelerator: npu', body.decode())

    def test_local_entries_come_first_and_keep_their_own_namespace(self):
        self.feed()
        _, body = self.get('/api/routing-config/fleet.json')
        names = [e['model_name'] for e in json.loads(body)['model_list']]
        local = [n for n in names if not n.startswith('ampere-')]
        self.assertTrue(names[:len(local)] == local, names)

    def test_a_peer_that_is_not_up_contributes_nothing(self):
        self.feed(state='stale', up=False)
        _, body = self.get('/api/routing-config/fleet.json')
        data = json.loads(body)
        self.assertNotIn('ampere-qwen3.6-coding', [e['model_name'] for e in data['model_list']])
        self.assertEqual(data['excluded'][0]['name'], 'ampere')
        self.assertEqual(data['excluded'][0]['state'], 'down')
        _, yml = self.get('/api/routing-config/fleet')
        self.assertIn('# excluded: ampere (down, stale: down)', yml.decode())

    def test_the_yaml_header_says_it_is_the_fleet_view(self):
        """§5.3's frozen spelling. Without the `(fleet)` marker the two documents
        disagree about what they are — the JSON twin already says it — and a consumer
        that saved a fragment cannot tell a fleet document from a local one."""
        self.feed()
        _, yml = self.get('/api/routing-config/fleet')
        _, jsn = self.get('/api/routing-config/fleet.json')
        generated_by = json.loads(jsn)['generated_by']
        self.assertEqual(generated_by, 'roundhouse@boltzmann (fleet)')
        self.assertIn(f'# generated-by: {generated_by}', yml.decode())

    def test_the_two_fleet_documents_agree_on_their_contributor(self):
        self.feed()
        _, yml = self.get('/api/routing-config/fleet')
        _, jsn = self.get('/api/routing-config/fleet.json')
        local = json.loads(jsn)['contributors'][0]['name']
        self.assertIn(f'# contributor: {local} (local)', yml.decode())
        self.assertIn(f'roundhouse@{local} (fleet)', yml.decode())

    def test_the_json_twin_timestamp_is_a_real_iso_z_stamp(self):
        """`datetime.isoformat()` on an aware datetime already ends in +00:00;
        appending 'Z' to it produces a stamp no parser accepts. The local route's
        format is the one to match — consumers read both documents."""
        self.feed()
        _, jsn = self.get('/api/routing-config/fleet.json')
        _, local = self.get('/api/routing-config.json')
        stamp = json.loads(jsn)['generated_at']
        self.assertRegex(stamp, r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
        self.assertEqual(len(stamp), len(json.loads(local)['generated_at']))

    def test_the_fragment_carries_no_warm_hook(self):
        """K5: one hook URL would lie for peer entries."""
        self.feed()
        _, body = self.get('/api/routing-config/fleet')
        self.assertNotIn('warm-hook', body.decode())
        _, jbody = self.get('/api/routing-config/fleet.json')
        self.assertNotIn('warm_hook', json.loads(jbody))

    def test_conflict_is_surfaced_in_both_documents(self):
        """Two hosts advertising one model_name: first wins, loudly."""
        _, local = self.get('/api/routing-config.json')
        clash = json.loads(local)['model_list'][0]['model_name']
        with self.watcher_lock:
            self.peer_watch.peers['ampere']['state'] = 'up'
            self.peer_watch.apply_fetch_unlocked(
                'ampere', True, {'mode': 'read-only', 'units': []},
                {'model_list': [{'model_name': clash,
                                 'litellm_params': {'model': 'openai/theirs'}}]},
                None, 1_755_159_990.0)
        _, jbody = self.get('/api/routing-config/fleet.json')
        data = json.loads(jbody)
        self.assertEqual(data['conflicts'],
                         [{'model_name': clash, 'kept_source': 'boltzmann',
                           'dropped_source': 'ampere'}])
        kept = [e for e in data['model_list'] if e['model_name'] == clash]
        self.assertEqual(len(kept), 1)
        self.assertNotEqual(kept[0]['litellm_params']['model'], 'openai/theirs')
        _, yml = self.get('/api/routing-config/fleet')
        self.assertIn(f"# conflict: model_name '{clash}' also advertised by ampere", yml.decode())


class TestFleetRosterShape(_FleetSurfaceHarness):
    """§6.1: the frozen shape of GET /api/fleet."""

    LOCAL_KEYS = {'unit', 'rung', 'port', 'alias', 'enabled', 'on_demand', 'retired',
                  'strategy_note', 'badges', 'source', 'stale'}
    PEER_BLOCK_KEYS = {'name', 'kind', 'url', 'state', 'mode', 'fed_state', 'stale',
                       'reason', 'fetched_at', 'attempted_at', 'unit_count',
                       'invalid_entries'}

    def test_envelope(self):
        self.feed()
        status, body = self.get('/api/fleet')
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(set(data), {'host', 'mode', 'generated_at', 'fetch', 'units', 'peers'})
        self.assertEqual(set(data['fetch']), {'timeout_seconds', 'max_bytes', 'cadence_seconds'})
        self.assertEqual(data['fetch']['timeout_seconds'], roundhouse.FETCH_TIMEOUT_SEC)
        self.assertEqual(data['fetch']['max_bytes'], roundhouse.FETCH_MAX_BYTES)

    def test_local_rows_are_keep_listed_first_and_never_stale(self):
        self.feed()
        _, body = self.get('/api/fleet')
        data = json.loads(body)
        local = [u for u in data['units'] if u['source'] == data['host']]
        self.assertTrue(data['units'][:len(local)] == local, 'local units are not first')
        for row in local:
            self.assertLessEqual(set(row) - {'port_conflict'}, self.LOCAL_KEYS,
                                 f'unexpected key on a local row: {set(row)}')
            self.assertIs(row['stale'], False)

    def test_peer_rows_are_keep_listed_and_tagged_with_their_source(self):
        self.feed()
        _, body = self.get('/api/fleet')
        peer_rows = [u for u in json.loads(body)['units'] if u['source'] == 'ampere']
        self.assertEqual(len(peer_rows), 1)
        self.assertNotIn('description', peer_rows[0],
                         'a peer key outside the keep-list reached the roster')
        self.assertNotIn('sensed_at', peer_rows[0])
        self.assertIs(peer_rows[0]['stale'], False)

    def test_a_null_port_conflict_is_omitted_on_every_row(self):
        """K6: I5's rule verbatim — port_conflict only when non-null."""
        self.feed()
        _, body = self.get('/api/fleet')
        rows = json.loads(body)['units']
        for row in rows:
            if 'port_conflict' in row:
                self.assertTrue(row['port_conflict'], row)   # present only when real
        clean = [r for r in rows if 'port_conflict' not in r]
        self.assertTrue(clean, 'every row carried a port_conflict key')

    def test_peer_block(self):
        self.feed()
        _, body = self.get('/api/fleet')
        peer = json.loads(body)['peers'][0]
        self.assertEqual(set(peer), self.PEER_BLOCK_KEYS)
        self.assertEqual(peer['kind'], 'roundhouse')
        self.assertEqual(peer['url'], 'https://ampere.fritz.box:8099')
        self.assertEqual(peer['mode'], 'read-only')
        self.assertEqual(peer['fed_state'], 'fresh')
        self.assertEqual(peer['unit_count'], 1)
        self.assertEqual(peer['invalid_entries'], 0)

    def test_retained_rows_serve_while_stale(self):
        """K4 retention: the roster keeps the last-known units and marks them."""
        self.feed(state='stale', up=False)
        _, body = self.get('/api/fleet')
        data = json.loads(body)
        peer_rows = [u for u in data['units'] if u['source'] == 'ampere']
        self.assertEqual(len(peer_rows), 1)
        self.assertIs(peer_rows[0]['stale'], True)
        self.assertIs(data['peers'][0]['stale'], True)
        self.assertEqual(data['peers'][0]['state'], 'down')
        self.assertTrue(data['peers'][0]['reason'].startswith('down: '))
        for row in [u for u in data['units'] if u['source'] == data['host']]:
            self.assertIs(row['stale'], False, 'a local row went stale')

    def test_untrusted_certificate_is_up_but_never_fresh(self):
        """The contract's oddest row: reachable by TCP, not trustworthy over TLS."""
        self.feed(state='never')
        _, body = self.get('/api/fleet')
        peer = json.loads(body)['peers'][0]
        self.assertEqual(peer['state'], 'up')
        self.assertEqual(peer['fed_state'], 'never')
        self.assertIs(peer['stale'], True)
        self.assertTrue(peer['reason'].startswith('tls: '))
        self.assertEqual(peer['unit_count'], 0)

    def test_post_is_405(self):
        for path in ('/api/fleet', '/api/routing-config/fleet', '/api/routing-config/fleet.json'):
            status, _ = self.post(path)
            self.assertEqual(status, 405, path)

    def test_route_is_unauthenticated(self):
        roundhouse.TOKEN = 'a-token'
        try:
            status, _ = self.get('/api/fleet')
        finally:
            roundhouse.TOKEN = None
        self.assertEqual(status, 200)


class TestFleetRosterWithoutFleetPeers(unittest.TestCase):
    """§6.1: the route always exists; with no fleet peers it is the local view."""

    @classmethod
    def setUpClass(cls):
        cls.watcher = StubWatcher()
        cls.event_bus = roundhouse.EventBus()
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        cls.port = sock.getsockname()[1]
        sock.close()
        cls.server = roundhouse.ThreadingHTTPServer(
            ('127.0.0.1', cls.port), roundhouse.RoundhouseRequestHandler,
            cls.watcher, cls.event_bus, cls.port)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_local_only(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        conn.request('GET', '/api/fleet')
        data = json.loads(conn.getresponse().read())
        conn.close()
        self.assertEqual(data['peers'], [])
        self.assertTrue(data['units'])
        self.assertTrue(all(u['source'] == data['host'] and u['stale'] is False
                            for u in data['units']))

    def test_fleet_fragment_equals_the_local_view(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        conn.request('GET', '/api/routing-config/fleet.json')
        fleet = json.loads(conn.getresponse().read())
        conn.close()
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        conn.request('GET', '/api/routing-config.json')
        local = json.loads(conn.getresponse().read())
        conn.close()
        self.assertEqual(fleet['model_list'], local['model_list'])
        self.assertEqual(fleet['excluded'], [])
        self.assertEqual(fleet['conflicts'], [])


class TestFedSSEEvents(_FleetSurfaceHarness):
    """§4.2/§6.3: a fed transition publishes exactly one `peer` event; a repeat
    success publishes none. Flat-lining peers stay silent, of either kind."""

    def _subscribe(self):
        q = self.event_bus.subscribe()
        return q

    def test_never_to_fresh_fires_once_then_goes_quiet(self):
        self.clear()
        q = self.event_bus.subscribe()
        try:
            with self.watcher_lock:
                self.peer_watch.peers['ampere']['state'] = 'up'
                first = self.peer_watch.apply_fetch_unlocked(
                    'ampere', True, {'mode': 'read-only', 'units': []},
                    {'model_list': []}, None, 1_755_159_990.0)
                second = self.peer_watch.apply_fetch_unlocked(
                    'ampere', True, {'mode': 'read-only', 'units': []},
                    {'model_list': []}, None, 1_755_159_995.0)
        finally:
            self.event_bus.unsubscribe(q)
        self.assertIsNotNone(first)
        self.assertEqual(first['fed_prev'], 'never')
        self.assertEqual(first['fed']['state'], 'fresh')
        self.assertIsNone(second, 'a repeated success must not publish')

    def test_fresh_to_stale_fires_with_the_reason(self):
        self.clear()
        with self.watcher_lock:
            self.peer_watch.peers['ampere']['state'] = 'up'
            self.peer_watch.apply_fetch_unlocked(
                'ampere', True, {'mode': 'read-only', 'units': []},
                {'model_list': []}, None, 1_755_159_990.0)
            payload = self.peer_watch.apply_fetch_unlocked(
                'ampere', False, None, None, 'tls: SSLCertVerificationError',
                1_755_159_995.0)
        self.assertIsNotNone(payload)
        self.assertEqual(payload['fed_prev'], 'fresh')
        self.assertEqual(payload['fed']['state'], 'stale')
        self.assertTrue(payload['fed']['reason'].startswith('tls: '))
        self.assertEqual(payload['kind'], 'roundhouse')


class TestFetchLockDiscipline(unittest.TestCase):
    """Risk #1: no fetch — and no _fetch_peer call of any kind — while a lock is held."""

    def setUp(self):
        self.lock = threading.Lock()
        self.free_during_fetch = []
        self.pw = roundhouse.PeerWatch(
            {'bee': ('127.0.0.1', 9)}, self.lock, roundhouse.EventBus(),
            fleet={'bee': 'http://127.0.0.1:9'}, now=lambda: 1000.0)

    def _opener(self, payloads):
        """An opener that checks the lock is FREE at the moment of the request."""
        test = self

        class _Resp:
            def __init__(self, body):
                self._body = body

            def read(self, n=-1):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _Opener:
            def __init__(self):
                self.calls = []

            def open(self, url, timeout=None):
                acquired = test.lock.acquire(blocking=False)
                test.free_during_fetch.append(acquired)
                if acquired:
                    test.lock.release()
                self.calls.append(url)
                return _Resp(json.dumps(payloads[len(self.calls) - 1]).encode())

        return _Opener()

    def test_the_watcher_lock_is_free_while_the_round_fetches(self):
        opener = self._opener([{'mode': 'read-only', 'units': []}, {'model_list': []}])
        real_fetch = roundhouse._fetch_peer

        def fetch(pw, name, path, _opener=None, index=0):
            # The parameter is _opener, NOT opener: this body closes over the
            # outer `opener` (the recording stub). Naming the parameter `opener`
            # shadows that closure, real_fetch gets None, and the round quietly
            # does a real connect to 127.0.0.1:9 — the test then reports "no
            # fetches" as if the lock discipline had broken. It had not.
            return real_fetch(pw, name, path, opener=opener)

        with patch.object(roundhouse, '_probe_peer', lambda pw, name, index=0, connect=None: (True, None)), \
             patch.object(roundhouse, '_fetch_peer', fetch):
            roundhouse.peer_watch_round(self.pw)

        self.assertEqual(self.free_during_fetch, [True, True],
                         'a lock was held across a federated fetch')
        self.assertEqual(opener.calls,
                         ['http://127.0.0.1:9/api/units',
                          'http://127.0.0.1:9/api/routing-config.json'])
        self.assertEqual(self.pw.fed['bee']['state'], 'fresh')

    def test_the_second_document_is_skipped_when_the_first_fails(self):
        calls = []

        def fetch(pw, name, path, opener=None, index=0):
            calls.append(path)
            return (False, None, 'connect: refused')

        with patch.object(roundhouse, '_probe_peer', lambda pw, name, index=0, connect=None: (True, None)), \
             patch.object(roundhouse, '_fetch_peer', fetch):
            roundhouse.peer_watch_round(self.pw)

        self.assertEqual(calls, ['/api/units'])
        self.assertEqual(self.pw.fed['bee']['reason'], 'connect: refused')

    def test_a_peer_that_is_not_up_is_never_fetched(self):
        calls = []

        def fetch(pw, name, path, opener=None, index=0):
            calls.append(path)
            return (True, {}, None)

        with patch.object(roundhouse, '_probe_peer',
                          lambda pw, name, index=0, connect=None: (False, 'refused')), \
             patch.object(roundhouse, '_fetch_peer', fetch):
            roundhouse.peer_watch_round(self.pw)
        self.assertEqual(calls, [])


class TestHangingPeerCostsOnlyItsTimeout(unittest.TestCase):
    """MVP8 acceptance, measured as MVP7 measured it: a fleet peer that accepts and
    never answers must cost only its own timeout — the 3 s tick, the API and the
    operation slot live on other threads and are provably unaffected."""

    def test_api_latency_is_unaffected_while_a_fetch_hangs(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(4)
        hang_port = listener.getsockname()[1]

        watcher = StubWatcher()
        bus = roundhouse.EventBus()
        lock = threading.Lock()
        pw = roundhouse.PeerWatch({'hang': ('127.0.0.1', hang_port)}, lock, bus,
                                  fleet={'hang': f'http://127.0.0.1:{hang_port}'})

        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.close()
        server = roundhouse.ThreadingHTTPServer(
            ('127.0.0.1', port), roundhouse.RoundhouseRequestHandler,
            watcher, bus, port, watcher_lock=lock, peer_watch=pw, peer_interval=5)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.1)

        round_done = threading.Event()
        started = time.monotonic()
        rounder = threading.Thread(
            target=lambda: (roundhouse.peer_watch_round(pw), round_done.set()),
            daemon=True)
        rounder.start()
        time.sleep(0.3)                       # the fetch is now blocked on the listener

        try:
            latencies = []
            for _ in range(5):
                t0 = time.monotonic()
                conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                conn.request('GET', '/api/units')
                self.assertEqual(conn.getresponse().status, 200)
                conn.close()
                latencies.append(time.monotonic() - t0)
            self.assertFalse(round_done.is_set(), 'the fetch did not actually hang')
            self.assertLess(max(latencies), 1.0,
                            f'API latency degraded while a peer hung: {latencies}')

            round_done.wait(timeout=roundhouse.FETCH_TIMEOUT_SEC * 3)
            self.assertTrue(round_done.is_set(), 'the round never gave up on the peer')
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, roundhouse.FETCH_TIMEOUT_SEC * 2 + 2,
                            f'the hang cost more than its own timeout: {elapsed:.1f}s')
            self.assertTrue(pw.fed['hang']['reason'].startswith(('timeout:', 'connect:')),
                            pw.fed['hang']['reason'])
        finally:
            server.shutdown()
            server.server_close()
            listener.close()


class TestBudgetPin(unittest.TestCase):
    """§7.4, L6: Executable budget test — worst-case round must fit 60s cadence."""

    def test_worst_case_round_under_60s(self):
        """Verify worst-case round = 56s at FLEET_CANDIDATE_MAX=3, PEER_MAX=8 (§L6)."""
        # Worst round = (PEER_MAX - FLEET_PEER_MAX) * PEER_TIMEOUT_SEC
        #             + FLEET_PEER_MAX * ((FLEET_CANDIDATE_MAX - 1) * PEER_TIMEOUT_SEC + 2 * FETCH_TIMEOUT_SEC)
        tcp_peers = roundhouse.PEER_MAX - roundhouse.FLEET_PEER_MAX
        fleet_peers = roundhouse.FLEET_PEER_MAX
        tcp_cost = tcp_peers * roundhouse.PEER_TIMEOUT_SEC
        fleet_cost = fleet_peers * ((roundhouse.FLEET_CANDIDATE_MAX - 1) * roundhouse.PEER_TIMEOUT_SEC +
                                    2 * roundhouse.FETCH_TIMEOUT_SEC)
        worst_round = tcp_cost + fleet_cost

        # Must be <= 60s (the cadence floor)
        self.assertLessEqual(worst_round, 60.0,
                           f"Worst round {worst_round}s exceeds 60s cadence; reduce constants per §L6")

        # With current constants, should be exactly 56s
        self.assertEqual(worst_round, 56.0)


class TestEndpointStatics(unittest.TestCase):
    """§7.5: UI statics for endpoint display (T2)."""

    def test_index_html_has_listener_strip_id(self):
        """index.html has #listener-strip (§5.4)."""
        with open(Path(__file__).parent.parent / 'static' / 'index.html') as f:
            html = f.read()
        self.assertIn('listener-strip', html, "Missing #listener-strip element per §5.4")

    def test_index_html_listening_title(self):
        """index.html contains 'listening' title string (§5.4)."""
        with open(Path(__file__).parent.parent / 'static' / 'index.html') as f:
            html = f.read()
        self.assertIn('listening', html.lower(), "Missing 'listening' title per §5.4")

    def test_index_html_peer_endpoint_marker(self):
        """index.html contains ' · via ' fragment for endpoint display (§5.4)."""
        with open(Path(__file__).parent.parent / 'static' / 'index.html') as f:
            html = f.read()
        self.assertIn(' · via ', html, "Missing ' · via ' endpoint marker per §5.4")

    def test_the_resolved_addresses_are_not_gated_on_a_kind_that_cannot_occur(self):
        """§5.4/L4: a listener row's `kind` is mandatory|optional. Gating the ` → a1, a2`
        rendering on kind === 'name' hides the resolved set on every row there is —
        and the resolved set IS the roaming moment the strip exists to show."""
        with open(Path(__file__).parent.parent / 'static' / 'index.html') as f:
            html = f.read()
        self.assertNotIn("kind === 'name'", html,
                         "listener rows are never kind 'name'; test `resolved` itself")
        self.assertIn(' → ', html, "Missing the resolved-address fragment per §5.4")


class TestPeerEndpointFields(unittest.TestCase):
    """§7.4: Peer row endpoint fields (endpoint_in_use, endpoint_changed_at)."""

    def test_rows_include_endpoint_fields(self):
        """Peer rows include endpoint_in_use and endpoint_changed_at (§4.3, §5.3)."""
        lock = threading.Lock()
        declared = {'tcp': [('h1', 8080)], 'fleet': [('h2', 8081)]}
        fleet = {'fleet': ['http://h2:8081']}
        bus = MagicMock()
        pw = roundhouse.PeerWatch(declared, lock, bus, fleet=fleet)

        rows = pw.rows_unlocked()
        for row in rows:
            self.assertIn('endpoint_in_use', row)
            self.assertIn('endpoint_changed_at', row)

    def test_fleet_rows_have_endpoints_list(self):
        """Fleet peer rows include 'endpoints' list of all candidate URLs (§4.3)."""
        lock = threading.Lock()
        declared = {'fleet': [('h1', 80), ('h2', 81)]}
        fleet = {'fleet': ['http://h1:80', 'http://h2:81']}
        bus = MagicMock()
        pw = roundhouse.PeerWatch(declared, lock, bus, fleet=fleet)

        rows = pw.rows_unlocked()
        fleet_row = [r for r in rows if r['name'] == 'fleet'][0]
        self.assertIn('endpoints', fleet_row)
        self.assertEqual(len(fleet_row['endpoints']), 2)

    def test_tcp_rows_have_no_endpoints_list(self):
        """TCP peer rows do NOT include 'endpoints' list (§4.3)."""
        lock = threading.Lock()
        declared = {'tcp': [('host', 8080)]}
        bus = MagicMock()
        pw = roundhouse.PeerWatch(declared, lock, bus)

        rows = pw.rows_unlocked()
        tcp_row = rows[0]
        self.assertNotIn('endpoints', tcp_row)

    def test_endpoint_changed_at_set_on_endpoint_change(self):
        """endpoint_changed_at is set when endpoint changes (§4.3)."""
        lock = threading.Lock()
        declared = {'fleet': [('h1', 80), ('h2', 81)]}
        fleet = {'fleet': ['http://h1:80', 'http://h2:81']}
        bus = MagicMock()
        pw = roundhouse.PeerWatch(declared, lock, bus, fleet=fleet)

        ts = time.time()
        payload = pw.apply_result_unlocked('fleet', ok=True, error=None, ts=ts, winner_index=1)
        self.assertIsNotNone(payload)
        self.assertEqual(pw.peers['fleet']['endpoint_index'], 1)
        self.assertIsNotNone(pw.peers['fleet']['endpoint_changed_at'])
        self.assertGreaterEqual(pw.peers['fleet']['endpoint_changed_at'], ts)


class _LiveServeHarness(unittest.TestCase):
    """Runs a real `roundhouse.py --serve` and talks to it over TCP (§7.4).

    The optional-bind story is made of real sockets: 127.0.0.0/8 binds without
    configuration, so 127.0.0.2 proves a genuine optional listener and 192.0.2.1
    (TEST-NET-1, unassigned) proves genuine absence — appearance itself needs the
    root container drill (recon 8).
    """

    def free_port(self):
        s = socket.socket()
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def serve(self, extra_args, port=None):
        import subprocess
        port = port or self.free_port()
        repo = Path(__file__).resolve().parents[2]
        proc = subprocess.Popen(
            [sys.executable, str(repo / 'mvp1' / 'roundhouse.py'), '--serve',
             '--unit-dir', str(repo / 'docs' / 'fixtures'), '--no-db',
             '--port', str(port)] + extra_args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self._reap, proc)
        return proc, port

    @staticmethod
    def _reap(proc):
        import subprocess
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()

    def get(self, host, port, path='/api/units', timeout=1.0):
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            conn.request('GET', path)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def wait_for(self, host, port, proc, path='/api/units', deadline=25.0):
        end = time.time() + deadline
        while time.time() < end:
            if proc.poll() is not None:
                out, err = proc.communicate()
                self.fail(f'server exited early: rc={proc.returncode} err={err[-800:]}')
            try:
                status, body = self.get(host, port, path)
                if status == 200:
                    return body
            except (OSError, http.client.HTTPException):
                time.sleep(0.2)
        self.fail(f'nothing answered on {host}:{port}{path}')

    def snapshot(self, port, path='/api/units'):   # /api/units IS the snapshot (§5.1)
        status, body = self.get('127.0.0.1', port, path)
        self.assertEqual(status, 200)
        return json.loads(body)


class TestOptionalBindsServeForReal(_LiveServeHarness):
    """§7.4 T1 integration: optional doors on real sockets, no root."""

    def listeners(self, port):
        snap = self.snapshot(port)
        self.assertIn('listeners', snap, 'the snapshot is the whole listeners surface (§5.1)')
        return {row['addr']: row for row in snap['listeners']}

    def test_an_absent_optional_address_never_stops_the_start(self):
        """The milestone in one line: the laptop starts on the sofa."""
        proc, port = self.serve(['--bind', '127.0.0.1',
                                 '--bind-optional', '127.0.0.2,192.0.2.1',
                                 '--bind-retry', '1'])
        self.wait_for('127.0.0.1', port, proc)

        # both real doors answer; the absent one is simply not there
        self.assertEqual(self.get('127.0.0.2', port)[0], 200)
        with self.assertRaises(OSError):
            self.get('192.0.2.1', port, timeout=0.5)

        rows = self.listeners(port)
        self.assertEqual(rows['127.0.0.1']['kind'], 'mandatory')
        self.assertEqual(rows['127.0.0.1']['state'], 'bound')
        self.assertEqual(rows['127.0.0.2']['kind'], 'optional')
        self.assertEqual(rows['127.0.0.2']['state'], 'bound')
        self.assertEqual(rows['192.0.2.1']['state'], 'absent')
        self.assertIsNone(rows['192.0.2.1']['last_error'],
                          'EADDRNOTAVAIL is absence, not an error (L2)')

    def test_the_listener_row_shape_is_frozen(self):
        """L4: {addr, port, kind, state, since, last_error, resolved} — no more, no less."""
        proc, port = self.serve(['--bind', '127.0.0.1', '--bind-optional', '127.0.0.2',
                                 '--bind-retry', '1'])
        self.wait_for('127.0.0.1', port, proc)

        snap = self.snapshot(port)
        expected = {'addr', 'port', 'kind', 'state', 'since', 'last_error', 'resolved'}
        for row in snap['listeners']:
            self.assertEqual(set(row.keys()), expected, f'row shape drifted: {row}')
            self.assertEqual(row['port'], port)
        self.assertIsNone(snap['listeners'][0]['resolved'],
                          'literal and mandatory rows carry no resolved list')

    def test_an_occupied_optional_address_is_an_error_row_and_serving_continues(self):
        """L2: EADDRINUSE is reported per address and never kills the process."""
        port = self.free_port()
        blocker = socket.socket()
        blocker.bind(('127.0.0.3', port))
        blocker.listen(1)
        self.addCleanup(blocker.close)

        proc, _ = self.serve(['--bind', '127.0.0.1', '--bind-optional', '127.0.0.3',
                              '--bind-retry', '1'], port=port)
        self.wait_for('127.0.0.1', port, proc)

        deadline, row = time.time() + 10, None
        while time.time() < deadline:
            row = self.listeners(port).get('127.0.0.3')
            if row and row['state'] == 'error':
                break
            time.sleep(0.5)
        self.assertEqual(row['state'], 'error')
        self.assertIn('Errno', row['last_error'])
        self.assertIsNone(proc.poll(), 'an occupied optional address is not fatal')
        self.assertEqual(self.get('127.0.0.1', port)[0], 200)

    def test_shutdown_closes_the_optional_door_too(self):
        """§3.4: the optional listener is torn down with the mandatory ones."""
        import signal
        proc, port = self.serve(['--bind', '127.0.0.1', '--bind-optional', '127.0.0.2',
                                 '--bind-retry', '1'])
        self.wait_for('127.0.0.1', port, proc)
        self.assertEqual(self.get('127.0.0.2', port)[0], 200)

        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 0)

        for host in ('127.0.0.1', '127.0.0.2'):
            with self.assertRaises(OSError, msg=f'{host} still answers after shutdown'):
                self.get(host, port, timeout=1.0)

    def test_the_watch_is_joined_before_the_doors_are_closed(self):
        """§3.4's load-bearing order, asserted on the source: no listener can be
        added after the close pass because the only thread that adds them is gone.
        Instrumented structurally because the race it prevents is unobservable when
        the code is right and intermittent when it is not."""
        import ast
        source = (Path(__file__).resolve().parents[1] / 'roundhouse.py').read_text()
        tree = ast.parse(source)
        serve = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == 'cmd_serve')

        join_lines = [n.lineno for n in ast.walk(serve)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                      and n.func.attr == 'join'
                      and isinstance(n.func.value, ast.Name)
                      and n.func.value.id == 'bind_watch_thread']
        close_lines = [n.lineno for n in ast.walk(serve)
                       if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                       and n.func.attr == 'server_close']

        self.assertEqual(len(join_lines), 1, 'the bind watch is joined exactly once')
        shutdown_closes = [ln for ln in close_lines if ln > join_lines[0]]
        self.assertTrue(shutdown_closes, 'the close pass must follow the join')
        self.assertTrue(all(ln > join_lines[0] for ln in shutdown_closes))


class TestFleetCandidatesDoNotDisturbTheServer(_LiveServeHarness):
    """§4.1/L7 wiring: per-candidate D2 runs, and it costs the server nothing."""

    def test_candidates_do_not_move_the_listening_port(self):
        """The per-candidate D2 loop unpacks (host, port, url) — over cmd_serve's own
        `port`. A peer declaration must not be able to decide which port this host
        serves on; the candidate's port is data about somewhere else."""
        proc, port = self.serve(['--bind', '127.0.0.1', '--peer-interval', '3600',
                                 '--fleet-peer',
                                 'ampere=http://ampere.fritz.box:9101,http://ampere.fritz.box:9102'])
        self.wait_for('127.0.0.1', port, proc)

        snap = self.snapshot(port)
        self.assertEqual(snap['self_port'], port)
        self.assertEqual(self.listener_ports(snap), {port})

    @staticmethod
    def listener_ports(snap):
        return {row['port'] for row in snap['listeners']}

    def test_a_candidate_that_targets_a_managed_unit_is_refused_by_name(self):
        """L7: every candidate is a full D2 citizen and the refusal says which one."""
        import subprocess
        port = self.free_port()
        repo = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            [sys.executable, str(repo / 'mvp1' / 'roundhouse.py'), '--serve',
             '--unit-dir', str(repo / 'docs' / 'fixtures'), '--no-db',
             '--port', str(port), '--bind', '127.0.0.1',
             '--fleet-peer', 'bee=http://127.0.0.1:9109,http://127.0.0.1:8085'],
            capture_output=True, text=True, timeout=30)

        self.assertEqual(result.returncode, 1)
        self.assertIn("peer 'bee[2]' targets 127.0.0.1:8085", result.stderr)
        self.assertIn('qwen3.6-coding', result.stderr)


if __name__ == '__main__':
    unittest.main()
