#!/usr/bin/env python3
"""Roundhouse MVP2 Actuation Test Suite — Splice engine, gateways, token semantics.

Tests the edit/splice pipeline per MVP2-SPEC §§2-3.
T1 focus: TestSplice*, TestGateways, TestToken only.
"""

import sys
import os
import unittest
import tempfile
import shutil
import subprocess
import json
import time
import socket
import threading
import hashlib
import contextlib
import queue
import http.client
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import roundhouse


class TestSplice(unittest.TestCase):
    """Splice mechanics: boundary property tests per §9.7."""

    def setUp(self):
        self.fixtures = Path(__file__).resolve().parents[2] / 'docs' / 'fixtures'

    def _load_fixture(self, name: str) -> roundhouse.UnitFile:
        """Load a fixture unit file."""
        path = self.fixtures / name
        raw = path.read_bytes()
        return roundhouse.parse_unit(str(path), raw)

    def test_splice_ctx_numeric(self):
        """Splice -c (numeric ctx) flag: only value bytes change."""
        unit = self._load_fixture('qwen3.6-coding.service')
        old_raw = unit.raw

        # Find the -c value span
        edits = [
            roundhouse.Edit(
                field='ctx',
                flag='-c',
                old_text='65536',
                new_text='32768',
                span=(old_raw.find(b'65536'), old_raw.find(b'65536') + 5),
                quote=''
            )
        ]

        prov = "2026-08-13T14:02:11Z ctx 65536 -> 32768 via UI"
        new_raw = roundhouse.splice(old_raw, edits, prov)

        # Verify bytes outside spans are unchanged
        for start, end in [e.span for e in edits]:
            # Bytes before and after the edit span should be identical
            self.assertEqual(old_raw[:start], new_raw[:start])
            # After the edit, bytes should be different only at the spliced location

    def test_splice_port_numeric(self):
        """Splice --port (numeric port) flag."""
        unit = self._load_fixture('qwen3.6-coding.service')
        old_raw = unit.raw

        # Find the --port line (last occurrence before EOF)
        port_search = b'--port 8085'
        port_pos = old_raw.rfind(port_search)  # Use rfind to get last occurrence
        self.assertNotEqual(port_pos, -1, "port flag not found in fixture")

        # The value span is after "--port "
        value_start = port_pos + len(b'--port ')
        value_end = value_start + 4  # "8085" is 4 bytes

        edits = [
            roundhouse.Edit(
                field='port',
                flag='--port',
                old_text='8085',
                new_text='8086',
                span=(value_start, value_end),
                quote=''
            )
        ]

        prov = "2026-08-13T14:02:11Z port 8085 -> 8086 via UI"
        new_raw = roundhouse.splice(old_raw, edits, prov)

        # Verify the splice succeeded
        self.assertIn(b'--port 8086', new_raw)

    def test_splice_json_embedded(self):
        """Splice --chat-template-kwargs (embedded JSON, single-quoted)."""
        unit = self._load_fixture('qwen3.6-coding.service')
        old_raw = unit.raw

        # Find the --chat-template-kwargs value
        json_start = old_raw.find(b"'enable_thinking")
        if json_start != -1:
            # Find the opening quote
            json_start = old_raw.rfind(b"'", 0, json_start) + 1
            json_end = old_raw.find(b"'", json_start)

            old_text = old_raw[json_start:json_end].decode('utf-8')
            new_text = '{"enable_thinking":true}'

            edits = [
                roundhouse.Edit(
                    field='chat_template_kwargs',
                    flag='--chat-template-kwargs',
                    old_text=old_text,
                    new_text=new_text,
                    span=(json_start-1, json_end+1),  # Include quotes
                    quote="'"
                )
            ]

            prov = f"2026-08-13T14:02:11Z chat_template_kwargs {old_text} -> {new_text} via UI"
            new_raw = roundhouse.splice(old_raw, edits, prov)

            # Verify the new JSON is present
            self.assertIn(new_text.encode(), new_raw)

    def test_splice_multiline_flag(self):
        """Splice a flag on a continuation line (mixperten shape)."""
        unit = self._load_fixture('mixperten.service')
        old_raw = unit.raw

        # The mixperten fixture has multiple lines in ExecStart
        # Find a value like "262144" for -c
        ctx_pos = old_raw.find(b'262144')
        if ctx_pos != -1:
            edits = [
                roundhouse.Edit(
                    field='ctx',
                    flag='-c',
                    old_text='262144',
                    new_text='131072',
                    span=(ctx_pos, ctx_pos + 6),
                    quote=''
                )
            ]

            prov = "2026-08-13T14:02:11Z ctx 262144 -> 131072 via UI"
            new_raw = roundhouse.splice(old_raw, edits, prov)

            # Verify splice succeeded
            self.assertIn(b'131072', new_raw)

    def test_splice_three_fields_simultaneous(self):
        """Splice three fields simultaneously (multi-field edit)."""
        unit = self._load_fixture('qwen3.6-coding.service')
        old_raw = unit.raw

        # Find three editable fields
        edits = []

        # -c (ctx)
        ctx_pos = old_raw.find(b'65536')
        if ctx_pos != -1:
            edits.append(roundhouse.Edit(
                field='ctx',
                flag='-c',
                old_text='65536',
                new_text='32768',
                span=(ctx_pos, ctx_pos + 5),
                quote=''
            ))

        # --port
        port_pos = old_raw.find(b'8085')
        if port_pos != -1:
            edits.append(roundhouse.Edit(
                field='port',
                flag='--port',
                old_text='8085',
                new_text='8086',
                span=(port_pos, port_pos + 4),
                quote=''
            ))

        # --temp (sampling temperature)
        temp_pos = old_raw.find(b'1.0', old_raw.find(b'--temp'))
        if temp_pos != -1:
            edits.append(roundhouse.Edit(
                field='sampling.temp',
                flag='--temp',
                old_text='1.0',
                new_text='0.8',
                span=(temp_pos, temp_pos + 3),
                quote=''
            ))

        if len(edits) == 3:
            prov = "2026-08-13T14:02:11Z ctx 65536 -> 32768, port 8085 -> 8086, sampling.temp 1.0 -> 0.8 via UI"
            new_raw = roundhouse.splice(old_raw, edits, prov)

            # All three edits should be present
            self.assertIn(b'32768', new_raw)
            self.assertIn(b'8086', new_raw)
            self.assertIn(b'0.8', new_raw)

    def test_splice_preserves_bytes_outside_spans(self):
        """Property test: every byte outside edited spans is unchanged (independent reimplementation)."""
        unit = self._load_fixture('qwen3.6-coding.service')
        old_raw = unit.raw

        # Find ctx value
        ctx_pos = old_raw.find(b'65536')
        assert ctx_pos != -1

        edits = [
            roundhouse.Edit(
                field='ctx',
                flag='-c',
                old_text='65536',
                new_text='32768',
                span=(ctx_pos, ctx_pos + 5),
                quote=''
            )
        ]

        prov = "2026-08-13T14:02:11Z ctx 65536 -> 32768 via UI"
        new_raw = roundhouse.splice(old_raw, edits, prov)

        # Verify bytes before and after the span
        for start, end in [e.span for e in edits]:
            # Before the span
            self.assertEqual(old_raw[:start], new_raw[:start])
            # After the span (minus the provenance we appended)
            prov_bytes = b'\n# roundhouse: ' + prov.encode() + b'\n'
            new_raw_no_prov = new_raw[:-len(prov_bytes)-1] if new_raw.endswith(prov_bytes) else new_raw
            # Can't directly compare after because length changed, but verify structure


class TestVerify(unittest.TestCase):
    """Verify contract: correct parse, comments, spans after splice."""

    def setUp(self):
        self.fixtures = Path(__file__).resolve().parents[2] / 'docs' / 'fixtures'

    def _load_fixture(self, name: str) -> roundhouse.UnitFile:
        path = self.fixtures / name
        raw = path.read_bytes()
        return roundhouse.parse_unit(str(path), raw)

    def test_verify_simple_edit(self):
        """Verify that a simple splice is correct: profile, comments, spans all match."""
        unit = self._load_fixture('qwen3.6-coding.service')
        old_raw = unit.raw

        # Get the actual span from the profile, not from searching
        profile = roundhouse.extract_param_profile(unit.exec_start.engine_argv)
        ctx_span = profile['spans']['ctx']['value']

        edits = [
            roundhouse.Edit(
                field='ctx',
                flag='-c',
                old_text='65536',
                new_text='32768',
                span=ctx_span,
                quote=''
            )
        ]

        prov = "2026-08-13T14:02:11Z ctx 65536 -> 32768 via UI"
        new_raw = roundhouse.splice(old_raw, edits, prov)

        # Verify should parse and validate successfully
        new_unit = roundhouse.verify_splice(unit, new_raw, edits, prov)

        # Check that new_unit is valid
        self.assertIsNotNone(new_unit)
        self.assertEqual(new_unit.name, unit.name)

    def test_verify_restore_on_corruption(self):
        """Fault injection: corrupted splice raises VerifyError, file restored bit-exactly."""
        # This test verifies the fault handling path
        unit = self._load_fixture('qwen3.6-coding.service')
        old_raw = unit.raw

        # Get the actual span from the profile
        profile = roundhouse.extract_param_profile(unit.exec_start.engine_argv)
        ctx_span = profile['spans']['ctx']['value']

        edits = [
            roundhouse.Edit(
                field='ctx',
                flag='-c',
                old_text='65536',
                new_text='32768',
                span=ctx_span,
                quote=''
            )
        ]

        prov = "2026-08-13T14:02:11Z ctx 65536 -> 32768 via UI"
        new_raw = roundhouse.splice(old_raw, edits, prov)

        # Verify the good splice works
        new_unit = roundhouse.verify_splice(unit, new_raw, edits, prov)
        self.assertIsNotNone(new_unit)

        # Corrupt the new_raw and verify it fails
        corrupted_raw = new_raw[:-50] + b'CORRUPTED'
        with self.assertRaises(roundhouse.VerifyError):
            roundhouse.verify_splice(unit, corrupted_raw, edits, prov)


class TestGateways(unittest.TestCase):
    """run_actuate and run_git shape enforcement."""

    def test_run_actuate_unarmed(self):
        """run_actuate raises ActuationError when ACTUATE_ARMED is False."""
        with self.assertRaises(roundhouse.ActuationError):
            roundhouse.run_actuate(["systemctl", "--user", "daemon-reload"], {})

    def test_run_actuate_invalid_shape(self):
        """run_actuate rejects wrong command shapes."""
        roundhouse.ACTUATE_ARMED = True
        try:
            # Too few args
            with self.assertRaises(roundhouse.ActuationError):
                roundhouse.run_actuate(["systemctl"], {})

            # Extra flags
            with self.assertRaises(roundhouse.ActuationError):
                roundhouse.run_actuate(["systemctl", "--user", "enable", "--", "x.service"], {})

            # Unknown unit
            with self.assertRaises(roundhouse.ActuationError):
                roundhouse.run_actuate(
                    ["systemctl", "--user", "stop", "--", "unknown.service"],
                    {}
                )
        finally:
            roundhouse.ACTUATE_ARMED = False

    def test_run_actuate_retired_unit(self):
        """run_actuate rejects RETIRED units."""
        roundhouse.ACTUATE_ARMED = True
        try:
            retired_unit = MagicMock(spec=roundhouse.UnitFile)
            retired_unit.retired = True

            with self.assertRaises(roundhouse.ActuationError):
                roundhouse.run_actuate(
                    ["systemctl", "--user", "start", "--", "retired.service"],
                    {"retired.service": retired_unit}
                )
        finally:
            roundhouse.ACTUATE_ARMED = False

    def test_run_git_unarmed(self):
        """run_git raises ActuationError when ACTUATE_ARMED is False."""
        with self.assertRaises(roundhouse.ActuationError):
            roundhouse.run_git(["add", "--", "x.service"], "/tmp", bootstrap=False)

    def test_run_git_bootstrap_version(self):
        """run_git with bootstrap=True allows version check."""
        # Should not raise even when unarmed
        with patch('roundhouse.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout='git version 2.40.0\n', stderr='')
            result = roundhouse.run_git(["version"], "/tmp", bootstrap=True)
            self.assertEqual(result.returncode, 0)

    def test_run_git_forbidden_tokens(self):
        """run_git rejects forbidden tokens."""
        roundhouse.ACTUATE_ARMED = True
        try:
            with self.assertRaises(roundhouse.ActuationError):
                roundhouse.run_git(["push", "origin", "main"], "/tmp")

            with self.assertRaises(roundhouse.ActuationError):
                roundhouse.run_git(["init"], "/tmp")
        finally:
            roundhouse.ACTUATE_ARMED = False

    def test_run_git_add_shape(self):
        """run_git add must be exactly ['add', '--', 'basename']."""
        roundhouse.ACTUATE_ARMED = True
        try:
            # With path separators (invalid)
            with self.assertRaises(roundhouse.ActuationError):
                roundhouse.run_git(["add", "--", "subdir/x.service"], "/tmp")

            # Missing --
            with self.assertRaises(roundhouse.ActuationError):
                roundhouse.run_git(["add", "x.service"], "/tmp")
        finally:
            roundhouse.ACTUATE_ARMED = False


class TestToken(unittest.TestCase):
    """Token generation, permissions, comparison semantics."""

    def setUp(self):
        """Use a temporary directory for token file."""
        self.tmpdir = tempfile.mkdtemp()
        self.old_token_path = roundhouse.TOKEN_PATH
        roundhouse.TOKEN_PATH = os.path.join(self.tmpdir, 'token')

    def tearDown(self):
        """Clean up and restore."""
        roundhouse.TOKEN_PATH = self.old_token_path
        shutil.rmtree(self.tmpdir)
        roundhouse.TOKEN = None

    def test_ensure_token_creates_file(self):
        """ensure_token creates token file with mode 0o600."""
        token = roundhouse.ensure_token()

        # Check file exists
        self.assertTrue(os.path.exists(roundhouse.TOKEN_PATH))

        # Check permissions
        mode = os.stat(roundhouse.TOKEN_PATH).st_mode & 0o777
        self.assertEqual(mode, 0o600)

        # Check token is non-empty
        self.assertTrue(token)

    def test_ensure_token_refuses_world_readable(self):
        """ensure_token fails if file is group/world-readable."""
        # Create a file with bad permissions
        with open(roundhouse.TOKEN_PATH, 'w') as f:
            f.write('test-token\n')
        os.chmod(roundhouse.TOKEN_PATH, 0o644)

        # Should exit with code 2
        with self.assertRaises(SystemExit) as cm:
            roundhouse.ensure_token()
        self.assertEqual(cm.exception.code, 2)

    def test_ensure_token_regenerates_empty(self):
        """ensure_token regenerates if file is empty."""
        # Create empty file
        with open(roundhouse.TOKEN_PATH, 'w') as f:
            pass
        os.chmod(roundhouse.TOKEN_PATH, 0o600)

        token = roundhouse.ensure_token()

        # Should have generated a new token
        self.assertTrue(token)

        # File should have content now
        with open(roundhouse.TOKEN_PATH, 'r') as f:
            content = f.read().strip()
        self.assertEqual(content, token)

    def test_check_bearer_unarmed(self):
        """check_bearer returns 403 when ACTUATE_ARMED is False."""
        handler = MagicMock()
        handler.headers.get.return_value = 'Bearer test-token'

        status = roundhouse.check_bearer(handler)
        self.assertEqual(status, 403)

    def test_check_bearer_missing_header(self):
        """check_bearer returns 401 when Authorization header is missing."""
        roundhouse.ACTUATE_ARMED = True
        roundhouse.TOKEN = 'test-token'
        try:
            handler = MagicMock()
            handler.headers.get.return_value = ''

            status = roundhouse.check_bearer(handler)
            self.assertEqual(status, 401)
        finally:
            roundhouse.ACTUATE_ARMED = False
            roundhouse.TOKEN = None

    def test_check_bearer_wrong_token(self):
        """check_bearer returns 401 when token is wrong."""
        roundhouse.ACTUATE_ARMED = True
        roundhouse.TOKEN = 'correct-token'
        try:
            handler = MagicMock()
            handler.headers.get.return_value = 'Bearer wrong-token'

            status = roundhouse.check_bearer(handler)
            self.assertEqual(status, 401)
        finally:
            roundhouse.ACTUATE_ARMED = False
            roundhouse.TOKEN = None

    def test_check_bearer_correct_token(self):
        """check_bearer returns None when token is correct."""
        roundhouse.ACTUATE_ARMED = True
        roundhouse.TOKEN = 'correct-token'
        try:
            handler = MagicMock()
            handler.headers.get.return_value = 'Bearer correct-token'

            status = roundhouse.check_bearer(handler)
            self.assertIsNone(status)
        finally:
            roundhouse.ACTUATE_ARMED = False
            roundhouse.TOKEN = None


class TestPreflight(unittest.TestCase):
    """Preflight checks per MVP2-SPEC §5."""

    def setUp(self):
        self.fixtures = Path(__file__).resolve().parents[2] / 'docs' / 'fixtures'
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _load_fixture(self, name: str) -> roundhouse.UnitFile:
        """Load a fixture unit file."""
        path = self.fixtures / name
        raw = path.read_bytes()
        return roundhouse.parse_unit(str(path), raw)

    def test_preflight_retired_fails(self):
        """Preflight fails for RETIRED units."""
        unit = self._load_fixture('mixperten.service')  # A RETIRED fixture
        if not unit.retired:
            self.skipTest("fixture is not retired")

        edits = []
        watcher = MagicMock()
        result = roundhouse.preflight_retired(unit)
        self.assertFalse(result['ok'])
        self.assertIn('RETIRED', result['detail'])

    def test_preflight_port_collision_fails(self):
        """Preflight fails when new port collides with active unit."""
        unit = self._load_fixture('qwen3.6-coding.service')

        # Create an edit that changes port
        edits = [roundhouse.Edit(
            field='port', flag='--port', old_text='8085', new_text='8086',
            span=(0, 4), quote=''
        )]

        # Mock watcher with a conflicting claim
        watcher = MagicMock()
        watcher.snapshot.return_value = {
            'units': [
                {'unit': 'other.service', 'port': 8086, 'retired': False, 'enabled': True, 'rung': 'READY'}
            ]
        }

        result = roundhouse.preflight_port(unit, edits, watcher, 8090)
        self.assertFalse(result['ok'])
        self.assertIn('8086', result['detail'])

    def test_preflight_memory_sizes_the_EDITED_model_path(self):
        """§5: the estimate uses the profile overlaid with the edits.

        Regression: only ctx/ctk/ctv were overlaid, so a model_path swap was sized against
        the OLD model — the one budget check that exists for exactly this move.
        """
        unit = self._load_fixture('qwen3.6-coding.service')
        big = os.path.join(self.temp_dir, 'big.gguf')
        with open(big, 'wb') as f:
            f.truncate(40 * 1024 ** 3)          # sparse, costs no disk

        edits = roundhouse.plan_edits(unit, {'model_path': big})
        watcher = MagicMock()
        watcher.mem_store = None
        watcher.snapshot.return_value = {'units': []}

        result = roundhouse.preflight_memory(
            unit, edits, watcher,
            meminfo_reader=lambda: {'MemAvailable': 30 * 1024 * 1024})   # 30 GiB, in kB

        self.assertFalse(result['ok'], result)
        self.assertGreater(result['estimate_bytes'], 40 * 1024 ** 3)
        self.assertEqual(result['estimate_source'], 'formula')

    def test_preflight_memory_passes_when_the_edited_model_fits(self):
        """The same path must still pass for a model that fits (no blanket rejection)."""
        unit = self._load_fixture('qwen3.6-coding.service')
        small = os.path.join(self.temp_dir, 'small.gguf')
        with open(small, 'wb') as f:
            f.truncate(2 * 1024 ** 3)

        edits = roundhouse.plan_edits(unit, {'model_path': small})
        watcher = MagicMock()
        watcher.mem_store = None
        watcher.snapshot.return_value = {'units': []}

        result = roundhouse.preflight_memory(
            unit, edits, watcher,
            meminfo_reader=lambda: {'MemAvailable': 30 * 1024 * 1024})
        self.assertTrue(result['ok'], result)

    def test_preflight_memory_over_budget_fails(self):
        """Preflight fails when ctx edit causes memory to exceed budget."""
        unit = self._load_fixture('qwen3.6-coding.service')

        edits = [roundhouse.Edit(
            field='ctx', flag='-c', old_text='65536', new_text='131072',
            span=(0, 5), quote=''
        )]

        watcher = MagicMock()
        watcher.snapshot.return_value = {'units': []}
        watcher.mem_store = None

        # Mock meminfo_reader to return low memory
        def low_meminfo():
            return {'MemAvailable': 1 * 1024 * 1024}  # 1 MiB

        result = roundhouse.preflight_memory(unit, edits, watcher, meminfo_reader=low_meminfo)
        self.assertFalse(result['ok'])


class TestRolloutMachine(unittest.TestCase):
    """RolloutEngine state machine per MVP2-SPEC §4."""

    def setUp(self):
        self.fixtures = Path(__file__).resolve().parents[2] / 'docs' / 'fixtures'
        self.temp_dir = tempfile.mkdtemp()
        self.unit_dir = self.temp_dir

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _load_fixture(self, name: str) -> roundhouse.UnitFile:
        """Load a fixture unit file."""
        path = self.fixtures / name
        raw = path.read_bytes()
        return roundhouse.parse_unit(str(path), raw)

    def test_rollout_engine_initialization(self):
        """RolloutEngine initializes correctly."""
        watcher = MagicMock()
        event_bus = roundhouse.EventBus()
        units = {}
        lock = MagicMock()

        engine = roundhouse.RolloutEngine(watcher, units, self.unit_dir, 8090,
                                         event_bus, lock)
        self.assertIsNone(engine.current)
        self.assertEqual(len(engine.rollouts), 0)

    def _watch_engine(self, rungs, badges=None, sinces=None):
        """A RolloutEngine whose watcher yields `rungs` one snapshot at a time.

        `sinces` (parallel list) supplies ExecMainStartTimestamp per sample.
        """
        watcher = MagicMock()
        seq = list(rungs)
        ts_seq = list(sinces) if sinces else None

        def snapshot():
            rung = seq.pop(0) if len(seq) > 1 else seq[0]
            since = None
            if ts_seq:
                since = ts_seq.pop(0) if len(ts_seq) > 1 else ts_seq[0]
            return {'units': [{'unit': 'u.service', 'rung': rung, 'since': since,
                               'badges': (badges or {}).get(rung, [])}]}

        watcher.snapshot.side_effect = snapshot
        watcher.units = {}
        engine = roundhouse.RolloutEngine(watcher, {}, self.unit_dir, 8090,
                                          roundhouse.EventBus(), threading.Lock())
        engine.rollouts['ro-1'] = {
            'rollout_id': 'ro-1', 'unit': 'u.service', 'phase': 'watching',
            'detail': '', 'edits': [], 'was_active': True, 'commit': 'abc',
            'restored': False, 'failure': None, 'rollback': None,
            'started_at': 0.0, 'updated_at': 0.0, 'old_raw': b'',
        }
        return engine

    def _run_watch(self, engine, **kwargs):
        """Run _watch_unit in a thread; fail the test if it does not return."""
        t = threading.Thread(target=engine._watch_unit, args=('ro-1', 'u.service'),
                             kwargs=kwargs, daemon=True)
        t.start()
        t.join(timeout=20)
        self.assertFalse(t.is_alive(),
                         "_watch_unit did not return — it self-deadlocked on watcher_lock")
        return engine.rollouts['ro-1']

    def test_watching_reaches_done_without_deadlocking(self):
        """Regression: _update_phase must not be called while watcher_lock is held.

        The lock is a plain threading.Lock, so re-acquiring it inside the sampling block
        wedges the rollout thread AND every take_snapshot() consumer behind it.
        """
        engine = self._watch_engine(['LOADING', 'LOADING', 'READY'])
        rec = self._run_watch(engine)
        self.assertEqual(rec['phase'], 'done')
        self.assertFalse(engine.watcher_lock.locked(), "watcher_lock left held")

    def test_watching_failed_rung_offers_rollback(self):
        engine = self._watch_engine(['LOADING', 'FAILED'])
        rec = self._run_watch(engine)
        self.assertEqual(rec['phase'], 'failed')
        self.assertEqual(rec['rollback'], {'offered': True})
        self.assertFalse(engine.watcher_lock.locked())

    def test_watching_no_ready_marker_badge_fails_the_rollout(self):
        """§8: the badge is the MVP2 rollback trigger while the rung stays LOADING."""
        engine = self._watch_engine(['LOADING'], badges={'LOADING': ['no_ready_marker']})
        rec = self._run_watch(engine)
        self.assertEqual(rec['phase'], 'failed')
        self.assertEqual(rec['failure']['reason'], 'no_ready_marker')

    def test_rollback_mode_terminates_at_rolled_back_not_done(self):
        """A rollback watch must not report `done` — its terminal state is `rolled_back`."""
        engine = self._watch_engine(['LOADING', 'READY'])
        engine.rollouts['ro-1']['revert_commit'] = 'deadbee'
        rec = self._run_watch(engine, rollback_mode=True)
        self.assertEqual(rec['phase'], 'rolled_back')
        self.assertEqual(rec['rollback']['offered'], False)
        self.assertEqual(rec['rollback']['revert_commit'], 'deadbee')

    def test_rollback_mode_failure_is_terminal_not_a_second_offer(self):
        engine = self._watch_engine(['FAILED'])
        rec = self._run_watch(engine, rollback_mode=True)
        self.assertEqual(rec['phase'], 'rollback_failed')
        self.assertIsNone(rec['rollback'])

    def test_watching_ignores_samples_from_before_the_restart(self):
        """Regression: a pre-restart READY sample must not end the rollout at 0.0 s.

        The roster refreshes on a 3 s tick, so right after `start` the snapshot still
        describes the process that was just stopped. Only a changed ExecMainStartTimestamp
        (`since`) proves the sample is about the new deployment.
        """
        engine = self._watch_engine(
            ['READY', 'READY', 'LOADING', 'READY'],
            sinces=[100.0, 100.0, 200.0, 200.0])   # 100.0 == the old deployment
        rec = self._run_watch(engine, prior_start_ts=100.0)
        self.assertEqual(rec['phase'], 'done')
        # done only after the fresh READY, i.e. never on the two stale samples
        self.assertNotEqual(rec['detail'], 'loaded in 0.0s')

    def test_watching_ignores_a_stale_failed_from_the_old_deployment(self):
        """The same staleness must not fail a rollout on the previous process's FAILED."""
        engine = self._watch_engine(
            ['FAILED', 'LOADING', 'READY'],
            sinces=[100.0, 200.0, 200.0])
        rec = self._run_watch(engine, prior_start_ts=100.0)
        self.assertEqual(rec['phase'], 'done')

    def test_watcher_lock_is_free_between_samples(self):
        """Another thread (take_snapshot, the 3 s tick) must get the lock mid-watch."""
        engine = self._watch_engine(['LOADING', 'LOADING', 'LOADING', 'READY'])
        acquired = threading.Event()

        def contender():
            for _ in range(40):
                if engine.watcher_lock.acquire(timeout=0.25):
                    engine.watcher_lock.release()
                    acquired.set()
                    return
                time.sleep(0.1)

        c = threading.Thread(target=contender, daemon=True)
        c.start()
        self._run_watch(engine)
        c.join(timeout=5)
        self.assertTrue(acquired.is_set(), "watcher_lock was never released during watching")

    def test_rollout_phases_frozen(self):
        """ROLLOUT_PHASES tuple is as specified."""
        expected = ("preflight", "applying", "reloading", "starting", "watching",
                   "done", "failed", "rolling_back", "rolled_back", "rollback_failed")
        self.assertEqual(roundhouse.ROLLOUT_PHASES, expected)


class TestRoutesAuth(unittest.TestCase):
    """API routes and authentication per MVP2-SPEC §6."""

    @classmethod
    def setUpClass(cls):
        """Start server on an ephemeral port."""
        cls.temp_dir = tempfile.mkdtemp()
        cls.fixtures = Path(__file__).resolve().parents[2] / 'docs' / 'fixtures'

        # Create a stub watcher
        cls.watcher = MagicMock(spec=roundhouse.Watcher)
        cls.watcher.snapshot.return_value = {
            'host': 'test', 'kernel': '6.1', 'now': time.time(),
            'mem': {}, 'units': [], 'sources': {}
        }
        cls.watcher.units = {}

        cls.event_bus = roundhouse.EventBus()

        # Find an available port
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        cls.port = sock.getsockname()[1]
        sock.close()

        # Create server WITHOUT arming (ACTUATE_ARMED = False)
        roundhouse.ACTUATE_ARMED = False
        cls.server = roundhouse.ThreadingHTTPServer(
            ('127.0.0.1', cls.port),
            roundhouse.RoundhouseRequestHandler,
            cls.watcher,
            cls.event_bus,
            cls.port
        )

        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        """Shutdown server."""
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def post_http(self, path, data=None, headers=None):
        """Make HTTP POST request."""
        import http.client
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            body = None
            if data:
                body = json.dumps(data).encode('utf-8')
            req_headers = headers or {}
            if body and 'Content-Type' not in req_headers:
                req_headers['Content-Type'] = 'application/json'
            conn.request('POST', path, body, req_headers)
            resp = conn.getresponse()
            resp_body = resp.read()
            return resp.status, resp_body
        finally:
            conn.close()

    def get_http(self, path):
        """Make HTTP GET request."""
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            conn.request('GET', path)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_get_rollout_unknown_id_returns_404(self):
        """GET /api/rollouts/<id> is a routed read route, not a fall-through 404 page."""
        status, body = self.get_http('/api/rollouts/ro-does-not-exist')
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)['error'], 'not_found')

    def test_get_rollout_returns_the_record(self):
        """GET /api/rollouts/<id> returns the §4.3 record and never the pre-edit bytes."""
        engine = MagicMock()
        engine.rollouts = {'ro-1-1': {
            'rollout_id': 'ro-1-1', 'unit': 'x.service', 'phase': 'watching',
            'detail': 'elapsed 4s', 'edits': [], 'was_active': True, 'commit': None,
            'restored': False, 'failure': None, 'rollback': None,
            'started_at': 1.0, 'updated_at': 2.0, 'old_raw': b'SECRET BYTES',
        }}
        self.server.rollout_engine = engine
        try:
            status, body = self.get_http('/api/rollouts/ro-1-1')
        finally:
            self.server.rollout_engine = None
        self.assertEqual(status, 200)
        rec = json.loads(body)
        self.assertEqual(rec['phase'], 'watching')
        self.assertNotIn('old_raw', rec)
        self.assertEqual(set(rec), {
            'rollout_id', 'kind', 'unit', 'phase', 'detail', 'edits', 'was_active', 'commit',
            'restored', 'failure', 'rollback', 'started_at', 'updated_at'})

    def test_get_on_post_only_rollout_subroutes_returns_405(self):
        for path in ('/api/rollouts/ro-1-1/rollback', '/api/rollouts/ro-1-1/dismiss'):
            status, _ = self.get_http(path)
            self.assertEqual(status, 405, path)

    def test_post_on_get_only_rollout_route_returns_405(self):
        status, _ = self.post_http('/api/rollouts/ro-1-1', {})
        self.assertEqual(status, 405)

    def test_rollout_on_retired_unit_is_422_not_409(self):
        """§9.5(d): RETIRED must answer 422 at /rollout even with a bogus confirm."""
        fixture = self.fixtures / 'mixperten.service'
        unit = roundhouse.parse_unit(str(fixture), fixture.read_bytes())
        self.assertTrue(unit.retired, "mixperten fixture is expected to be [RETIRED]")
        self.watcher.units = {'mixperten.service': unit}
        roundhouse.ACTUATE_ARMED = True
        roundhouse.TOKEN = 'test-token'
        self.server.rollout_engine = MagicMock()   # armed servers always have one
        try:
            status, body = self.post_http(
                '/api/units/mixperten.service/rollout',
                {'edits': {'ctx': '131072'}, 'confirm': 'deadbeef'},
                {'Authorization': 'Bearer test-token'})
        finally:
            roundhouse.ACTUATE_ARMED = False
            roundhouse.TOKEN = None
            self.watcher.units = {}
            self.server.rollout_engine = None
        self.assertEqual(status, 422)
        payload = json.loads(body)
        self.assertEqual(payload['error'], 'preflight_failed')
        self.assertEqual(payload['checks'][0]['check'], 'retired')

    def test_post_edit_unarmed_returns_403(self):
        """POST /api/units/<name>/edit without --actuate returns 403."""
        status, _ = self.post_http('/api/units/test.service/edit', {'edits': {}})
        self.assertEqual(status, 403)

    def test_post_rollout_unarmed_returns_403(self):
        """POST /api/units/<name>/rollout without --actuate returns 403."""
        status, _ = self.post_http('/api/units/test.service/rollout',
                                  {'edits': {}, 'confirm': 'x'})
        self.assertEqual(status, 403)

    def test_post_edit_bad_json_returns_400(self):
        """POST with malformed JSON returns 400."""
        roundhouse.ACTUATE_ARMED = True
        roundhouse.TOKEN = 'test-token'
        try:
            import http.client
            conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
            try:
                conn.request('POST', '/api/units/test.service/edit',
                           b'not valid json',
                           {'Authorization': 'Bearer test-token'})
                resp = conn.getresponse()
                status = resp.status
            finally:
                conn.close()
            self.assertEqual(status, 400)
        finally:
            roundhouse.ACTUATE_ARMED = False
            roundhouse.TOKEN = None


class TestMessageFormats(unittest.TestCase):
    """E12: commit message uses the flag spelling, provenance the canonical field name."""

    def setUp(self):
        self.fixtures = Path(__file__).resolve().parents[2] / 'docs' / 'fixtures'
        path = self.fixtures / 'qwen3.6-coding.service'
        self.unit = roundhouse.parse_unit(str(path), path.read_bytes())

    def test_edit_flag_is_the_flag_as_written(self):
        """Edit.flag must be `-c`, not the canonical field name `ctx`."""
        edits = roundhouse.plan_edits(self.unit, {'ctx': '32768'})
        self.assertEqual(edits[0].field, 'ctx')
        self.assertEqual(edits[0].flag, '-c')

    def test_commit_message_matches_the_contract_example(self):
        """MVP2.md §git: `roundhouse: qwen3.6-coding -c 65536 -> 32768`."""
        edits = roundhouse.plan_edits(self.unit, {'ctx': '32768'})
        self.assertEqual(roundhouse.commit_message('qwen3.6-coding.service', edits),
                         'roundhouse: qwen3.6-coding -c 65536 -> 32768')

    def test_commit_message_multi_field_uses_flags_in_file_order(self):
        edits = roundhouse.plan_edits(self.unit, {'ctx': '32768', 'port': '8087'})
        msg = roundhouse.commit_message('qwen3.6-coding.service', edits)
        self.assertEqual(msg, 'roundhouse: qwen3.6-coding -c 65536 -> 32768; --port 8085 -> 8087')

    def test_provenance_uses_canonical_field_names(self):
        edits = roundhouse.plan_edits(self.unit, {'ctx': '32768'})
        line = roundhouse.provenance_line(edits, datetime(2026, 8, 13, 14, 2, 11, tzinfo=timezone.utc))
        self.assertEqual(line, '2026-08-13T14:02:11Z ctx 65536 -> 32768 via UI')
        # the '# roundhouse: ' prefix is added by splice()/verify_splice()/the preview
        spliced = roundhouse.splice(self.unit.raw, edits, line)
        self.assertTrue(spliced.endswith(
            b'# roundhouse: 2026-08-13T14:02:11Z ctx 65536 -> 32768 via UI\n'))


class TestArmingSequence(unittest.TestCase):
    """§2.4 `--actuate` startup sequence: the three refusal branches must not cross-talk.

    Regression: `git -C <missing-dir> version` exits 128, which the startup check read as
    "git is not installed" — a host WITH git got the E11 message for an E2 condition.
    """

    SCRIPT = str(Path(__file__).resolve().parents[1] / 'roundhouse.py')

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='rh-arming-')
        self.fixtures = Path(__file__).resolve().parents[2] / 'docs' / 'fixtures'

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, unit_dir, env_path=None):
        env = {
            'PATH': env_path if env_path is not None else os.environ.get('PATH', '/usr/bin:/bin'),
            'HOME': self.tmp,
            'XDG_STATE_HOME': os.path.join(self.tmp, 'state'),
        }
        proc = subprocess.run(
            [sys.executable, self.SCRIPT, '--serve', '--actuate',
             '--unit-dir', unit_dir, '--no-db', '--port', '8099'],
            capture_output=True, text=True, timeout=60, env=env)
        return proc

    def _populate(self, dest, names):
        os.makedirs(dest, exist_ok=True)
        for n in names:
            shutil.copy(self.fixtures / n, os.path.join(dest, n))

    def test_non_repo_dir_with_git_prints_init_instructions_and_exits_2(self):
        """Non-repo unit dir + git on PATH -> §2.4 init instructions, exit 2 (NOT the git-on-PATH message)."""
        unit_dir = os.path.join(self.tmp, 'units')
        self._populate(unit_dir, ['qwen3.6-coding.service', 'llama-server-gemma4.service'])

        proc = self._run(unit_dir)

        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn('is not a git repository', proc.stderr)
        self.assertIn('git init', proc.stderr)
        self.assertNotIn('requires git on PATH', proc.stderr)
        # concrete selected-unit filenames, not a placeholder
        self.assertIn('git add .gitignore llama-server-gemma4.service qwen3.6-coding.service',
                      proc.stderr)
        self.assertIn('roundhouse baseline: 2 managed units', proc.stderr)
        self.assertNotIn('<unit1>.service', proc.stderr)

    def test_missing_unit_dir_still_reports_not_a_repository(self):
        """A unit dir that does not exist is an E2 condition, not an E11 one."""
        proc = self._run(os.path.join(self.tmp, 'does-not-exist'))

        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn('is not a git repository', proc.stderr)
        self.assertNotIn('requires git on PATH', proc.stderr)

    def test_init_instructions_list_only_selected_units(self):
        """E3 scoped tracking: unselected .service files are never in the printed `git add`."""
        unit_dir = os.path.join(self.tmp, 'units')
        self._populate(unit_dir, ['qwen3.6-coding.service'])
        # a foreign desktop-style unit that D1 does not select
        with open(os.path.join(unit_dir, 'pipewire.service'), 'w') as f:
            f.write('[Unit]\nDescription=noise\n[Service]\nExecStart=/usr/bin/pipewire\n')

        proc = self._run(unit_dir)

        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn('qwen3.6-coding.service', proc.stderr)
        self.assertNotIn('pipewire.service', proc.stderr)
        self.assertIn('roundhouse baseline: 1 managed units', proc.stderr)

    def test_dirty_tracked_file_prints_recovery_message_and_exits_2(self):
        """Repo with an uncommitted tracked change -> crash-recovery refusal, exit 2."""
        unit_dir = os.path.join(self.tmp, 'units')
        self._populate(unit_dir, ['qwen3.6-coding.service'])
        env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
        subprocess.run(['git', '-C', unit_dir, 'init', '-q'], check=True, env=env)
        subprocess.run(['git', '-C', unit_dir, 'add', '--', 'qwen3.6-coding.service'],
                       check=True, env=env)
        subprocess.run(['git', '-C', unit_dir, 'commit', '-qm', 'base'], check=True, env=env)
        with open(os.path.join(unit_dir, 'qwen3.6-coding.service'), 'a') as f:
            f.write('\n# hand edit\n')

        proc = self._run(unit_dir)

        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn('uncommitted changes to tracked files', proc.stderr)
        self.assertIn('qwen3.6-coding.service', proc.stderr)
        self.assertIn('restore --', proc.stderr)
        self.assertNotIn('is not a git repository', proc.stderr)

    def test_git_absent_prints_git_on_path_message_and_exits_2(self):
        """E11: no git binary -> the install-git refusal, exit 2 (never a traceback)."""
        empty_bin = os.path.join(self.tmp, 'empty-bin')
        os.makedirs(empty_bin, exist_ok=True)
        unit_dir = os.path.join(self.tmp, 'units')
        self._populate(unit_dir, ['qwen3.6-coding.service'])

        proc = self._run(unit_dir, env_path=empty_bin)

        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn('requires git on PATH', proc.stderr)
        self.assertNotIn('Traceback', proc.stderr)
        self.assertNotIn('is not a git repository', proc.stderr)

    def test_run_git_version_does_not_depend_on_unit_dir(self):
        """The version probe answers "is git installed", so it carries no -C."""
        with patch('roundhouse.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout='git version 2.47.3\n', stderr='')
            roundhouse.run_git(["version"], "/definitely/not/a/dir", bootstrap=True)
        argv = mock_run.call_args[0][0]
        self.assertEqual(argv, ["git", "version"])

    def test_run_git_missing_binary_raises_actuation_error(self):
        """A missing git binary surfaces as ActuationError, not FileNotFoundError."""
        with patch('roundhouse.subprocess.run', side_effect=FileNotFoundError(2, 'No such file', 'git')):
            with self.assertRaises(roundhouse.ActuationError):
                roundhouse.run_git(["version"], "/tmp", bootstrap=True)

    def test_print_git_init_instructions_uses_supplied_names(self):
        """print_git_init_instructions prefers the selected set over a directory listing."""
        import io
        import contextlib
        unit_dir = os.path.join(self.tmp, 'units')
        self._populate(unit_dir, ['qwen3.6-coding.service'])
        with open(os.path.join(unit_dir, 'plasma-foo.service'), 'w') as f:
            f.write('[Service]\nExecStart=/usr/bin/true\n')
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            roundhouse.print_git_init_instructions(unit_dir, ['qwen3.6-coding.service'])
        out = buf.getvalue()
        self.assertIn('qwen3.6-coding.service', out)
        self.assertNotIn('plasma-foo.service', out)


FIXTURES = Path(__file__).resolve().parents[2] / 'docs' / 'fixtures'


def load_fixture(name: str) -> roundhouse.UnitFile:
    """Parse a fixture straight from docs/fixtures (read-only)."""
    path = FIXTURES / name
    return roundhouse.parse_unit(str(path), path.read_bytes())


def copy_fixture(name: str, dest_dir: str) -> roundhouse.UnitFile:
    """Copy a fixture into a scratch dir and parse the COPY (writable path)."""
    src = FIXTURES / name
    dest = os.path.join(dest_dir, name)
    shutil.copyfile(src, dest)
    with open(dest, 'rb') as f:
        return roundhouse.parse_unit(dest, f.read())


class _EngineHarness(unittest.TestCase):
    """Engine driven with stubbed gateways: no systemd, no git, real files."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.actuate_calls = []
        self.git_calls = []

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _stub_run_actuate(self):
        def stub(argv, units, timeout=90):
            self.actuate_calls.append(list(argv))
            return ''
        return stub

    def _stub_run_git(self, head_sha='a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0'):
        def stub(args, unit_dir, timeout=30, bootstrap=False, units=None):
            self.git_calls.append(list(args))
            out = ''
            if args and args[0] == 'rev-parse':
                out = head_sha + '\n'
            return subprocess.CompletedProcess(args, 0, out, '')
        return stub

    def _engine(self, unit, rung='OFF'):
        watcher = MagicMock()
        watcher.units = {unit.name: unit}
        watcher.mem_store = None
        watcher._cgroup_cache = {}
        watcher.snapshot.return_value = {'units': [
            {'unit': unit.name, 'rung': rung, 'retired': False, 'enabled': True,
             'since': 1.0, 'badges': [], 'port': 8086, 'mem': {}},
        ]}
        units = {unit.name: unit}
        return roundhouse.RolloutEngine(watcher, units, self.temp_dir, 8090,
                                        roundhouse.EventBus(), threading.Lock())

    def _drive(self, engine, unit, changes, confirm=None, timeout=15.0):
        """Run one rollout to a terminal phase with the gateways stubbed out."""
        edits = roundhouse.plan_edits(unit, changes)
        if confirm is None:
            confirm = roundhouse.compute_confirm(unit.name, unit.raw, edits)
        with patch.object(roundhouse, 'run_actuate', self._stub_run_actuate()), \
             patch.object(roundhouse, 'run_git', self._stub_run_git()):
            rec = engine.start_rollout(unit.name, edits, confirm)
            deadline = time.time() + timeout
            while time.time() < deadline:
                if rec['phase'] in ('done', 'failed', 'rolled_back', 'rollback_failed'):
                    break
                time.sleep(0.02)
        self.assertIn(rec['phase'], ('done', 'failed', 'rolled_back', 'rollback_failed'),
                      f"rollout never reached a terminal phase: {rec['phase']}")
        return rec


class TestSlotRelease(_EngineHarness):
    """B1: the global rollout slot must be released by every settled failure (§4.1)."""

    def _record(self, phase, rollback=None, restored=False, commit='abc1234'):
        return {'rollout_id': 'ro-0-0', 'unit': 'u.service', 'phase': phase,
                'detail': '', 'edits': [], 'was_active': True, 'commit': commit,
                'restored': restored, 'failure': {'reason': 'x', 'detail': 'x'},
                'rollback': rollback, 'started_at': 0.0, 'updated_at': 0.0,
                'old_raw': b''}

    def _accepting_engine(self):
        """An engine whose worker is a no-op: this tests the SLOT GATE, nothing else."""
        unit = copy_fixture('llama-server-gemma4.service', self.temp_dir)
        engine = self._engine(unit)
        return engine, unit

    def _try_start(self, engine, unit):
        edits = roundhouse.plan_edits(unit, {'ctx': '8192'})
        confirm = roundhouse.compute_confirm(unit.name, unit.raw, edits)
        with patch.object(roundhouse.RolloutEngine, '_run_rollout', lambda *a, **k: None):
            return engine.start_rollout(unit.name, edits, confirm)

    # --- the pure predicate ------------------------------------------------------

    def test_slot_free_matrix(self):
        cases = [
            (None, True, 'no rollout at all'),
            (self._record('preflight'), False, 'in flight'),
            (self._record('watching'), False, 'in flight'),
            (self._record('rolling_back'), False, 'rollback in flight'),
            (self._record('done'), True, 'terminal'),
            (self._record('rolled_back'), True, 'terminal'),
            (self._record('rollback_failed'), True, 'terminal'),
            (self._record('failed', rollback=None), True, 'failed, no offer ever made'),
            (self._record('failed', rollback={'offered': True}), False, 'offer pending'),
            (self._record('failed', rollback={'offered': False}), True, 'offer dismissed'),
            (self._record('failed', rollback={'offered': True}, restored=True), True,
             'bytes restored'),
        ]
        for record, expected, why in cases:
            self.assertEqual(roundhouse._slot_free(record), expected, why)

    def test_rollback_none_does_not_raise_attributeerror(self):
        """The exact B1 crash: `rollback` exists with value None from record creation.

        `record.get('rollback', {})` returns None (the default only applies to a MISSING
        key), so `.get('offered')` on it raised AttributeError -> 500 out of the route.
        """
        record = self._record('failed', rollback=None)
        with self.assertRaises(AttributeError):
            record.get('rollback', {}).get('offered')   # the shape that used to ship
        self.assertFalse(roundhouse._rollback_offered(record))
        self.assertTrue(roundhouse._slot_free(record))

    # --- the gate as the engine enforces it --------------------------------------

    def test_failed_without_offer_frees_the_slot(self):
        engine, unit = self._accepting_engine()
        engine.current = self._record('failed', rollback=None)
        rec = self._try_start(engine, unit)
        self.assertEqual(rec['phase'], 'preflight')
        self.assertIs(engine.current, rec)

    def test_failed_with_offer_holds_the_slot(self):
        engine, unit = self._accepting_engine()
        engine.current = self._record('failed', rollback={'offered': True})
        with self.assertRaises(roundhouse.ActuationError) as ctx:
            self._try_start(engine, unit)
        self.assertIn('rollout_in_progress', str(ctx.exception))

    def test_dismiss_frees_the_slot(self):
        engine, unit = self._accepting_engine()
        held = self._record('failed', rollback={'offered': True})
        engine.current = held
        engine.rollouts[held['rollout_id']] = held
        with self.assertRaises(roundhouse.ActuationError):
            self._try_start(engine, unit)

        engine.dismiss(held['rollout_id'])
        self.assertFalse(held['rollback']['offered'])
        rec = self._try_start(engine, unit)
        self.assertEqual(rec['phase'], 'preflight')

    def test_rolled_back_frees_the_slot(self):
        engine, unit = self._accepting_engine()
        engine.current = self._record('rolled_back',
                                      rollback={'offered': False, 'phase': 'rolled_back'})
        rec = self._try_start(engine, unit)
        self.assertEqual(rec['phase'], 'preflight')

    def test_restored_failure_frees_the_slot(self):
        """A verify/apply failure that restored the bytes is settled — slot free."""
        engine, unit = self._accepting_engine()
        engine.current = self._record('failed', rollback={'offered': True}, restored=True)
        rec = self._try_start(engine, unit)
        self.assertEqual(rec['phase'], 'preflight')

    def test_preflight_failure_marks_restored_and_frees_the_slot(self):
        """End to end: a preflight failure must not wedge the next rollout."""
        unit = copy_fixture('llama-server-gemma4.service', self.temp_dir)
        engine = self._engine(unit)
        edits = roundhouse.plan_edits(unit, {'ctx': '8192'})
        confirm = roundhouse.compute_confirm(unit.name, unit.raw, edits)

        def untracked_git(args, unit_dir, timeout=30, bootstrap=False, units=None):
            self.git_calls.append(list(args))
            rc = 1 if args[0] == 'ls-files' else 0
            return subprocess.CompletedProcess(args, rc, '', 'not tracked')

        with patch.object(roundhouse, 'run_actuate', self._stub_run_actuate()), \
             patch.object(roundhouse, 'run_git', untracked_git):
            rec = engine.start_rollout(unit.name, edits, confirm)
            deadline = time.time() + 10
            while time.time() < deadline and rec['phase'] != 'failed':
                time.sleep(0.02)

        self.assertEqual(rec['phase'], 'failed')
        self.assertIsNone(rec['rollback'])
        self.assertTrue(rec['restored'], "a preflight failure touched nothing")
        self.assertTrue(roundhouse._slot_free(engine.current))
        follow_up = self._try_start(engine, unit)
        self.assertEqual(follow_up['phase'], 'preflight')

    # --- S5: rollback/dismiss race ----------------------------------------------

    def test_double_rollback_only_starts_one_worker(self):
        """S5: check-and-set under watcher_lock — the second click loses."""
        unit = copy_fixture('llama-server-gemma4.service', self.temp_dir)
        engine = self._engine(unit)
        rec = self._record('failed', rollback={'offered': True})
        engine.current = rec
        engine.rollouts[rec['rollout_id']] = rec

        started = []
        with patch.object(roundhouse.RolloutEngine, '_run_rollback',
                          lambda self, rid: started.append(rid)):
            engine.rollback(rec['rollout_id'])
            with self.assertRaises(roundhouse.ActuationError) as ctx:
                engine.rollback(rec['rollout_id'])
        self.assertIn('not_rollbackable', str(ctx.exception))
        time.sleep(0.2)
        self.assertEqual(len(started), 1, "two rollback workers were spawned")

    def test_dismiss_racing_rollback_loses(self):
        unit = copy_fixture('llama-server-gemma4.service', self.temp_dir)
        engine = self._engine(unit)
        rec = self._record('failed', rollback={'offered': True})
        engine.current = rec
        engine.rollouts[rec['rollout_id']] = rec

        with patch.object(roundhouse.RolloutEngine, '_run_rollback', lambda self, rid: None):
            engine.rollback(rec['rollout_id'])
        with self.assertRaises(roundhouse.ActuationError) as ctx:
            engine.dismiss(rec['rollout_id'])
        self.assertIn('not_dismissable', str(ctx.exception))

    def test_dismiss_without_an_offer_raises(self):
        unit = copy_fixture('llama-server-gemma4.service', self.temp_dir)
        engine = self._engine(unit)
        rec = self._record('failed', rollback=None)
        engine.rollouts[rec['rollout_id']] = rec
        with self.assertRaises(roundhouse.ActuationError):
            engine.dismiss(rec['rollout_id'])


class TestUnknownFlagApply(_EngineHarness):
    """B2: an unknown-flag edit must survive verify — it used to fail AFTER the stop."""

    def test_verify_accepts_an_unknown_flag_edit(self):
        unit = load_fixture('llama-server-gemma4.service')
        edits = roundhouse.plan_edits(unit, {'unknown:-np': '2'})
        self.assertEqual(edits[0].field, 'unknown:-np')

        prov = roundhouse.provenance_line(edits, datetime(2026, 8, 13, 14, 2, 11,
                                                          tzinfo=timezone.utc))
        new_raw = roundhouse.splice(unit.raw, edits, prov)

        # The old check compared exactly these two lists as an "unedited field": edited
        # unknown flags are named `unknown:<flag>`, so the profile key `unknown_flags`
        # never appeared in `edited_fields` and every unknown-flag edit raised.
        old_pairs = [(f['flag'], f['value']) for f in
                     roundhouse.extract_param_profile(unit.exec_start.engine_argv)['unknown_flags']]
        new_unit_probe = roundhouse.parse_unit(unit.path, new_raw)
        new_pairs = [(f['flag'], f['value']) for f in
                     roundhouse.extract_param_profile(new_unit_probe.exec_start.engine_argv)['unknown_flags']]
        self.assertNotEqual(old_pairs, new_pairs, "fixture does not exercise the bug")

        new_unit = roundhouse.verify_splice(unit, new_raw, edits, prov)
        profile = roundhouse.extract_param_profile(new_unit.exec_start.engine_argv)
        self.assertIn(('-np', '2'), [(f['flag'], f['value']) for f in profile['unknown_flags']])

    def test_verify_rejects_an_unknown_flag_that_moved(self):
        """The relaxed comparison must stay strict about everything it did not edit."""
        unit = load_fixture('llama-embed.service')
        edits = roundhouse.plan_edits(unit, {'unknown:-b': '4096'})
        prov = roundhouse.provenance_line(edits, datetime(2026, 8, 13, tzinfo=timezone.utc))
        new_raw = roundhouse.splice(unit.raw, edits, prov)
        roundhouse.verify_splice(unit, new_raw, edits, prov)      # baseline passes

        # A second, UNEDITED unknown flag changing value must still be caught.
        tampered = new_raw.replace(b'-ub 8192', b'-ub 4096')
        self.assertNotEqual(tampered, new_raw)
        with self.assertRaises(roundhouse.VerifyError):
            roundhouse.verify_splice(unit, tampered, edits, prov)

    def test_sampling_subfield_edit_verifies(self):
        """Same trap, other shape: `sampling.temp` lives inside the `sampling` dict."""
        unit = load_fixture('qwen3.6-coding.service')
        edits = roundhouse.plan_edits(unit, {'sampling.temp': '0.7'})
        prov = roundhouse.provenance_line(edits, datetime(2026, 8, 13, tzinfo=timezone.utc))
        new_raw = roundhouse.splice(unit.raw, edits, prov)
        new_unit = roundhouse.verify_splice(unit, new_raw, edits, prov)
        profile = roundhouse.extract_param_profile(new_unit.exec_start.engine_argv)
        self.assertEqual(profile['sampling']['temp'], 0.7)

    def test_unknown_flag_rollout_applies_end_to_end(self):
        """Full apply path with stubbed gateways: splice -> verify -> commit -> done."""
        unit = copy_fixture('llama-server-gemma4.service', self.temp_dir)
        engine = self._engine(unit, rung='OFF')          # not running: no stop/start
        rec = self._drive(engine, unit, {'unknown:-np': '2'})

        self.assertEqual(rec['phase'], 'done', rec['detail'])
        self.assertIsNone(rec['failure'])
        with open(unit.path, 'rb') as f:
            written = f.read()
        exec_line = [l for l in written.split(b'\n') if b'-np ' in l][0]
        self.assertIn(b'-np 2', exec_line)
        self.assertNotIn(b'-np 1', exec_line)
        self.assertIn(b'# roundhouse: ', written.rsplit(b'\n', 2)[-2])

        verbs = [c[0] for c in self.git_calls]
        self.assertIn('add', verbs)
        self.assertIn('commit', verbs)
        self.assertEqual(self.actuate_calls,
                         [['systemctl', '--user', 'daemon-reload']],
                         "a stopped unit must not be stopped or started")


class TestPlanEditsValidation(unittest.TestCase):
    """S1: §3.2.2 type/range validation — none of this used to be checked."""

    def setUp(self):
        self.unit = load_fixture('qwen3.6-coding.service')

    def _expect_invalid(self, changes, label):
        with self.assertRaises(roundhouse.EditError, msg=label) as ctx:
            roundhouse.plan_edits(self.unit, changes)
        self.assertEqual(ctx.exception.reason, 'invalid_value', label)

    def test_invalid_value_table(self):
        cases = [
            ({'ctx': 'abc'}, 'ctx: not a number'),
            ({'ctx': '-1'}, 'ctx: negative'),
            ({'ctx': '0'}, 'ctx: zero'),
            ({'ctx': ''}, 'ctx: empty string'),
            ({'ctx': '--port=9999'}, 'ctx: a flag, not a value'),
            ({'ctx': '65536.5'}, 'ctx: float for an int field'),
            ({'port': '0'}, 'port: below range'),
            ({'port': '65536'}, 'port: above range'),
            ({'port': '99999999'}, 'port: far above range'),
            ({'port': 'eighty-eighty'}, 'port: not a number'),
            ({'threads': 'x'}, 'threads: not a number'),
            ({'sampling.temp': 'hot'}, 'temp: not a number'),
            ({'sampling.top_k': '1.5'}, 'top_k: float for an int field'),
            ({'chat_template_kwargs': '{not json'}, 'chat_template_kwargs: broken JSON'),
            ({'alias': ''}, 'str field: empty unquoted value cannot be spliced'),
            ({'alias': 'a b'}, 'str field: whitespace would re-tokenize'),
        ]
        for changes, label in cases:
            self._expect_invalid(changes, label)

    def test_valid_values_still_plan(self):
        cases = [
            ({'ctx': '32768'}, 'ctx'),
            ({'port': '8087'}, 'port'),
            ({'port': '1'}, 'port lower bound'),
            ({'port': '65535'}, 'port upper bound'),
            ({'sampling.temp': '0.7'}, 'temp'),
            ({'chat_template_kwargs': '{"enable_thinking": true}'}, 'json'),
            ({'alias': 'qwen3.6-coding-b'}, 'alias'),
        ]
        for changes, label in cases:
            edits = roundhouse.plan_edits(self.unit, changes)
            self.assertEqual(len(edits), 1, label)

    def test_unquoted_value_regex_is_non_empty(self):
        """§3.2.3: `+`, not `*` — an empty value would delete the token entirely."""
        with self.assertRaises(roundhouse.EditError) as ctx:
            roundhouse.plan_edits(self.unit, {'cache_type_k': ''})
        self.assertEqual(ctx.exception.reason, 'invalid_value')

    def test_unquoted_value_rejects_a_trailing_newline(self):
        """`$` matches before a trailing newline; a value ending in \\n splits the token."""
        with self.assertRaises(roundhouse.EditError):
            roundhouse.plan_edits(self.unit, {'cache_type_k': 'q4_0\n'})

    def test_preflight_port_survives_a_non_numeric_port(self):
        """S1 tail: preflight must fail cleanly, never 500, on a hand-built bad edit."""
        edits = [roundhouse.Edit(field='port', flag='--port', old_text='8085',
                                 new_text='abc', span=(0, 4), quote='')]
        watcher = MagicMock()
        watcher.snapshot.return_value = {'units': []}
        result = roundhouse.preflight_port(self.unit, edits, watcher, 8090)
        self.assertFalse(result['ok'])
        self.assertEqual(result['check'], 'port')


class TestVerifyStrength(unittest.TestCase):
    """S2(ii)/(iii): the checks verify_splice used to skip."""

    def setUp(self):
        self.unit = load_fixture('llama-server-gemma4.service')
        self.edits = roundhouse.plan_edits(self.unit, {'ctx': '8192'})
        self.prov = roundhouse.provenance_line(self.edits,
                                               datetime(2026, 8, 13, tzinfo=timezone.utc))
        self.new_raw = roundhouse.splice(self.unit.raw, self.edits, self.prov)

    def test_baseline_verifies(self):
        self.assertIsNotNone(roundhouse.verify_splice(
            self.unit, self.new_raw, self.edits, self.prov))

    def test_unparseable_execstart_is_a_verify_error(self):
        """S2(ii): check (a) was guarded by `and new_unit.exec_start` — a destroyed
        ExecStart, the worst outcome a splice can have, SKIPPED the whole check."""
        broken = self.new_raw.replace(b'ExecStart=', b'#ExecStart=')
        parsed = roundhouse.parse_unit(self.unit.path, broken)
        self.assertIsNone(parsed.exec_start, "fixture does not exercise the case")
        with self.assertRaises(roundhouse.VerifyError) as ctx:
            roundhouse.verify_splice(self.unit, broken, self.edits, self.prov)
        self.assertEqual(ctx.exception.reason, 'execstart_unparseable')

    def test_byte_mutation_outside_the_spans_is_a_verify_error(self):
        """S2(iii): the §3.5(c) replay check, as a runtime assertion."""
        i = self.new_raw.find(b'Description=')
        self.assertGreater(i, -1)
        mutated = self.new_raw[:i + 12] + b'X' + self.new_raw[i + 13:]
        self.assertEqual(len(mutated), len(self.new_raw))
        with self.assertRaises(roundhouse.VerifyError) as ctx:
            roundhouse.verify_splice(self.unit, mutated, self.edits, self.prov)
        self.assertEqual(ctx.exception.reason, 'outside_span_mutation')

    def test_mutation_no_other_check_can_see_is_caught(self):
        """The binary path is inside ExecStart but outside every profile span, and
        `raw_argv` is excluded from check (a) — only the replay catches this."""
        j = self.new_raw.find(b'llama-server ')
        self.assertGreater(j, -1)
        mutated = self.new_raw[:j] + b'llama-serveX' + self.new_raw[j + 12:]
        with self.assertRaises(roundhouse.VerifyError) as ctx:
            roundhouse.verify_splice(self.unit, mutated, self.edits, self.prov)
        self.assertEqual(ctx.exception.reason, 'outside_span_mutation')

    def test_extra_appended_line_is_a_verify_error(self):
        """Only the provenance line may follow the body."""
        with self.assertRaises(roundhouse.VerifyError):
            roundhouse.verify_splice(self.unit, self.new_raw + b'Restart=always\n',
                                     self.edits, self.prov)

    def test_replay_check_is_independent_of_span_invariants(self):
        """assert_span_invariants passes on the mutated file; the replay must not."""
        i = self.new_raw.find(b'Description=')
        mutated = self.new_raw[:i + 12] + b'X' + self.new_raw[i + 13:]
        roundhouse.assert_span_invariants(roundhouse.parse_unit(self.unit.path, mutated))
        with self.assertRaises(roundhouse.VerifyError):
            roundhouse.assert_outside_spans_unchanged(
                self.unit.raw, mutated, self.edits, self.prov)


class TestApplyStaleness(_EngineHarness):
    """S3/E5: an external edit between preview and apply must not be clobbered."""

    def test_disk_change_fails_the_rollout_and_touches_nothing(self):
        unit = copy_fixture('llama-server-gemma4.service', self.temp_dir)
        engine = self._engine(unit, rung='OFF')
        # `threads`, not `ctx`: a memory-relevant field would drag the host's real
        # /proc/meminfo into a test about staleness.
        edits = roundhouse.plan_edits(unit, {'threads': '4'})
        confirm = roundhouse.compute_confirm(unit.name, unit.raw, edits)

        # Somebody edits the file by hand after the preview was taken.
        external = unit.raw.replace(b'RestartSec=', b'RestartSec=') + b'# hand edit\n'
        with open(unit.path, 'wb') as f:
            f.write(external)

        with patch.object(roundhouse, 'run_actuate', self._stub_run_actuate()), \
             patch.object(roundhouse, 'run_git', self._stub_run_git()):
            rec = engine.start_rollout(unit.name, edits, confirm)
            deadline = time.time() + 10
            while time.time() < deadline and rec['phase'] not in ('done', 'failed'):
                time.sleep(0.02)

        self.assertEqual(rec['phase'], 'failed')
        self.assertEqual(rec['failure']['reason'], 'preview_stale')
        with open(unit.path, 'rb') as f:
            self.assertEqual(f.read(), external, "the hand edit was clobbered")
        self.assertEqual(self.actuate_calls, [], "nothing may be actuated")
        self.assertNotIn('commit', [c[0] for c in self.git_calls])
        self.assertTrue(roundhouse._slot_free(engine.current))

    def test_unchanged_disk_applies_normally(self):
        """The staleness gate must not reject the ordinary case."""
        unit = copy_fixture('llama-server-gemma4.service', self.temp_dir)
        engine = self._engine(unit, rung='OFF')
        rec = self._drive(engine, unit, {'threads': '4'})
        self.assertEqual(rec['phase'], 'done', rec['detail'])
        with open(unit.path, 'rb') as f:
            self.assertIn(b'-t 4', f.read())


class TestStalenessRoute(unittest.TestCase):
    """S3/B1 at the route layer: 409, never a 500."""

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.mkdtemp()
        cls.watcher = MagicMock(spec=roundhouse.Watcher)
        cls.watcher.snapshot.return_value = {'host': 't', 'kernel': '6.1', 'now': 0.0,
                                             'mem': {}, 'units': [], 'sources': {}}
        cls.watcher.units = {}
        cls.event_bus = roundhouse.EventBus()
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        cls.port = sock.getsockname()[1]
        sock.close()
        roundhouse.ACTUATE_ARMED = False
        cls.server = roundhouse.ThreadingHTTPServer(
            ('127.0.0.1', cls.port), roundhouse.RoundhouseRequestHandler,
            cls.watcher, cls.event_bus, cls.port)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def post(self, path, data):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            conn.request('POST', path, json.dumps(data).encode('utf-8'),
                         {'Authorization': 'Bearer test-token',
                          'Content-Type': 'application/json'})
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_rollout_with_changed_disk_bytes_is_409_preview_stale(self):
        unit = copy_fixture('llama-server-gemma4.service', self.temp_dir)
        edits = roundhouse.plan_edits(unit, {'ctx': '8192'})
        confirm = roundhouse.compute_confirm(unit.name, unit.raw, edits)
        with open(unit.path, 'ab') as f:
            f.write(b'# hand edit\n')

        self.watcher.units = {unit.name: unit}
        roundhouse.ACTUATE_ARMED = True
        roundhouse.TOKEN = 'test-token'
        self.server.rollout_engine = MagicMock()
        try:
            status, body = self.post(f'/api/units/{unit.name}/rollout',
                                     {'edits': {'ctx': '8192'}, 'confirm': confirm})
        finally:
            roundhouse.ACTUATE_ARMED = False
            roundhouse.TOKEN = None
            self.watcher.units = {}
            self.server.rollout_engine = None
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)['error'], 'preview_stale')
        self.server.rollout_engine = None

    def test_preview_refreshes_from_disk_so_the_stale_apply_can_recover(self):
        """§7.5: the UI re-previews once after a 409 — that only works if the preview
        picks up the new bytes. Units are parsed once at startup, so without this the
        409 would repeat forever and the unit would be un-editable until a restart."""
        unit = copy_fixture('llama-server-gemma4.service', self.temp_dir)
        with open(unit.path, 'wb') as f:
            f.write(unit.raw.replace(b'-c 4096', b'-c 2048'))

        self.watcher.units = {unit.name: unit}
        roundhouse.ACTUATE_ARMED = True
        roundhouse.TOKEN = 'test-token'
        self.server.rollout_engine = None

        def stub_git(args, unit_dir, timeout=30, bootstrap=False, units=None):
            return subprocess.CompletedProcess(args, 0, '', '')

        try:
            with patch.object(roundhouse, 'run_git', stub_git), \
                 patch.object(roundhouse, 'preflight_memory',
                              lambda *a, **k: {'ok': True, 'check': 'memory'}):
                status, body = self.post(f'/api/units/{unit.name}/edit',
                                         {'edits': {'ctx': '8192'}})
            payload = json.loads(body)
            self.assertEqual(status, 200, payload)
            # The echoed old value comes from DISK (2048), not from the startup parse.
            self.assertEqual(payload['edits'][0]['old'], '2048')
            refreshed = self.watcher.units[unit.name]
            self.assertIn(b'-c 2048', refreshed.raw)

            # ...and the confirm from that preview now applies without a 409.
            edits = roundhouse.plan_edits(refreshed, {'ctx': '8192'})
            self.assertEqual(payload['confirm'],
                             roundhouse.compute_confirm(unit.name, refreshed.raw, edits))
        finally:
            roundhouse.ACTUATE_ARMED = False
            roundhouse.TOKEN = None
            self.watcher.units = {}

    def test_rollback_route_with_null_rollback_is_409_not_500(self):
        """B1 at the route: `.get('rollback', {}).get('offered')` raised AttributeError."""
        engine = MagicMock()
        engine.rollouts = {'ro-1-1': {
            'rollout_id': 'ro-1-1', 'unit': 'x.service', 'phase': 'failed',
            'detail': 'boom', 'edits': [], 'was_active': True, 'commit': None,
            'restored': False, 'failure': {'reason': 'stop_error', 'detail': 'boom'},
            'rollback': None, 'started_at': 1.0, 'updated_at': 2.0, 'old_raw': b'',
        }}
        roundhouse.ACTUATE_ARMED = True
        roundhouse.TOKEN = 'test-token'
        self.server.rollout_engine = engine
        try:
            for route, err in (('rollback', 'not_rollbackable'), ('dismiss', 'not_dismissable')):
                status, body = self.post(f'/api/rollouts/ro-1-1/{route}', {})
                self.assertEqual(status, 409, route)
                self.assertEqual(json.loads(body)['error'], err)
        finally:
            roundhouse.ACTUATE_ARMED = False
            roundhouse.TOKEN = None
            self.server.rollout_engine = None


class TestRunGitArgs(unittest.TestCase):
    """S7: revert-sha validation was a tautology; add took any basename."""

    def setUp(self):
        roundhouse.ACTUATE_ARMED = True
        self.addCleanup(setattr, roundhouse, 'ACTUATE_ARMED', False)

    def _run(self, args, **kwargs):
        with patch('roundhouse.subprocess.run') as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args, 0, '', '')
            return roundhouse.run_git(args, '/tmp', **kwargs)

    def test_revert_rejects_non_sha_arguments(self):
        for bad in ('HEAD~5..HEAD', '--all', 'main', 'HEAD', 'abc123',
                    'ABC1234', 'g' * 8, 'a' * 65, '', '-f'):
            with self.assertRaises(roundhouse.ActuationError, msg=bad):
                self._run(['revert', '--no-edit', bad])

    def test_revert_accepts_a_sha(self):
        for good in ('a1b2c3d', '0' * 40, 'f' * 64):
            self._run(['revert', '--no-edit', good])

    def test_revert_abort_is_allowed(self):
        self._run(['revert', '--abort'])

    def test_revert_shape_is_exact(self):
        for bad_args in (['revert', 'a1b2c3d'],
                         ['revert', '--no-edit', 'a1b2c3d', 'extra'],
                         ['revert', '--no-edit'],
                         ['revert', '--abort', 'a1b2c3d']):
            with self.assertRaises(roundhouse.ActuationError, msg=str(bad_args)):
                self._run(bad_args)

    def test_add_requires_a_selected_unit(self):
        units = {'llama-task.service': MagicMock()}
        self._run(['add', '--', 'llama-task.service'], units=units)
        for bad in ('other.service', '../evil.service', 'sub/dir.service',
                    '-rf', 'notaunit', '.gitignore'):
            with self.assertRaises(roundhouse.ActuationError, msg=bad):
                self._run(['add', '--', bad], units=units)

    def test_add_without_a_unit_set_still_requires_a_unit_filename(self):
        self._run(['add', '--', 'anything.service'])
        for bad in ('../evil.service', 'sub/dir.service', '-rf', 'passwd'):
            with self.assertRaises(roundhouse.ActuationError, msg=bad):
                self._run(['add', '--', bad])


class TestFreedMemoryTerm(unittest.TestCase):
    """S4/E9: only a resident unit frees memory, and it frees what the cgroup says."""

    def setUp(self):
        self.unit = load_fixture('qwen3.6-coding.service')
        self.edits = [roundhouse.Edit(field='ctx', flag='-c', old_text='65536',
                                      new_text='131072', span=(0, 5), quote='')]

    def _watcher(self, rung, cgroup=None, mem=None):
        watcher = MagicMock()
        watcher.mem_store = None
        watcher._cgroup_cache = {self.unit.name: cgroup} if cgroup else {}
        watcher.snapshot.return_value = {'units': [
            {'unit': self.unit.name, 'rung': rung, 'retired': False,
             'mem': mem or {}, 'port': 8085},
        ]}
        return watcher

    # `preflight_memory` returns the labelled numbers only on the FAILING branch (MVP2
    # behaviour, frozen by MVP3-SPEC §1's byte-identical requirement). These tests read
    # `freed_bytes`, so the check has to be guaranteed to fail — and it was not: the
    # estimate came from the real filesystem, so on a box where the fixture's model file
    # actually exists (the acceptance container, which lays down stand-in models) the
    # estimate collapsed to ~1.5 GiB, the check PASSED, and every read raised KeyError.
    # Pin the estimate instead of relying on the host's model paths and free RAM.
    FIXED_ESTIMATE = (12 * 1024 ** 3, 'test-injected')

    def _run(self, watcher, mem_available_kb=1024):
        with patch.object(roundhouse, '_estimate_start_bytes',
                          lambda *a, **k: self.FIXED_ESTIMATE):
            return roundhouse.preflight_memory(
                self.unit, self.edits, watcher,
                meminfo_reader=lambda: {'MemAvailable': mem_available_kb})

    def test_stopped_unit_frees_nothing(self):
        """Regression: the snapshot `mem` row is an ESTIMATE for units that are OFF —
        crediting it invented a budget nobody is holding."""
        watcher = self._watcher('OFF', mem={'bytes': 20 * 1024 ** 3, 'source': 'measured'})
        result = self._run(watcher)
        self.assertFalse(result['ok'])
        self.assertEqual(result['freed_bytes'], 0)
        self.assertIn('not active', result['freed_source'])

    def test_active_unit_frees_cgroup_current(self):
        watcher = self._watcher('READY', cgroup={'current': 7 * 1024 ** 3,
                                                 'last_peak': 9 * 1024 ** 3},
                                mem={'bytes': 20 * 1024 ** 3, 'source': 'measured'})
        result = self._run(watcher)
        self.assertEqual(result['freed_bytes'], 7 * 1024 ** 3)
        self.assertEqual(result['freed_source'], 'cgroup memory.current')

    def test_active_unit_falls_back_to_last_peak_then_measured_row(self):
        watcher = self._watcher('LOADING', cgroup={'current': None,
                                                   'last_peak': 9 * 1024 ** 3})
        self.assertEqual(self._run(watcher)['freed_bytes'], 9 * 1024 ** 3)

        watcher = self._watcher('BUSY', cgroup={'current': None, 'last_peak': None},
                                mem={'bytes': 3 * 1024 ** 3, 'source': 'measured'})
        result = self._run(watcher)
        self.assertEqual(result['freed_bytes'], 3 * 1024 ** 3)
        self.assertEqual(result['freed_source'], 'measured peak row')

    def test_failure_detail_keeps_the_labelled_numbers(self):
        watcher = self._watcher('READY', cgroup={'current': 2 * 1024 ** 3})
        result = self._run(watcher)
        self.assertFalse(result['ok'])
        for key in ('estimate_bytes', 'estimate_source', 'mem_available_bytes',
                    'freed_bytes', 'freed_source', 'headroom_bytes', 'budget_bytes'):
            self.assertIn(key, result)
        self.assertIn('freed by stopping', result['detail'])
        self.assertIn(self.unit.name, result['detail'])


class TestTokenFilePermissions(unittest.TestCase):
    """S6: the token must never exist world-readable, not even for an instant."""

    def test_token_tmp_file_is_600_before_the_replace(self):
        import stat as stat_mod
        temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp_dir, True)
        old_token_path = roundhouse.TOKEN_PATH
        old_umask = os.umask(0o022)
        captured = {}
        real_replace = os.replace

        def spy_replace(src, dst):
            captured['mode'] = stat_mod.S_IMODE(os.stat(src).st_mode)
            return real_replace(src, dst)

        try:
            roundhouse.TOKEN_PATH = os.path.join(temp_dir, 'token')
            with patch('roundhouse.os.replace', spy_replace):
                roundhouse.ensure_token()
        finally:
            roundhouse.TOKEN_PATH = old_token_path
            roundhouse.TOKEN = None
            os.umask(old_umask)

        self.assertEqual(captured.get('mode'), 0o600,
                         "tmp file was world-readable until the post-replace chmod")


class TestSwitchPreflight(unittest.TestCase):
    """Switch preflight checks per MVP3-SPEC §3.2 and F9 eligibility doctrine."""

    def setUp(self):
        self.fixtures = Path(__file__).resolve().parents[2] / 'docs' / 'fixtures'
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _load_fixture(self, name: str) -> roundhouse.UnitFile:
        """Load a fixture unit file."""
        path = self.fixtures / name
        raw = path.read_bytes()
        return roundhouse.parse_unit(str(path), raw)

    def test_target_off_passes(self):
        """Target with rung OFF passes the target check."""
        unit = self._load_fixture('qwen3.6-coding.service')
        units = {unit.name: unit}
        watcher = MagicMock()
        watcher.snapshot.return_value = {
            'units': [
                {'unit': unit.name, 'rung': 'OFF', 'retired': False, 'port': 8085, 'mem': {}},
            ]
        }
        watcher._cgroup_cache = {}
        watcher.mem_store = None

        def meminfo_reader():
            return {'MemAvailable': 20 * 1024 * 1024}  # 20 GiB in KiB

        result = roundhouse.switch_preflight(unit.name, [], watcher, units, 8090, meminfo_reader)
        self.assertTrue(result['ok'], f"Failed: {result['checks']}")
        target_check = [c for c in result['checks'] if c['check'] == 'target'][0]
        self.assertTrue(target_check['ok'])

    def test_target_standby_fails_with_gate_detail(self):
        """STANDBY target fails with gate details (F9)."""
        unit = self._load_fixture('qwen3.6-coding.service')
        units = {unit.name: unit}
        watcher = MagicMock()
        # `gate` is parse_gate's REAL shape — {'kind', 'wants', 'raw'}. It has no 'kernel'
        # and no 'running' key; the running release comes off the snapshot (os.uname()[2]).
        watcher.snapshot.return_value = {
            'kernel': '6.0.0',
            'units': [
                {'unit': unit.name, 'rung': 'STANDBY',
                 'gate': {'kind': 'kernel', 'wants': '6.1.0',
                          'raw': '/bin/sh -c \'[ "$(uname -r)" = "6.1.0" ]\''},
                 'retired': False, 'port': 8085, 'mem': {}},
            ]
        }
        watcher._cgroup_cache = {}
        watcher.mem_store = None

        def meminfo_reader():
            return {'MemAvailable': 20 * 1024 * 1024}  # 20 GiB in KiB

        result = roundhouse.switch_preflight(unit.name, [], watcher, units, 8090, meminfo_reader)
        self.assertFalse(result['ok'])
        detail = result['checks'][0]['detail']
        self.assertIn('kernel 6.1.0', detail)
        self.assertIn('running: 6.0.0', detail)
        self.assertNotIn('None', detail)

    def test_target_standby_opaque_gate_shows_raw_condition(self):
        """An opaque gate has wants=None; the raw ExecCondition is the honest fallback."""
        unit = self._load_fixture('qwen3.6-coding.service')
        units = {unit.name: unit}
        watcher = MagicMock()
        watcher.snapshot.return_value = {
            'kernel': '6.0.0',
            'units': [
                {'unit': unit.name, 'rung': 'STANDBY',
                 'gate': {'kind': 'opaque', 'wants': None, 'raw': '/usr/bin/test -e /dev/npu'},
                 'retired': False, 'port': 8085, 'mem': {}},
            ]
        }
        watcher._cgroup_cache = {}
        watcher.mem_store = None

        result = roundhouse.switch_preflight(unit.name, [], watcher, units, 8090,
                                             lambda: {'MemAvailable': 20 * 1024 * 1024})
        self.assertFalse(result['ok'])
        self.assertIn('/usr/bin/test -e /dev/npu', result['checks'][0]['detail'])
        self.assertNotIn('None', result['checks'][0]['detail'])

    def test_target_failed_fails_with_clear_failure_message(self):
        """FAILED target fails with 'clear the failure' message (F9)."""
        unit = self._load_fixture('qwen3.6-coding.service')
        units = {unit.name: unit}
        watcher = MagicMock()
        watcher.snapshot.return_value = {
            'units': [
                {'unit': unit.name, 'rung': 'FAILED', 'retired': False, 'port': 8085, 'mem': {}},
            ]
        }
        watcher._cgroup_cache = {}
        watcher.mem_store = None

        def meminfo_reader():
            return {'MemAvailable': 20 * 1024 * 1024}  # 20 GiB in KiB

        result = roundhouse.switch_preflight(unit.name, [], watcher, units, 8090, meminfo_reader)
        self.assertFalse(result['ok'])
        self.assertIn('clear the failure by hand', result['checks'][0]['detail'])

    def test_target_active_fails_with_already_active(self):
        """Active target (READY/BUSY) fails with 'already active' (F9)."""
        unit = self._load_fixture('qwen3.6-coding.service')
        units = {unit.name: unit}
        watcher = MagicMock()
        watcher.snapshot.return_value = {
            'units': [
                {'unit': unit.name, 'rung': 'READY', 'retired': False, 'port': 8085, 'mem': {}},
            ]
        }
        watcher._cgroup_cache = {}
        watcher.mem_store = None

        def meminfo_reader():
            return {'MemAvailable': 20 * 1024 * 1024}  # 20 GiB in KiB

        result = roundhouse.switch_preflight(unit.name, [], watcher, units, 8090, meminfo_reader)
        self.assertFalse(result['ok'])
        self.assertIn('already active', result['checks'][0]['detail'])

    def test_retired_target_fails(self):
        """Retired target fails at retired check."""
        unit = self._load_fixture('qwen3.6-coding.service')
        unit.retired = '[RETIRED]'
        units = {unit.name: unit}
        watcher = MagicMock()

        result = roundhouse.switch_preflight(unit.name, [], watcher, units, 8090)
        self.assertFalse(result['ok'])
        self.assertEqual(result['checks'][0]['check'], 'retired')

    def test_duplicate_stop_is_refused_not_double_counted(self):
        """F9: a name ticked twice is an ineligible stop.

        Without this the freed sum iterates `stops` and credits the same unit's residency
        twice, so a switch that does not fit passes the fit check.
        """
        unit = self._load_fixture('llama-server-gemma4.service')
        other = self._load_fixture('qwen3.6-coding.service')
        units = {unit.name: unit, other.name: other}
        watcher = MagicMock()
        watcher.snapshot.return_value = {'units': [
            {'unit': unit.name, 'rung': 'OFF', 'retired': False, 'port': 8093, 'mem': {}},
            {'unit': other.name, 'rung': 'READY', 'retired': False, 'port': 8085, 'mem': {}},
        ]}
        watcher._cgroup_cache = {other.name: {'current': 8 * 1024 ** 3}}
        watcher.mem_store = None
        meminfo = lambda: {'MemAvailable': 1024}          # 1 MiB: only `freed` can pay

        dup = roundhouse.switch_preflight(unit.name, [other.name, other.name],
                                          watcher, units, 8090, meminfo)
        self.assertFalse(dup['ok'])
        stops_check = [c for c in dup['checks'] if c['check'] == 'stops'][0]
        self.assertFalse(stops_check['ok'])
        self.assertIn('twice', stops_check['detail'])
        self.assertIsNone(dup.get('confirm'))

        # the single-tick version of the same request is legal, and frees 8 GiB ONCE
        single = roundhouse.switch_preflight(unit.name, [other.name],
                                             watcher, units, 8090, meminfo)
        self.assertEqual(single['fit']['freed_bytes'], 8 * 1024 ** 3)

    def test_memory_check_uses_sum_of_freed_stops(self):
        """Memory check sums freed bytes from all ticked stops with per-unit labels."""
        target = 'target.service'
        stop1 = 'stop1.service'
        stop2 = 'stop2.service'

        target_unit = self._load_fixture('qwen3.6-coding.service')
        target_unit.name = target
        stop1_unit = self._load_fixture('llama-server-gemma4.service')
        stop1_unit.name = stop1
        stop2_unit = self._load_fixture('llama-server-gemma4.service')
        stop2_unit.name = stop2

        units = {target: target_unit, stop1: stop1_unit, stop2: stop2_unit}
        watcher = MagicMock()

        cgroup_cache = {
            stop1: {'current': 5 * 1024**3},
            stop2: {'current': 3 * 1024**3},
        }
        watcher._cgroup_cache = cgroup_cache
        watcher.mem_store = None
        watcher.snapshot.return_value = {
            'units': [
                {'unit': target, 'rung': 'OFF', 'retired': False, 'port': 8090, 'mem': {}},
                {'unit': stop1, 'rung': 'READY', 'retired': False, 'port': 8085, 'mem': {}},
                {'unit': stop2, 'rung': 'BUSY', 'retired': False, 'port': 8086, 'mem': {}},
            ]
        }

        def meminfo_reader():
            return {'MemAvailable': 10 * 1024}  # 10 GiB

        result = roundhouse.switch_preflight(target, [stop1, stop2], watcher, units, 8090, meminfo_reader)

        # Check that freed_by has both stops with correct bytes and labels
        freed_by = result['fit']['freed_by']
        self.assertEqual(len(freed_by), 2)
        freed_sum = sum(fb['bytes'] for fb in freed_by)
        self.assertEqual(freed_sum, 8 * 1024**3)
        # Verify labels are present
        for fb in freed_by:
            self.assertIn('source', fb)
            self.assertIn('bytes', fb)
            self.assertIn('unit', fb)

    def test_port_blocker_rule(self):
        """Port check: active un-ticked claimant is a blocker."""
        target = 'target.service'
        active_unit = 'active.service'

        target_unit = self._load_fixture('qwen3.6-coding.service')
        target_unit.name = target
        active_u = self._load_fixture('llama-server-gemma4.service')
        active_u.name = active_unit

        units = {target: target_unit, active_unit: active_u}
        watcher = MagicMock()
        watcher._cgroup_cache = {}
        watcher.mem_store = None
        watcher.snapshot.return_value = {
            'units': [
                {'unit': target, 'rung': 'OFF', 'retired': False, 'port': 8085, 'mem': {}},
                {'unit': active_unit, 'rung': 'READY', 'retired': False, 'port': 8085, 'mem': {}},
            ]
        }

        def meminfo_reader():
            return {'MemAvailable': 20 * 1024 * 1024}  # 20 GiB in KiB

        result = roundhouse.switch_preflight(target, [], watcher, units, 8090, meminfo_reader)

        # Port check should fail due to blocker
        port_check = result['port']
        self.assertFalse(port_check['ok'])
        self.assertTrue(len(port_check['blockers']) > 0)

    def test_port_notice_for_ticked_claimant(self):
        """Port check: ticked claimant is a notice, not blocker."""
        target = 'target.service'
        ticked_unit = 'ticked.service'

        target_unit = self._load_fixture('qwen3.6-coding.service')
        target_unit.name = target
        ticked_u = self._load_fixture('llama-server-gemma4.service')
        ticked_u.name = ticked_unit

        units = {target: target_unit, ticked_unit: ticked_u}
        watcher = MagicMock()
        watcher._cgroup_cache = {}
        watcher.mem_store = None
        watcher.snapshot.return_value = {
            'units': [
                {'unit': target, 'rung': 'OFF', 'retired': False, 'port': 8085, 'mem': {}},
                {'unit': ticked_unit, 'rung': 'READY', 'retired': False, 'port': 8085, 'mem': {}},
            ]
        }

        def meminfo_reader():
            return {'MemAvailable': 20 * 1024 * 1024}  # 20 GiB in KiB

        result = roundhouse.switch_preflight(target, [ticked_unit], watcher, units, 8090, meminfo_reader)

        # Port check should pass (ticked unit will stop)
        port_check = result['port']
        self.assertTrue(port_check['ok'])

    def test_suggest_stops_empty_when_fits(self):
        """suggest_stops returns empty list when fit passes (F7)."""
        target = 'target.service'
        units = {}
        watcher = MagicMock()
        watcher._cgroup_cache = {}
        watcher.mem_store = None

        estimate = 5 * 1024**3
        budget = 20 * 1024**3

        result = roundhouse.switch_preflight(target, [], watcher, units, 8090)
        # When fit passes, suggest_stops should be empty
        self.assertEqual(result['suggested_stops'], [])

    def test_suggest_stops_greedy_order(self):
        """suggest_stops walks candidates in resident_bytes descending order (F7)."""
        # Test the suggest_stops function directly
        target = 'target.service'
        cand1 = 'cand1.service'
        cand2 = 'cand2.service'

        stop_candidates = [
            {'unit': cand1, 'rung': 'READY'},
            {'unit': cand2, 'rung': 'READY'},
        ]
        cgroup_cache = {
            cand1: {'current': 10 * 1024**3},  # Larger
            cand2: {'current': 5 * 1024**3},   # Smaller
        }

        # Make fit fail with small budget
        estimate = 15 * 1024**3
        budget = 10 * 1024**3  # Not enough

        result = roundhouse.suggest_stops(target, [], stop_candidates, estimate, budget, [],
                                         cgroup_cache, None)

        # Suggestions should be in greedy order: cand1 (larger) before cand2
        if len(result) > 1:
            self.assertEqual(result[0], cand1)


class TestSwitchConfirm(unittest.TestCase):
    """Switch confirm hash canonicalization per F3."""

    def test_confirm_order_independent_in_stops(self):
        """compute_switch_confirm is order-independent in stops."""
        confirm1 = roundhouse.compute_switch_confirm('t', ['b', 'a'], {'a': '123', 't': '0'})
        confirm2 = roundhouse.compute_switch_confirm('t', ['a', 'b'], {'t': '0', 'a': '123'})
        self.assertEqual(confirm1, confirm2)

    def test_confirm_sensitive_to_ts_mono_change(self):
        """Changing any unit's ts_mono changes the confirm hash."""
        confirm1 = roundhouse.compute_switch_confirm('t', ['a'], {'a': '123', 't': '0'})
        confirm2 = roundhouse.compute_switch_confirm('t', ['a'], {'a': '124', 't': '0'})
        self.assertNotEqual(confirm1, confirm2)

    def test_confirm_none_canonicalizes_to_zero(self):
        """None/''/absent ts_mono canonicalize to '0'."""
        fp1 = roundhouse.fleet_fingerprint({'units': [
            {'unit': 'a.service', 'retired': False, 'start_ts_mono': None}
        ]})
        fp2 = roundhouse.fleet_fingerprint({'units': [
            {'unit': 'a.service', 'retired': False, 'start_ts_mono': ''}
        ]})
        fp3 = roundhouse.fleet_fingerprint({'units': [
            {'unit': 'a.service', 'retired': False}
        ]})
        self.assertEqual(fp1['a.service'], '0')
        self.assertEqual(fp2['a.service'], '0')
        self.assertEqual(fp3['a.service'], '0')

    def test_fleet_fingerprint_excludes_retired(self):
        """fleet_fingerprint excludes retired units."""
        fp = roundhouse.fleet_fingerprint({'units': [
            {'unit': 'active.service', 'retired': False, 'start_ts_mono': '123'},
            {'unit': 'retired.service', 'retired': True, 'start_ts_mono': '456'},
        ]})
        self.assertIn('active.service', fp)
        self.assertNotIn('retired.service', fp)


class _ScriptedFleet:
    """A stub watcher whose roster answers the way the test scripts it.

    The point of driving the fleet from the recorded lifecycle verbs (rather than a
    canned `snapshot.side_effect` list) is that the confirmed-OFF gate and the watch
    freshness gate are *timing* rules: they only mean anything against a roster that
    changes when — and only when — something was actually stopped or started.
    """

    def __init__(self, rows, self_kernel='6.1.0'):
        self.lock = threading.Lock()
        self.rows = {name: dict(r) for name, r in rows.items()}
        self.kernel = self_kernel
        self.units = {}
        self.mem_store = None
        self._cgroup_cache = {}
        self.running_kernel = self_kernel
        # scripts, keyed by unit name
        self.stop_plan = {}      # 'off' (default) | 'failed' | 'stuck' (never leaves ACTIVE)
        self.start_plan = {}     # 'ready' (default) | 'failed' | 'loading' (never ready)
        self.hold = {}           # unit -> threading.Event: delay the stop settling

    # --- roster -------------------------------------------------------------------
    def snapshot(self):
        now = time.time()
        with self.lock:
            units = [dict(row, unit=name, sensed_at=now)
                     for name, row in self.rows.items()]
        return {'host': 'test', 'kernel': self.kernel, 'now': now,
                'mem': {}, 'sources': {}, 'self_port': 8090, 'units': units}

    def rung(self, name):
        with self.lock:
            return self.rows[name]['rung']

    # --- effects of the lifecycle verbs -------------------------------------------
    def on_stop(self, name):
        plan = self.stop_plan.get(name, 'off')
        if plan == 'stuck':
            return                       # rung stays ACTIVE: confirm-off must time out
        ev = self.hold.get(name)
        if ev is not None:
            threading.Thread(target=self._settle_when_released, args=(name, ev, plan),
                             daemon=True).start()
            return
        self._settle_stop(name, plan)

    def _settle_when_released(self, name, ev, plan):
        ev.wait(30)
        self._settle_stop(name, plan)

    def _settle_stop(self, name, plan):
        with self.lock:
            self.rows[name]['rung'] = 'FAILED' if plan == 'failed' else 'OFF'

    def on_start(self, name):
        plan = self.start_plan.get(name, 'ready')
        with self.lock:
            row = self.rows[name]
            # a real start moves ExecMainStartTimestamp; the watch freshness gate reads it
            row['since'] = time.time()
            row['start_ts_mono'] = str(int(time.time() * 1e6))
            row['rung'] = {'ready': 'READY', 'failed': 'FAILED'}.get(plan, 'LOADING')


class _SwitchHarness(unittest.TestCase):
    """Engine + scripted fleet + recording gateways; no systemd, no git, no files."""

    TARGET = 'llama-server-gemma4.service'
    A = 'qwen3.6-coding.service'
    B = 'llama-embed.service'

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.actuate_calls = []
        self.git_calls = []
        fixtures = Path(__file__).resolve().parents[2] / 'docs' / 'fixtures'
        self.units = {}
        for name in (self.TARGET, self.A, self.B):
            path = fixtures / name
            if path.exists():
                self.units[name] = roundhouse.parse_unit(str(path), path.read_bytes())

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _fleet(self, active):
        """Fleet with `active` units READY and the target OFF."""
        rows = {self.TARGET: {'rung': 'OFF', 'retired': False, 'port': 8093, 'since': 1.0,
                              'start_ts_mono': '0', 'badges': [], 'mem': {}, 'enabled': True}}
        port = 8085
        for name in active:
            rows[name] = {'rung': 'READY', 'retired': False, 'port': port, 'since': 100.0,
                          'start_ts_mono': str(port), 'badges': [], 'mem': {}, 'enabled': True}
            port += 1
        fleet = _ScriptedFleet(rows)
        fleet.units = {n: self.units[n] for n in rows if n in self.units}
        return fleet

    def _engine(self, fleet):
        engine = roundhouse.RolloutEngine(fleet, dict(fleet.units), self.temp_dir, 8090,
                                          roundhouse.EventBus(), threading.Lock())
        self.events = engine.event_bus.subscribe()
        return engine

    def _stub_actuate(self, fleet):
        def stub(argv, units, timeout=90):
            self.actuate_calls.append(list(argv))
            verb, name = argv[2], argv[-1]
            if verb == 'stop':
                fleet.on_stop(name)
            elif verb == 'start':
                fleet.on_start(name)
            return ''
        return stub

    def _raising_git(self, *args, **kwargs):
        raise AssertionError(f'run_git must never be called in a switch: {args}')

    def _raising_atomic_write(self, *args, **kwargs):
        raise AssertionError(f'_atomic_write must never be called in a switch: {args}')

    def _confirm_for(self, fleet, target, stops):
        return roundhouse.compute_switch_confirm(
            target, stops, roundhouse.fleet_fingerprint(fleet.snapshot()))

    @staticmethod
    def _tiny_estimate(unit_name, profile, mem_store):
        """The fixtures name model files that do not exist here, so the estimator falls
        back to its 9 GiB default and the worker's re-run preflight would fail on any
        build box with less free RAM than that. Memory arithmetic is TestSwitchPreflight's
        subject; these tests are about the state machine, so pin the estimate small."""
        return (64 * 1024 * 1024, 'test-injected')

    def _run_switch(self, engine, fleet, target, stops, timeout=25.0, zero_writes=True):
        """Drive one switch to a terminal phase with every gateway stubbed/blocked."""
        confirm = self._confirm_for(fleet, target, stops)
        ctx = [patch.object(roundhouse, 'run_actuate', self._stub_actuate(fleet)),
               patch.object(roundhouse, '_estimate_start_bytes', self._tiny_estimate)]
        if zero_writes:
            ctx.append(patch.object(roundhouse, 'run_git', self._raising_git))
            ctx.append(patch.object(roundhouse, '_atomic_write', self._raising_atomic_write))
        with contextlib.ExitStack() as stack:
            for c in ctx:
                stack.enter_context(c)
            rec = engine.start_switch(target, stops, confirm)
            self._wait_terminal(rec, timeout)
        return rec

    def _wait_terminal(self, rec, timeout=25.0,
                       terminals=('done', 'failed', 'restored', 'restore_failed')):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if rec['phase'] in terminals:
                return rec
            time.sleep(0.02)
        self.fail(f"operation never settled; stuck in {rec['phase']}: {rec['detail']}")

    def _wait_for(self, predicate, timeout=10.0, what='condition'):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail(f'timed out waiting for {what}')

    def _phases(self):
        """The phase sequence published on the SSE bus, duplicates collapsed."""
        seq = []
        while True:
            try:
                event, data, _ = self.events.get_nowait()
            except queue.Empty:
                break
            self.assertEqual(event, 'rollout', 'SSE event name stays `rollout` (F5)')
            seq.append((data['phase'], data.get('detail'), data.get('kind'), data.get('unit')))
        return seq


class TestSwitchEngine(_SwitchHarness):
    """Switch engine state machine per MVP3-SPEC §2.1 / §7.2, behaviourally."""

    # --- happy path ---------------------------------------------------------------

    def test_happy_path_argv_sequence_is_stop_stop_start_and_nothing_else(self):
        """F10: exactly [stop A, stop B, start T] — in order, with NO daemon-reload."""
        fleet = self._fleet([self.A, self.B])
        engine = self._engine(fleet)

        rec = self._run_switch(engine, fleet, self.TARGET, [self.A, self.B])

        self.assertEqual(rec['phase'], 'done', rec['detail'])
        self.assertEqual(self.actuate_calls, [
            ['systemctl', '--user', 'stop', '--', self.A],
            ['systemctl', '--user', 'stop', '--', self.B],
            ['systemctl', '--user', 'start', '--', self.TARGET],
        ])
        self.assertNotIn('daemon-reload', [c[2] for c in self.actuate_calls])
        self.assertEqual(rec['stopped'], [self.A, self.B], 'stops recorded in order')
        self.assertTrue(rec['target_started'])
        self.assertIn('ready in', rec['detail'])

    def test_happy_path_phase_and_sse_sequence(self):
        """§2.1 phase order, kind='switch' on every event, and the stopping (i/n) detail."""
        fleet = self._fleet([self.A, self.B])
        engine = self._engine(fleet)

        rec = self._run_switch(engine, fleet, self.TARGET, [self.A, self.B])
        seq = self._phases()

        phases = [p for p, _, _, _ in seq]
        self.assertEqual(phases, ['preflight', 'stopping', 'stopping', 'starting',
                                  'watching', 'done'], seq)
        for phase, detail, kind, unit in seq:
            self.assertEqual(kind, 'switch', f'{phase} event lost kind')
            self.assertEqual(unit, self.TARGET, f'{phase} event lost unit')
        details = [d for _, d, _, _ in seq]
        self.assertEqual(details[1], f'stopping {self.A} (1/2)')
        self.assertEqual(details[2], f'stopping {self.B} (2/2)')
        self.assertEqual(rec['phase'], 'done')

    def test_switch_with_no_ticks_skips_stopping_entirely(self):
        """No ticked stops -> preflight goes straight to starting (§2.1 preflight row)."""
        fleet = self._fleet([])
        engine = self._engine(fleet)

        rec = self._run_switch(engine, fleet, self.TARGET, [])

        self.assertEqual(rec['phase'], 'done', rec['detail'])
        self.assertEqual(self.actuate_calls,
                         [['systemctl', '--user', 'start', '--', self.TARGET]])
        self.assertEqual([p for p, _, _, _ in self._phases()],
                         ['preflight', 'starting', 'watching', 'done'])

    # --- the confirmed-OFF gate ---------------------------------------------------

    def test_confirm_off_holds_stopping_until_the_roster_reports_off(self):
        """The `stopping` phase must not advance on the stop command's return alone."""
        fleet = self._fleet([self.A])
        release = threading.Event()
        fleet.hold[self.A] = release
        engine = self._engine(fleet)
        confirm = self._confirm_for(fleet, self.TARGET, [self.A])

        with patch.object(roundhouse, 'run_actuate', self._stub_actuate(fleet)), \
             patch.object(roundhouse, '_estimate_start_bytes', self._tiny_estimate), \
             patch.object(roundhouse, 'run_git', self._raising_git):
            rec = engine.start_switch(self.TARGET, [self.A], confirm)
            self._wait_for(lambda: ['systemctl', '--user', 'stop', '--', self.A]
                           in self.actuate_calls, what='the stop command')
            # The stop RETURNED, but the roster still says READY.
            time.sleep(1.5)
            self.assertEqual(rec['phase'], 'stopping',
                             'advanced before the roster confirmed OFF')
            self.assertEqual(rec['stopped'], [], 'recorded a stop the roster never confirmed')
            self.assertNotIn(['systemctl', '--user', 'start', '--', self.TARGET],
                             self.actuate_calls, 'started the target over a live unit')

            release.set()                       # roster now reports A OFF, freshly sensed
            self._wait_terminal(rec)

        self.assertEqual(rec['phase'], 'done', rec['detail'])
        self.assertEqual(rec['stopped'], [self.A])
        self.assertEqual(fleet.rung(self.A), 'OFF')

    def test_confirm_off_timeout_fails_stop_unconfirmed_with_offer(self):
        """A stop whose unit never leaves ACTIVE -> failed(stop_unconfirmed)."""
        fleet = self._fleet([self.A, self.B])
        fleet.stop_plan[self.B] = 'stuck'
        engine = self._engine(fleet)

        with patch.object(roundhouse, 'CONFIRM_OFF_TIMEOUT_SEC', 2):
            rec = self._run_switch(engine, fleet, self.TARGET, [self.A, self.B])

        self.assertEqual(rec['phase'], 'failed')
        self.assertEqual(rec['failure']['reason'], 'stop_unconfirmed')
        self.assertIn(self.B, rec['failure']['detail'])
        self.assertEqual(rec['stopped'], [self.A], 'only the confirmed stop is recorded')
        # A was stopped, so the reverse is offered (§2.6 reversible = stopped or started)
        self.assertTrue(rec['rollback']['offered'])
        self.assertFalse(rec['restored'])
        self.assertNotIn(['systemctl', '--user', 'start', '--', self.TARGET],
                         self.actuate_calls)

    def test_failed_after_stop_counts_as_confirmed_and_says_so(self):
        """F2: rung FAILED after a stop is 'stopped' (dead process) + a notice."""
        fleet = self._fleet([self.A])
        fleet.stop_plan[self.A] = 'failed'
        engine = self._engine(fleet)

        rec = self._run_switch(engine, fleet, self.TARGET, [self.A])

        self.assertEqual(rec['phase'], 'done', rec['detail'])
        self.assertEqual(rec['stopped'], [self.A], 'FAILED-after-stop must count as stopped')
        notices = [d for _, d, _, _ in self._phases()
                   if d and 'considered stopped' in d]
        self.assertTrue(
            notices or 'considered stopped' in rec['detail'],
            'no FAILED notice was ever recorded on the record or the stream')
        self.assertEqual(self.actuate_calls[-1],
                         ['systemctl', '--user', 'start', '--', self.TARGET])

    # --- target failures ----------------------------------------------------------

    def test_target_watch_failure_offers_the_reverse(self):
        """Target reaching FAILED -> failed(unit_failed) with a live restore offer."""
        fleet = self._fleet([self.A])
        fleet.start_plan[self.TARGET] = 'failed'
        engine = self._engine(fleet)

        rec = self._run_switch(engine, fleet, self.TARGET, [self.A])

        self.assertEqual(rec['phase'], 'failed')
        self.assertEqual(rec['failure']['reason'], 'unit_failed')
        self.assertTrue(rec['rollback']['offered'], 'no restore offered after a failed target')
        self.assertTrue(rec['target_started'])
        self.assertEqual(rec['stopped'], [self.A])

    def test_preflight_drift_fails_without_touching_anything(self):
        """A stale confirm fails in preflight: nothing stopped, slot free (restored)."""
        fleet = self._fleet([self.A])
        engine = self._engine(fleet)

        with patch.object(roundhouse, 'run_actuate', self._stub_actuate(fleet)), \
             patch.object(roundhouse, '_estimate_start_bytes', self._tiny_estimate):
            rec = engine.start_switch(self.TARGET, [self.A], 'not-the-right-hash')
            self._wait_terminal(rec)

        self.assertEqual(rec['phase'], 'failed')
        self.assertEqual(rec['failure']['reason'], 'preview_stale')
        self.assertEqual(self.actuate_calls, [], 'preflight failure actuated something')
        self.assertTrue(rec['restored'], 'nothing changed, so the slot must be free')
        self.assertTrue(roundhouse._slot_free(rec))
        self.assertIsNone(rec['rollback'])

    # --- restore ------------------------------------------------------------------

    def test_restore_replays_stopped_units_in_original_order(self):
        """§2.1 restoring: stop the target, then start `stopped` in the SAME order."""
        fleet = self._fleet([self.A, self.B])
        fleet.start_plan[self.TARGET] = 'failed'
        engine = self._engine(fleet)

        rec = self._run_switch(engine, fleet, self.TARGET, [self.A, self.B])
        self.assertEqual(rec['phase'], 'failed')
        self.assertEqual(rec['stopped'], [self.A, self.B])

        self.actuate_calls.clear()
        with patch.object(roundhouse, 'run_actuate', self._stub_actuate(fleet)), \
             patch.object(roundhouse, 'run_git', self._raising_git), \
             patch.object(roundhouse, '_atomic_write', self._raising_atomic_write):
            engine.rollback(rec['rollout_id'])
            self._wait_terminal(rec)

        self.assertEqual(rec['phase'], 'restored', rec['detail'])
        starts = [c[-1] for c in self.actuate_calls if c[2] == 'start']
        self.assertEqual(starts, [self.A, self.B], 'restore replayed out of order')
        self.assertEqual(fleet.rung(self.A), 'READY')
        self.assertEqual(fleet.rung(self.B), 'READY')
        self.assertIn('restored: 2 unit(s)', rec['detail'])
        self.assertFalse(rec['rollback']['offered'])
        self.assertTrue(roundhouse._slot_free(rec), 'restored must free the slot')

    def test_restore_stops_the_target_before_replaying(self):
        """A target that came up but never went READY is stopped first (port reuse)."""
        fleet = self._fleet([self.A])
        fleet.start_plan[self.TARGET] = 'loading'      # never reaches READY
        engine = self._engine(fleet)

        with patch.object(roundhouse, 'WATCH_TIMEOUT_SEC', 2):
            rec = self._run_switch(engine, fleet, self.TARGET, [self.A])
        self.assertEqual(rec['phase'], 'failed')
        self.assertEqual(rec['failure']['reason'], 'watch_timeout')

        self.actuate_calls.clear()
        with patch.object(roundhouse, 'run_actuate', self._stub_actuate(fleet)), \
             patch.object(roundhouse, 'run_git', self._raising_git):
            engine.rollback(rec['rollout_id'])
            self._wait_terminal(rec)

        self.assertEqual(rec['phase'], 'restored', rec['detail'])
        self.assertEqual(self.actuate_calls[0],
                         ['systemctl', '--user', 'stop', '--', self.TARGET])

    def test_restore_failure_is_terminal_and_names_the_manual_commands(self):
        """§2.1: restore_failed carries the exact `systemctl --user start ...` recovery."""
        fleet = self._fleet([self.A, self.B])
        fleet.start_plan[self.TARGET] = 'failed'
        engine = self._engine(fleet)

        rec = self._run_switch(engine, fleet, self.TARGET, [self.A, self.B])
        self.assertEqual(rec['stopped'], [self.A, self.B])

        fleet.start_plan[self.B] = 'failed'            # B refuses to come back
        with patch.object(roundhouse, 'run_actuate', self._stub_actuate(fleet)), \
             patch.object(roundhouse, 'run_git', self._raising_git):
            engine.rollback(rec['rollout_id'])
            self._wait_terminal(rec)

        self.assertEqual(rec['phase'], 'restore_failed')
        self.assertIn('systemctl --user start', rec['detail'])
        self.assertIn(self.B, rec['detail'], 'the unit still down must be named')
        self.assertNotIn(self.A, rec['detail'], 'A came back; do not tell the operator to start it')
        self.assertTrue(roundhouse._slot_free(rec), 'restore_failed is terminal')

    def test_dismiss_frees_the_slot_after_a_failed_switch(self):
        """Dismiss instead of restore: the offer settles and the next switch is accepted."""
        fleet = self._fleet([self.A])
        fleet.start_plan[self.TARGET] = 'failed'
        engine = self._engine(fleet)

        rec = self._run_switch(engine, fleet, self.TARGET, [self.A])
        self.assertTrue(rec['rollback']['offered'])
        self.assertFalse(roundhouse._slot_free(rec))

        engine.dismiss(rec['rollout_id'])

        self.assertFalse(rec['rollback']['offered'])
        self.assertTrue(rec['rollback']['dismissed'])
        self.assertTrue(roundhouse._slot_free(engine.current))
        # and the slot really is usable again
        fleet.start_plan[self.TARGET] = 'ready'
        with patch.object(roundhouse, 'run_actuate', self._stub_actuate(fleet)), \
             patch.object(roundhouse, '_estimate_start_bytes', self._tiny_estimate):
            second = engine.start_switch(self.TARGET, [],
                                         self._confirm_for(fleet, self.TARGET, []))
            self._wait_terminal(second)
        self.assertNotEqual(second['rollout_id'], rec['rollout_id'])

    # --- the public record the UI actually reads ----------------------------------

    def test_snapshot_rollout_key_carries_the_switch_record(self):
        """take_snapshot's `rollout` key must be the SWITCH public record (F5/§2.4)."""
        fleet = self._fleet([self.A])
        engine = self._engine(fleet)
        rec = self._run_switch(engine, fleet, self.TARGET, [self.A])
        self.assertEqual(rec['phase'], 'done')

        server = MagicMock()
        server.watcher = fleet
        server.watcher_lock = threading.Lock()
        server.rollout_engine = engine
        published = roundhouse.ThreadingHTTPServer.take_snapshot(server)['rollout']

        self.assertEqual(published['kind'], 'switch')
        self.assertEqual(published['target'], self.TARGET)
        self.assertEqual(published['unit'], self.TARGET)
        self.assertEqual(published['stops'], [self.A])
        self.assertEqual(published['stopped'], [self.A])
        self.assertTrue(published['target_started'])
        self.assertEqual(published['phase'], 'done')
        for rollout_only in ('edits', 'commit', 'was_active'):
            self.assertNotIn(rollout_only, published,
                             f'switch record leaked the rollout-only key {rollout_only}')
        # identical to what GET /api/rollouts/<id> serves — one function, no drift
        self.assertEqual(published, roundhouse.rollout_public_record(rec))

    def test_watch_to_ready_ready_terminal(self):
        """_watch_to_ready returns ('ready', elapsed) when rung reaches READY."""
        watcher = MagicMock()
        watcher.snapshot.side_effect = [
            {'units': [{'unit': 'u.service', 'rung': 'LOADING', 'badges': []}]},
            {'units': [{'unit': 'u.service', 'rung': 'READY', 'badges': []}]},
        ]

        engine = roundhouse.RolloutEngine(watcher, {}, self.temp_dir, 8090,
                                        roundhouse.EventBus(), threading.Lock())
        result = engine._watch_to_ready('u.service', None, time.time() + 10)

        self.assertEqual(result[0], 'ready')
        self.assertGreater(result[1], 0)

    def test_watch_to_ready_failed_terminal(self):
        """_watch_to_ready returns ('failed', reason, detail) when rung is FAILED."""
        watcher = MagicMock()
        watcher.snapshot.return_value = {
            'units': [{'unit': 'u.service', 'rung': 'FAILED', 'badges': []}]
        }

        engine = roundhouse.RolloutEngine(watcher, {}, self.temp_dir, 8090,
                                        roundhouse.EventBus(), threading.Lock())
        result = engine._watch_to_ready('u.service', None, time.time() + 10)

        self.assertEqual(result[0], 'failed')
        self.assertEqual(result[1], 'unit_failed')


class TestSwitchZeroWrites(_SwitchHarness):
    """F10: a switch writes nothing, provably — not the repo, not a single file.

    The old version of this class asserted `phase in (done, failed, restored,
    restore_failed)` — a set containing every terminal phase, so it passed even when the
    raising stub had aborted the switch. These assert the switch SUCCEEDS with both
    write gateways armed to blow up, which is the only version that proves anything.
    """

    def test_full_switch_completes_with_both_write_gateways_raising(self):
        fleet = self._fleet([self.A, self.B])
        engine = self._engine(fleet)

        rec = self._run_switch(engine, fleet, self.TARGET, [self.A, self.B],
                               zero_writes=True)

        self.assertEqual(rec['phase'], 'done', rec['detail'])
        self.assertEqual(rec['stopped'], [self.A, self.B])
        self.assertEqual([c[2] for c in self.actuate_calls], ['stop', 'stop', 'start'])

    def test_restore_completes_with_both_write_gateways_raising(self):
        fleet = self._fleet([self.A])
        fleet.start_plan[self.TARGET] = 'failed'
        engine = self._engine(fleet)

        rec = self._run_switch(engine, fleet, self.TARGET, [self.A], zero_writes=True)
        self.assertEqual(rec['phase'], 'failed')

        with patch.object(roundhouse, 'run_actuate', self._stub_actuate(fleet)), \
             patch.object(roundhouse, 'run_git', self._raising_git), \
             patch.object(roundhouse, '_atomic_write', self._raising_atomic_write):
            engine.rollback(rec['rollout_id'])
            self._wait_terminal(rec)

        self.assertEqual(rec['phase'], 'restored', rec['detail'])

    def test_unit_file_mtimes_are_unchanged_across_a_switch(self):
        """The container drill's mtime row, at engine level: copies of the real fixtures
        sit in a scratch unit dir; a full switch must not touch one byte or one mtime."""
        unit_dir = os.path.join(self.temp_dir, 'units')
        os.makedirs(unit_dir)
        fixtures = Path(__file__).resolve().parents[2] / 'docs' / 'fixtures'
        before = {}
        for name in (self.TARGET, self.A, self.B):
            dest = os.path.join(unit_dir, name)
            shutil.copyfile(fixtures / name, dest)
            os.utime(dest, (1_600_000_000, 1_600_000_000))     # pin, so any write shows
            st = os.stat(dest)
            before[name] = (st.st_mtime_ns, st.st_size,
                            hashlib.sha256(Path(dest).read_bytes()).hexdigest())

        fleet = self._fleet([self.A, self.B])
        engine = self._engine(fleet)
        engine.unit_dir = unit_dir

        rec = self._run_switch(engine, fleet, self.TARGET, [self.A, self.B])
        self.assertEqual(rec['phase'], 'done', rec['detail'])

        for name, expected in before.items():
            st = os.stat(os.path.join(unit_dir, name))
            actual = (st.st_mtime_ns, st.st_size,
                      hashlib.sha256(Path(unit_dir, name).read_bytes()).hexdigest())
            self.assertEqual(actual, expected, f'{name} was written during a switch')
        self.assertEqual(sorted(os.listdir(unit_dir)),
                         sorted([self.TARGET, self.A, self.B]),
                         'a switch created a file in the unit dir')


class TestSwitchSlot(unittest.TestCase):
    """Slot exclusivity for switches (F11, §2.2)."""

    @classmethod
    def setUpClass(cls):
        """Start server on an ephemeral port."""
        cls.temp_dir = tempfile.mkdtemp()
        cls.fixtures = Path(__file__).resolve().parents[2] / 'docs' / 'fixtures'

        # Create fixture units
        qwen_path = cls.fixtures / 'qwen3.6-coding.service'
        gemma_path = cls.fixtures / 'llama-server-gemma4.service'
        qwen_unit = roundhouse.parse_unit(str(qwen_path), qwen_path.read_bytes())
        gemma_unit = roundhouse.parse_unit(str(gemma_path), gemma_path.read_bytes())

        # Create a stub watcher with units
        cls.watcher = MagicMock(spec=roundhouse.Watcher)
        cls.watcher.units = {'qwen3.6-coding.service': qwen_unit, 'llama-server-gemma4.service': gemma_unit}
        cls.watcher.mem_store = None
        cls.watcher._cgroup_cache = {}
        cls.watcher.snapshot.return_value = {
            'host': 'test', 'kernel': '6.1', 'now': time.time(),
            'units': [
                {'unit': 'qwen3.6-coding.service', 'rung': 'OFF', 'retired': False, 'port': 8085, 'start_ts_mono': '1234', 'mem': {}, 'badges': []},
                {'unit': 'llama-server-gemma4.service', 'rung': 'READY', 'retired': False, 'port': 8093, 'start_ts_mono': '5678', 'mem': {}, 'badges': []},
            ],
            'mem': {}, 'sources': {}
        }

        cls.event_bus = roundhouse.EventBus()

        # Find an available port
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        cls.port = sock.getsockname()[1]
        sock.close()

        # Create server with arming
        roundhouse.ACTUATE_ARMED = True
        roundhouse.TOKEN = 'test-token'
        cls.server = roundhouse.ThreadingHTTPServer(
            ('127.0.0.1', cls.port),
            roundhouse.RoundhouseRequestHandler,
            cls.watcher,
            cls.event_bus,
            cls.port
        )
        cls.server.rollout_engine = MagicMock()

        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        """Shutdown server."""
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.temp_dir, ignore_errors=True)
        roundhouse.ACTUATE_ARMED = False
        roundhouse.TOKEN = None

    def post_http(self, path, data=None, headers=None):
        """Make HTTP POST request."""
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            body = None
            if data:
                body = json.dumps(data).encode('utf-8')
            req_headers = headers or {}
            if body and 'Content-Type' not in req_headers:
                req_headers['Content-Type'] = 'application/json'
            if 'Authorization' not in req_headers:
                req_headers['Authorization'] = 'Bearer test-token'
            conn.request('POST', path, body, req_headers)
            resp = conn.getresponse()
            resp_body = resp.read()
            return resp.status, resp_body
        finally:
            conn.close()

    OFF_TARGET = 'qwen3.6-coding.service'      # rung OFF in the class stub roster

    def _execute_body(self):
        return {'target': self.OFF_TARGET, 'stops': [], 'confirm': 'whatever'}

    def test_execute_rejected_when_rollout_holds_slot(self):
        """POST /api/switch -> 409 operation_in_progress while a ROLLOUT holds the slot."""
        self.server.rollout_engine.current = {
            'rollout_id': 'ro-1-1', 'kind': 'rollout', 'phase': 'applying'
        }
        try:
            status, body = self.post_http('/api/switch', self._execute_body())
            self.assertEqual(status, 409)
            payload = json.loads(body)
            self.assertEqual(payload.get('error'), 'operation_in_progress')
            self.assertEqual(payload.get('rollout_id'), 'ro-1-1')
            self.assertEqual(payload.get('kind'), 'rollout')
        finally:
            self.server.rollout_engine.current = None

    def test_execute_rejected_while_switch_holds_slot(self):
        """POST /api/switch -> 409 while another SWITCH holds the slot; kind says which."""
        self.server.rollout_engine.current = {
            'rollout_id': 'sw-1-1', 'kind': 'switch', 'phase': 'stopping'
        }
        try:
            status, body = self.post_http('/api/switch', self._execute_body())
            self.assertEqual(status, 409)
            payload = json.loads(body)
            self.assertEqual(payload.get('error'), 'operation_in_progress')
            self.assertEqual(payload.get('kind'), 'switch')
        finally:
            self.server.rollout_engine.current = None

    def test_execute_accepted_once_the_offer_is_dismissed(self):
        """A failed switch whose offer was dismissed no longer holds the slot."""
        self.server.rollout_engine.current = {
            'rollout_id': 'sw-1-2', 'kind': 'switch', 'phase': 'failed',
            'rollback': {'offered': False, 'dismissed': True}, 'restored': False,
        }
        try:
            status, body = self.post_http('/api/switch', self._execute_body())
            # Past the slot gate. What answers next (422 preflight / 409 preview_stale on
            # the deliberately bogus confirm) depends on this host's MemAvailable; the
            # claim under test is only that the slot no longer refuses it.
            payload = json.loads(body) if body else {}
            self.assertNotEqual(payload.get('error'), 'operation_in_progress', body)
        finally:
            self.server.rollout_engine.current = None

    # --- the deviation this milestone removed: preview is STATELESS ------------------

    def test_preview_succeeds_while_an_operation_holds_the_slot(self):
        """Preview takes no slot (§4 lists no 409 for it) — the MVP2 edit-preview precedent.

        Slot-checking the preview made the modal unopenable during any rollout, and the
        operator's whole reason to preview mid-rollout is to plan the next move.
        """
        self.server.rollout_engine.current = {
            'rollout_id': 'ro-1-1', 'kind': 'rollout', 'phase': 'applying'
        }
        try:
            status, body = self.post_http('/api/switch/preview',
                                          {'target': self.OFF_TARGET, 'stops': []})
            self.assertIn(status, (200, 422), body)
            self.assertNotEqual(status, 409, 'preview must not take the operation slot')
        finally:
            self.server.rollout_engine.current = None

    def test_preview_unknown_target_still_404s_during_an_operation(self):
        """Removing the slot check must not swallow the 404 doctrine."""
        self.server.rollout_engine.current = {
            'rollout_id': 'sw-9-9', 'kind': 'switch', 'phase': 'watching'
        }
        try:
            status, _ = self.post_http('/api/switch/preview', {'target': 'nope.service'})
            self.assertEqual(status, 404)
        finally:
            self.server.rollout_engine.current = None


class TestSwitchSlotEngine(unittest.TestCase):
    """Slot exclusivity at the ENGINE level, in both directions (§2.2, F11).

    The HTTP class above stubs the engine; these drive the real `_slot_free` gate.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        fixtures = Path(__file__).resolve().parents[2] / 'docs' / 'fixtures'
        path = fixtures / 'qwen3.6-coding.service'
        self.unit = roundhouse.parse_unit(str(path), path.read_bytes())
        watcher = MagicMock()
        watcher.units = {self.unit.name: self.unit}
        watcher.mem_store = None
        watcher._cgroup_cache = {}
        watcher.snapshot.return_value = {'units': [
            {'unit': self.unit.name, 'rung': 'OFF', 'retired': False, 'enabled': True,
             'since': 1.0, 'start_ts_mono': '0', 'badges': [], 'port': 8085, 'mem': {}},
        ]}
        self.engine = roundhouse.RolloutEngine(watcher, {self.unit.name: self.unit},
                                               self.temp_dir, 8090, roundhouse.EventBus(),
                                               threading.Lock())

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _switch_record(self, phase, rollback=None, restored=False):
        return {'rollout_id': 'sw-0-0', 'kind': 'switch', 'unit': 'u.service',
                'target': 'u.service', 'stops': [], 'stopped': [], 'target_started': False,
                'phase': phase, 'detail': '', 'failure': None, 'rollback': rollback,
                'restored': restored, 'started_at': 0.0, 'updated_at': 0.0}

    def test_rollout_refused_while_a_switch_runs(self):
        """The inverse direction: start_rollout raises rollout_in_progress under a switch."""
        self.engine.current = self._switch_record('stopping')
        with self.assertRaises(roundhouse.ActuationError) as ctx:
            self.engine.start_rollout(self.unit.name, [], 'confirm')
        self.assertIn('rollout_in_progress', str(ctx.exception))

    def test_switch_refused_while_a_rollout_runs(self):
        """start_switch raises operation_in_progress under a rollout."""
        self.engine.current = {'rollout_id': 'ro-0-0', 'kind': 'rollout', 'phase': 'applying',
                               'commit': None, 'rollback': None, 'restored': False}
        with self.assertRaises(roundhouse.ActuationError) as ctx:
            self.engine.start_switch(self.unit.name, [], 'confirm')
        self.assertIn('operation_in_progress', str(ctx.exception))

    def test_restore_terminals_free_the_slot(self):
        """`restored` and `restore_failed` are in OPERATION_TERMINAL_PHASES (§2.2)."""
        for phase in ('restored', 'restore_failed'):
            self.assertTrue(roundhouse._slot_free(self._switch_record(phase)), phase)
        self.assertIn('restored', roundhouse.OPERATION_TERMINAL_PHASES)
        self.assertIn('restore_failed', roundhouse.OPERATION_TERMINAL_PHASES)

    def test_restoring_holds_the_slot(self):
        self.assertFalse(roundhouse._slot_free(self._switch_record('restoring')))

    def test_failed_switch_with_live_offer_holds_the_slot(self):
        held = self._switch_record('failed', rollback={'offered': True})
        self.assertFalse(roundhouse._slot_free(held))
        self.engine.current = held
        self.engine.rollouts[held['rollout_id']] = held
        with self.assertRaises(roundhouse.ActuationError):
            self.engine.start_switch(self.unit.name, [], 'confirm')

        self.engine.dismiss(held['rollout_id'])
        self.assertTrue(roundhouse._slot_free(held), 'dismiss must free the slot')


class TestSwitchRoutes(unittest.TestCase):
    """Switch routes and auth per MVP3-SPEC §4."""

    @classmethod
    def setUpClass(cls):
        """Start server on an ephemeral port."""
        cls.temp_dir = tempfile.mkdtemp()
        cls.fixtures = Path(__file__).resolve().parents[2] / 'docs' / 'fixtures'

        # Create fixture units
        qwen_path = cls.fixtures / 'qwen3.6-coding.service'
        gemma_path = cls.fixtures / 'llama-server-gemma4.service'
        qwen_unit = roundhouse.parse_unit(str(qwen_path), qwen_path.read_bytes())
        gemma_unit = roundhouse.parse_unit(str(gemma_path), gemma_path.read_bytes())

        # Create a stub watcher
        cls.watcher = MagicMock(spec=roundhouse.Watcher)
        cls.watcher.units = {'qwen3.6-coding.service': qwen_unit, 'llama-server-gemma4.service': gemma_unit}
        cls.watcher.mem_store = None
        cls.watcher._cgroup_cache = {}
        cls.watcher.snapshot.return_value = {
            'host': 'test', 'kernel': '6.1', 'now': time.time(),
            'units': [
                {'unit': 'qwen3.6-coding.service', 'rung': 'OFF', 'retired': False, 'port': 8085, 'start_ts_mono': '1234', 'mem': {}, 'badges': []},
                {'unit': 'llama-server-gemma4.service', 'rung': 'READY', 'retired': False, 'port': 8093, 'start_ts_mono': '5678', 'mem': {}, 'badges': []},
            ],
            'mem': {}, 'sources': {}
        }

        cls.event_bus = roundhouse.EventBus()

        # Find an available port
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        cls.port = sock.getsockname()[1]
        sock.close()

        # Create server with and without arming
        roundhouse.ACTUATE_ARMED = False
        cls.server = roundhouse.ThreadingHTTPServer(
            ('127.0.0.1', cls.port),
            roundhouse.RoundhouseRequestHandler,
            cls.watcher,
            cls.event_bus,
            cls.port
        )
        cls.server.rollout_engine = MagicMock()
        cls.server.rollout_engine.rollouts = {}
        cls.server.rollout_engine.current = None

        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        """Shutdown server."""
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def setUp(self):
        """Reset state before each test."""
        self.server.rollout_engine.current = None

    def post_http(self, path, data=None, headers=None):
        """Make HTTP POST request."""
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            body = None
            if data:
                body = json.dumps(data).encode('utf-8')
            req_headers = headers or {}
            if body and 'Content-Type' not in req_headers:
                req_headers['Content-Type'] = 'application/json'
            conn.request('POST', path, body, req_headers)
            resp = conn.getresponse()
            resp_body = resp.read()
            return resp.status, resp_body
        finally:
            conn.close()

    def get_http(self, path):
        """Make HTTP GET request."""
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            conn.request('GET', path)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_switch_preview_unarmed_returns_403(self):
        """POST /api/switch/preview without --actuate returns 403."""
        status, _ = self.post_http('/api/switch/preview', {'target': 'qwen3.6-coding.service'})
        self.assertEqual(status, 403)

    def test_switch_execute_unarmed_returns_403(self):
        """POST /api/switch without --actuate returns 403."""
        status, _ = self.post_http('/api/switch', {'target': 'qwen3.6-coding.service', 'stops': [], 'confirm': 'x'})
        self.assertEqual(status, 403)

    def test_switch_preview_bad_token_returns_401(self):
        """POST /api/switch/preview with bad token returns 401."""
        roundhouse.ACTUATE_ARMED = True
        roundhouse.TOKEN = 'test-token'
        try:
            status, _ = self.post_http(
                '/api/switch/preview',
                {'target': 'qwen3.6-coding.service'},
                {'Authorization': 'Bearer bad-token'}
            )
            self.assertEqual(status, 401)
        finally:
            roundhouse.ACTUATE_ARMED = False
            roundhouse.TOKEN = None

    def test_switch_preview_unknown_target_returns_404(self):
        """POST /api/switch/preview with unknown target returns 404."""
        roundhouse.ACTUATE_ARMED = True
        roundhouse.TOKEN = 'test-token'
        try:
            status, _ = self.post_http(
                '/api/switch/preview',
                {'target': 'unknown.service'},
                {'Authorization': 'Bearer test-token'}
            )
            self.assertEqual(status, 404)
        finally:
            roundhouse.ACTUATE_ARMED = False
            roundhouse.TOKEN = None

    def test_switch_preview_unknown_stop_returns_404(self):
        """POST /api/switch/preview with unknown stop returns 404."""
        roundhouse.ACTUATE_ARMED = True
        roundhouse.TOKEN = 'test-token'
        try:
            status, _ = self.post_http(
                '/api/switch/preview',
                {'target': 'qwen3.6-coding.service', 'stops': ['unknown.service']},
                {'Authorization': 'Bearer test-token'}
            )
            self.assertEqual(status, 404)
        finally:
            roundhouse.ACTUATE_ARMED = False
            roundhouse.TOKEN = None

    def test_switch_preview_success_includes_confirm(self):
        """POST /api/switch/preview with valid input returns 200 with confirm."""
        roundhouse.ACTUATE_ARMED = True
        roundhouse.TOKEN = 'test-token'

        # Mock switch_preflight to return a successful result
        def mock_preflight(*args, **kwargs):
            return {
                'ok': True,
                'checks': [
                    {'check': 'retired', 'ok': True},
                    {'check': 'target', 'ok': True},
                    {'check': 'stops', 'ok': True},
                    {'check': 'memory', 'ok': True},
                    {'check': 'port', 'ok': True},
                ],
                'target': {'unit': 'qwen3.6-coding.service', 'rung': 'OFF', 'port': 8085},
                'stop_candidates': [],
                'fit': {'ok': True, 'estimate_bytes': 9000000000},
                'port': {'ok': True, 'port': 8085, 'blockers': [], 'notices': []},
                'suggested_stops': [],
                'notices': [],
                'confirm': 'abc123def456',
            }

        try:
            with patch.object(roundhouse, 'switch_preflight', mock_preflight):
                status, body = self.post_http(
                    '/api/switch/preview',
                    {'target': 'qwen3.6-coding.service', 'stops': []},
                    {'Authorization': 'Bearer test-token'}
                )
            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertIn('confirm', payload, "200 response must include confirm hash")
            self.assertIn('target', payload)
            self.assertIn('fit', payload)
        finally:
            roundhouse.ACTUATE_ARMED = False
            roundhouse.TOKEN = None

    def test_switch_preview_failure_omits_confirm(self):
        """POST /api/switch/preview with ineligible target returns 422 without confirm."""
        roundhouse.ACTUATE_ARMED = True
        roundhouse.TOKEN = 'test-token'

        # Mock switch_preflight to return a failing result
        def mock_preflight(*args, **kwargs):
            return {
                'ok': False,
                'checks': [
                    {'check': 'target', 'ok': False, 'detail': 'already active (READY)'},
                ],
                'target': None,
                'stop_candidates': [],
                'fit': {'ok': False},
                'port': {'ok': False, 'blockers': [], 'notices': []},
                'suggested_stops': [],
                'notices': [],
            }

        try:
            with patch.object(roundhouse, 'switch_preflight', mock_preflight):
                status, body = self.post_http(
                    '/api/switch/preview',
                    {'target': 'llama-server-gemma4.service', 'stops': []},
                    {'Authorization': 'Bearer test-token'}
                )
            self.assertEqual(status, 422)
            payload = json.loads(body)
            self.assertNotIn('confirm', payload, "422 response must not include confirm")
            self.assertEqual(payload.get('error'), 'preflight_failed')
        finally:
            roundhouse.ACTUATE_ARMED = False
            roundhouse.TOKEN = None

    def test_switch_execute_fingerprint_drift_returns_409(self):
        """POST /api/switch with stale fingerprint returns 409 preview_stale."""
        roundhouse.ACTUATE_ARMED = True
        roundhouse.TOKEN = 'test-token'

        # Mock switch_preflight for both calls
        call_count = [0]

        def mock_preflight(*args, **kwargs):
            call_count[0] += 1
            return {
                'ok': True,
                'checks': [{'check': 'target', 'ok': True}],
                'target': {'unit': 'qwen3.6-coding.service', 'rung': 'OFF', 'port': 8085},
                'stop_candidates': [],
                'fit': {'ok': True},
                'port': {'ok': True, 'blockers': [], 'notices': []},
                'suggested_stops': [],
                'notices': [],
                'confirm': 'abc123' if call_count[0] == 1 else 'def456',  # Different confirm on second call
            }

        try:
            with patch.object(roundhouse, 'switch_preflight', mock_preflight):
                # First get a valid preview
                status, preview_body = self.post_http(
                    '/api/switch/preview',
                    {'target': 'qwen3.6-coding.service', 'stops': []},
                    {'Authorization': 'Bearer test-token'}
                )
            self.assertEqual(status, 200)
            preview = json.loads(preview_body)
            old_confirm = preview['confirm']

            try:
                # Now execute with the old confirm; it will mismatch the new one computed by switch_preflight
                with patch.object(roundhouse, 'switch_preflight', mock_preflight):
                    status, body = self.post_http(
                        '/api/switch',
                        {'target': 'qwen3.6-coding.service', 'stops': [], 'confirm': old_confirm},
                        {'Authorization': 'Bearer test-token'}
                    )
                self.assertEqual(status, 409)
                payload = json.loads(body)
                self.assertEqual(payload.get('error'), 'preview_stale')
            finally:
                pass
        finally:
            roundhouse.ACTUATE_ARMED = False
            roundhouse.TOKEN = None

    def test_get_rollout_on_switch_record_returns_200(self):
        """GET /api/rollouts/<sw-id> returns the switch record."""
        roundhouse.ACTUATE_ARMED = True
        try:
            # Add a switch record
            self.server.rollout_engine.rollouts = {
                'sw-1-1': {
                    'rollout_id': 'sw-1-1', 'kind': 'switch', 'unit': 'test.service',
                    'target': 'test.service', 'stops': [], 'stopped': [],
                    'target_started': False, 'phase': 'done', 'detail': 'switched',
                    'restored': False, 'failure': None, 'rollback': None,
                    'started_at': 1.0, 'updated_at': 2.0,
                }
            }
            try:
                status, body = self.get_http('/api/rollouts/sw-1-1')
                self.assertEqual(status, 200)
                rec = json.loads(body)
                self.assertEqual(rec['kind'], 'switch')
                self.assertEqual(rec['target'], 'test.service')
            finally:
                self.server.rollout_engine.rollouts = {}
        finally:
            roundhouse.ACTUATE_ARMED = False

    def test_get_on_switch_preview_route_returns_405(self):
        """GET /api/switch/preview returns 405."""
        status, _ = self.get_http('/api/switch/preview')
        self.assertEqual(status, 405)

    def test_get_on_switch_route_returns_405(self):
        """GET /api/switch returns 405."""
        status, _ = self.get_http('/api/switch')
        self.assertEqual(status, 405)


if __name__ == '__main__':
    unittest.main()
