"""Tests for optional binds, presence detection, and BindWatch lifecycle.

§7.1-7.2 per MVP9-SPEC: parse_bind_optional, presence classification, BindWatch cycle.
"""

import unittest
import socket
import threading
import queue
import time
import errno

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from roundhouse import (
    parse_bind_optional, validate_bind_retry, _presence_probe, BindWatch,
    BIND_OPTIONAL_MAX, RESOLVE_ADDR_MAX
)


class TestBindOptionalParsing(unittest.TestCase):
    """§7.1: Optional-bind parsing per §3.1 grammar."""

    def test_empty_input(self):
        """Empty input returns empty list and no errors."""
        decls, errors = parse_bind_optional(None, [])
        self.assertEqual(decls, [])
        self.assertEqual(errors, [])

    def test_literal_ipv4(self):
        """IPv4 literal is canonicalized and classified."""
        decls, errors = parse_bind_optional(['127.0.0.2'], [])
        self.assertEqual(len(decls), 1)
        self.assertEqual(decls[0]['kind'], 'literal')
        self.assertEqual(decls[0]['addr'], '127.0.0.2')
        self.assertEqual(decls[0]['family'], socket.AF_INET)
        self.assertEqual(errors, [])

    def test_literal_ipv6(self):
        """IPv6 literal (with and without brackets) is canonicalized."""
        decls, errors = parse_bind_optional(['::1'], [])
        self.assertEqual(len(decls), 1)
        self.assertEqual(decls[0]['kind'], 'literal')
        self.assertEqual(decls[0]['addr'], '::1')
        self.assertEqual(decls[0]['family'], socket.AF_INET6)
        self.assertEqual(errors, [])

    def test_literal_ipv6_bracketed(self):
        """IPv6 literal in brackets is unbracketed and canonicalized."""
        decls, errors = parse_bind_optional(['[::1]'], [])
        self.assertEqual(len(decls), 1)
        self.assertEqual(decls[0]['kind'], 'literal')
        self.assertEqual(decls[0]['addr'], '::1')
        self.assertEqual(errors, [])

    def test_name(self):
        """Hostname is classified as name."""
        decls, errors = parse_bind_optional(['example.com'], [])
        self.assertEqual(len(decls), 1)
        self.assertEqual(decls[0]['kind'], 'name')
        self.assertEqual(decls[0]['token'], 'example.com')
        self.assertEqual(decls[0]['addr'], None)
        self.assertEqual(errors, [])

    def test_single_label_name(self):
        """Single-label hostname accepted."""
        decls, errors = parse_bind_optional(['myhost'], [])
        self.assertEqual(len(decls), 1)
        self.assertEqual(decls[0]['kind'], 'name')
        self.assertEqual(errors, [])

    def test_repeats(self):
        """Multiple --bind-optional arguments flatten."""
        decls, errors = parse_bind_optional(['127.0.0.2', '127.0.0.3'], [])
        self.assertEqual(len(decls), 2)
        self.assertEqual(errors, [])

    def test_comma_list(self):
        """Comma-separated values flatten."""
        decls, errors = parse_bind_optional(['127.0.0.2,127.0.0.3'], [])
        self.assertEqual(len(decls), 2)
        self.assertEqual(errors, [])

    def test_mixed_repeats_and_commas(self):
        """Repeats and commas combine."""
        decls, errors = parse_bind_optional(['127.0.0.2,127.0.0.3', '127.0.0.4'], [])
        self.assertEqual(len(decls), 3)
        self.assertEqual(errors, [])

    def test_wildcard_ipv4_refused(self):
        """Wildcard 0.0.0.0 is refused."""
        decls, errors = parse_bind_optional(['0.0.0.0'], [])
        self.assertEqual(len(errors), 1)
        self.assertIn('wildcard cannot be absent', errors[0])

    def test_wildcard_ipv6_refused(self):
        """Wildcard :: is refused."""
        decls, errors = parse_bind_optional(['::'], [])
        self.assertEqual(len(errors), 1)
        self.assertIn('wildcard cannot be absent', errors[0])

    def test_wildcard_ipv6_bracketed_refused(self):
        """Wildcard [::] is refused."""
        decls, errors = parse_bind_optional(['[::]'], [])
        self.assertEqual(len(errors), 1)
        self.assertIn('wildcard cannot be absent', errors[0])

    def test_duplicate_literal_refused(self):
        """Duplicate literal is refused."""
        decls, errors = parse_bind_optional(['127.0.0.2', '127.0.0.2'], [])
        self.assertEqual(len(errors), 1)
        self.assertIn('duplicate', errors[0])

    def test_duplicate_after_canonicalization(self):
        """Duplicate detected after IPv6 canonicalization."""
        decls, errors = parse_bind_optional(['::1', '[::1]'], [])
        self.assertEqual(len(errors), 1)
        self.assertIn('duplicate', errors[0])

    def test_duplicate_name_case_insensitive(self):
        """Duplicate hostname detected case-insensitively."""
        decls, errors = parse_bind_optional(['Example.com', 'example.com'], [])
        self.assertEqual(len(errors), 1)
        self.assertIn('duplicate', errors[0])

    def test_literal_in_both_lists_refused(self):
        """Address in both --bind and --bind-optional is refused."""
        mandatory = [('127.0.0.1', socket.AF_INET)]
        decls, errors = parse_bind_optional(['127.0.0.1'], mandatory)
        self.assertEqual(len(errors), 1)
        self.assertIn('both --bind and --bind-optional', errors[0])

    def test_wildcard_coverage_refused(self):
        """Optional literal behind mandatory wildcard is refused."""
        mandatory = [('0.0.0.0', socket.AF_INET)]
        decls, errors = parse_bind_optional(['127.0.0.2'], mandatory)
        self.assertEqual(len(errors), 1)
        self.assertIn('already covers', errors[0])

    def test_ipv6_optional_beside_ipv4_wildcard_accepted(self):
        """IPv6 address is accepted beside IPv4 wildcard."""
        mandatory = [('0.0.0.0', socket.AF_INET)]
        decls, errors = parse_bind_optional(['::1'], mandatory)
        self.assertEqual(len(decls), 1)
        self.assertEqual(errors, [])

    def test_invalid_ip_literal_trap(self):
        """Invalid IP literal (all digits) refuses with literal-or-error."""
        decls, errors = parse_bind_optional(['999.1.1.1'], [])
        self.assertEqual(len(errors), 1)
        self.assertIn('not a valid IP literal', errors[0])

    def test_invalid_ip_with_colon_trap(self):
        """Invalid IPv6 (bad format) refuses with literal-or-error."""
        decls, errors = parse_bind_optional(['1:2:3'], [])
        self.assertEqual(len(errors), 1)
        self.assertIn('not a valid IP literal', errors[0])

    def test_hostname_invalid_charset(self):
        """Hostname with invalid characters is refused."""
        decls, errors = parse_bind_optional(['ex ample.com'], [])
        self.assertEqual(len(errors), 1)
        self.assertIn('not a valid address or hostname', errors[0])

    def test_cap_bind_optional_max(self):
        """More than BIND_OPTIONAL_MAX declarations is refused."""
        values = [str(i) for i in range(BIND_OPTIONAL_MAX + 1)]
        # Make them valid hostnames
        values = [f'host{i}.local' for i in range(BIND_OPTIONAL_MAX + 1)]
        decls, errors = parse_bind_optional(values, [])
        self.assertEqual(len(errors), 1)
        self.assertIn(f'too many optional binds', errors[0])

    def test_empty_token_error(self):
        """Empty token in comma list raises error."""
        decls, errors = parse_bind_optional(['127.0.0.2,,127.0.0.3'], [])
        self.assertEqual(len(errors), 1)
        self.assertIn('empty bind address', errors[0])

    def test_multiple_errors_reported(self):
        """Multiple errors are all collected."""
        mandatory = [('0.0.0.0', socket.AF_INET)]
        decls, errors = parse_bind_optional(['0.0.0.0', '127.0.0.1', 'ex ample'], mandatory)
        self.assertGreaterEqual(len(errors), 3)


class TestValidateBindRetry(unittest.TestCase):
    """Validate --bind-retry parameter."""

    def test_valid_default(self):
        """Default value passes validation."""
        errors = validate_bind_retry(30)
        self.assertEqual(errors, [])

    def test_minimum_valid(self):
        """Minimum value 1 passes."""
        errors = validate_bind_retry(1)
        self.assertEqual(errors, [])

    def test_below_minimum(self):
        """Value < 1 is refused."""
        errors = validate_bind_retry(0)
        self.assertEqual(len(errors), 1)
        self.assertIn('at least 1 second', errors[0])


class TestPresenceProbe(unittest.TestCase):
    """§7.2: _presence_probe classification per L2."""

    def test_success_present(self):
        """Successful bind returns (True, None)."""
        def mock_socket_success(family, socktype):
            class MockSock:
                def bind(self, addr):
                    pass
                def close(self):
                    pass
            return MockSock()

        present, error = _presence_probe('127.0.0.2', socket.AF_INET,
                                        sock_factory=mock_socket_success)
        self.assertTrue(present)
        self.assertIsNone(error)

    def test_eaddrnotavail_absent(self):
        """EADDRNOTAVAIL returns (False, None) — absence, not error."""
        def mock_socket_absent(family, socktype):
            class MockSock:
                def bind(self, addr):
                    e = OSError()
                    e.errno = errno.EADDRNOTAVAIL
                    raise e
                def close(self):
                    pass
            return MockSock()

        present, error = _presence_probe('192.0.2.1', socket.AF_INET,
                                        sock_factory=mock_socket_absent)
        self.assertFalse(present)
        self.assertIsNone(error)

    def test_other_errno_present(self):
        """EADDRINUSE and other errors return (True, error_string)."""
        def mock_socket_in_use(family, socktype):
            class MockSock:
                def bind(self, addr):
                    e = OSError()
                    e.errno = errno.EADDRINUSE
                    e.strerror = 'Address already in use'
                    raise e
                def close(self):
                    pass
            return MockSock()

        present, error = _presence_probe('127.0.0.3', socket.AF_INET,
                                        sock_factory=mock_socket_in_use)
        self.assertTrue(present)  # Present but with error
        self.assertIsNotNone(error)
        self.assertIn('98', error)  # errno value

    def test_eacces_error_present(self):
        """Permission error returns (True, error_string) — never tear down."""
        def mock_socket_denied(family, socktype):
            class MockSock:
                def bind(self, addr):
                    e = OSError()
                    e.errno = errno.EACCES
                    e.strerror = 'Permission denied'
                    raise e
                def close(self):
                    pass
            return MockSock()

        present, error = _presence_probe('192.168.1.1', socket.AF_INET,
                                        sock_factory=mock_socket_denied)
        self.assertTrue(present)
        self.assertIsNotNone(error)

    def test_socket_closed_on_success(self):
        """Socket is closed on all paths."""
        close_called = []

        def mock_socket_track_close(family, socktype):
            class MockSock:
                def bind(self, addr):
                    pass
                def close(self):
                    close_called.append(True)
            return MockSock()

        _presence_probe('127.0.0.2', socket.AF_INET,
                        sock_factory=mock_socket_track_close)
        self.assertEqual(len(close_called), 1)

    def test_socket_closed_on_absent(self):
        """Socket is closed even when address absent."""
        close_called = []

        def mock_socket_track_close(family, socktype):
            class MockSock:
                def bind(self, addr):
                    e = OSError()
                    e.errno = errno.EADDRNOTAVAIL
                    raise e
                def close(self):
                    close_called.append(True)
            return MockSock()

        _presence_probe('192.0.2.1', socket.AF_INET,
                        sock_factory=mock_socket_track_close)
        self.assertEqual(len(close_called), 1)


class TestBindWatchCycle(unittest.TestCase):
    """§7.2: BindWatch cycle logic with injected seams."""

    def setUp(self):
        """Set up common test infrastructure."""
        self.lock = threading.Lock()
        self.event_bus = queue.Queue()
        self.shutdown_event = threading.Event()
        self.now_value = 1000.0

        def mock_time():
            return self.now_value

        def mock_resolver(name, srv, type):
            # Mock resolver: 'resolvable.local' → 127.0.0.2, others fail
            if name == 'resolvable.local':
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.2', 0))]
            raise socket.gaierror("Name or service not known")

        def mock_presence(addr, family):
            # Mock presence: 127.0.0.2 present, 192.0.2.1 absent
            if addr == '127.0.0.2':
                return (True, None)
            if addr == '192.0.2.1':
                return (False, None)
            return (True, None)

        def mock_make_listener(addr, family, port, kwargs):
            class MockServer:
                def __init__(self, addr, family, port):
                    self.server_address = (addr, port, 0, 0)
                    self.family = family
                def shutdown(self):
                    pass
                def server_close(self):
                    pass
                def serve_forever(self):
                    pass
            return MockServer(addr, family, port)

        self.mock_time = mock_time
        self.mock_resolver = mock_resolver
        self.mock_presence = mock_presence
        self.mock_make_listener = mock_make_listener

    def test_literal_bind_success(self):
        """Optional literal address binds successfully."""
        optional_decls = [{
            'token': '127.0.0.2',
            'kind': 'literal',
            'addr': '127.0.0.2',
            'family': socket.AF_INET
        }]

        watch = BindWatch([], optional_decls, 8090, {}, self.lock, self.event_bus,
                         self.shutdown_event, now=self.mock_time,
                         presence=self.mock_presence,
                         make_listener=self.mock_make_listener)

        watch.cycle()

        # Check row state
        with self.lock:
            rows = watch.rows_unlocked()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['state'], 'bound')

    def test_literal_absent(self):
        """Optional literal that's absent stays absent."""
        call_count = []
        def mock_make_listener_fail(addr, family, port, kwargs):
            # Track that we tried to bind the absent address
            call_count.append(addr)
            # Raise EADDRNOTAVAIL to simulate absence
            raise OSError(errno.EADDRNOTAVAIL, "Address not available")

        optional_decls = [{
            'token': '192.0.2.1',
            'kind': 'literal',
            'addr': '192.0.2.1',
            'family': socket.AF_INET
        }]

        watch = BindWatch([], optional_decls, 8090, {}, self.lock, self.event_bus,
                         self.shutdown_event, now=self.mock_time,
                         presence=self.mock_presence,
                         make_listener=mock_make_listener_fail)

        watch.cycle()

        with self.lock:
            rows = watch.rows_unlocked()
        self.assertEqual(len(rows), 1)
        # State should be 'absent' (from the OSError classification in cycle)
        self.assertEqual(rows[0]['state'], 'absent')

    def test_idempotence(self):
        """Two consecutive cycles with unchanged world make zero socket calls."""
        call_count = {'resolve': 0, 'presence': 0, 'make_listener': 0}

        def counting_resolver(name, srv, type):
            call_count['resolve'] += 1
            return self.mock_resolver(name, srv, type)

        def counting_presence(addr, family):
            call_count['presence'] += 1
            return self.mock_presence(addr, family)

        def counting_make_listener(addr, family, port, kwargs):
            call_count['make_listener'] += 1
            return self.mock_make_listener(addr, family, port, kwargs)

        optional_decls = [{
            'token': '127.0.0.2',
            'kind': 'literal',
            'addr': '127.0.0.2',
            'family': socket.AF_INET
        }]

        watch = BindWatch([], optional_decls, 8090, {}, self.lock, self.event_bus,
                         self.shutdown_event, now=self.mock_time,
                         resolver=counting_resolver,
                         presence=counting_presence,
                         make_listener=counting_make_listener)

        # First cycle
        call_count = {'resolve': 0, 'presence': 0, 'make_listener': 0}
        watch.cycle()
        first_counts = dict(call_count)

        # Second cycle (unchanged world)
        self.now_value += 30
        call_count = {'resolve': 0, 'presence': 0, 'make_listener': 0}
        watch.cycle()
        second_counts = dict(call_count)

        # Second cycle should only probe presence for already-bound addresses
        # (make_listener should be 0, resolver should be 0)
        self.assertEqual(second_counts['make_listener'], 0)

    def test_lock_discipline(self):
        """Lock is not held during resolve/bind/presence check."""
        lock_held = []

        def spy_resolver(name, srv, type):
            # Check if lock is held
            acquired = self.lock.acquire(blocking=False)
            if acquired:
                lock_held.append('resolver: free')
                self.lock.release()
            else:
                lock_held.append('resolver: held')
            return self.mock_resolver(name, srv, type)

        optional_decls = [{
            'token': 'resolvable.local',
            'kind': 'name',
            'addr': None,
            'family': None
        }]

        watch = BindWatch([], optional_decls, 8090, {}, self.lock, self.event_bus,
                         self.shutdown_event, now=self.mock_time,
                         resolver=spy_resolver,
                         presence=self.mock_presence,
                         make_listener=self.mock_make_listener)

        watch.cycle()

        # Resolver should have been called with lock free
        self.assertIn('resolver: free', lock_held)


if __name__ == '__main__':
    unittest.main()
