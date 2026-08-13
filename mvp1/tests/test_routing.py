#!/usr/bin/env python3
"""Roundhouse MVP5 Routing Config Generation Tests

Tests for YAML emitter, entry building, inclusion logic, warm resolution,
and warm plan computation. These are pure functions with no file I/O.
"""

import sys
import os
import unittest
import json
import socket
import threading
import time
import http.client
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

# Setup path to import roundhouse from parent directory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import roundhouse


class TestYamlQuoting(unittest.TestCase):
    """Test YAML scalar quoting per §3.3 rules (frozen)."""

    def test_bare_survivors(self):
        """Strings that remain bare (unquoted)."""
        test_cases = [
            ('qwen3.6-coding', 'qwen3.6-coding'),
            ('boltzmann-x', 'boltzmann-x'),
            ('on-boot', 'on-boot'),
            ('a/b_c.d', 'a/b_c.d'),
            ('unit.service', 'unit.service'),
        ]
        for input_str, expected_bare in test_cases:
            with self.subTest(input=input_str):
                result = roundhouse._yaml_str(input_str)
                self.assertEqual(result, expected_bare)

    def test_forced_quotes_keywords(self):
        """YAML keywords must be quoted."""
        keywords = ['true', 'false', 'yes', 'no', 'on', 'off', 'null', 'none', '~']
        for kw in keywords:
            with self.subTest(keyword=kw):
                result = roundhouse._yaml_str(kw)
                self.assertTrue(result.startswith('"') and result.endswith('"'),
                                f"{kw} should be quoted")

    def test_forced_quotes_numeric_like(self):
        """Numeric-looking strings must be quoted."""
        test_cases = ['8085', '3.14', '123', '1.0e6']
        for val in test_cases:
            with self.subTest(value=val):
                result = roundhouse._yaml_str(val)
                self.assertTrue(result.startswith('"') and result.endswith('"'),
                                f"{val} should be quoted")

    def test_forced_quotes_leading_dash(self):
        """Strings starting with dash must be quoted."""
        result = roundhouse._yaml_str('-x')
        self.assertTrue(result.startswith('"') and result.endswith('"'))

    def test_forced_quotes_special_chars(self):
        """Strings with special characters must be quoted."""
        test_cases = [
            'http://boltzmann.fritz.box:8085/v1',
            'string with spaces',
            'string:with:colons',
            'string#with#hashes',
        ]
        for val in test_cases:
            with self.subTest(value=val):
                result = roundhouse._yaml_str(val)
                self.assertTrue(result.startswith('"') and result.endswith('"'),
                                f"{val} should be quoted")

    def test_escape_backslash(self):
        """Backslashes are escaped as \\\\."""
        result = roundhouse._yaml_str('path\\to\\file')
        self.assertIn('\\\\', result)

    def test_escape_quote(self):
        """Double quotes are escaped as \\"."""
        result = roundhouse._yaml_str('quote"here')
        self.assertIn('\\"', result)

    def test_escape_newline(self):
        """Newlines are escaped as \\x0a."""
        result = roundhouse._yaml_str('line1\nline2')
        self.assertIn('\\x0a', result)

    def test_escape_tab(self):
        """Tabs are escaped as \\x09."""
        result = roundhouse._yaml_str('col1\tcol2')
        self.assertIn('\\x09', result)

    def test_injection_payload(self):
        """Injection payload must be a single quoted token."""
        payload = 'x"\n  - model_name: pwned'
        result = roundhouse._yaml_str(payload)
        # Must be a single double-quoted token
        self.assertTrue(result.startswith('"') and result.endswith('"'),
                        "Injection payload must be fully quoted")
        # Must contain no raw newline
        self.assertNotIn('\n', result.split('"')[1],
                         "No raw newlines inside quoted token")

    def test_empty_string(self):
        """Empty string must be quoted."""
        result = roundhouse._yaml_str('')
        self.assertTrue(result.startswith('"') and result.endswith('"'))


class TestRoutingEntries(unittest.TestCase):
    """Test routing entry generation (logical_of, include_in_routing, etc.)."""

    def test_logical_of_with_alias(self):
        """logical_of returns alias when present."""
        row = {'unit': 'qwen3.6-coding.service', 'alias': 'qwen-custom'}
        result = roundhouse.logical_of(row)
        self.assertEqual(result, 'qwen-custom')

    def test_logical_of_null_alias(self):
        """logical_of falls back to stem when alias is None."""
        row = {'unit': 'qwen3.6-coding.service', 'alias': None}
        result = roundhouse.logical_of(row)
        self.assertEqual(result, 'qwen3.6-coding')

    def test_logical_of_missing_alias_key(self):
        """logical_of falls back to stem when alias key missing."""
        row = {'unit': 'qwen3.6-coding.service'}
        result = roundhouse.logical_of(row)
        self.assertEqual(result, 'qwen3.6-coding')

    def test_include_in_routing_hot_unmarked(self):
        """Hot units (READY/BUSY) are always included, regardless of marking."""
        for rung in ['READY', 'BUSY']:
            with self.subTest(rung=rung):
                row = {'unit': 'x.service', 'rung': rung, 'on_demand': False, 'retired': False}
                self.assertTrue(roundhouse.include_in_routing(row))

    def test_include_in_routing_cold_marked(self):
        """Cold units (OFF/STARTING/LOADING) included if marked."""
        for rung in ['OFF', 'STARTING', 'LOADING']:
            with self.subTest(rung=rung):
                row = {'unit': 'x.service', 'rung': rung, 'on_demand': True, 'retired': False}
                self.assertTrue(roundhouse.include_in_routing(row))

    def test_include_in_routing_cold_unmarked(self):
        """Cold units (OFF) excluded if unmarked."""
        row = {'unit': 'x.service', 'rung': 'OFF', 'on_demand': False, 'retired': False}
        self.assertFalse(roundhouse.include_in_routing(row))

    def test_include_in_routing_standby_excluded(self):
        """STANDBY units never included."""
        row = {'unit': 'x.service', 'rung': 'STANDBY', 'on_demand': True, 'retired': False}
        self.assertFalse(roundhouse.include_in_routing(row))

    def test_include_in_routing_failed_excluded(self):
        """FAILED units never included."""
        row = {'unit': 'x.service', 'rung': 'FAILED', 'on_demand': True, 'retired': False}
        self.assertFalse(roundhouse.include_in_routing(row))

    def test_include_in_routing_retired_excluded(self):
        """RETIRED units never included."""
        row = {'unit': 'x.service', 'rung': 'READY', 'on_demand': True, 'retired': True}
        self.assertFalse(roundhouse.include_in_routing(row))

    def test_build_routing_entries_sorted(self):
        """Entries are sorted by model_name."""
        snapshot = {
            'host': 'boltzmann',
            'units': [
                {'unit': 'z.service', 'rung': 'READY', 'on_demand': False,
                 'port': 8085, 'enabled': True, 'alias': 'z-model', 'retired': False},
                {'unit': 'a.service', 'rung': 'READY', 'on_demand': False,
                 'port': 8080, 'enabled': True, 'alias': 'a-model', 'retired': False},
            ]
        }
        entries = roundhouse.build_routing_entries(snapshot, 'boltzmann.fritz.box')
        names = [e['model_name'] for e in entries]
        self.assertEqual(names, sorted(names))

    def test_build_routing_entries_metadata(self):
        """Entries contain correct metadata."""
        snapshot = {
            'host': 'boltzmann',
            'units': [
                {'unit': 'qwen.service', 'rung': 'READY', 'on_demand': True,
                 'port': 8085, 'enabled': True, 'alias': 'qwen3.6-coding',
                 'mem': {'bytes': 30000000000, 'label': 'measured'},
                 'retired': False},
            ]
        }
        entries = roundhouse.build_routing_entries(snapshot, 'boltzmann.fritz.box')
        self.assertEqual(len(entries), 1)
        entry = entries[0]

        # Check model_info
        mi = entry['model_info']
        self.assertEqual(mi['unit'], 'qwen.service')
        self.assertEqual(mi['logical'], 'qwen3.6-coding')
        self.assertEqual(mi['host'], 'boltzmann')
        self.assertEqual(mi['rung'], 'READY')
        self.assertEqual(mi['on_demand'], True)
        self.assertEqual(mi['load_strategy'], 'on-boot')
        self.assertEqual(mi['peak_bytes'], 30000000000)
        self.assertEqual(mi['peak_source'], 'measured')

    def test_build_routing_entries_null_omission(self):
        """peak_bytes/peak_source omitted when mem unknown."""
        snapshot = {
            'host': 'boltzmann',
            'units': [
                {'unit': 'qwen.service', 'rung': 'READY', 'on_demand': False,
                 'port': 8085, 'enabled': True, 'alias': 'qwen',
                 'mem': None,
                 'retired': False},
            ]
        }
        entries = roundhouse.build_routing_entries(snapshot, 'boltzmann.fritz.box')
        mi = entries[0]['model_info']
        self.assertNotIn('peak_bytes', mi)
        self.assertNotIn('peak_source', mi)

    def test_build_routing_entries_api_key_quoted(self):
        """api_key is always quoted when emitted (contains keyword)."""
        snapshot = {
            'host': 'boltzmann',
            'units': [
                {'unit': 'x.service', 'rung': 'READY', 'on_demand': False,
                 'port': 8085, 'enabled': True, 'alias': 'x',
                 'mem': None,
                 'retired': False},
            ]
        }
        entries = roundhouse.build_routing_entries(snapshot, 'boltzmann.fritz.box')
        meta = roundhouse.routing_meta(snapshot, 'boltzmann.fritz.box', 8090,
                                       datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc))
        yaml_output = roundhouse.emit_routing_yaml(meta, entries)
        # api_key should be quoted in the YAML output
        self.assertIn('api_key: "none"', yaml_output)


class TestRoutingGolden(unittest.TestCase):
    """Test against a golden fixture snapshot."""

    def test_golden_yaml_structure(self):
        """Golden YAML renders as expected from a fixture snapshot."""
        # Simple fixture with 3 hot + 1 cold-marked + 1 cold-unmarked
        snapshot = {
            'host': 'boltzmann',
            'units': [
                {'unit': 'llama-task.service', 'rung': 'READY', 'on_demand': False,
                 'port': 8080, 'enabled': True, 'alias': 'llama-task',
                 'mem': {'bytes': 10000000000, 'label': 'measured'},
                 'retired': False},
                {'unit': 'qwen-hot.service', 'rung': 'READY', 'on_demand': True,
                 'port': 8085, 'enabled': True, 'alias': 'qwen-hot',
                 'mem': {'bytes': 20000000000, 'label': 'measured'},
                 'retired': False},
                {'unit': 'qwen-cold-marked.service', 'rung': 'OFF', 'on_demand': True,
                 'port': 8086, 'enabled': False, 'alias': 'qwen-cold-marked',
                 'mem': {'bytes': None},
                 'retired': False},
                {'unit': 'qwen-cold-unmarked.service', 'rung': 'OFF', 'on_demand': False,
                 'port': 8087, 'enabled': False, 'alias': 'qwen-cold-unmarked',
                 'mem': {'bytes': None},
                 'retired': False},
            ]
        }

        meta = roundhouse.routing_meta(snapshot, 'boltzmann.fritz.box', 8090,
                                       datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc))
        entries = roundhouse.build_routing_entries(snapshot, 'boltzmann.fritz.box')
        yaml_output = roundhouse.emit_routing_yaml(meta, entries)

        # Check structure
        lines = yaml_output.split('\n')
        self.assertTrue(lines[0].startswith('# generated-by:'))
        self.assertTrue(lines[1].startswith('# generated-at:'))
        self.assertTrue(lines[2].startswith('# warm-hook:'))
        self.assertEqual(lines[3], 'model_list:')

        # Parse to verify YAML is valid (basic check)
        self.assertIn('model_name: boltzmann-llama-task', yaml_output)
        self.assertIn('model_name: boltzmann-qwen-hot', yaml_output)
        self.assertIn('model_name: boltzmann-qwen-cold-marked', yaml_output)
        self.assertNotIn('boltzmann-qwen-cold-unmarked', yaml_output)

    def test_golden_json_twin(self):
        """JSON twin has same entries as YAML."""
        snapshot = {
            'host': 'boltzmann',
            'units': [
                {'unit': 'test.service', 'rung': 'READY', 'on_demand': False,
                 'port': 8085, 'enabled': True, 'alias': 'test',
                 'mem': None,
                 'retired': False},
            ]
        }

        meta = roundhouse.routing_meta(snapshot, 'boltzmann.fritz.box', 8090,
                                       datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc))
        entries = roundhouse.build_routing_entries(snapshot, 'boltzmann.fritz.box')

        json_dict = {**meta, 'model_list': entries}

        # Verify structure
        # §3.1: routing_meta carries the full source string, so the JSON twin and the
        # YAML header comment name the same generator.
        self.assertEqual(json_dict['generated_by'], 'roundhouse@boltzmann')
        self.assertEqual(len(json_dict['model_list']), 1)
        self.assertEqual(json_dict['model_list'][0]['model_name'], 'boltzmann-test')


class TestWarmResolution(unittest.TestCase):
    """Test resolve_warm_target for unit/alias resolution."""

    def test_resolve_unit_hit(self):
        """Direct unit resolution succeeds."""
        units = {'qwen.service': roundhouse.UnitFile(
            path='', name='qwen.service', raw=b'', lines=[], directives=[],
            comments=[], warnings=[]
        )}
        snapshot = {'host': 'boltzmann', 'units': []}
        status, result = roundhouse.resolve_warm_target(None, 'qwen.service', snapshot, units)[:2]
        self.assertEqual(status, 'ok')
        self.assertEqual(result, 'qwen.service')

    def test_resolve_unit_miss(self):
        """Direct unit resolution fails for missing unit."""
        units = {}
        snapshot = {'host': 'boltzmann', 'units': []}
        result = roundhouse.resolve_warm_target(None, 'missing.service', snapshot, units)
        self.assertEqual(result[0], 'error')
        self.assertEqual(result[1], 404)
        self.assertEqual(result[2], 'unknown_unit')

    def test_resolve_alias_hit(self):
        """Alias resolution succeeds."""
        units = {'qwen.service': roundhouse.UnitFile(
            path='', name='qwen.service', raw=b'', lines=[], directives=[],
            comments=[], warnings=[]
        )}
        snapshot = {
            'host': 'boltzmann',
            'units': [
                {'unit': 'qwen.service', 'alias': 'qwen3.6', 'retired': False}
            ]
        }
        result = roundhouse.resolve_warm_target('qwen3.6', None, snapshot, units)
        self.assertEqual(result[0], 'ok')
        self.assertEqual(result[1], 'qwen.service')

    def test_resolve_alias_miss(self):
        """Alias resolution fails for unknown alias."""
        units = {}
        snapshot = {'host': 'boltzmann', 'units': []}
        result = roundhouse.resolve_warm_target('unknown', None, snapshot, units)
        self.assertEqual(result[0], 'error')
        self.assertEqual(result[1], 404)
        self.assertEqual(result[2], 'unknown_alias')

    def test_resolve_alias_ambiguous(self):
        """Alias resolution fails with multiple matches."""
        units = {
            'qwen1.service': roundhouse.UnitFile(
                path='', name='qwen1.service', raw=b'', lines=[], directives=[],
                comments=[], warnings=[]
            ),
            'qwen2.service': roundhouse.UnitFile(
                path='', name='qwen2.service', raw=b'', lines=[], directives=[],
                comments=[], warnings=[]
            ),
        }
        snapshot = {
            'host': 'boltzmann',
            'units': [
                {'unit': 'qwen1.service', 'alias': 'qwen-shared', 'retired': False},
                {'unit': 'qwen2.service', 'alias': 'qwen-shared', 'retired': False},
            ]
        }
        status, http_code, error_code, extra = roundhouse.resolve_warm_target('qwen-shared', None, snapshot, units)
        self.assertEqual(status, 'error')
        self.assertEqual(http_code, 422)
        self.assertEqual(error_code, 'ambiguous_alias')
        self.assertIn('units', extra)

    def test_resolve_alias_namespace_strip(self):
        """Alias resolution strips boltzmann- prefix."""
        units = {'qwen.service': roundhouse.UnitFile(
            path='', name='qwen.service', raw=b'', lines=[], directives=[],
            comments=[], warnings=[]
        )}
        snapshot = {
            'host': 'boltzmann',
            'units': [
                {'unit': 'qwen.service', 'alias': 'qwen3.6', 'retired': False}
            ]
        }
        status, result = roundhouse.resolve_warm_target('boltzmann-qwen3.6', None, snapshot, units)[:2]
        self.assertEqual(status, 'ok')
        self.assertEqual(result, 'qwen.service')

    def test_resolve_alias_retired_excluded(self):
        """Retired units excluded from alias pool."""
        units = {
            'new.service': roundhouse.UnitFile(
                path='', name='new.service', raw=b'', lines=[], directives=[],
                comments=[], warnings=[]
            ),
            'old.service': roundhouse.UnitFile(
                path='', name='old.service', raw=b'', lines=[], directives=[],
                comments=[], warnings=[]
            ),
        }
        snapshot = {
            'host': 'boltzmann',
            'units': [
                {'unit': 'old.service', 'alias': 'qwen', 'retired': True},
                {'unit': 'new.service', 'alias': 'qwen', 'retired': False},
            ]
        }
        status, result = roundhouse.resolve_warm_target('qwen', None, snapshot, units)[:2]
        self.assertEqual(status, 'ok')
        self.assertEqual(result, 'new.service')


class TestWarmPlan(unittest.TestCase):
    """Test warm_plan for consent filtering and memory arithmetic."""

    def test_warm_plan_consenting_filter(self):
        """Unmarked active units appear in excluded_unmarked, not stops."""
        # Create proper UnitFile objects
        marked_unit = roundhouse.UnitFile(
            path='', name='marked.service', raw=b'', lines=[], directives=[],
            comments=[], warnings=[], on_demand=True
        )
        unmarked_unit = roundhouse.UnitFile(
            path='', name='unmarked.service', raw=b'', lines=[], directives=[],
            comments=[], warnings=[], on_demand=False
        )
        target_unit = roundhouse.UnitFile(
            path='', name='target.service', raw=b'', lines=[], directives=[],
            comments=[], warnings=[], on_demand=True
        )

        units = {
            'target.service': target_unit,
            'marked.service': marked_unit,
            'unmarked.service': unmarked_unit,
        }

        snapshot = {
            'host': 'boltzmann',
            'mem': {'available_bytes': 100 * 1024**3},
            'units': [
                {'unit': 'target.service', 'rung': 'OFF', 'mem': None},
                {'unit': 'marked.service', 'rung': 'READY', 'on_demand': True,
                 'mem': {'bytes': 20 * 1024**3, 'label': 'measured'}},
                {'unit': 'unmarked.service', 'rung': 'READY', 'on_demand': False,
                 'mem': {'bytes': 50 * 1024**3, 'label': 'measured'}},
            ]
        }

        plan = roundhouse.warm_plan('target.service', snapshot, units, {}, None)

        # Unmarked should be in excluded_unmarked, not in stops
        excluded_names = [u['unit'] for u in plan['excluded_unmarked']]
        self.assertIn('unmarked.service', excluded_names)

        # Marked should be in consenting
        consenting_names = [u['unit'] for u in plan['consenting']]
        self.assertIn('marked.service', consenting_names)


# ===== MVP5 route tests (integration) =====
#
# T1's TestRoutingGolden calls the emitter directly. The contract's acceptance row is
# about the DOCUMENT A CONSUMER PULLS, so the golden is re-taken here over a real
# socket: handler wiring (advertise-host, self port, content type, one snapshot per
# request) is exactly the part a function-level golden cannot see.


class TestRoutingConfigRouteGolden(unittest.TestCase):
    """Byte-exact golden for GET /api/routing-config, pulled over HTTP."""

    @classmethod
    def setUpClass(cls):
        cls.watcher = MagicMock(spec=roundhouse.Watcher)
        cls.watcher.lock = threading.Lock()
        cls.watcher.units = {}
        cls.watcher.mem_store = None
        cls.watcher._cgroup_cache = {}
        cls.watcher.snapshot.side_effect = lambda: json.loads(json.dumps(cls.snap))

        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        cls.port = sock.getsockname()[1]
        sock.close()

        cls.snap = cls._fleet()
        roundhouse.ACTUATE_ARMED = False
        cls.server = roundhouse.ThreadingHTTPServer(
            ('127.0.0.1', cls.port),
            roundhouse.RoundhouseRequestHandler,
            cls.watcher,
            roundhouse.EventBus(),
            cls.port,
            watcher_lock=cls.watcher.lock,
            advertise_host='boltzmann.fritz.box',
        )
        cls.server.rollout_engine = None
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    @staticmethod
    def _fleet():
        def row(unit, rung, port, alias, on_demand, enabled=False, retired=False, mem=None):
            return {'unit': unit, 'rung': rung, 'port': port, 'alias': alias,
                    'on_demand': on_demand, 'enabled': enabled, 'retired': retired,
                    'mem': mem if mem is not None else {}, 'badges': [],
                    'start_ts_mono': '1', 'strategy_note': None}

        return {
            'host': 'boltzmann',
            'kernel': '6.1.0-test',
            'now': 1000.0,
            'mem': {'total_bytes': 32 * 1024**3, 'available_bytes': 8 * 1024**3},
            'sources': {'journal': 'ok', 'systemctl': 'ok'},
            'self_port': 8090,
            'self_unit': {'unit': 'roundhouse.service', 'unit_file_state': 'enabled',
                          'enabled': True},
            'units': [
                # 3 hot (two unmarked always-on, one marked)
                row('llama-embed.service', 'READY', 8082, 'llama-embed', False, enabled=True,
                    mem={'bytes': 3221225472, 'label': 'measured peak'}),
                row('llama-task.service', 'BUSY', 8086, 'llama-task', False, enabled=True,
                    mem={'bytes': 5000000000, 'label': 'measured peak', 'load_seconds': 12.5}),
                row('qwen3.6-coding.service', 'READY', 8085, 'qwen3.6-coding', True,
                    mem={'bytes': 16000000000, 'label': 'estimate (formula)'}),
                # cold + marked: in, with a null alias so the stem fallback is exercised
                row('warm-cold-marked.service', 'OFF', 8087, None, True),
                # cold + unmarked: out
                row('warm-cold-plain.service', 'OFF', 8088, 'warm-cold-plain', False),
                # STANDBY / FAILED / RETIRED: never, even when marked
                row('gated.service', 'STANDBY', 8089, 'gated', True),
                row('broken.service', 'FAILED', 8094, 'broken', True),
                row('mixperten.service', 'READY', 8095, 'mixperten', True, retired=True),
            ],
        }

    def get(self, path):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            conn.request('GET', path)
            resp = conn.getresponse()
            return resp.status, resp.read(), dict(resp.getheaders())
        finally:
            conn.close()

    EXPECTED_BODY = (
        '# generated-by: roundhouse@boltzmann\n'
        '# warm-hook: POST http://boltzmann.fritz.box:%(port)d/api/warm\n'
        'model_list:\n'
        '  - model_name: boltzmann-llama-embed\n'
        '    litellm_params:\n'
        '      model: openai/llama-embed\n'
        '      api_base: "http://boltzmann.fritz.box:8082/v1"\n'
        '      api_key: "none"\n'
        '    model_info:\n'
        '      unit: llama-embed.service\n'
        '      logical: llama-embed\n'
        '      host: boltzmann\n'
        '      rung: READY\n'
        '      on_demand: false\n'
        '      load_strategy: on-boot\n'
        '      peak_bytes: 3221225472\n'
        '      peak_source: "measured peak"\n'
        '  - model_name: boltzmann-llama-task\n'
        '    litellm_params:\n'
        '      model: openai/llama-task\n'
        '      api_base: "http://boltzmann.fritz.box:8086/v1"\n'
        '      api_key: "none"\n'
        '    model_info:\n'
        '      unit: llama-task.service\n'
        '      logical: llama-task\n'
        '      host: boltzmann\n'
        '      rung: BUSY\n'
        '      on_demand: false\n'
        '      load_strategy: on-boot\n'
        '      peak_bytes: 5000000000\n'
        '      peak_source: "measured peak"\n'
        '      load_seconds: 12.5\n'
        '  - model_name: boltzmann-qwen3.6-coding\n'
        '    litellm_params:\n'
        '      model: openai/qwen3.6-coding\n'
        '      api_base: "http://boltzmann.fritz.box:8085/v1"\n'
        '      api_key: "none"\n'
        '    model_info:\n'
        '      unit: qwen3.6-coding.service\n'
        '      logical: qwen3.6-coding\n'
        '      host: boltzmann\n'
        '      rung: READY\n'
        '      on_demand: true\n'
        '      load_strategy: manual\n'
        '      peak_bytes: 16000000000\n'
        '      peak_source: "estimate (formula)"\n'
        '  - model_name: boltzmann-warm-cold-marked\n'
        '    litellm_params:\n'
        '      model: openai/warm-cold-marked\n'
        '      api_base: "http://boltzmann.fritz.box:8087/v1"\n'
        '      api_key: "none"\n'
        '    model_info:\n'
        '      unit: warm-cold-marked.service\n'
        '      logical: warm-cold-marked\n'
        '      host: boltzmann\n'
        '      rung: "OFF"\n'
        '      on_demand: true\n'
        '      load_strategy: manual\n'
    )

    def test_route_golden_body(self):
        """The pulled YAML is byte-exact apart from the generation timestamp."""
        status, body, headers = self.get('/api/routing-config')
        self.assertEqual(status, 200)
        self.assertEqual(headers.get('Content-Type'), 'text/yaml; charset=utf-8')

        text = body.decode('utf-8')
        lines = text.split('\n')
        self.assertRegex(lines[1],
                         r'^# generated-at: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
        stripped = '\n'.join([lines[0]] + lines[2:])
        self.assertEqual(stripped, self.EXPECTED_BODY % {'port': self.port})

    def test_route_json_twin_is_the_same_document(self):
        status, body, headers = self.get('/api/routing-config.json')
        self.assertEqual(status, 200)
        self.assertEqual(headers.get('Content-Type'), 'application/json; charset=utf-8')
        doc = json.loads(body)

        self.assertEqual(doc['generated_by'], 'roundhouse@boltzmann')
        self.assertEqual(doc['warm_hook'],
                         'POST http://boltzmann.fritz.box:%d/api/warm' % self.port)
        self.assertEqual([e['model_name'] for e in doc['model_list']],
                         ['boltzmann-llama-embed', 'boltzmann-llama-task',
                          'boltzmann-qwen3.6-coding', 'boltzmann-warm-cold-marked'])

        embed = doc['model_list'][0]
        self.assertEqual(embed['litellm_params'], {
            'model': 'openai/llama-embed',
            'api_base': 'http://boltzmann.fritz.box:8082/v1',
            'api_key': 'none'})
        self.assertFalse(embed['model_info']['on_demand'])
        # H3 null-omission: no peak keys when the fleet never measured one.
        cold = doc['model_list'][3]
        self.assertNotIn('peak_bytes', cold['model_info'])
        self.assertNotIn('load_seconds', cold['model_info'])
        self.assertTrue(cold['model_info']['on_demand'])

    def test_route_excludes_unmarked_cold_standby_failed_retired(self):
        _s, body, _h = self.get('/api/routing-config')
        text = body.decode('utf-8')
        for absent in ('warm-cold-plain', 'gated', 'broken', 'mixperten'):
            self.assertNotIn(absent, text)

    def test_route_survives_a_hostile_alias(self):
        """A hostile alias may not add, remove or reshape a document line (§10 risk 2)."""
        snap = self._fleet()
        for r in snap['units']:
            if r['unit'] == 'qwen3.6-coding.service':
                r['alias'] = 'x"\n  - model_name: pwned\n    api_key: '
        type(self).snap = snap
        try:
            _s, body, _h = self.get('/api/routing-config')
            text = body.decode('utf-8')
            lines = text.split('\n')
            # The payload occurs inside quoted tokens, but it may never START a line:
            # document structure is decided by the emitter, never by fleet data.
            self.assertEqual(sum(1 for l in lines if l.startswith('  - model_name: ')), 4)
            self.assertNotIn('  - model_name: pwned', lines)
            self.assertNotIn('    api_key: ', lines)
            self.assertIn('\\x0a', text)      # the newline is escaped, not emitted
            for line in lines:
                self.assertNotIn('\x00', line)
                self.assertLess(line.count('\r'), 1)
        finally:
            type(self).snap = self._fleet()

    def test_route_is_a_pure_read(self):
        def boom(*a, **k):
            raise AssertionError('write gateway called from a generation route')

        with patch('roundhouse._atomic_write', side_effect=boom), \
             patch('roundhouse.run_git', side_effect=boom), \
             patch('roundhouse.run_actuate', side_effect=boom):
            for _ in range(10):
                status, _b, _h = self.get('/api/routing-config')
                self.assertEqual(status, 200)
                status, _b, _h = self.get('/api/routing-config.json')
                self.assertEqual(status, 200)


class TestWarmPlanStops(unittest.TestCase):
    """warm_plan with a NON-EMPTY stop list — the leg the arithmetic actually runs on."""

    @staticmethod
    def _units(*specs):
        return {name: roundhouse.UnitFile(
            path='', name=name, raw=b'', lines=[], directives=[], comments=[],
            warnings=[], on_demand=marked) for name, marked in specs}

    def _snapshot(self, available):
        return {
            'host': 'boltzmann',
            'mem': {'available_bytes': available},
            'units': [
                {'unit': 'target.service', 'rung': 'OFF', 'on_demand': True,
                 'retired': False, 'mem': {}},
                {'unit': 'big-marked.service', 'rung': 'READY', 'on_demand': True,
                 'retired': False,
                 'mem': {'bytes': 12 * 1024**3, 'source': 'measured',
                         'label': 'measured peak'}},
                {'unit': 'small-marked.service', 'rung': 'READY', 'on_demand': True,
                 'retired': False,
                 'mem': {'bytes': 2 * 1024**3, 'source': 'measured',
                         'label': 'measured peak'}},
                {'unit': 'huge-plain.service', 'rung': 'READY', 'on_demand': False,
                 'retired': False,
                 'mem': {'bytes': 24 * 1024**3, 'source': 'measured',
                         'label': 'measured peak'}},
            ],
        }

    UNITS = ('target.service', True), ('big-marked.service', True), \
            ('small-marked.service', True), ('huge-plain.service', False)

    def test_stops_are_priced_and_ordered_by_residency(self):
        """F7 order over the consenting pool, with freed bytes actually accounted."""
        plan = roundhouse.warm_plan('target.service', self._snapshot(1024**3),
                                    self._units(*self.UNITS), {}, None)
        # estimate is the 9 GiB default (no model file), headroom 1 GiB, budget 1 GiB:
        # the 12 GiB marked resident alone closes the gap, and it is picked first.
        self.assertEqual(plan['stops'], ['big-marked.service'])
        self.assertTrue(plan['fits'])
        self.assertEqual(plan['freed_by'],
                         [{'unit': 'big-marked.service', 'bytes': 12 * 1024**3,
                           'source': 'measured peak row'}])
        self.assertEqual(plan['shortfall_bytes'], 0)

    def test_unmarked_resident_never_pays_for_the_fit(self):
        """The huge unmarked unit could fit it single-handed — and must not be used."""
        plan = roundhouse.warm_plan('target.service', self._snapshot(0),
                                    self._units(*self.UNITS), {}, None)
        self.assertNotIn('huge-plain.service', plan['stops'])
        self.assertEqual([e['unit'] for e in plan['excluded_unmarked']],
                         ['huge-plain.service'])
        self.assertEqual([c['unit'] for c in plan['consenting']],
                         ['big-marked.service', 'small-marked.service'])
        for c in plan['consenting']:
            self.assertIsInstance(c['resident_bytes'], int)
            self.assertEqual(c['resident_source'], 'measured peak row')

    def test_unfittable_reports_shortfall(self):
        units = self._units(('target.service', True), ('huge-plain.service', False))
        snap = {
            'host': 'boltzmann',
            'mem': {'available_bytes': 0},
            'units': [
                {'unit': 'target.service', 'rung': 'OFF', 'on_demand': True,
                 'retired': False, 'mem': {}},
                {'unit': 'huge-plain.service', 'rung': 'READY', 'on_demand': False,
                 'retired': False,
                 'mem': {'bytes': 24 * 1024**3, 'source': 'measured',
                         'label': 'measured peak'}},
            ],
        }
        plan = roundhouse.warm_plan('target.service', snap, units, {}, None)
        self.assertFalse(plan['fits'])
        self.assertEqual(plan['stops'], [])
        self.assertEqual(plan['consenting'], [])
        self.assertEqual(plan['shortfall_bytes'],
                         plan['estimate_bytes'] + roundhouse.HEADROOM_BYTES)


class TestYamlTrailingNewlineIsQuoted(unittest.TestCase):
    """The `$`-vs-fullmatch trap in the safe-bare rule (§3.3 says re.fullmatch).

    `re.compile(r'[A-Za-z0-9._/-]+$').match('alias\\n')` SUCCEEDS — in a non-MULTILINE
    pattern `$` also matches just before a trailing newline — so a value ending in a
    newline would have been emitted bare, putting a raw line break in the document.
    """

    def test_trailing_newline_never_goes_bare(self):
        for payload in ('alias\n', 'alias\n\n', 'boltzmann-x\n'):
            with self.subTest(payload=payload):
                out = roundhouse._yaml_str(payload)
                self.assertTrue(out.startswith('"') and out.endswith('"'), out)
                self.assertNotIn('\n', out)
                self.assertIn('\\x0a', out)

    def test_emitted_document_has_no_stray_line_break(self):
        entries = [{'model_name': 'boltzmann-x',
                    'litellm_params': {'model': 'openai/x',
                                       'api_base': 'http://h:1/v1', 'api_key': 'none'},
                    'model_info': {'unit': 'x.service', 'logical': 'x\n',
                                   'host': 'boltzmann', 'rung': 'READY',
                                   'on_demand': True, 'load_strategy': 'manual'}}]
        meta = {'generated_by': 'roundhouse@boltzmann',
                'generated_at': '2026-08-13T12:00:00Z',
                'warm_hook': 'POST http://h:8090/api/warm'}
        text = roundhouse.emit_routing_yaml(meta, entries)
        self.assertEqual(len(text.split('\n')), 17)   # 16 content lines + trailing ''
        self.assertTrue(text.endswith('load_strategy: manual\n'))


if __name__ == '__main__':
    unittest.main()
