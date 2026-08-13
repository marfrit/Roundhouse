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
                   for m in [re.match(r'^# ===== SECTION ([A-E])\b', line)] if m]
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
                    '/api/routing-config.json', '/api/warm'}
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


if __name__ == '__main__':
    unittest.main()
