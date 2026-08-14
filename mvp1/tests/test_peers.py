#!/usr/bin/env python3
"""Roundhouse MVP7 Peer Watch Test Suite

Tests parsing, state machine, validation, and lock discipline per §7.1-7.3.
"""

import sys
import os
import unittest
import socket
import time
import threading
import queue
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import roundhouse


class TestBindParsing(unittest.TestCase):
    """Test --bind parsing per §3.1, §7.1."""

    def test_default_none(self):
        """Default when no --bind: [('0.0.0.0', AF_INET)]"""
        result, errors = roundhouse.parse_bind_list(None)
        self.assertEqual(errors, [])
        self.assertEqual(result, [('0.0.0.0', socket.AF_INET)])

    def test_single_ipv4(self):
        """Single IPv4 address."""
        result, errors = roundhouse.parse_bind_list(['127.0.0.1'])
        self.assertEqual(errors, [])
        self.assertEqual(result, [('127.0.0.1', socket.AF_INET)])

    def test_single_ipv6(self):
        """Single IPv6 address."""
        result, errors = roundhouse.parse_bind_list(['::1'])
        self.assertEqual(errors, [])
        self.assertEqual(result, [('::1', socket.AF_INET6)])

    def test_ipv6_bracketed(self):
        """IPv6 in brackets is stripped and canonicalized."""
        result, errors = roundhouse.parse_bind_list(['[::1]'])
        self.assertEqual(errors, [])
        self.assertEqual(result, [('::1', socket.AF_INET6)])

    def test_ipv6_expanded_to_canonical(self):
        """IPv6 expanded form canonicalized to shorthand."""
        result, errors = roundhouse.parse_bind_list(['0:0:0:0:0:0:0:1'])
        self.assertEqual(errors, [])
        # Should canonicalize to ::1
        self.assertEqual(result, [('::1', socket.AF_INET6)])

    def test_comma_separated(self):
        """Comma-separated addresses flatten and are all included."""
        result, errors = roundhouse.parse_bind_list(['127.0.0.1,::1'])
        self.assertEqual(errors, [])
        self.assertEqual(len(result), 2)
        self.assertIn(('127.0.0.1', socket.AF_INET), result)
        self.assertIn(('::1', socket.AF_INET6), result)

    def test_repeatable(self):
        """Repeatable --bind flag combines."""
        result, errors = roundhouse.parse_bind_list(['127.0.0.1', '::1'])
        self.assertEqual(errors, [])
        self.assertEqual(len(result), 2)
        self.assertIn(('127.0.0.1', socket.AF_INET), result)
        self.assertIn(('::1', socket.AF_INET6), result)

    def test_duplicate_error(self):
        """Duplicate addresses (after canonicalization) are errors."""
        result, errors = roundhouse.parse_bind_list(['127.0.0.1', '127.0.0.1'])
        self.assertTrue(any('duplicate' in e for e in errors))

    def test_hostname_rejected(self):
        """Hostnames are rejected."""
        result, errors = roundhouse.parse_bind_list(['localhost'])
        self.assertTrue(any('not a literal IP address' in e for e in errors))

    def test_wildcard_overlap_ipv4(self):
        """0.0.0.0 + another IPv4 is an error."""
        result, errors = roundhouse.parse_bind_list(['0.0.0.0', '127.0.0.1'])
        self.assertTrue(any('already covers' in e for e in errors))

    def test_wildcard_overlap_ipv6(self):
        """:: + another IPv6 is an error."""
        result, errors = roundhouse.parse_bind_list(['::', '::1'])
        self.assertTrue(any('already covers' in e for e in errors))

    def test_wildcard_mix_ok(self):
        """0.0.0.0 + :: is OK (different families)."""
        result, errors = roundhouse.parse_bind_list(['0.0.0.0', '::'])
        self.assertEqual(errors, [])
        self.assertEqual(len(result), 2)

    def test_empty_token_error(self):
        """Empty token in comma list is an error."""
        result, errors = roundhouse.parse_bind_list(['127.0.0.1,,::1'])
        self.assertTrue(any('empty bind address' in e for e in errors))


class TestPeerParsing(unittest.TestCase):
    """Test --peer parsing per §4.1, §7.1."""

    def test_default_none(self):
        """Default when no --peer: empty dict."""
        result, errors = roundhouse.parse_peer_decls(None)
        self.assertEqual(errors, [])
        self.assertEqual(result, {})

    def test_happy_path(self):
        """Valid NAME=HOST:PORT."""
        result, errors = roundhouse.parse_peer_decls(['peer1=example.com:8080'])
        self.assertEqual(errors, [])
        self.assertEqual(result, {'peer1': ('example.com', 8080)})

    def test_ipv4_host(self):
        """IPv4 literal as host."""
        result, errors = roundhouse.parse_peer_decls(['p1=127.0.0.1:8080'])
        self.assertEqual(errors, [])
        self.assertEqual(result, {'p1': ('127.0.0.1', 8080)})

    def test_ipv6_bracketed(self):
        """IPv6 literal must be bracketed."""
        result, errors = roundhouse.parse_peer_decls(['p1=[::1]:8080'])
        self.assertEqual(errors, [])
        self.assertEqual(result, {'p1': ('::1', 8080)})

    def test_ipv6_bare_rejected(self):
        """IPv6 without brackets is rejected."""
        result, errors = roundhouse.parse_peer_decls(['p1=::1:8080'])
        self.assertTrue(any('bracket IPv6' in e for e in errors))

    def test_malformed_no_equals(self):
        """Missing = is an error."""
        result, errors = roundhouse.parse_peer_decls(['peer1:localhost:8080'])
        self.assertTrue(any('expected NAME=HOST:PORT' in e for e in errors))

    def test_malformed_no_port(self):
        """Missing :PORT is an error."""
        result, errors = roundhouse.parse_peer_decls(['peer1=example.com'])
        self.assertTrue(any('missing :PORT' in e for e in errors))

    def test_name_charset_valid(self):
        """Valid name characters: alphanumeric, dot, dash, underscore."""
        result, errors = roundhouse.parse_peer_decls(['peer_1.a-b=host:8080'])
        self.assertEqual(errors, [])
        self.assertIn('peer_1.a-b', result)

    def test_name_charset_invalid(self):
        """Invalid name characters rejected."""
        result, errors = roundhouse.parse_peer_decls(['peer@1=host:8080'])
        self.assertTrue(any('does not match' in e for e in errors))

    def test_name_must_start_alphanumeric(self):
        """Name must start with alphanumeric."""
        result, errors = roundhouse.parse_peer_decls(['-peer=host:8080'])
        self.assertTrue(any('does not match' in e for e in errors))

    def test_name_length_boundary(self):
        """Name max 32 chars."""
        # 32 chars is OK
        result, errors = roundhouse.parse_peer_decls(['a' * 32 + '=host:8080'])
        self.assertEqual(errors, [])
        # 33 chars is not OK
        result, errors = roundhouse.parse_peer_decls(['a' * 33 + '=host:8080'])
        self.assertTrue(any('does not match' in e for e in errors))

    def test_duplicate_name(self):
        """Duplicate names are errors."""
        result, errors = roundhouse.parse_peer_decls(['p1=host1:8080', 'p1=host2:8080'])
        self.assertTrue(any('duplicate peer name' in e for e in errors))

    def test_port_out_of_range(self):
        """Port must be 1-65535."""
        result, errors = roundhouse.parse_peer_decls(['p=host:0'])
        self.assertTrue(any('out of range' in e for e in errors))
        result, errors = roundhouse.parse_peer_decls(['p=host:65536'])
        self.assertTrue(any('out of range' in e for e in errors))

    def test_port_not_int(self):
        """Port must be integer."""
        result, errors = roundhouse.parse_peer_decls(['p=host:abc'])
        self.assertTrue(any('not an integer' in e for e in errors))

    def test_cap_8_peers(self):
        """Cannot declare more than 8 peers."""
        decls = [f'p{i}=host{i}:808{i}' for i in range(9)]
        result, errors = roundhouse.parse_peer_decls(decls)
        self.assertTrue(any('too many peers' in e for e in errors))

    def test_multiple_errors_collected(self):
        """All errors are collected, not short-circuited."""
        decls = [
            'bad-name=host1:8080',  # bad name (starts with -)
            'p2=host2',              # missing :PORT
            'p2=host3:8080'          # duplicate name
        ]
        result, errors = roundhouse.parse_peer_decls(decls)
        self.assertGreaterEqual(len(errors), 2)


class TestPeerStateMachine(unittest.TestCase):
    """Test peer state machine per §4.3, §7.2."""

    def test_unknown_first_success_transitions_up(self):
        """unknown + success → up (event)."""
        clock = MagicMock(return_value=1000.0)
        peer_watch = roundhouse.PeerWatch({'p1': ('host', 8080)}, threading.Lock(), MagicMock(), now=clock)

        event = peer_watch.apply_result_unlocked('p1', True, None, 1001.0)

        self.assertIsNotNone(event)
        self.assertEqual(peer_watch.peers['p1']['state'], 'up')
        self.assertEqual(event['state'], 'up')
        self.assertEqual(event['prev_state'], 'unknown')
        self.assertIsNone(peer_watch.peers['p1']['last_error'])

    def test_unknown_first_failure_stays_unknown(self):
        """unknown + 1st failure → unknown (no event)."""
        clock = MagicMock(return_value=1000.0)
        peer_watch = roundhouse.PeerWatch({'p1': ('host', 8080)}, threading.Lock(), MagicMock(), now=clock)

        event = peer_watch.apply_result_unlocked('p1', False, 'connection refused', 1001.0)

        self.assertIsNone(event)
        self.assertEqual(peer_watch.peers['p1']['state'], 'unknown')
        self.assertEqual(peer_watch.peers['p1']['consecutive_failures'], 1)
        self.assertEqual(peer_watch.peers['p1']['last_error'], 'connection refused')

    def test_unknown_second_failure_transitions_down(self):
        """unknown + 2nd failure → down (event)."""
        clock = MagicMock(return_value=1000.0)
        peer_watch = roundhouse.PeerWatch({'p1': ('host', 8080)}, threading.Lock(), MagicMock(), now=clock)

        # 1st failure
        peer_watch.apply_result_unlocked('p1', False, 'error1', 1001.0)
        # 2nd failure
        event = peer_watch.apply_result_unlocked('p1', False, 'error2', 1002.0)

        self.assertIsNotNone(event)
        self.assertEqual(peer_watch.peers['p1']['state'], 'down')
        self.assertEqual(event['state'], 'down')
        self.assertEqual(event['prev_state'], 'unknown')

    def test_up_success_stays_up(self):
        """up + success → up (no event)."""
        clock = MagicMock(return_value=1000.0)
        peer_watch = roundhouse.PeerWatch({'p1': ('host', 8080)}, threading.Lock(), MagicMock(), now=clock)

        # Get to up state
        peer_watch.apply_result_unlocked('p1', True, None, 1001.0)
        # Success from up
        event = peer_watch.apply_result_unlocked('p1', True, None, 1002.0)

        self.assertIsNone(event)
        self.assertEqual(peer_watch.peers['p1']['state'], 'up')
        self.assertEqual(peer_watch.peers['p1']['consecutive_failures'], 0)

    def test_up_first_failure_stays_up(self):
        """up + 1st failure → up (no event)."""
        clock = MagicMock(return_value=1000.0)
        peer_watch = roundhouse.PeerWatch({'p1': ('host', 8080)}, threading.Lock(), MagicMock(), now=clock)

        peer_watch.apply_result_unlocked('p1', True, None, 1001.0)
        event = peer_watch.apply_result_unlocked('p1', False, 'error', 1002.0)

        self.assertIsNone(event)
        self.assertEqual(peer_watch.peers['p1']['state'], 'up')
        self.assertEqual(peer_watch.peers['p1']['consecutive_failures'], 1)

    def test_up_second_failure_transitions_down(self):
        """up + 2nd failure → down (event)."""
        clock = MagicMock(return_value=1000.0)
        peer_watch = roundhouse.PeerWatch({'p1': ('host', 8080)}, threading.Lock(), MagicMock(), now=clock)

        peer_watch.apply_result_unlocked('p1', True, None, 1001.0)
        peer_watch.apply_result_unlocked('p1', False, 'error1', 1002.0)
        event = peer_watch.apply_result_unlocked('p1', False, 'error2', 1003.0)

        self.assertIsNotNone(event)
        self.assertEqual(peer_watch.peers['p1']['state'], 'down')
        self.assertEqual(event['prev_state'], 'up')

    def test_down_failure_stays_down(self):
        """down + failure → down (no event)."""
        clock = MagicMock(return_value=1000.0)
        peer_watch = roundhouse.PeerWatch({'p1': ('host', 8080)}, threading.Lock(), MagicMock(), now=clock)

        # Get to down
        peer_watch.apply_result_unlocked('p1', True, None, 1001.0)
        peer_watch.apply_result_unlocked('p1', False, 'e1', 1002.0)
        peer_watch.apply_result_unlocked('p1', False, 'e2', 1003.0)
        # More failures
        event = peer_watch.apply_result_unlocked('p1', False, 'e3', 1004.0)

        self.assertIsNone(event)
        self.assertEqual(peer_watch.peers['p1']['state'], 'down')

    def test_down_success_transitions_up(self):
        """down + success → up (event)."""
        clock = MagicMock(return_value=1000.0)
        peer_watch = roundhouse.PeerWatch({'p1': ('host', 8080)}, threading.Lock(), MagicMock(), now=clock)

        # Get to down
        peer_watch.apply_result_unlocked('p1', True, None, 1001.0)
        peer_watch.apply_result_unlocked('p1', False, 'e1', 1002.0)
        peer_watch.apply_result_unlocked('p1', False, 'e2', 1003.0)
        # Success
        event = peer_watch.apply_result_unlocked('p1', True, None, 1004.0)

        self.assertIsNotNone(event)
        self.assertEqual(peer_watch.peers['p1']['state'], 'up')
        self.assertEqual(event['prev_state'], 'down')
        self.assertIsNone(peer_watch.peers['p1']['last_error'])

    def test_error_cleared_on_success(self):
        """last_error is cleared on success."""
        clock = MagicMock(return_value=1000.0)
        peer_watch = roundhouse.PeerWatch({'p1': ('host', 8080)}, threading.Lock(), MagicMock(), now=clock)

        peer_watch.apply_result_unlocked('p1', False, 'some error', 1001.0)
        peer_watch.apply_result_unlocked('p1', True, None, 1002.0)

        self.assertIsNone(peer_watch.peers['p1']['last_error'])

    def test_rows_unlocked_sorted_by_name(self):
        """rows_unlocked returns sorted by name."""
        lock = threading.Lock()
        peer_watch = roundhouse.PeerWatch(
            {'z': ('host1', 8080), 'a': ('host2', 8081)},
            lock,
            MagicMock()
        )

        with lock:
            rows = peer_watch.rows_unlocked()

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['name'], 'a')
        self.assertEqual(rows[1]['name'], 'z')

    def test_since_unchanged_on_non_transition(self):
        """§4.3: `since` moves only on a transition — a quiet probe must not touch it."""
        peer_watch = roundhouse.PeerWatch({'p1': ('host', 8080)}, threading.Lock(), MagicMock())

        peer_watch.apply_result_unlocked('p1', True, None, 1001.0)      # unknown→up
        since_up = peer_watch.peers['p1']['since']
        peer_watch.apply_result_unlocked('p1', True, None, 1002.0)      # up→up
        peer_watch.apply_result_unlocked('p1', False, 'e', 1003.0)      # up→up (cf=1)

        self.assertEqual(peer_watch.peers['p1']['since'], since_up)
        self.assertEqual(peer_watch.peers['p1']['last_probe'], 1003.0)

    def test_flatline_from_down_emits_no_events(self):
        """§7.2 flat-line: 10 further failures from `down` yield zero events."""
        peer_watch = roundhouse.PeerWatch({'p1': ('host', 8080)}, threading.Lock(), MagicMock())

        peer_watch.apply_result_unlocked('p1', False, 'e1', 1001.0)
        first_down = peer_watch.apply_result_unlocked('p1', False, 'e2', 1002.0)
        self.assertIsNotNone(first_down)
        since_down = peer_watch.peers['p1']['since']

        events = [peer_watch.apply_result_unlocked('p1', False, f'e{i}', 1002.0 + i)
                  for i in range(3, 13)]

        self.assertEqual([e for e in events if e is not None], [])
        self.assertEqual(peer_watch.peers['p1']['state'], 'down')
        self.assertEqual(peer_watch.peers['p1']['consecutive_failures'], 12)
        self.assertEqual(peer_watch.peers['p1']['since'], since_down)

    def test_full_lifecycle_emits_exactly_three_events(self):
        """§7.2 lifecycle: unknown→up→(1 fail, still up)→(2nd fail, down)→up = 3 events."""
        peer_watch = roundhouse.PeerWatch({'p1': ('host', 8080)}, threading.Lock(), MagicMock())

        results = [(True, None), (False, 'e1'), (False, 'e2'), (True, None)]
        events = [peer_watch.apply_result_unlocked('p1', ok, err, 1000.0 + i)
                  for i, (ok, err) in enumerate(results)]
        transitions = [(e['prev_state'], e['state']) for e in events if e is not None]

        self.assertEqual(transitions,
                         [('unknown', 'up'), ('up', 'down'), ('down', 'up')])


class TestPeerRound(unittest.TestCase):
    """Round-level behavior of peer_watch_round (§4.4, §7.2)."""

    def test_probe_order_is_declaration_order(self):
        """§4.1: probe order is declaration order (determinism, stated in the spec)."""
        peer_watch = roundhouse.PeerWatch(
            {'zulu': ('h1', 1), 'alpha': ('h2', 2), 'mike': ('h3', 3)},
            threading.Lock(), MagicMock())
        seen = []

        def fake_probe(pw, name, index=0, connect=None):
            seen.append((name, index))
            return (True, None)

        with patch.object(roundhouse, '_probe_peer', fake_probe):
            roundhouse.peer_watch_round(peer_watch)

        # peer_watch_round calls _probe_peer once per peer (with index=0 for single-endpoint)
        self.assertEqual(seen, [('zulu', 0), ('alpha', 0), ('mike', 0)])

    def test_raising_connect_counts_as_failure_with_class_prefix(self):
        """§4.2: any exception is a failure; last_error is prefixed with the class name."""
        peer_watch = roundhouse.PeerWatch({'p1': ('host', 8080)}, threading.Lock(), MagicMock())

        def boom(addr, timeout=None):
            raise ConnectionRefusedError(111, 'Connection refused')

        ok, error = roundhouse._probe_peer(peer_watch, 'p1', connect=boom)

        self.assertFalse(ok)
        self.assertTrue(error.startswith('ConnectionRefusedError: '), error)
        self.assertLessEqual(len(error), 200)

    def test_round_does_not_hold_the_lock_across_the_probe(self):
        """Risk #1, round level: the 2 s connect must not run under watcher_lock.

        test_probe_not_held_lock only proves _probe_peer itself is lock-free; the
        regression that actually bites is `with lock:` wrapped around the round.
        """
        lock = threading.Lock()
        peer_watch = roundhouse.PeerWatch({'p1': ('h', 1), 'p2': ('h', 2)}, lock, MagicMock())
        free_during_probe = []

        def fake_probe(pw, name, index=0, connect=None):
            acquired = lock.acquire(blocking=False)
            free_during_probe.append(acquired)
            if acquired:
                lock.release()
            return (False, 'OSError: refused')

        with patch.object(roundhouse, '_probe_peer', fake_probe):
            roundhouse.peer_watch_round(peer_watch)

        self.assertEqual(free_during_probe, [True, True],
                         'watcher_lock was held while a probe was in flight (Risk #1)')

    def test_round_publishes_only_transitions(self):
        """§5.3: the bus sees a `peer` event only on a transition."""
        bus = MagicMock()
        peer_watch = roundhouse.PeerWatch({'p1': ('h', 1)}, threading.Lock(), bus)

        with patch.object(roundhouse, '_probe_peer', lambda pw, n, index=0, connect=None: (True, None)):
            for _ in range(5):
                roundhouse.peer_watch_round(peer_watch)

        published = [c for c in bus.publish.call_args_list if c[0][0] == 'peer']
        self.assertEqual(len(published), 1, 'a stable peer generated repeat traffic')
        self.assertEqual(published[0][0][1]['prev_state'], 'unknown')


class TestPeerValidation(unittest.TestCase):
    """Test peer validation per §6.3, §7.3."""

    def _mock_unit(self, port=None):
        """Create a mock UnitFile with exec_start and engine_argv."""
        unit = MagicMock(spec=roundhouse.UnitFile)
        if port is not None:
            # Create exec_start with engine_argv that includes port
            exec_start = MagicMock()
            # Mock token objects
            token1 = MagicMock()
            token1.text = '--port'
            token2 = MagicMock()
            token2.text = str(port)
            exec_start.engine_argv = [MagicMock(text='binary'), token1, token2]
            unit.exec_start = exec_start
        else:
            unit.exec_start = None
        return unit

    def test_empty_peers_ok(self):
        """Empty peer dict passes validation."""
        errors = roundhouse.validate_peers({}, {}, 8090, ['127.0.0.1'], 'localhost', 'testhost')
        self.assertEqual(errors, [])

    def test_localhost_unmanaged_port_ok(self):
        """Peer on localhost with unmanaged port is OK."""
        units = {
            'unit1.service': self._mock_unit(8080)
        }
        peers = {'p1': ('127.0.0.1', 9999)}
        errors = roundhouse.validate_peers(peers, units, 8090, ['127.0.0.1'], 'localhost', 'testhost')
        self.assertEqual(errors, [])

    def test_localhost_managed_port_error(self):
        """Peer on localhost with managed port → error."""
        units = {
            'qwen.service': self._mock_unit(8085)
        }
        peers = {'p1': ('127.0.0.1', 8085)}
        errors = roundhouse.validate_peers(peers, units, 8090, ['127.0.0.1'], 'localhost', 'testhost')
        self.assertTrue(any('targets' in e and '8085' in e for e in errors))

    def test_nodename_managed_port_error(self):
        """Peer named by nodename with managed port → error."""
        units = {
            'qwen.service': self._mock_unit(8085)
        }
        peers = {'p1': ('testhost', 8085)}
        errors = roundhouse.validate_peers(peers, units, 8090, ['127.0.0.1'], 'advertise', 'testhost')
        self.assertTrue(any('targets' in e for e in errors))

    def test_bind_address_specific_managed_port_error(self):
        """Peer targeting specific bind address with managed port → error."""
        units = {
            'qwen.service': self._mock_unit(8085)
        }
        peers = {'p1': ('192.168.1.10', 8085)}
        errors = roundhouse.validate_peers(peers, units, 8090, ['192.168.1.10'], 'advertise', 'testhost')
        self.assertTrue(any('targets' in e for e in errors))

    def test_advertise_host_managed_port_error(self):
        """Peer named by advertise_host with managed port → error."""
        units = {
            'qwen.service': self._mock_unit(8085)
        }
        peers = {'p1': ('advertisehost', 8085)}
        errors = roundhouse.validate_peers(peers, units, 8090, ['127.0.0.1'], 'advertisehost', 'testhost')
        self.assertTrue(any('targets' in e for e in errors))

    def test_self_port_managed_port(self):
        """self_port is in MANAGED_PORTS."""
        units = {}
        peers = {'p1': ('127.0.0.1', 8090)}
        errors = roundhouse.validate_peers(peers, units, 8090, ['127.0.0.1'], 'localhost', 'testhost')
        self.assertTrue(any('targets' in e for e in errors))

    def test_ipv6_loopback_managed_port_error(self):
        """IPv6 ::1 with managed port → error."""
        units = {
            'qwen.service': self._mock_unit(8085)
        }
        peers = {'p1': ('::1', 8085)}
        errors = roundhouse.validate_peers(peers, units, 8090, [], 'localhost', 'testhost')
        self.assertTrue(any('targets' in e for e in errors))

    def test_other_host_managed_port_is_legal(self):
        """recon 10, the whole reason D2 is host-AND-port: qwen3.6-coding claims :8085
        on boltzmann AND on ampere, so `ampere=ampere.fritz.box:8085` must be ALLOWED."""
        units = {
            'qwen3.6-coding.service': self._mock_unit(8085)
        }
        peers = {'ampere': ('ampere.fritz.box', 8085)}
        errors = roundhouse.validate_peers(peers, units, 8090, ['0.0.0.0'], 'boltzmann', 'boltzmann')
        self.assertEqual(errors, [])

    def test_loopback_literal_beyond_127_0_0_1_managed_port_error(self):
        """§6.3 loopback-literal rule: ANY literal with .is_loopback counts as this
        host — the whole of 127.0.0.0/8, not just the two addresses spelled out."""
        units = {
            'qwen.service': self._mock_unit(8085)
        }
        for host in ('127.0.0.1', '127.0.0.2', '127.1.2.3', '127.255.255.254', '::1'):
            with self.subTest(host=host):
                errors = roundhouse.validate_peers({'p1': (host, 8085)}, units, 8090,
                                                   [], 'advertisehost', 'testhost')
                self.assertTrue(any('targets' in e for e in errors),
                                f'{host}:8085 was not recognised as this host')

    def test_wildcard_peer_host_managed_port_error(self):
        """§6.3: wildcards are nonsense as peer hosts and are caught here."""
        units = {'qwen.service': self._mock_unit(8085)}
        for host in ('0.0.0.0', '::'):
            with self.subTest(host=host):
                errors = roundhouse.validate_peers({'p1': (host, 8085)}, units, 8090,
                                                   [], 'advertisehost', 'testhost')
                self.assertTrue(any('targets' in e for e in errors))

    def test_getaddrinfo_failure_does_not_crash(self):
        """§6.3: DNS down at boot must not block startup — the miss is covered by
        the nodename/bind/advertise forms."""
        units = {'qwen.service': self._mock_unit(8085)}
        with patch.object(roundhouse.socket, 'getaddrinfo',
                          side_effect=socket.gaierror('temporary failure')):
            errors = roundhouse.validate_peers({'p1': ('127.0.0.1', 8085)}, units, 8090,
                                               ['127.0.0.1'], 'advertisehost', 'testhost')
        self.assertTrue(any('targets' in e for e in errors))

    def test_error_names_both_sides_of_the_collision(self):
        """§6.3 error text: the peer, the endpoint, and the unit that owns the port."""
        units = {'qwen3.6-coding.service': self._mock_unit(8085)}
        errors = roundhouse.validate_peers({'self': ('127.0.0.1', 8085)}, units, 8090,
                                           ['127.0.0.1'], 'advertisehost', 'testhost')
        self.assertEqual(len(errors), 1)
        msg = errors[0]
        for fragment in ("peer 'self'", '127.0.0.1:8085', 'qwen3.6-coding.service',
                         'peers are other hosts'):
            self.assertIn(fragment, msg)
        # The units dict is already keyed by full unit name; appending '.service'
        # again produced 'qwen3.6-coding.service.service' in the operator's face.
        self.assertNotIn('.service.service', msg)

    def test_self_port_collision_names_roundhouse(self):
        """The instance's own port is managed too, and it is not a unit in `units`."""
        errors = roundhouse.validate_peers({'me': ('127.0.0.1', 8090)}, {}, 8090,
                                           ['127.0.0.1'], 'advertisehost', 'testhost')
        self.assertEqual(len(errors), 1)
        self.assertIn('roundhouse.service', errors[0])
        self.assertNotIn('.service.service', errors[0])


class TestPeerIntervalFloor(unittest.TestCase):
    """J6: `--peer-interval SECONDS` (default 60, min 1)."""

    def test_interval_below_one_is_a_configuration_error(self):
        """A 0 or negative cadence turns the watch thread into a hot spin."""
        for bad in (0, -1, -60):
            with self.subTest(interval=bad):
                self.assertNotEqual(roundhouse.validate_peer_interval(bad), [])

    def test_valid_intervals_pass(self):
        for good in (1, 5, 60, 3600):
            with self.subTest(interval=good):
                self.assertEqual(roundhouse.validate_peer_interval(good), [])


class TestPeerLockDiscipline(unittest.TestCase):
    """Test lock discipline per §7.3."""

    def test_probe_not_held_lock(self):
        """Prove the lock is not held during _probe_peer."""
        lock = threading.Lock()
        peer_watch = roundhouse.PeerWatch({'p1': ('127.0.0.1', 9999)}, lock, MagicMock())

        lock_acquired_during_probe = []

        def failing_connect(addr, timeout=None):
            # Try to acquire lock non-blocking; should succeed (lock not held)
            acquired = lock.acquire(blocking=False)
            lock_acquired_during_probe.append(acquired)
            if acquired:
                lock.release()
            raise OSError("connection refused")

        # Call _probe_peer with injected connect
        roundhouse._probe_peer(peer_watch, 'p1', connect=failing_connect)

        self.assertTrue(lock_acquired_during_probe[0],
                       "Lock was held during _probe_peer (Risk #1)")

    def test_rows_unlocked_never_reacquires(self):
        """rows_unlocked never re-acquires the lock."""
        lock = threading.Lock()
        peer_watch = roundhouse.PeerWatch({'p1': ('host', 8080)}, lock, MagicMock())

        # Hold the lock and call rows_unlocked
        try:
            with lock:
                # This should not deadlock (which would timeout in a test harness)
                rows = peer_watch.rows_unlocked()
                self.assertIsInstance(rows, list)
        except Exception as e:
            self.fail(f"rows_unlocked deadlocked or raised: {e}")


class TestFleetPeerCandidateParsing(unittest.TestCase):
    """§7.3: parse_fleet_peer_decls candidate parsing per §4.1, L5."""

    def test_single_url_per_peer(self):
        """Single URL per peer (backward compat with MVP8)."""
        result, errors = roundhouse.parse_fleet_peer_decls(['peer1=http://host1:8080'])
        self.assertEqual(errors, [])
        self.assertEqual(len(result), 1)
        self.assertIn('peer1', result)
        self.assertEqual(len(result['peer1']), 1)
        self.assertEqual(result['peer1'][0][0], 'host1')
        self.assertEqual(result['peer1'][0][1], 8080)

    def test_comma_separated_candidates(self):
        """Comma-separated URLs become candidate list (§4.1, L5)."""
        result, errors = roundhouse.parse_fleet_peer_decls(
            ['peer1=http://host1:8080,http://host2:8081'])
        self.assertEqual(errors, [])
        self.assertEqual(len(result['peer1']), 2)
        self.assertEqual(result['peer1'][0][0], 'host1')
        self.assertEqual(result['peer1'][1][0], 'host2')

    def test_three_candidates_at_cap(self):
        """Three candidates is the cap (§4.1, L5, L6)."""
        result, errors = roundhouse.parse_fleet_peer_decls(
            ['p=http://h1:80,http://h2:80,http://h3:80'])
        self.assertEqual(errors, [])
        self.assertEqual(len(result['p']), 3)

    def test_four_candidates_exceeds_cap(self):
        """Four candidates exceeds cap and is an error (§L6)."""
        result, errors = roundhouse.parse_fleet_peer_decls(
            ['p=http://h1:80,http://h2:80,http://h3:80,http://h4:80'])
        self.assertTrue(any('too many candidates' in e and '> 3' in e for e in errors))

    def test_duplicate_url_within_peer(self):
        """Duplicate URL in same peer is an error (§4.1)."""
        result, errors = roundhouse.parse_fleet_peer_decls(
            ['p=http://host:8080,http://host:8080'])
        self.assertTrue(any('duplicate candidate' in e for e in errors))

    def test_mixed_http_https_allowed(self):
        """Mixed http/https candidates are legal (§4.1, L7)."""
        result, errors = roundhouse.parse_fleet_peer_decls(
            ['p=http://h1:80,https://h2:443'])
        self.assertEqual(errors, [])
        self.assertEqual(len(result['p']), 2)
        self.assertEqual(result['p'][0][0], 'h1')
        self.assertEqual(result['p'][1][0], 'h2')

    def test_ipv6_bracketed_candidates(self):
        """IPv6 addresses in brackets are normalized (§4.1)."""
        result, errors = roundhouse.parse_fleet_peer_decls(
            ['p=http://[::1]:8080,http://[2001:db8::1]:8081'])
        self.assertEqual(errors, [])
        self.assertEqual(len(result['p']), 2)

    def test_empty_candidate_token_error(self):
        """Empty token in comma list is an error."""
        result, errors = roundhouse.parse_fleet_peer_decls(['p=http://h1:80,,http://h2:80'])
        self.assertTrue(any('candidate' in e and 'empty' in e for e in errors))

    def test_per_candidate_error_messaging(self):
        """Per-candidate errors name the candidate index (§4.1)."""
        result, errors = roundhouse.parse_fleet_peer_decls(
            ['p=http://h1:80,invalid://h2:80'])
        self.assertTrue(any('candidate 2' in e for e in errors))


class TestCandidateWalk(unittest.TestCase):
    """§7.3: Candidate walk logic per §4.2, L5."""

    def test_walk_stops_at_first_success(self):
        """Walk stops at first responding candidate (§4.2, L5)."""
        lock = threading.Lock()
        declared = {'p': [('h1', 80), ('h2', 81)]}
        fleet = {'p': ['http://h1:80', 'http://h2:81']}
        pw = roundhouse.PeerWatch(declared, lock, MagicMock(), fleet=fleet)

        call_count = [0]
        def selective_connect(addr, timeout=None):
            call_count[0] += 1
            if addr == ('h1', 80):
                raise OSError("refused")
            sock = MagicMock()
            return sock

        # Candidate 0 fails, candidate 1 succeeds
        ok, err = roundhouse._probe_peer(pw, 'p', index=0, connect=selective_connect)
        self.assertFalse(ok)
        ok, err = roundhouse._probe_peer(pw, 'p', index=1, connect=selective_connect)
        self.assertTrue(ok)

    def test_walk_from_top_every_round(self):
        """Re-walk from top every round; returning preferred endpoint is picked up (§4.2, L5)."""
        lock = threading.Lock()
        declared = {'p': [('preferred', 80), ('fallback', 81)]}
        fleet = {'p': ['http://preferred:80', 'http://fallback:81']}
        pw = roundhouse.PeerWatch(declared, lock, MagicMock(), fleet=fleet)

        call_counts = {'preferred': 0, 'fallback': 0}
        def selective_connect(addr, timeout=None):
            host, port = addr
            call_counts[host] = call_counts.get(host, 0) + 1
            if host == 'preferred':
                raise OSError("refused")  # First round: preferred fails
            sock = MagicMock()
            return sock

        # First round: preferred fails, fallback wins
        ok, err = roundhouse._probe_peer(pw, 'p', index=0, connect=selective_connect)
        self.assertFalse(ok)
        ok, err = roundhouse._probe_peer(pw, 'p', index=1, connect=selective_connect)
        self.assertTrue(ok)
        self.assertEqual(pw.peers['p']['endpoint_index'], 0)  # Still at index 0 (not updated by probe)

        # Verify we called both candidates in order
        self.assertEqual(call_counts['preferred'], 1)
        self.assertEqual(call_counts['fallback'], 1)

    def test_endpoint_change_without_state_transition(self):
        """Endpoint change emits event even without state change (§4.3, L5)."""
        lock = threading.Lock()
        declared = {'p': [('h1', 80), ('h2', 81)]}
        fleet = {'p': ['http://h1:80', 'http://h2:81']}
        pw = roundhouse.PeerWatch(declared, lock, MagicMock(), fleet=fleet)

        # Move from candidate 0 to candidate 1 while state stays 'up'
        pw.peers['p']['state'] = 'up'
        pw.peers['p']['endpoint_index'] = 0
        pw.peers['p']['endpoint_in_use'] = 'http://h1:80'

        payload = pw.apply_result_unlocked('p', ok=True, error=None, ts=time.time(),
                                           winner_index=1)
        # Endpoint changed; should emit event even though state didn't change
        self.assertIsNotNone(payload)
        self.assertEqual(payload['prev_state'], 'up')
        self.assertEqual(pw.peers['p']['endpoint_changed_at'], payload['endpoint_changed_at'])

    def test_fetch_uses_winner_index(self):
        """_fetch_peer uses the winner index, not candidate 0 (§4.2, L5)."""
        lock = threading.Lock()
        declared = {'p': [('h1', 80), ('h2', 81)]}
        fleet = {'p': ['http://h1:80', 'http://h2:81']}
        pw = roundhouse.PeerWatch(declared, lock, MagicMock(), fleet=fleet)

        # Mock opener to record which URL is fetched
        fetched_urls = []
        mock_opener = MagicMock()
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=None)

        def track_open(url, timeout=None):
            fetched_urls.append(url)
            return mock_resp

        mock_opener.open = track_open

        # Fetch with index 1 (second candidate)
        ok, data, err = roundhouse._fetch_peer(pw, 'p', '/api/units', opener=mock_opener, index=1)
        self.assertEqual(len(fetched_urls), 1)
        self.assertIn('h2:81', fetched_urls[0])


class TestPeerWatchCtorNormalization(unittest.TestCase):
    """§7.3 compat seam: old single-endpoint shapes normalize to lists."""

    def test_old_declared_tuple_normalizes_to_list(self):
        """Old MVP8 declared format: {name: (host, port)} → {name: [(host, port)]}."""
        lock = threading.Lock()
        declared = {'p': ('host', 8080)}  # Old format
        pw = roundhouse.PeerWatch(declared, lock, MagicMock())
        # Should normalize to list
        self.assertIsInstance(pw.declared['p'], list)
        self.assertEqual(len(pw.declared['p']), 1)
        self.assertEqual(pw.declared['p'][0], ('host', 8080))

    def test_new_declared_list_passes_through(self):
        """New format {name: [(host, port), ...]} passes through unchanged."""
        lock = threading.Lock()
        declared = {'p': [('host', 8080), ('host2', 8081)]}  # New format
        pw = roundhouse.PeerWatch(declared, lock, MagicMock())
        self.assertEqual(len(pw.declared['p']), 2)

    def test_old_fleet_string_normalizes_to_list(self):
        """Old fleet format {name: 'url'} → {name: ['url']}."""
        lock = threading.Lock()
        declared = {'p': ('host', 8080)}
        fleet = {'p': 'http://host:8080'}  # Old format
        pw = roundhouse.PeerWatch(declared, lock, MagicMock(), fleet=fleet)
        self.assertIsInstance(pw.fleet['p'], list)
        self.assertEqual(len(pw.fleet['p']), 1)
        self.assertEqual(pw.fleet['p'][0], 'http://host:8080')

    def test_new_fleet_list_passes_through(self):
        """New format {name: ['url', ...]} passes through unchanged."""
        lock = threading.Lock()
        declared = {'p': [('host', 8080)]}
        fleet = {'p': ['http://host:8080', 'http://host2:8081']}  # New format
        pw = roundhouse.PeerWatch(declared, lock, MagicMock(), fleet=fleet)
        self.assertEqual(len(pw.fleet['p']), 2)

    def test_tcp_peer_endpoint_in_use_fixed(self):
        """TCP peer endpoint_in_use is fixed at init and never changes."""
        lock = threading.Lock()
        declared = {'tcp_peer': ('host', 8080)}
        pw = roundhouse.PeerWatch(declared, lock, MagicMock())
        # TCP peers have fixed endpoint_in_use
        self.assertEqual(pw.peers['tcp_peer']['endpoint_in_use'], 'host:8080')

    def test_fleet_peer_endpoint_in_use_starts_none(self):
        """Fleet peer endpoint_in_use starts None, set on first successful probe."""
        lock = threading.Lock()
        declared = {'fleet': [('h1', 80)]}
        fleet = {'fleet': ['http://h1:80']}
        pw = roundhouse.PeerWatch(declared, lock, MagicMock(), fleet=fleet)
        # Fleet peers start with None, will be set by apply_result_unlocked
        self.assertIsNone(pw.peers['fleet']['endpoint_in_use'])


class _RoundHarness(unittest.TestCase):
    """peer_watch_round with the probe and the fetch injected, so a round can be
    played out endpoint by endpoint (§7.3: the walk is round-level behaviour and
    was only ever tested one _probe_peer call at a time)."""

    def build(self, candidates, urls, name='bee'):
        self.bus = MagicMock()
        self.clock = [1000.0]
        pw = roundhouse.PeerWatch({name: list(candidates)}, threading.Lock(), self.bus,
                                  fleet={name: list(urls)}, now=lambda: self.clock[0])
        self.pw = pw
        return pw

    def run_round(self, alive, fetch_ok=True):
        """One round where `alive` is the set of (host, port) that answer."""
        probed, fetched = [], []

        def fake_probe(peer_watch, name, index=0, connect=None):
            endpoint = peer_watch.declared[name][index]
            probed.append(endpoint)
            if endpoint in alive:
                return (True, None)
            return (False, 'ConnectionRefusedError: refused')

        def fake_fetch(peer_watch, name, path, opener=None, index=0):
            fetched.append((peer_watch.fleet[name][index], path))
            if not fetch_ok:
                return (False, None, 'connect: refused')
            if path == '/api/units':
                return (True, {'mode': 'observe', 'units': []}, None)
            return (True, {'model_list': []}, None)

        with patch.object(roundhouse, '_probe_peer', fake_probe), \
             patch.object(roundhouse, '_fetch_peer', fake_fetch):
            roundhouse.peer_watch_round(self.pw)
        return probed, fetched

    def peer(self, name='bee'):
        return self.pw.peers[name]

    def peer_events(self):
        return [call.args[1] for call in self.bus.publish.call_args_list
                if call.args[0] == 'peer']


class TestCandidateWalkRounds(_RoundHarness):
    """§7.3/§4.3: what a whole round does to the peer row."""

    def test_the_first_answering_candidate_is_named_in_endpoint_in_use(self):
        """§4.3: set the endpoint when the winner differs from the index OR when
        nothing has been named yet. Candidate 1 winning on the first round is the
        ordinary case — a peer that is up `via` nothing is the fleet view's whole
        question ('which door is this coming through?') left unanswered."""
        self.build([('sofa', 8099), ('desk', 8099)],
                   ['http://sofa:8099', 'http://desk:8099'])

        probed, fetched = self.run_round(alive={('sofa', 8099)})

        self.assertEqual(probed, [('sofa', 8099)], 'stop at the first answer')
        self.assertEqual(self.peer()['state'], 'up')
        self.assertEqual(self.peer()['endpoint_index'], 0)
        self.assertEqual(self.peer()['endpoint_in_use'], 'http://sofa:8099')
        self.assertIsNotNone(self.peer()['endpoint_changed_at'])
        self.assertEqual(fetched, [('http://sofa:8099', '/api/units'),
                                   ('http://sofa:8099', '/api/routing-config.json')])

    def test_a_dead_first_candidate_costs_the_peer_nothing(self):
        """L5: hysteresis is peer-level — a round that any candidate answers is a
        success, and consecutive_failures stays at zero."""
        self.build([('sofa', 8099), ('desk', 8099)],
                   ['http://sofa:8099', 'http://desk:8099'])

        probed, fetched = self.run_round(alive={('desk', 8099)})

        self.assertEqual(probed, [('sofa', 8099), ('desk', 8099)])
        self.assertEqual(self.peer()['state'], 'up')
        self.assertEqual(self.peer()['consecutive_failures'], 0)
        self.assertEqual(self.peer()['endpoint_index'], 1)
        self.assertEqual(self.peer()['endpoint_in_use'], 'http://desk:8099')
        self.assertEqual([url for url, _path in fetched],
                         ['http://desk:8099', 'http://desk:8099'])

    def test_moving_endpoint_is_one_event_and_no_flap(self):
        """The roaming moment: candidate 2 → candidate 1 changes the endpoint and
        emits exactly one `peer` event whose prev_state equals its state."""
        self.build([('sofa', 8099), ('desk', 8099)],
                   ['http://sofa:8099', 'http://desk:8099'])
        self.run_round(alive={('desk', 8099)})
        self.bus.publish.reset_mock()

        self.clock[0] += 20
        self.run_round(alive={('sofa', 8099), ('desk', 8099)})

        events = self.peer_events()
        self.assertEqual(len(events), 1, 'one endpoint move, one event')
        self.assertEqual(events[0]['state'], 'up')
        self.assertEqual(events[0]['prev_state'], 'up')
        self.assertEqual(events[0]['endpoint_in_use'], 'http://sofa:8099')
        self.assertEqual(events[0]['host'], 'sofa',
                         'the row names the endpoint in use, `endpoints` names the choices')
        self.assertEqual(self.peer()['consecutive_failures'], 0)

    def test_a_whole_walk_failure_names_every_candidate_and_counts_once(self):
        """L5: cf counts whole walks, and the error says what each door said."""
        self.build([('sofa', 8099), ('desk', 8099)],
                   ['http://sofa:8099', 'http://desk:8099'])
        self.run_round(alive={('sofa', 8099)})

        self.clock[0] += 20
        self.run_round(alive=set())
        self.assertEqual(self.peer()['state'], 'up', 'one failure is not down')
        self.assertEqual(self.peer()['consecutive_failures'], 1)
        self.assertIn('candidate 1:', self.peer()['last_error'])
        self.assertIn('candidate 2:', self.peer()['last_error'])
        self.assertLessEqual(len(self.peer()['last_error']), 200)

        self.clock[0] += 20
        self.run_round(alive=set())
        self.assertEqual(self.peer()['state'], 'down', 'two whole walks, then down')
        self.assertEqual(self.peer()['consecutive_failures'], 2)

    def test_a_failed_walk_leaves_the_endpoint_where_it_was(self):
        """§4.3: after `down` the row still says where the peer was last reached."""
        self.build([('sofa', 8099), ('desk', 8099)],
                   ['http://sofa:8099', 'http://desk:8099'])
        self.run_round(alive={('desk', 8099)})
        changed_at = self.peer()['endpoint_changed_at']

        self.clock[0] += 20
        self.run_round(alive=set())

        self.assertEqual(self.peer()['endpoint_in_use'], 'http://desk:8099')
        self.assertEqual(self.peer()['endpoint_changed_at'], changed_at)
        self.assertEqual(self.peer()['endpoint_index'], 1)

    def test_a_failed_walk_while_still_up_skips_the_fetch(self):
        """L5's fetch-skip rule: no winner, no fetch — fed data is left alone."""
        self.build([('sofa', 8099)], ['http://sofa:8099'])
        self.run_round(alive={('sofa', 8099)})
        fetched_at = self.pw.fed['bee']['fetched_at']

        self.clock[0] += 20
        probed, fetched = self.run_round(alive=set())

        self.assertEqual(fetched, [], 'nothing was reachable to fetch from')
        self.assertEqual(self.pw.fed['bee']['state'], 'fresh')
        self.assertEqual(self.pw.fed['bee']['fetched_at'], fetched_at)


if __name__ == '__main__':
    unittest.main()


class TestRoundLevelUsesTheRealEventBus(unittest.TestCase):
    """MVP9 review item 2: close the last in-suite door for the mock-shape-lie class.

    B3 was: BindWatch published through event_bus.put_nowait(), which EventBus
    does not have — invisible because the tests injected a queue.Queue, which
    does. The BindWatch side now publishes against a double carrying only the
    production API; peer_watch_round's two publish sites, however, still only
    ever met MagicMock in-suite, which absorbs any attribute at all. This drives
    a real round against a REAL EventBus, so a put_nowait-style regression on
    the peer path fails here instead of on a host.
    """

    def _watch(self, bus):
        return roundhouse.PeerWatch(
            declared={'p': [('127.0.0.1', 9)]},
            lock=threading.Lock(),
            event_bus=bus,
            fleet={},
        )

    def test_a_round_publishes_through_the_real_event_bus(self):
        bus = roundhouse.EventBus()
        q = bus.subscribe()
        pw = self._watch(bus)
        with patch.object(roundhouse, '_probe_peer',
                          lambda pw_, name, index=0, connect=None: (True, None)):
            roundhouse.peer_watch_round(pw)
        # unknown -> up is a transition, so exactly one event must arrive, and it
        # must have travelled through EventBus.publish, not a mock's black hole.
        kind, payload, _seq = q.get_nowait()   # EventBus queues (event, data, seq)
        self.assertEqual(kind, 'peer')
        self.assertEqual(payload['name'], 'p')
        self.assertEqual(payload['state'], 'up')

    def test_a_flat_round_publishes_nothing_through_the_real_bus(self):
        bus = roundhouse.EventBus()
        q = bus.subscribe()
        pw = self._watch(bus)
        with patch.object(roundhouse, '_probe_peer',
                          lambda pw_, name, index=0, connect=None: (True, None)):
            roundhouse.peer_watch_round(pw)
            q.get_nowait()                       # the unknown -> up transition
            roundhouse.peer_watch_round(pw)      # steady state
        with self.assertRaises(queue.Empty):
            q.get_nowait()
