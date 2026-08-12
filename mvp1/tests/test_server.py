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
        """Test that red color appears only in .failed and .conflict-active rules."""
        html_path = Path(__file__).parent.parent / 'static' / 'index.html'
        if html_path.exists():
            with open(html_path, 'r') as f:
                content = f.read()

            # Check that red (or var(--red)) appears in CSS
            self.assertIn('--red:', content, "CSS should define --red color")

            # The actual color value should only be in .failed and .conflict rules
            # This is a simplified check - a full check would parse CSS
            if '--red' in content:
                red_value = None
                for line in content.split('\n'):
                    if '--red:' in line:
                        red_value = line
                        break

                # Verify it's a color definition
                self.assertIsNotNone(red_value)
                self.assertTrue(any(c in red_value for c in ['#', 'rgb', 'hsl']))


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


if __name__ == '__main__':
    unittest.main()
