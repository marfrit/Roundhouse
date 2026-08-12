#!/usr/bin/env python3
"""Roundhouse MVP1 Watcher Test Suite

Tests the Watcher class, MemStore, and subprocess gateway functions.
Uses real captured journal samples and systemctl output.
"""

import sys
import os
import unittest
import json
import sqlite3
import tempfile
import socket
from pathlib import Path
from unittest.mock import patch, MagicMock

# Setup path to import roundhouse from parent directory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import roundhouse


class TestSubprocessGates(unittest.TestCase):
    """Test run_ro and spawn_ro_stream subprocess gates."""

    def test_run_ro_rejects_arbitrary_commands(self):
        """run_ro must reject commands other than systemctl/journalctl."""
        with self.assertRaises(ValueError):
            roundhouse.run_ro(["rm", "-rf", "/"])

    def test_run_ro_accepts_systemctl_show(self):
        """run_ro should accept systemctl show."""
        # Mock subprocess.run
        with patch('roundhouse.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(stdout='', returncode=0)
            result = roundhouse.run_ro(["systemctl", "show", "-p", "ActiveState", "--", "test.service"])
            self.assertEqual(result, '')
            mock_run.assert_called_once()

    def test_run_ro_rejects_systemctl_start(self):
        """run_ro must reject write verbs like start."""
        with self.assertRaises(ValueError):
            roundhouse.run_ro(["systemctl", "start", "test.service"])

    def test_run_ro_rejects_systemctl_enable(self):
        """run_ro must reject write verbs like enable."""
        with self.assertRaises(ValueError):
            roundhouse.run_ro(["systemctl", "enable", "test.service"])

    def test_spawn_ro_stream_rejects_arbitrary_commands(self):
        """spawn_ro_stream must reject commands other than systemctl/journalctl."""
        with self.assertRaises(ValueError):
            roundhouse.spawn_ro_stream(["nc", "-l", "8080"])

    def test_spawn_ro_stream_rejects_write_verbs(self):
        """spawn_ro_stream must reject write verbs."""
        with self.assertRaises(ValueError):
            roundhouse.spawn_ro_stream(["systemctl", "restart", "test.service"])

    def test_run_ro_accepts_journalctl(self):
        """run_ro should accept journalctl."""
        with patch('roundhouse.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(stdout='', returncode=0)
            result = roundhouse.run_ro(["journalctl", "--user", "-f"])
            self.assertEqual(result, '')
            mock_run.assert_called_once()


class TestParseShowBlocks(unittest.TestCase):
    """Test parse_show_blocks function."""

    def test_parse_single_block(self):
        """Parse a single systemctl show block."""
        text = "ActiveState=active\nSubState=running"
        result = roundhouse.parse_show_blocks(text, ["test.service"])
        self.assertIn("test.service", result)
        self.assertEqual(result["test.service"]["ActiveState"], "active")
        self.assertEqual(result["test.service"]["SubState"], "running")

    def test_parse_multiple_blocks(self):
        """Parse multiple blocks separated by blank lines."""
        text = """ActiveState=active
SubState=running

ActiveState=inactive
SubState=dead"""
        result = roundhouse.parse_show_blocks(text, ["svc1.service", "svc2.service"])
        self.assertEqual(result["svc1.service"]["ActiveState"], "active")
        self.assertEqual(result["svc2.service"]["ActiveState"], "inactive")

    def test_parse_real_systemctl_show(self):
        """Parse real systemctl show output from fixtures."""
        repo_root = Path(__file__).resolve().parents[2]
        sample_file = repo_root / "mvp1" / "tests" / "journal_samples" / "systemctl-show.txt"

        if sample_file.exists():
            with open(sample_file, 'r') as f:
                text = f.read()

            result = roundhouse.parse_show_blocks(
                text,
                ["qwen3.6-coding.service", "llama-task.service", "llama-server-qwen35-npu.service"]
            )

            # Check qwen3.6-coding
            self.assertIn("qwen3.6-coding.service", result)
            self.assertEqual(result["qwen3.6-coding.service"]["ActiveState"], "active")
            self.assertEqual(result["qwen3.6-coding.service"]["NRestarts"], "0")

            # Check llama-task
            self.assertIn("llama-task.service", result)
            self.assertEqual(result["llama-task.service"]["ActiveState"], "active")

            # Check qwen35-npu
            self.assertIn("llama-server-qwen35-npu.service", result)
            self.assertEqual(result["llama-server-qwen35-npu.service"]["ActiveState"], "inactive")


class TestWatcher(unittest.TestCase):
    """Test the Watcher state machine."""

    def setUp(self):
        """Set up test fixtures."""
        self.repo_root = Path(__file__).resolve().parents[2]
        self.fixtures_dir = self.repo_root / "docs" / "fixtures"

    def _load_unit(self, filename: str) -> roundhouse.UnitFile:
        """Load a fixture unit file."""
        fpath = self.fixtures_dir / filename
        if not fpath.exists():
            self.skipTest(f"Fixture {filename} not found")

        with open(fpath, 'rb') as f:
            raw = f.read()
        return roundhouse.parse_unit(str(fpath), raw)

    def test_watcher_initialization(self):
        """Watcher should initialize with no exceptions."""
        units = {"test.service": self._load_unit("qwen3.6-coding.service")}
        watcher = roundhouse.Watcher(units, "6.12.0", None)
        self.assertIsNotNone(watcher)
        self.assertIn("test.service", watcher._state)

    def test_apply_systemctl_show_qwen_active(self):
        """Apply systemctl show; qwen3.6-coding should be LOADING initially."""
        unit = self._load_unit("qwen3.6-coding.service")
        units = {unit.name: unit}

        watcher = roundhouse.Watcher(units, "6.12.0", None)

        # Parse systemctl show
        sample_file = self.repo_root / "mvp1" / "tests" / "journal_samples" / "systemctl-show.txt"
        if sample_file.exists():
            with open(sample_file, 'r') as f:
                text = f.read()

            props = roundhouse.parse_show_blocks(
                text,
                ["qwen3.6-coding.service", "llama-task.service", "llama-server-qwen35-npu.service"]
            )

            # Apply only qwen3.6-coding
            events = watcher.apply_systemctl_show({
                unit.name: props.get(unit.name, {})
            })

            # Should transition to LOADING (active but no journal marker yet)
            rung = watcher._compute_rung(unit.name)
            self.assertEqual(rung, 'LOADING')

    def test_apply_journal_line_model_loaded(self):
        """Journal line with 'model loaded' should set READY."""
        unit = self._load_unit("qwen3.6-coding.service")
        units = {unit.name: unit}

        watcher = roundhouse.Watcher(units, "6.12.0", None)

        # Set to active first
        watcher._state[unit.name]['active_state'] = 'active'
        watcher._state[unit.name]['exec_main_start_ts'] = roundhouse.time.time()

        # Apply journal line
        rec = {
            '_SYSTEMD_USER_UNIT': unit.name,
            'MESSAGE': 'llama_server: model loaded'
        }

        events = watcher.apply_journal_line(rec)

        # Check state
        self.assertTrue(watcher._state[unit.name]['ready'])
        rung = watcher._compute_rung(unit.name)
        self.assertEqual(rung, 'READY')

    def test_apply_journal_line_launch_slot(self):
        """Journal line with 'launch_slot_' should set BUSY and READY."""
        unit = self._load_unit("qwen3.6-coding.service")
        units = {unit.name: unit}

        watcher = roundhouse.Watcher(units, "6.12.0", None)

        # Set to active first
        watcher._state[unit.name]['active_state'] = 'active'

        # Apply journal line
        rec = {
            '_SYSTEMD_USER_UNIT': unit.name,
            'MESSAGE': 'slot launch_slot_: id 0 | task 0 | processing task, is_child = 0'
        }

        events = watcher.apply_journal_line(rec)

        # Check state
        self.assertTrue(watcher._state[unit.name]['ready'])
        self.assertTrue(watcher._state[unit.name]['busy'])
        rung = watcher._compute_rung(unit.name)
        self.assertEqual(rung, 'BUSY')

    def test_apply_journal_line_slot_release(self):
        """Journal line with 'slot      release:' (variable whitespace) should end BUSY."""
        unit = self._load_unit("qwen3.6-coding.service")
        units = {unit.name: unit}

        watcher = roundhouse.Watcher(units, "6.12.0", None)

        # Start in BUSY state
        watcher._state[unit.name]['active_state'] = 'active'
        watcher._state[unit.name]['ready'] = True
        watcher._state[unit.name]['busy'] = True
        watcher._state[unit.name]['busy_since'] = roundhouse.time.time() - 10

        # Apply slot release line (variable whitespace)
        rec = {
            '_SYSTEMD_USER_UNIT': unit.name,
            'MESSAGE': 'slot      release: id 0 | task 0 | stop processing: n_tokens = 1'
        }

        events = watcher.apply_journal_line(rec)

        # Check state
        self.assertFalse(watcher._state[unit.name]['busy'])
        self.assertTrue(watcher._state[unit.name]['ready'])
        rung = watcher._compute_rung(unit.name)
        self.assertEqual(rung, 'READY')

    def test_qwen35_npu_gate_standby(self):
        """qwen35-npu with wrong kernel should be STANDBY, not FAILED."""
        unit = self._load_unit("llama-server-qwen35-npu.service")
        units = {unit.name: unit}

        # Running kernel doesn't match the gate
        watcher = roundhouse.Watcher(units, "6.12.0-fake", None)

        # Set inactive state
        watcher._state[unit.name]['active_state'] = 'inactive'
        watcher._state[unit.name]['sub_state'] = 'dead'
        watcher._state[unit.name]['condition_result'] = 'no'

        rung = watcher._compute_rung(unit.name)
        self.assertEqual(rung, 'STANDBY')

    def test_qwen35_npu_failed_on_active_state(self):
        """qwen35-npu with ActiveState=failed should be FAILED, not STANDBY."""
        unit = self._load_unit("llama-server-qwen35-npu.service")
        units = {unit.name: unit}

        watcher = roundhouse.Watcher(units, "6.12.0-fake", None)

        # Set failed state
        watcher._state[unit.name]['active_state'] = 'failed'
        watcher._state[unit.name]['sub_state'] = 'failed'

        rung = watcher._compute_rung(unit.name)
        self.assertEqual(rung, 'FAILED')

    def test_retired_unit_rung(self):
        """RETIRED unit should always render as RETIRED."""
        unit = self._load_unit("qwen3.6-coding.service")
        # Manually mark as retired
        unit.retired = True
        units = {unit.name: unit}

        watcher = roundhouse.Watcher(units, "6.12.0", None)

        # Even if active and ready
        watcher._state[unit.name]['active_state'] = 'active'
        watcher._state[unit.name]['ready'] = True

        rung = watcher._compute_rung(unit.name)
        self.assertEqual(rung, 'RETIRED')

    def test_restart_resets_journal_state(self):
        """New ExecMainStartTimestampMonotonic should reset journal state."""
        unit = self._load_unit("qwen3.6-coding.service")
        units = {unit.name: unit}

        watcher = roundhouse.Watcher(units, "6.12.0", None)

        # Set initial state
        watcher._state[unit.name]['exec_main_start_ts_mono'] = '100'
        watcher._state[unit.name]['active_state'] = 'active'
        watcher._state[unit.name]['ready'] = True
        watcher._state[unit.name]['busy'] = False

        # Apply new timestamp
        props = {
            unit.name: {
                'ActiveState': 'active',
                'SubState': 'running',
                'ExecMainStartTimestampMonotonic': '200',
                'ExecMainStartTimestamp': 'Wed 2026-08-12 13:30:00 CEST',
                'NRestarts': '0',
                'ControlGroup': '/sys/fs/cgroup/...'
            }
        }

        events = watcher.apply_systemctl_show(props)

        # State should be reset
        self.assertFalse(watcher._state[unit.name]['ready'])
        self.assertFalse(watcher._state[unit.name]['busy'])

    def test_activating_with_auto_restart_is_failed(self):
        """ActiveState=activating + SubState=auto-restart should be FAILED."""
        unit = self._load_unit("qwen3.6-coding.service")
        units = {unit.name: unit}

        watcher = roundhouse.Watcher(units, "6.12.0", None)

        watcher._state[unit.name]['active_state'] = 'activating'
        watcher._state[unit.name]['sub_state'] = 'auto-restart'
        watcher._state[unit.name]['n_restarts'] = 3

        rung = watcher._compute_rung(unit.name)
        self.assertEqual(rung, 'FAILED')

    def test_activating_otherwise_is_starting(self):
        """ActiveState=activating (no auto-restart) should be STARTING."""
        unit = self._load_unit("qwen3.6-coding.service")
        units = {unit.name: unit}

        watcher = roundhouse.Watcher(units, "6.12.0", None)

        watcher._state[unit.name]['active_state'] = 'activating'
        watcher._state[unit.name]['sub_state'] = 'start-pre'

        rung = watcher._compute_rung(unit.name)
        self.assertEqual(rung, 'STARTING')

    def test_snapshot_shape(self):
        """snapshot() should return dict with correct top-level keys."""
        unit = self._load_unit("qwen3.6-coding.service")
        units = {unit.name: unit}

        watcher = roundhouse.Watcher(units, "6.12.0", None)
        watcher._state[unit.name]['active_state'] = 'active'
        watcher._state[unit.name]['unit_file_state'] = 'enabled'

        snap = watcher.snapshot()

        # Check top-level keys
        required_keys = {'host', 'kernel', 'now', 'mem', 'sources', 'self_port', 'units'}
        self.assertEqual(set(snap.keys()), required_keys)

        # Check mem dict
        self.assertIn('total_bytes', snap['mem'])
        self.assertIn('available_bytes', snap['mem'])

        # Check sources
        self.assertIn('journal', snap['sources'])
        self.assertIn('systemctl', snap['sources'])

        # Check units list
        self.assertIsInstance(snap['units'], list)
        if snap['units']:
            unit_dict = snap['units'][0]
            required_unit_keys = {
                'unit', 'description', 'retired', 'rung', 'roster', 'since',
                'detail', 'badges', 'stale', 'sensed_at', 'enabled', 'active_state',
                'sub_state', 'n_restarts', 'port', 'port_source', 'alias', 'gate',
                'model_file', 'quant_hint', 'ctx', 'mem', 'port_conflict'
            }
            self.assertEqual(set(unit_dict.keys()), required_unit_keys)

    def test_long_running_badge(self):
        """BUSY for >30 min should get long_running badge."""
        unit = self._load_unit("qwen3.6-coding.service")
        units = {unit.name: unit}

        watcher = roundhouse.Watcher(units, "6.12.0", None)

        watcher._state[unit.name]['active_state'] = 'active'
        watcher._state[unit.name]['ready'] = True
        watcher._state[unit.name]['busy'] = True
        # Set busy_since to 2 hours ago
        watcher._state[unit.name]['busy_since'] = roundhouse.time.time() - (2 * 3600)

        badges = watcher._compute_badges(unit.name, 'BUSY')
        self.assertIn('long_running', badges)


class TestMemStore(unittest.TestCase):
    """Test the MemStore class."""

    def test_memstore_inert_mode(self):
        """MemStore with db_path=None should be inert."""
        store = roundhouse.MemStore(db_path=None)

        # record() should be no-op
        store.record(
            unit='test.service',
            model_path='/path/to/model.gguf',
            file_id='sz1000:mt1234',
            ctx=1024,
            ctk='q8_0',
            ctv='q8_0',
            phase='ready',
            peak_bytes=1000000000
        )

        # lookup() should return None
        result = store.lookup('test.service', 'sz1000:mt1234', 1024)
        self.assertIsNone(result)

        # history() should return empty list
        history = store.history('test.service')
        self.assertEqual(history, [])

    def test_memstore_sqlite_roundtrip(self):
        """MemStore should record and lookup measurements."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, 'test.sqlite')
            store = roundhouse.MemStore(db_path=db_path)

            # Record a measurement
            store.record(
                unit='test.service',
                model_path='/path/to/model.gguf',
                file_id='sz16000000000:mt1765500000',
                ctx=65536,
                ctk='q8_0',
                ctv='q8_0',
                phase='ready',
                peak_bytes=19110000000,
                load_seconds=72.4
            )

            # Lookup should find it
            result = store.lookup('test.service', 'sz16000000000:mt1765500000', 65536)
            self.assertIsNotNone(result)
            self.assertEqual(result['bytes'], 19110000000)
            self.assertEqual(result['load_seconds'], 72.4)
            self.assertEqual(result['source'], 'measured')

    def test_memstore_history(self):
        """MemStore history() should return list of measurements."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, 'test.sqlite')
            store = roundhouse.MemStore(db_path=db_path)

            # Record two measurements
            store.record(
                unit='test.service',
                model_path='/path/to/model.gguf',
                file_id='sz16000000000:mt1765500000',
                ctx=65536,
                ctk='q8_0',
                ctv='q8_0',
                phase='ready',
                peak_bytes=19110000000,
                load_seconds=72.4
            )

            store.record(
                unit='test.service',
                model_path='/path/to/model.gguf',
                file_id='sz16000000000:mt1765500000',
                ctx=65536,
                ctk='q8_0',
                ctv='q8_0',
                phase='exit',
                peak_bytes=19100000000
            )

            # History should return both
            history = store.history('test.service')
            self.assertEqual(len(history), 2)
            self.assertEqual(history[0]['phase'], 'exit')  # Most recent first
            self.assertEqual(history[1]['phase'], 'ready')


class TestEstimateMemory(unittest.TestCase):
    """Test the estimate_memory function."""

    def test_estimate_memory_missing_file(self):
        """estimate_memory should handle missing model file."""
        dep = {
            'artifact': {
                'path': '/nonexistent/model.gguf'
            }
        }
        result = roundhouse.estimate_memory(dep, None)

        self.assertIsNone(result['bytes'])
        self.assertEqual(result['source'], 'unknown')
        self.assertEqual(result['label'], 'model file not found')

    def test_estimate_memory_with_file(self):
        """estimate_memory should estimate from file size."""
        with tempfile.NamedTemporaryFile(suffix='.gguf', delete=False) as f:
            # Create a 100MB file
            f.write(b'\x00' * (100 * 1024 * 1024))
            f.flush()

            try:
                dep = {
                    'artifact': {
                        'path': f.name
                    }
                }
                result = roundhouse.estimate_memory(dep, None)

                self.assertIsNotNone(result['bytes'])
                self.assertEqual(result['source'], 'estimate')
                # Should be approximately: 100MB * 1.10 + 1.5GB
                expected = int(100 * 1024 * 1024 * 1.10 + 1.5 * 2**30)
                # Allow some tolerance
                self.assertAlmostEqual(result['bytes'], expected, delta=1000000)
            finally:
                os.unlink(f.name)


class TestIntegration(unittest.TestCase):
    """Integration tests combining multiple components."""

    def setUp(self):
        """Set up test fixtures."""
        self.repo_root = Path(__file__).resolve().parents[2]
        self.fixtures_dir = self.repo_root / "docs" / "fixtures"

    def test_zero_socket_connections(self):
        """Watcher should never open socket connections (no health probes)."""
        unit = roundhouse.parse_unit(
            str(self.fixtures_dir / "qwen3.6-coding.service"),
            (self.fixtures_dir / "qwen3.6-coding.service").read_bytes()
        )
        units = {unit.name: unit}

        # Monkeypatch socket.connect to raise if called
        original_connect = socket.socket.connect

        def mock_connect(self, *args, **kwargs):
            raise RuntimeError("Unexpected socket connection attempted!")

        with patch.object(socket.socket, 'connect', mock_connect):
            # Run through a full cycle
            watcher = roundhouse.Watcher(units, "6.12.0", None)

            # Apply systemctl show
            sample_file = self.repo_root / "mvp1" / "tests" / "journal_samples" / "systemctl-show.txt"
            with open(sample_file, 'r') as f:
                text = f.read()

            props = roundhouse.parse_show_blocks(
                text,
                ["qwen3.6-coding.service", "llama-task.service", "llama-server-qwen35-npu.service"]
            )

            watcher.apply_systemctl_show({
                unit.name: props.get(unit.name, {})
            })

            # Apply journal lines
            journal_file = self.repo_root / "mvp1" / "tests" / "journal_samples" / "llama-server-start.jsonl"
            if journal_file.exists():
                with open(journal_file, 'r') as f:
                    for line in f:
                        try:
                            rec = json.loads(line)
                            if rec.get('_SYSTEMD_USER_UNIT') == unit.name:
                                watcher.apply_journal_line(rec)
                        except json.JSONDecodeError:
                            pass

            # Get snapshot (should not raise)
            snap = watcher.snapshot()
            self.assertIsNotNone(snap)


class TestRegexPatterns(unittest.TestCase):
    """Test journal regex patterns."""

    def test_ls_ready_pattern_matches(self):
        """LS_READY patterns should match model loaded."""
        pattern = roundhouse.LS_READY[0]
        self.assertIsNotNone(__import__('re').search(pattern, "llama_server: model loaded"))

    def test_ls_busy_start_launch_slot(self):
        """LS_BUSY_START should match launch_slot_."""
        pattern = roundhouse.LS_BUSY_START[0]
        self.assertIsNotNone(__import__('re').search(pattern, "slot launch_slot_: id 0"))

    def test_ls_busy_end_variable_whitespace(self):
        """LS_BUSY_END should match 'slot      release:' with variable whitespace."""
        pattern = roundhouse.LS_BUSY_END[0]
        self.assertIsNotNone(__import__('re').search(pattern, "slot      release: id 0"))
        self.assertIsNotNone(__import__('re').search(pattern, "slot\t\trelease: id 0"))
        self.assertIsNotNone(__import__('re').search(pattern, "slot release: id 0"))


if __name__ == '__main__':
    unittest.main()
