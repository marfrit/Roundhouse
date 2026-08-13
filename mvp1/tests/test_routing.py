#!/usr/bin/env python3
"""Roundhouse MVP5 Routing Config Generation Tests

Tests for YAML emitter, entry building, inclusion logic, warm resolution,
and warm plan computation. These are pure functions with no file I/O.
"""

import sys
import os
import unittest
import json
from pathlib import Path
from datetime import datetime, timezone

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
        self.assertEqual(json_dict['generated_by'], 'boltzmann')
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


if __name__ == '__main__':
    unittest.main()
