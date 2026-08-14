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
    BIND_OPTIONAL_MAX, RESOLVE_ADDR_MAX, EventBus
)


class RecordingBus:
    """An event bus with the production API and nothing else.

    The bind watch publishes through `EventBus.publish(event, data)` exactly as
    the peer watch does. A test double that also answers `put_nowait` would let a
    queue-shaped call slip past every cycle test and only fail in the field, so
    this double carries `publish` and nothing else on purpose.
    """

    def __init__(self):
        self.events = []

    def publish(self, event, data):
        self.events.append((event, data))

    def listener_events(self):
        return [data for name, data in self.events if name == 'listener']


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
        self.event_bus = RecordingBus()
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


class _RecordingServer:
    """A listener stand-in that records the teardown calls made against it."""

    def __init__(self, addr, family, port):
        self.server_address = (addr, port, 0, 0)
        self.family = family
        self.calls = []

    def shutdown(self):
        self.calls.append('shutdown')

    def server_close(self):
        self.calls.append('server_close')

    def serve_forever(self):
        self.calls.append('serve_forever')


class TestBindWatchCycleGaps(unittest.TestCase):
    """§7.2 legs the first pass left unproven: the event API, teardown, the errno
    table on a name row, the resolved-set flip, filtering, caps, and the race gate."""

    def setUp(self):
        self.lock = threading.Lock()
        self.bus = RecordingBus()
        self.shutdown_event = threading.Event()
        self.now_value = 1000.0
        self.built = []

    def now(self):
        return self.now_value

    def make_listener(self, addr, family, port, kwargs):
        srv = _RecordingServer(addr, family, port)
        self.built.append(srv)
        return srv

    @staticmethod
    def literal(addr, family=socket.AF_INET):
        return {'token': addr, 'kind': 'literal', 'addr': addr, 'family': family}

    @staticmethod
    def name(token):
        return {'token': token, 'kind': 'name', 'addr': None, 'family': None}

    def watch(self, optional_decls, mandatory=(), **kw):
        kw.setdefault('now', self.now)
        kw.setdefault('make_listener', self.make_listener)
        kw.setdefault('presence', lambda addr, family: (True, None))
        return BindWatch(list(mandatory), optional_decls, 8090, {}, self.lock,
                         self.bus, self.shutdown_event, **kw)

    def rows(self, watch):
        with self.lock:
            return watch.rows_unlocked()

    # ---- the event API is the production one ----

    def test_the_watch_publishes_through_the_real_event_bus(self):
        """The bind watch shares the SSE bus with the peer watch (§3.3 phase 4).

        A queue-shaped `put_nowait` passes every test that injects a Queue and
        then raises AttributeError against the real EventBus on the first
        transition — which is the first successful bind, so the watch thread dies
        at startup and the milestone's whole reconciliation loop never runs again.
        """
        bus = EventBus()
        subscriber = bus.subscribe()
        watch = BindWatch([], [self.literal('127.0.0.2')], 8090, {}, self.lock, bus,
                          self.shutdown_event, now=self.now,
                          presence=lambda a, f: (True, None),
                          make_listener=self.make_listener)

        watch.cycle()

        event, data, event_id = subscriber.get_nowait()
        self.assertEqual(event, 'listener')
        self.assertEqual(data['addr'], '127.0.0.2')
        self.assertEqual(data['state'], 'bound')
        self.assertEqual(data['prev_state'], 'absent')

    # ---- teardown ----

    def test_disappearance_tears_down_exactly_that_listener(self):
        """Presence says gone → that server is shut down, closed, and the row flips."""
        present = {'127.0.0.2': True, '127.0.0.3': True}

        def only_what_is_here(addr, family, port, kwargs):
            # One world, two probes: an address that presence calls gone also
            # refuses a fresh bind, exactly as the kernel behaves.
            if not present[addr]:
                raise OSError(errno.EADDRNOTAVAIL, 'Cannot assign requested address')
            return self.make_listener(addr, family, port, kwargs)

        watch = self.watch([self.literal('127.0.0.2'), self.literal('127.0.0.3')],
                           presence=lambda addr, family: (present[addr], None),
                           make_listener=only_what_is_here)

        watch.cycle()
        self.assertEqual([r['state'] for r in self.rows(watch)], ['bound', 'bound'])

        present['127.0.0.2'] = False
        self.now_value += 30
        self.bus.events.clear()
        watch.cycle()

        gone, kept = self.built[0], self.built[1]
        teardown = [c for c in gone.calls if c in ('shutdown', 'server_close')]
        self.assertEqual(teardown, ['shutdown', 'server_close'],
                         'stop serving first, then release the socket')
        self.assertNotIn('server_close', kept.calls)
        self.assertEqual([r['state'] for r in self.rows(watch)], ['absent', 'bound'])
        events = self.bus.listener_events()
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]['addr'], events[0]['prev_state']), ('127.0.0.2', 'bound'))

    # ---- the errno table on the rows ----

    def test_eaddrinuse_is_an_error_row_and_the_next_cycle_retries(self):
        """L2: a real errno is per-address error, never fatal, and is retried."""
        attempts = []

        def occupied(addr, family, port, kwargs):
            attempts.append(addr)
            if len(attempts) == 1:
                raise OSError(errno.EADDRINUSE, 'Address already in use')
            return self.make_listener(addr, family, port, kwargs)

        watch = self.watch([self.literal('127.0.0.2')], make_listener=occupied)
        watch.cycle()

        row = self.rows(watch)[0]
        self.assertEqual(row['state'], 'error')
        self.assertIn(str(errno.EADDRINUSE), row['last_error'])

        self.now_value += 30
        watch.cycle()
        self.assertEqual(len(attempts), 2, 'an error row must be retried next cycle')
        self.assertEqual(self.rows(watch)[0]['state'], 'bound')
        self.assertIsNone(self.rows(watch)[0]['last_error'])

    def test_a_gaierror_lands_its_text_on_the_name_row(self):
        """L2: an unresolvable name contributes nothing and says why."""
        def dead_resolver(name, service, type):
            raise socket.gaierror(-2, 'Name or service not known')

        watch = self.watch([self.name('ampere.vpn')], resolver=dead_resolver)
        watch.cycle()

        row = self.rows(watch)[0]
        self.assertEqual(row['state'], 'absent')
        self.assertEqual(row['resolved'], [])
        self.assertIsNotNone(row['last_error'],
                             'the resolver failure is the only clue the operator gets')
        self.assertIn('Name or service not known', row['last_error'])

    def test_a_name_resolving_to_an_absent_address_reads_absent_not_error(self):
        """The milestone's own case: ampere.vpn still resolves on the sofa, but the
        address is not on this machine. EADDRNOTAVAIL is absence, on name rows too."""
        def resolver(name, service, type):
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, '',
                     ('fd96:cafe:cafe::1000', 0, 0, 0))]

        def not_here(addr, family, port, kwargs):
            raise OSError(errno.EADDRNOTAVAIL, 'Cannot assign requested address')

        watch = self.watch([self.name('ampere.vpn')], resolver=resolver,
                           make_listener=not_here)
        watch.cycle()

        row = self.rows(watch)[0]
        self.assertEqual(row['state'], 'absent')
        self.assertIsNone(row['last_error'])
        self.assertEqual(self.bus.listener_events(), [],
                         'absence at startup is the resting state, not an event storm')

    # ---- the roaming moment ----

    def test_a_resolution_change_publishes_one_resolved_flip(self):
        """L4: a bound name row whose resolved set changed is an event of its own —
        the address moved, the peer did not."""
        current = ['127.0.0.2']

        def resolver(name, service, type):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (a, 0)) for a in current]

        watch = self.watch([self.name('roaming.local')], resolver=resolver)
        watch.cycle()
        first = self.rows(watch)[0]
        self.assertEqual(first['state'], 'bound')
        self.assertEqual(first['resolved'], ['127.0.0.2'])

        self.bus.events.clear()
        current[:] = ['127.0.0.3']
        self.now_value += 30
        watch.cycle()

        row = self.rows(watch)[0]
        self.assertEqual(row['state'], 'bound', 'the door moved; the row never left')
        self.assertEqual(row['resolved'], ['127.0.0.3'])
        events = self.bus.listener_events()
        self.assertEqual(len(events), 1, 'exactly one resolved-flip event')
        self.assertEqual(events[0]['prev_state'], 'bound')
        self.assertEqual(events[0]['resolved'], ['127.0.0.3'])
        self.assertIn('server_close', self.built[0].calls)

    def test_resolved_names_the_addresses_that_are_actually_bound(self):
        """L4: `resolved` is the currently-bound canonical addresses, not the
        wish-list from the resolver — one of them is a door, the other is DNS."""
        def resolver(name, service, type):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.2', 0)),
                    (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.7', 0))]

        def half_here(addr, family, port, kwargs):
            if addr == '192.0.2.7':
                raise OSError(errno.EADDRNOTAVAIL, 'Cannot assign requested address')
            return self.make_listener(addr, family, port, kwargs)

        watch = self.watch([self.name('half.local')], resolver=resolver,
                           make_listener=half_here)
        watch.cycle()

        row = self.rows(watch)[0]
        self.assertEqual(row['state'], 'bound')
        self.assertEqual(row['resolved'], ['127.0.0.2'])

    # ---- filtering, caps, contest ----

    def test_names_resolving_onto_mandatory_or_a_wildcard_are_skipped(self):
        """§3.3 phase 1: a name already served by a mandatory door is not re-bound
        against ourselves, and a wildcard-covered family is filtered at cycle time."""
        def resolver(name, service, type):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 0)),
                    (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('::1', 0, 0, 0))]

        watch = self.watch([self.name('both.local')],
                           mandatory=[('127.0.0.1', socket.AF_INET), ('::', socket.AF_INET6)])
        watch.cycle()

        self.assertEqual(self.built, [], 'nothing new was bindable')
        row = self.rows(watch)[-1]
        self.assertEqual(row['state'], 'absent')
        self.assertEqual(row['resolved'], [])

    def test_resolve_addr_max_caps_the_addresses_per_name(self):
        """L1: at most RESOLVE_ADDR_MAX addresses per name per cycle, deterministic."""
        def resolver(name, service, type):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (f'127.0.0.{i}', 0))
                    for i in range(2, 2 + RESOLVE_ADDR_MAX + 3)]

        watch = self.watch([self.name('many.local')], resolver=resolver)
        watch.cycle()

        self.assertEqual(len(self.built), RESOLVE_ADDR_MAX)
        self.assertEqual(len(self.rows(watch)[0]['resolved']), RESOLVE_ADDR_MAX)

    def test_a_contested_address_belongs_to_the_first_declaration(self):
        """L1/§3.3: declaration order decides who owns an address claimed twice."""
        def resolver(name, service, type):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.2', 0))]

        watch = self.watch([self.literal('127.0.0.2'), self.name('same.local')],
                           resolver=resolver)
        watch.cycle()

        self.assertEqual(len(self.built), 1,
                         'one address is one listener however many declarations name it')
        rows = self.rows(watch)
        self.assertEqual(rows[0]['state'], 'bound')
        # The name resolves onto the door the literal already opened: both rows report
        # it honestly, and the single listener is the first declaration's to tear down.
        self.assertEqual(rows[1]['resolved'], ['127.0.0.2'])

    def test_a_flat_cycle_publishes_nothing(self):
        """Idempotence, §3.3: an unchanged world is silent on the bus."""
        watch = self.watch([self.literal('127.0.0.2')])
        watch.cycle()
        self.bus.events.clear()
        self.now_value += 30
        watch.cycle()
        self.assertEqual(self.bus.events, [])
        self.assertEqual(len(self.built), 1)

    # ---- shutdown race gate + lock discipline ----

    def test_the_race_gate_closes_a_listener_born_during_shutdown(self):
        """§3.3 phase 3: if shutdown arrived while we were binding, that door is
        closed here — the main thread's close pass has already been by."""
        def late(addr, family, port, kwargs):
            self.shutdown_event.set()
            return self.make_listener(addr, family, port, kwargs)

        watch = self.watch([self.literal('127.0.0.2')], make_listener=late)
        watch.cycle()

        self.assertEqual(self.built[0].calls, ['server_close'])
        self.assertEqual(watch.servers, {})

    def test_the_lock_is_free_during_presence_and_bind(self):
        """Risk #1: no socket work under the watcher lock."""
        seen = []

        def spy_presence(addr, family):
            seen.append(('presence', self._lock_free()))
            return (True, None)

        def spy_listener(addr, family, port, kwargs):
            seen.append(('bind', self._lock_free()))
            return self.make_listener(addr, family, port, kwargs)

        watch = self.watch([self.literal('127.0.0.2')], presence=spy_presence,
                           make_listener=spy_listener)
        watch.cycle()          # binds (no presence probe yet: nothing was bound)
        self.now_value += 30
        watch.cycle()          # probes presence of the bound door

        self.assertIn(('bind', True), seen)
        self.assertIn(('presence', True), seen)

    def _lock_free(self):
        if self.lock.acquire(blocking=False):
            self.lock.release()
            return True
        return False

    def test_rows_unlocked_never_acquires(self):
        """take_snapshot calls this with the non-reentrant lock held (§L4)."""
        watch = self.watch([self.literal('127.0.0.2')])
        watch.cycle()

        done = []

        def caller():
            with self.lock:
                done.append(watch.rows_unlocked())

        t = threading.Thread(target=caller, daemon=True)
        t.start()
        t.join(timeout=5.0)
        self.assertFalse(t.is_alive(), 'rows_unlocked re-acquired the lock and deadlocked')
        self.assertEqual(len(done), 1)


if __name__ == '__main__':
    unittest.main()
