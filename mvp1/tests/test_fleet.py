#!/usr/bin/env python3
"""Roundhouse MVP8 Fleet Federation Test Suite

Tests fleet-peer parsing, federated HTTP client, staleness state machine,
merge logic, and ingestion validation per §3-5, §9/T1.
"""

import sys
import os
import unittest
import time
import json
import ssl
import socket
from pathlib import Path
from unittest.mock import MagicMock, patch, Mock
from io import BytesIO

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import roundhouse


class TestFleetPeerParsing(unittest.TestCase):
    """Test --fleet-peer parsing per §3.1, K1."""

    def test_parse_valid_https(self):
        """Valid HTTPS URL."""
        decls, errors = roundhouse.parse_fleet_peer_decls(['peer1=https://example.com:8099'])
        self.assertEqual(errors, [])
        self.assertIn('peer1', decls)
        host, port, url = decls['peer1']
        self.assertEqual(host, 'example.com')
        self.assertEqual(port, 8099)
        self.assertEqual(url, 'https://example.com:8099')

    def test_parse_valid_http(self):
        """Valid HTTP URL."""
        decls, errors = roundhouse.parse_fleet_peer_decls(['peer1=http://example.com:8080'])
        self.assertEqual(errors, [])
        host, port, url = decls['peer1']
        self.assertEqual(host, 'example.com')
        self.assertEqual(port, 8080)

    def test_parse_default_https_port(self):
        """HTTPS defaults to port 443."""
        decls, errors = roundhouse.parse_fleet_peer_decls(['peer1=https://example.com'])
        self.assertEqual(errors, [])
        host, port, url = decls['peer1']
        self.assertEqual(port, 443)
        self.assertEqual(url, 'https://example.com:443')

    def test_parse_default_http_port(self):
        """HTTP defaults to port 80."""
        decls, errors = roundhouse.parse_fleet_peer_decls(['peer1=http://example.com'])
        self.assertEqual(errors, [])
        host, port, url = decls['peer1']
        self.assertEqual(port, 80)
        self.assertEqual(url, 'http://example.com:80')

    def test_parse_ipv6_literal(self):
        """IPv6 literals are bracketed and normalized."""
        decls, errors = roundhouse.parse_fleet_peer_decls(['peer1=https://[::1]:8099'])
        self.assertEqual(errors, [])
        host, port, url = decls['peer1']
        self.assertEqual(host, '::1')
        self.assertEqual(port, 8099)
        # Verify IPv6 is bracketed in normalized URL
        self.assertIn('[::1]', url)

    def test_parse_ipv4_literal(self):
        """IPv4 literals work."""
        decls, errors = roundhouse.parse_fleet_peer_decls(['peer1=https://192.0.2.1:8099'])
        self.assertEqual(errors, [])
        host, port, url = decls['peer1']
        self.assertEqual(host, '192.0.2.1')
        self.assertEqual(port, 8099)

    def test_parse_missing_equals(self):
        """Missing = is an error."""
        decls, errors = roundhouse.parse_fleet_peer_decls(['peer1https://example.com'])
        self.assertTrue(any('malformed' in e and 'NAME=URL' in e for e in errors))

    def test_parse_bad_scheme(self):
        """Non-http/https scheme is rejected."""
        decls, errors = roundhouse.parse_fleet_peer_decls(['peer1=ftp://example.com'])
        self.assertTrue(any('http or https' in e for e in errors))

    def test_parse_path_rejected(self):
        """Path component is rejected."""
        decls, errors = roundhouse.parse_fleet_peer_decls(['peer1=https://example.com/api/units'])
        self.assertTrue(any('path' in e.lower() for e in errors))

    def test_parse_query_rejected(self):
        """Query component is rejected."""
        decls, errors = roundhouse.parse_fleet_peer_decls(['peer1=https://example.com?key=value'])
        self.assertTrue(any('query' in e.lower() for e in errors))

    def test_parse_fragment_rejected(self):
        """Fragment component is rejected."""
        decls, errors = roundhouse.parse_fleet_peer_decls(['peer1=https://example.com#section'])
        self.assertTrue(any('fragment' in e.lower() or 'path' in e.lower() for e in errors))

    def test_parse_credentials_rejected(self):
        """Credentials are rejected."""
        decls, errors = roundhouse.parse_fleet_peer_decls(['peer1=https://user:pass@example.com'])
        self.assertTrue(any('credentials' in e.lower() or 'path' in e.lower() for e in errors))

    def test_parse_empty_hostname(self):
        """Empty hostname is rejected."""
        decls, errors = roundhouse.parse_fleet_peer_decls(['peer1=https://:8099'])
        self.assertTrue(any(e for e in errors))

    def test_parse_duplicate_names(self):
        """Duplicate names within the flag are rejected."""
        decls, errors = roundhouse.parse_fleet_peer_decls([
            'peer1=https://example1.com',
            'peer1=https://example2.com'
        ])
        self.assertTrue(any('duplicate' in e for e in errors))

    def test_parse_cap_exceeded(self):
        """More than FLEET_PEER_MAX is rejected."""
        values = [f'peer{i}=https://example{i}.com' for i in range(roundhouse.FLEET_PEER_MAX + 1)]
        decls, errors = roundhouse.parse_fleet_peer_decls(values)
        self.assertTrue(any('too many' in e for e in errors))

    def test_parse_none_returns_empty(self):
        """None returns empty dict and no errors."""
        decls, errors = roundhouse.parse_fleet_peer_decls(None)
        self.assertEqual(decls, {})
        self.assertEqual(errors, [])

    def test_parse_invalid_name(self):
        """Invalid name (not matching regex) is rejected."""
        # Name must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$
        decls, errors = roundhouse.parse_fleet_peer_decls(['@invalid=https://example.com'])
        self.assertTrue(any(e for e in errors))


class TestValidatePeerSets(unittest.TestCase):
    """Test shared-namespace and cap validation per §3.2, K1."""

    def test_shared_namespace_duplicate(self):
        """A name declared as both --peer and --fleet-peer is rejected."""
        tcp = {'peer1': ('example.com', 8080)}
        fleet = {'peer1': ('example.com', 8099, 'https://example.com:8099')}
        errors = roundhouse.validate_peer_sets(tcp, fleet)
        self.assertTrue(any('both --peer and --fleet-peer' in e for e in errors))

    def test_combined_cap_exceeded(self):
        """Combined count > PEER_MAX is rejected."""
        tcp = {f'tcp{i}': (f'ex{i}.com', 8080 + i) for i in range(5)}
        fleet = {f'fleet{i}': (f'ex{i}.com', 8099 + i, f'https://ex{i}.com:{8099 + i}')
                 for i in range(5)}
        errors = roundhouse.validate_peer_sets(tcp, fleet)
        self.assertTrue(any('combined' in e.lower() or 'too many' in e for e in errors))

    def test_valid_combined_no_cap_exceeded(self):
        """Valid combined count <= PEER_MAX."""
        tcp = {'tcp1': ('example.com', 8080), 'tcp2': ('example.com', 8081)}
        fleet = {'fleet1': ('example.com', 8099, 'https://example.com:8099')}
        errors = roundhouse.validate_peer_sets(tcp, fleet)
        self.assertEqual(errors, [])

    def test_valid_no_duplicates(self):
        """No duplicates, different names."""
        tcp = {'tcp1': ('example.com', 8080)}
        fleet = {'fleet1': ('example.com', 8099, 'https://example.com:8099')}
        errors = roundhouse.validate_peer_sets(tcp, fleet)
        self.assertEqual(errors, [])


class TestFetchPeer(unittest.TestCase):
    """Test _fetch_peer behavior per §3.3, K2."""

    def test_fetch_success_units(self):
        """Successful fetch of /api/units."""
        peer_watch = MagicMock()
        peer_watch.fleet = {'peer1': 'https://example.com:8099'}

        payload = {'units': [{'unit': 'test.service'}], 'mode': 'read-only'}

        def mock_opener_open(url, timeout=None):
            self.assertEqual(url, 'https://example.com:8099/api/units')
            self.assertEqual(timeout, roundhouse.FETCH_TIMEOUT_SEC)
            resp = MagicMock()
            resp.read.return_value = json.dumps(payload).encode('utf-8')
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=None)
            return resp

        mock_opener = MagicMock()
        mock_opener.open = mock_opener_open

        ok, data, err = roundhouse._fetch_peer(peer_watch, 'peer1', '/api/units', opener=mock_opener)
        self.assertTrue(ok)
        self.assertEqual(data, payload)
        self.assertIsNone(err)

    def test_fetch_http_error(self):
        """HTTP error (non-200) returns http: code."""
        import urllib.error
        peer_watch = MagicMock()
        peer_watch.fleet = {'peer1': 'https://example.com:8099'}

        def mock_opener_open(url, timeout=None):
            raise urllib.error.HTTPError(url, 404, 'Not Found', {}, None)

        mock_opener = MagicMock()
        mock_opener.open = mock_opener_open

        ok, data, err = roundhouse._fetch_peer(peer_watch, 'peer1', '/api/units', opener=mock_opener)
        self.assertFalse(ok)
        self.assertIsNone(data)
        self.assertIn('http:', err)
        self.assertIn('404', err)

    def test_fetch_redirect_not_followed(self):
        """Redirects are not followed; returned as http: code."""
        import urllib.error
        peer_watch = MagicMock()
        peer_watch.fleet = {'peer1': 'https://example.com:8099'}

        def mock_opener_open(url, timeout=None):
            raise urllib.error.HTTPError(url, 301, 'Moved Permanently', {}, None)

        mock_opener = MagicMock()
        mock_opener.open = mock_opener_open

        ok, data, err = roundhouse._fetch_peer(peer_watch, 'peer1', '/api/units', opener=mock_opener)
        self.assertFalse(ok)
        self.assertIn('http: 301', err)

    def test_fetch_timeout_error(self):
        """Timeout returns timeout: prefix."""
        import urllib.error
        peer_watch = MagicMock()
        peer_watch.fleet = {'peer1': 'https://example.com:8099'}

        def mock_opener_open(url, timeout=None):
            raise TimeoutError('timed out')

        mock_opener = MagicMock()
        mock_opener.open = mock_opener_open

        ok, data, err = roundhouse._fetch_peer(peer_watch, 'peer1', '/api/units', opener=mock_opener)
        self.assertFalse(ok)
        self.assertIn('timeout:', err)

    def test_fetch_ssl_error(self):
        """SSL error returns tls: prefix."""
        peer_watch = MagicMock()
        peer_watch.fleet = {'peer1': 'https://example.com:8099'}

        def mock_opener_open(url, timeout=None):
            raise ssl.SSLCertVerificationError('certificate verify failed')

        mock_opener = MagicMock()
        mock_opener.open = mock_opener_open

        ok, data, err = roundhouse._fetch_peer(peer_watch, 'peer1', '/api/units', opener=mock_opener)
        self.assertFalse(ok)
        self.assertIn('tls:', err)

    def test_fetch_connect_error(self):
        """Connection error returns connect: prefix."""
        import urllib.error
        peer_watch = MagicMock()
        peer_watch.fleet = {'peer1': 'https://example.com:8099'}

        def mock_opener_open(url, timeout=None):
            raise urllib.error.URLError(OSError('Connection refused'))

        mock_opener = MagicMock()
        mock_opener.open = mock_opener_open

        ok, data, err = roundhouse._fetch_peer(peer_watch, 'peer1', '/api/units', opener=mock_opener)
        self.assertFalse(ok)
        self.assertIn('connect:', err)

    def test_fetch_oversized_body(self):
        """Body larger than FETCH_MAX_BYTES is rejected."""
        peer_watch = MagicMock()
        peer_watch.fleet = {'peer1': 'https://example.com:8099'}

        def mock_opener_open(url, timeout=None):
            resp = MagicMock()
            # Return more than FETCH_MAX_BYTES
            resp.read.return_value = b'x' * (roundhouse.FETCH_MAX_BYTES + 1)
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=None)
            return resp

        mock_opener = MagicMock()
        mock_opener.open = mock_opener_open

        ok, data, err = roundhouse._fetch_peer(peer_watch, 'peer1', '/api/units', opener=mock_opener)
        self.assertFalse(ok)
        self.assertIn('body: oversized', err)

    def test_fetch_invalid_json(self):
        """Invalid JSON returns body: error."""
        peer_watch = MagicMock()
        peer_watch.fleet = {'peer1': 'https://example.com:8099'}

        def mock_opener_open(url, timeout=None):
            resp = MagicMock()
            resp.read.return_value = b'not valid json {'
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=None)
            return resp

        mock_opener = MagicMock()
        mock_opener.open = mock_opener_open

        ok, data, err = roundhouse._fetch_peer(peer_watch, 'peer1', '/api/units', opener=mock_opener)
        self.assertFalse(ok)
        self.assertIn('body: invalid JSON', err)

    def test_fetch_json_not_object(self):
        """JSON that is not an object is rejected."""
        peer_watch = MagicMock()
        peer_watch.fleet = {'peer1': 'https://example.com:8099'}

        def mock_opener_open(url, timeout=None):
            resp = MagicMock()
            resp.read.return_value = b'[1, 2, 3]'
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=None)
            return resp

        mock_opener = MagicMock()
        mock_opener.open = mock_opener_open

        ok, data, err = roundhouse._fetch_peer(peer_watch, 'peer1', '/api/units', opener=mock_opener)
        self.assertFalse(ok)
        self.assertIn('body: not a JSON object', err)

    def test_fetch_invalid_path(self):
        """Fetch with invalid path (not in FLEET_PATHS) asserts."""
        peer_watch = MagicMock()
        peer_watch.fleet = {'peer1': 'https://example.com:8099'}

        with self.assertRaises(AssertionError):
            roundhouse._fetch_peer(peer_watch, 'peer1', '/invalid/path')


class TestValidatePeerEntry(unittest.TestCase):
    """Test entry validation per §5.1."""

    def test_valid_entry_minimal(self):
        """Minimal valid entry with just model_name."""
        entry = {'model_name': 'test-model'}
        self.assertTrue(roundhouse.validate_peer_entry(entry))

    def test_valid_entry_full(self):
        """Valid entry with all expected fields."""
        entry = {
            'model_name': 'test-model',
            'litellm_params': {
                'model': 'openai/test',
                'api_base': 'http://example.com:8080/v1',
                'api_key': 'none'
            },
            'model_info': {
                'unit': 'test.service',
                'logical': 'test',
                'host': 'example.com',
                'rung': 'READY',
                'on_demand': False
            }
        }
        self.assertTrue(roundhouse.validate_peer_entry(entry))

    def test_invalid_not_dict(self):
        """Entry that is not a dict is invalid."""
        self.assertFalse(roundhouse.validate_peer_entry('not a dict'))

    def test_invalid_missing_model_name(self):
        """Entry without model_name is invalid."""
        entry = {'litellm_params': {}}
        self.assertFalse(roundhouse.validate_peer_entry(entry))

    def test_invalid_empty_model_name(self):
        """Entry with empty model_name is invalid."""
        entry = {'model_name': ''}
        self.assertFalse(roundhouse.validate_peer_entry(entry))

    def test_invalid_model_name_not_string(self):
        """Entry with non-string model_name is invalid."""
        entry = {'model_name': 123}
        self.assertFalse(roundhouse.validate_peer_entry(entry))

    def test_invalid_list_value(self):
        """Entry with list value is invalid."""
        entry = {'model_name': 'test', 'tags': ['a', 'b']}
        self.assertFalse(roundhouse.validate_peer_entry(entry))

    def test_invalid_none_value(self):
        """Entry with None value is invalid."""
        entry = {'model_name': 'test', 'extra': None}
        self.assertFalse(roundhouse.validate_peer_entry(entry))

    def test_invalid_nested_dict_with_list(self):
        """Entry with list inside nested dict is invalid."""
        entry = {'model_name': 'test', 'params': {'items': [1, 2]}}
        self.assertFalse(roundhouse.validate_peer_entry(entry))

    def test_invalid_depth_3(self):
        """Entry with depth > 2 is invalid."""
        entry = {'model_name': 'test', 'a': {'b': {'c': 'value'}}}
        self.assertFalse(roundhouse.validate_peer_entry(entry))


class TestStalenessStateMachine(unittest.TestCase):
    """Test fed state machine transitions per §4.2, K4."""

    def setUp(self):
        """Set up a PeerWatch with fed tracking."""
        lock = MagicMock()
        event_bus = MagicMock()
        self.peer_watch = roundhouse.PeerWatch(
            declared={'fleet1': ('example.com', 8099)},
            lock=lock,
            event_bus=event_bus,
            fleet={'fleet1': 'https://example.com:8099'},
            now=time.time
        )

    def test_never_to_fresh_on_success(self):
        """never + fetch success → fresh + SSE."""
        units_doc = {'units': [], 'mode': 'read-only'}
        routing_doc = {'model_list': []}

        payload = self.peer_watch.apply_fetch_unlocked(
            'fleet1', True, units_doc, routing_doc, None, time.time())

        self.assertEqual(self.peer_watch.fed['fleet1']['state'], 'fresh')
        self.assertIsNotNone(payload)
        self.assertIsNone(self.peer_watch.fed['fleet1']['reason'])

    def test_never_to_never_on_first_failure(self):
        """never + first failure → never with reason."""
        payload = self.peer_watch.apply_fetch_unlocked(
            'fleet1', False, None, None, 'tls: SSLCertVerificationError', time.time())

        self.assertEqual(self.peer_watch.fed['fleet1']['state'], 'never')
        self.assertIsNotNone(payload)
        self.assertIn('tls:', self.peer_watch.fed['fleet1']['reason'])

    def test_fresh_to_stale_on_failure(self):
        """fresh + fetch failure → stale."""
        # First succeed
        units_doc = {'units': [], 'mode': 'read-only'}
        routing_doc = {'model_list': []}
        self.peer_watch.apply_fetch_unlocked('fleet1', True, units_doc, routing_doc, None, time.time())

        # Then fail
        payload = self.peer_watch.apply_fetch_unlocked(
            'fleet1', False, None, None, 'connect: Connection refused', time.time())

        self.assertEqual(self.peer_watch.fed['fleet1']['state'], 'stale')
        self.assertIsNotNone(payload)

    def test_stale_to_fresh_on_success(self):
        """stale + fetch success → fresh."""
        # Get to stale first
        units_doc = {'units': [], 'mode': 'read-only'}
        routing_doc = {'model_list': []}
        self.peer_watch.apply_fetch_unlocked('fleet1', True, units_doc, routing_doc, None, time.time())
        self.peer_watch.apply_fetch_unlocked('fleet1', False, None, None, 'timeout: error', time.time())

        # Now succeed
        payload = self.peer_watch.apply_fetch_unlocked(
            'fleet1', True, units_doc, routing_doc, None, time.time())

        self.assertEqual(self.peer_watch.fed['fleet1']['state'], 'fresh')
        self.assertIsNotNone(payload)
        self.assertIsNone(self.peer_watch.fed['fleet1']['reason'])


class TestBuildFleetMerge(unittest.TestCase):
    """Test merge logic per §5.2."""

    def test_merge_local_only(self):
        """Merge with only local entries."""
        local_entries = [
            {'model_name': 'host-a', 'litellm_params': {'model': 'a'}},
            {'model_name': 'host-b', 'litellm_params': {'model': 'b'}}
        ]
        fed_rows = roundhouse.FedRows()

        result = roundhouse.build_fleet_merge(local_entries, 'host', fed_rows)

        self.assertEqual(len(result['model_list']), 2)
        self.assertEqual(result['model_list'][0]['model_name'], 'host-a')

    def test_merge_with_peer(self):
        """Merge with peer entries."""
        local_entries = [{'model_name': 'host-a', 'litellm_params': {'model': 'a'}}]

        peer_fed = {
            'state': 'fresh',
            'entries': [{'model_name': 'peer-b', 'litellm_params': {'model': 'b'}}],
            'fetched_at': time.time()
        }
        fed_rows = roundhouse.FedRows({'peer1': peer_fed})

        result = roundhouse.build_fleet_merge(local_entries, 'host', fed_rows)

        self.assertEqual(len(result['model_list']), 2)
        self.assertIn('host', [c['name'] for c in result['contributors']])
        self.assertIn('peer1', [c['name'] for c in result['contributors']])

    def test_merge_conflict_first_wins(self):
        """Conflict on model_name: first occurrence kept."""
        local_entries = [{'model_name': 'both-model', 'litellm_params': {'model': 'v1'}}]

        peer_fed = {
            'state': 'fresh',
            'entries': [{'model_name': 'both-model', 'litellm_params': {'model': 'v2'}}],
            'fetched_at': time.time()
        }
        fed_rows = roundhouse.FedRows({'peer1': peer_fed})

        result = roundhouse.build_fleet_merge(local_entries, 'host', fed_rows)

        # Should have only one entry, the local one
        matching = [e for e in result['model_list'] if e['model_name'] == 'both-model']
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]['litellm_params']['model'], 'v1')

        # Conflict should be recorded
        self.assertTrue(any(c['model_name'] == 'both-model' for c in result['conflicts']))

    def test_merge_excludes_stale_peer(self):
        """Merge excludes peers that are not fresh."""
        local_entries = [{'model_name': 'host-a', 'litellm_params': {'model': 'a'}}]

        peer_fed = {
            'state': 'stale',
            'entries': [{'model_name': 'peer-b', 'litellm_params': {'model': 'b'}}],
            'reason': 'connect: refused'
        }
        fed_rows = roundhouse.FedRows({'peer1': peer_fed})

        result = roundhouse.build_fleet_merge(local_entries, 'host', fed_rows)

        # Only local entry should be in model_list
        self.assertEqual(len(result['model_list']), 1)
        self.assertEqual(result['model_list'][0]['model_name'], 'host-a')

        # Peer should be in excluded
        excluded = [e for e in result['excluded'] if e['name'] == 'peer1']
        self.assertEqual(len(excluded), 1)


class TestEmitFleetYaml(unittest.TestCase):
    """Test fleet YAML emission per §5.3."""

    def test_emit_header_only(self):
        """Emit with no entries."""
        meta = {
            'generated_by': 'roundhouse@host',
            'generated_at': '2026-08-14T12:00:00Z'
        }
        merge = {
            'model_list': [],
            'contributors': [{'name': 'host', 'source': 'local'}],
            'excluded': [],
            'conflicts': []
        }

        yaml = roundhouse.emit_fleet_yaml(meta, merge)

        self.assertIn('generated-by: roundhouse@host', yaml)
        self.assertIn('generated-at: 2026-08-14T12:00:00Z', yaml)
        self.assertIn('model_list:', yaml)

    def test_emit_with_entries(self):
        """Emit with entries."""
        meta = {
            'generated_by': 'roundhouse@host',
            'generated_at': '2026-08-14T12:00:00Z'
        }
        merge = {
            'model_list': [
                {
                    'model_name': 'test-model',
                    'litellm_params': {'model': 'openai/test', 'api_base': 'http://localhost:8080/v1'},
                    'model_info': {'unit': 'test.service', 'rung': 'READY'}
                }
            ],
            'contributors': [{'name': 'host', 'source': 'local'}],
            'excluded': [],
            'conflicts': []
        }

        yaml = roundhouse.emit_fleet_yaml(meta, merge)

        self.assertIn('model_name: test-model', yaml)
        self.assertIn('model: openai/test', yaml)

    def test_emit_contributor_comments(self):
        """Emit includes contributor comments."""
        meta = {
            'generated_by': 'roundhouse@host',
            'generated_at': '2026-08-14T12:00:00Z'
        }
        merge = {
            'model_list': [],
            'contributors': [
                {'name': 'host', 'source': 'local'},
                {'name': 'peer1', 'fetched_at': 1755159990.0}
            ],
            'excluded': [],
            'conflicts': []
        }

        yaml = roundhouse.emit_fleet_yaml(meta, merge)

        self.assertIn('contributor: host (local)', yaml)
        self.assertIn('contributor: peer1', yaml)

    def test_emit_excluded_comments(self):
        """Emit includes excluded peer comments."""
        meta = {
            'generated_by': 'roundhouse@host',
            'generated_at': '2026-08-14T12:00:00Z'
        }
        merge = {
            'model_list': [],
            'contributors': [{'name': 'host', 'source': 'local'}],
            'excluded': [
                {
                    'name': 'peer1',
                    'state': 'down',
                    'fed_state': 'stale',
                    'reason': 'connect: refused'
                }
            ],
            'conflicts': []
        }

        yaml = roundhouse.emit_fleet_yaml(meta, merge)

        self.assertIn('excluded: peer1', yaml)

    def test_emit_conflict_comments(self):
        """Emit includes conflict comments."""
        meta = {
            'generated_by': 'roundhouse@host',
            'generated_at': '2026-08-14T12:00:00Z'
        }
        merge = {
            'model_list': [
                {'model_name': 'both', 'litellm_params': {'model': 'a'}}
            ],
            'contributors': [{'name': 'host', 'source': 'local'}],
            'excluded': [],
            'conflicts': [
                {
                    'model_name': 'both',
                    'kept_source': 'host',
                    'dropped_source': 'peer1'
                }
            ]
        }

        yaml = roundhouse.emit_fleet_yaml(meta, merge)

        self.assertIn('conflict:', yaml)
        self.assertIn('both', yaml)


if __name__ == '__main__':
    unittest.main()
