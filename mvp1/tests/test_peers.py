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


if __name__ == '__main__':
    unittest.main()
