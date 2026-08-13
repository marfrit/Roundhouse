#!/usr/bin/env python3
"""Roundhouse MVP1 Parser Test Suite

Tests the parse_unit and related functions defined in roundhouse.py.
Covers all 23 fixtures plus synthetic test cases.
"""

import sys
import os
import unittest
import json
from pathlib import Path

# Setup path to import roundhouse from parent directory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import roundhouse


class TestParserBasics(unittest.TestCase):
    """Basic parser functionality tests."""

    def setUp(self):
        """Set up test fixtures directory."""
        self.repo_root = Path(__file__).resolve().parents[2]
        self.fixtures_dir = self.repo_root / "docs" / "fixtures"
        self.fixtures_extra = Path(__file__).resolve().parents[0] / "fixtures-extra"

    def test_parse_unit_signature(self):
        """Test that parse_unit has the correct signature."""
        self.assertTrue(callable(roundhouse.parse_unit))
        # parse_unit should accept path and raw bytes
        fpath = str(self.fixtures_dir / "qwen3.6-coding.service")
        with open(fpath, 'rb') as f:
            raw = f.read()
        result = roundhouse.parse_unit(fpath, raw)
        self.assertIsInstance(result, roundhouse.UnitFile)

    def test_dataclass_existence(self):
        """Test that all required dataclasses exist."""
        self.assertTrue(hasattr(roundhouse, 'Line'))
        self.assertTrue(hasattr(roundhouse, 'Directive'))
        self.assertTrue(hasattr(roundhouse, 'Token'))
        self.assertTrue(hasattr(roundhouse, 'ExecStart'))
        self.assertTrue(hasattr(roundhouse, 'UnitFile'))

    def test_function_existence(self):
        """Test that all required functions exist."""
        functions = [
            'parse_unit', 'tokenize_execstart', 'extract_param_profile',
            'parse_gate', 'select_units', 'build_deployment',
            'quant_hint', 'assert_no_paid_offload'
        ]
        for func_name in functions:
            self.assertTrue(hasattr(roundhouse, func_name), f"Missing function: {func_name}")


class TestByteOffsets(unittest.TestCase):
    """Test byte offset preservation (acceptance criterion)."""

    def setUp(self):
        self.repo_root = Path(__file__).resolve().parents[2]
        self.fixtures_dir = self.repo_root / "docs" / "fixtures"

    def test_line_byte_reconstruction(self):
        """Test that all bytes are accounted for in lines (invariant)."""
        for service_file in sorted(self.fixtures_dir.glob("*.service")):
            with self.subTest(file=service_file.name):
                with open(service_file, 'rb') as f:
                    raw = f.read()

                unit = roundhouse.parse_unit(str(service_file), raw)

                # Reconstruct from lines
                reconstructed = b''.join(
                    raw[line.start:line.end] for line in unit.lines
                )

                self.assertEqual(reconstructed, raw,
                    f"Line reconstruction failed for {service_file.name}")


class TestComments(unittest.TestCase):
    """Test comment extraction and preservation."""

    def setUp(self):
        self.repo_root = Path(__file__).resolve().parents[2]
        self.fixtures_dir = self.repo_root / "docs" / "fixtures"

    def test_comments_verbatim(self):
        """Test that comments are extracted verbatim and none are lost."""
        for service_file in sorted(self.fixtures_dir.glob("*.service")):
            with self.subTest(file=service_file.name):
                with open(service_file, 'rb') as f:
                    raw = f.read()

                unit = roundhouse.parse_unit(str(service_file), raw)

                # Extract expected comments from raw
                expected_comments = []
                for line in raw.split(b'\n'):
                    line_str = line.decode('utf-8', errors='replace')
                    stripped = line_str.lstrip()
                    if stripped.startswith('#') or stripped.startswith(';'):
                        expected_comments.append(line_str.rstrip())

                # Extract actual comments
                actual_comments = [c['text'] for c in unit.comments]

                self.assertEqual(actual_comments, expected_comments,
                    f"Comment mismatch in {service_file.name}")


class TestGateParsing(unittest.TestCase):
    """Test kernel gate detection."""

    def setUp(self):
        self.repo_root = Path(__file__).resolve().parents[2]
        self.fixtures_dir = self.repo_root / "docs" / "fixtures"

    def test_qwen35_npu_gate(self):
        """Test that llama-server-qwen35-npu.service has correct gate."""
        fpath = self.fixtures_dir / "llama-server-qwen35-npu.service"
        with open(fpath, 'rb') as f:
            raw = f.read()

        unit = roundhouse.parse_unit(str(fpath), raw)

        self.assertIsNotNone(unit.gate)
        self.assertEqual(unit.gate['kind'], 'kernel')
        self.assertEqual(unit.gate['wants'], '6.1.75-npu-port')


class TestRetiredDetection(unittest.TestCase):
    """Test retired unit detection."""

    def setUp(self):
        self.repo_root = Path(__file__).resolve().parents[2]
        self.fixtures_dir = self.repo_root / "docs" / "fixtures"

    def test_mixperten_retired(self):
        """Test that mixperten.service is detected as retired."""
        fpath = self.fixtures_dir / "mixperten.service"
        with open(fpath, 'rb') as f:
            raw = f.read()

        unit = roundhouse.parse_unit(str(fpath), raw)

        self.assertTrue(unit.retired)
        self.assertIsNotNone(unit.retired_note)
        self.assertTrue(unit.retired_note.startswith('[RETIRED'))


class TestPortBoard(unittest.TestCase):
    """Test port board collision detection."""

    def setUp(self):
        self.repo_root = Path(__file__).resolve().parents[2]
        self.fixtures_dir = self.repo_root / "docs" / "fixtures"

    def test_port_collisions(self):
        """Test that port collisions are detected."""
        # Parse all units
        units = {}
        port_claims = {}

        for service_file in sorted(self.fixtures_dir.glob("*.service")):
            with open(service_file, 'rb') as f:
                raw = f.read()

            unit = roundhouse.parse_unit(str(service_file), raw)
            units[unit.name] = unit

            if unit.exec_start:
                profile = roundhouse.extract_param_profile(unit.exec_start.engine_argv)
                port = profile.get('port', 8080)

                if port not in port_claims:
                    port_claims[port] = []
                port_claims[port].append({
                    'unit': unit.name,
                    'enabled': True,
                    'rung': 'READY',
                    'retired': unit.retired,
                    'gate': unit.gate
                })

        # Check for known collisions
        # Port 8085: qwen3.6-coding + mixperten
        self.assertIn(8085, port_claims)
        port_8085_units = [c['unit'] for c in port_claims[8085]]
        self.assertIn('qwen3.6-coding.service', port_8085_units)
        self.assertIn('mixperten.service', port_8085_units)

        # Port 8086: llama-task + llama-server-qwen35-npu
        self.assertIn(8086, port_claims)
        port_8086_units = [c['unit'] for c in port_claims[8086]]
        self.assertTrue(
            ('llama-task.service' in port_8086_units or 'llama-task' in str(port_8086_units)) or
            len(port_claims[8086]) >= 2
        )


class TestParamProfile(unittest.TestCase):
    """Test parameter profile extraction."""

    def setUp(self):
        self.repo_root = Path(__file__).resolve().parents[2]
        self.fixtures_dir = self.repo_root / "docs" / "fixtures"

    def test_qwen36_coding_profile(self):
        """Test param profile extraction for qwen3.6-coding.service."""
        fpath = self.fixtures_dir / "qwen3.6-coding.service"
        with open(fpath, 'rb') as f:
            raw = f.read()

        unit = roundhouse.parse_unit(str(fpath), raw)
        self.assertIsNotNone(unit.exec_start)

        profile = roundhouse.extract_param_profile(unit.exec_start.engine_argv)

        # Check for known parameters
        self.assertEqual(profile['ctx'], 65536)
        self.assertEqual(profile['alias'], 'qwen3.6-coding')
        self.assertEqual(profile['port'], 8085)
        self.assertTrue(profile['jinja'])
        self.assertEqual(profile['flash_attn'], 'on')

    def test_chat_template_kwargs_json(self):
        """Test JSON parsing in chat_template_kwargs."""
        fpath = self.fixtures_dir / "qwen3.6-coding.service"
        with open(fpath, 'rb') as f:
            raw = f.read()

        unit = roundhouse.parse_unit(str(fpath), raw)
        profile = roundhouse.extract_param_profile(unit.exec_start.engine_argv)

        self.assertIsNotNone(profile['chat_template_kwargs_json'])
        self.assertIsInstance(profile['chat_template_kwargs_json'], dict)
        self.assertIn('enable_thinking', profile['chat_template_kwargs_json'])


class TestAllFixtures(unittest.TestCase):
    """Test that all 23 fixtures parse without error."""

    def setUp(self):
        self.repo_root = Path(__file__).resolve().parents[2]
        self.fixtures_dir = self.repo_root / "docs" / "fixtures"

    def test_count_and_parse_all(self):
        """Test that all 23 fixtures exist and parse."""
        service_files = sorted(self.fixtures_dir.glob("*.service"))
        self.assertEqual(len(service_files), 23, f"Expected 23 fixtures, got {len(service_files)}")

        for service_file in service_files:
            with self.subTest(file=service_file.name):
                with open(service_file, 'rb') as f:
                    raw = f.read()

                # Should not raise
                unit = roundhouse.parse_unit(str(service_file), raw)

                # Basic checks
                self.assertIsNotNone(unit)
                self.assertEqual(unit.name, service_file.name)


class TestSelectUnits(unittest.TestCase):
    """Test unit selection logic."""

    def test_select_fixtures(self):
        """Test that select_units finds the fixtures directory."""
        repo_root = Path(__file__).resolve().parents[2]
        fixtures_dir = repo_root / "docs" / "fixtures"

        # select_units expects a directory with actual .service files
        # For this test, we just verify it returns a list of paths
        result = roundhouse.select_units(str(fixtures_dir))
        self.assertIsInstance(result, list)
        # All results should be .service files
        for path in result:
            self.assertTrue(path.endswith('.service'))


class TestQuantHint(unittest.TestCase):
    """Test quantization hint extraction."""

    def test_q4_k_m(self):
        """Test Q4_K_M detection."""
        hint = roundhouse.quant_hint("qwen36-27b-a3b-coder-Q4_K_M.gguf")
        self.assertEqual(hint, "Q4_K_M")

    def test_iq_types(self):
        """Test IQ type detection."""
        hint = roundhouse.quant_hint("model-IQ3_XXS.gguf")
        self.assertEqual(hint, "IQ3_XXS")

    def test_no_quant(self):
        """Test handling of files without quant hints."""
        hint = roundhouse.quant_hint("model.gguf")
        self.assertIsNone(hint)


class TestPaidOffloadGuard(unittest.TestCase):
    """Test the no-paid-offloading assertion."""

    def test_assert_no_paid_offload_clean(self):
        """Test that clean deployments pass."""
        dep = {
            'unit': 'test.service',
            'exec_start': None,
            'known': {}
        }
        # Should not raise
        roundhouse.assert_no_paid_offload(dep)

    def test_assert_no_paid_offload_blocked(self):
        """Test that paid APIs are blocked."""
        # Create a mock token with openai
        class MockToken:
            def __init__(self, text):
                self.text = text

        class MockExecStart:
            def __init__(self):
                self.tokens = [MockToken("api.openai.com/v1")]

        dep = {
            'unit': 'test.service',
            'exec_start': MockExecStart(),
            'known': {}
        }
        with self.assertRaises(AssertionError):
            roundhouse.assert_no_paid_offload(dep)


class TestBuildDeployment(unittest.TestCase):
    """Test deployment record building."""

    def setUp(self):
        self.repo_root = Path(__file__).resolve().parents[2]
        self.fixtures_dir = self.repo_root / "docs" / "fixtures"

    def test_qwen36_coding_deployment(self):
        """Test build_deployment for qwen3.6-coding.service."""
        fpath = self.fixtures_dir / "qwen3.6-coding.service"
        with open(fpath, 'rb') as f:
            raw = f.read()

        unit = roundhouse.parse_unit(str(fpath), raw)

        # Mock stat to avoid file system issues
        def mock_stat(path):
            class StatResult:
                st_size = 16040000000
                st_mtime = 1765500000
            return StatResult()

        dep = roundhouse.build_deployment(unit, "boltzmann", statf=mock_stat)

        # Check structure
        self.assertIn('deployment_id', dep)
        self.assertIn('unit', dep)
        self.assertIn('artifact', dep)
        self.assertIn('host_artifact', dep)
        self.assertIn('engine', dep)
        self.assertIn('param_profile', dep)

        # Check values
        self.assertEqual(dep['unit'], 'qwen3.6-coding.service')
        self.assertTrue(dep['deployment_id'].startswith('boltzmann/'))


if __name__ == '__main__':
    unittest.main()


class TestByteOffsetProperties(unittest.TestCase):
    """SPEC.md §8 property tests (2) and (3): the acceptance criterion
    'byte offsets retained per extracted token', proven on every fixture.
    These are the tests whose absence let a span-drift bug ship to review."""

    def _fixtures(self):
        fixdir = Path(__file__).resolve().parents[2] / "docs" / "fixtures"
        extra = Path(__file__).resolve().parent / "fixtures-extra"
        for d in (fixdir, extra):
            for p in sorted(d.glob("*.service")):
                yield p

    def test_token_spans_slice_to_raw(self):
        """Property (2): raw[t.start:t.end] == t.raw for every token of every fixture."""
        checked = 0
        for p in self._fixtures():
            raw = p.read_bytes()
            unit = roundhouse.parse_unit(str(p), raw)
            if not unit.exec_start:
                continue
            for t in unit.exec_start.tokens:
                self.assertEqual(
                    raw[t.start:t.end], t.raw,
                    f"{p.name}: token {t.text!r} span [{t.start}:{t.end}] "
                    f"slices to {raw[t.start:t.end]!r}, not its raw bytes")
                checked += 1
        self.assertGreater(checked, 400, "property test must cover the full corpus")

    def test_profile_value_spans_rederive(self):
        """Property (3): every ParamProfile value span slices to the bytes of the
        token that produced the field (re-derivable without re-tokenizing)."""
        checked = 0
        for p in self._fixtures():
            raw = p.read_bytes()
            unit = roundhouse.parse_unit(str(p), raw)
            if not unit.exec_start:
                continue
            by_start = {t.start: t for t in unit.exec_start.tokens}
            profile = roundhouse.extract_param_profile(unit.exec_start.engine_argv)
            for name, span in (profile.get("spans") or {}).items():
                for part in ("flag", "value"):
                    s = span.get(part)
                    if not s:
                        continue
                    tok = by_start.get(s[0])
                    self.assertIsNotNone(
                        tok, f"{p.name}: {name}.{part} span start {s[0]} matches no token")
                    self.assertEqual(
                        raw[s[0]:s[1]], tok.raw,
                        f"{p.name}: {name}.{part} span does not slice to its token")
                    checked += 1
        self.assertGreater(checked, 100)


class TestOnDemandMarker(unittest.TestCase):
    """Test on-demand marker parsing (Section A)."""

    def test_marker_hash_form(self):
        """Marker in hash form: # roundhouse: on-demand."""
        raw = b"""[Unit]
Description=Test
# roundhouse: on-demand
[Service]
ExecStart=/usr/bin/test
"""
        unit = roundhouse.parse_unit('/tmp/test.service', raw)
        self.assertTrue(unit.on_demand)

    def test_marker_semicolon_form(self):
        """Marker in semicolon form: ; roundhouse: on-demand."""
        raw = b"""[Unit]
Description=Test
; roundhouse: on-demand
[Service]
ExecStart=/usr/bin/test
"""
        unit = roundhouse.parse_unit('/tmp/test.service', raw)
        self.assertTrue(unit.on_demand)

    def test_marker_absent(self):
        """Marker absent: on_demand defaults to False."""
        raw = b"""[Unit]
Description=Test
[Service]
ExecStart=/usr/bin/test
"""
        unit = roundhouse.parse_unit('/tmp/test.service', raw)
        self.assertFalse(unit.on_demand)

    def test_marker_partial_no_match(self):
        """Partial marker (e.g. on-demandX) matches as substring."""
        raw = b"""[Unit]
Description=Test
# roundhouse: on-demandX
[Service]
ExecStart=/usr/bin/test
"""
        unit = roundhouse.parse_unit('/tmp/test.service', raw)
        # This matches as a substring per the wart (consistency with manage/ignore)
        self.assertTrue(unit.on_demand)

    def test_marker_in_execstart(self):
        """Marker inside a quoted ExecStart argument still counts."""
        raw = b"""[Unit]
Description=Test
[Service]
ExecStart=/usr/bin/test "arg # roundhouse: on-demand"
"""
        unit = roundhouse.parse_unit('/tmp/test.service', raw)
        # Per H1 and recon 7: substring scan anywhere in raw, including quoted strings
        self.assertTrue(unit.on_demand)

    def test_marker_default_value(self):
        """on_demand field defaults to False in UnitFile."""
        raw = b"""[Unit]
Description=Test
[Service]
ExecStart=/usr/bin/test
"""
        unit = roundhouse.parse_unit('/tmp/test.service', raw)
        self.assertFalse(unit.on_demand)
        self.assertIsInstance(unit.on_demand, bool)
