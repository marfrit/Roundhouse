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


class TestUnclassifiedManagedUnitIsProbed(unittest.TestCase):
    """A '# roundhouse: manage' unit with no engine Roundhouse knows (bladerunner's
    python harness on bosch) was active and never ready: LOADING forever. Its
    readiness now comes from GET /v1/models like the OpenAI-style engines."""

    def setUp(self):
        self.path = Path(__file__).resolve().parents[0] / "fixtures-extra" / "bladerunner-mneme.service"
        self.unit = roundhouse.parse_unit(str(self.path), self.path.read_bytes())

    def test_selected_by_marker_and_unclassified(self):
        names = [os.path.basename(p) for p in roundhouse.select_units(str(self.path.parent))]
        self.assertIn('bladerunner-mneme.service', names)
        self.assertEqual(self.unit.exec_start.engine, {})

    def test_probe_gated_with_a_port(self):
        self.assertTrue(roundhouse.probes_models(self.unit.exec_start.engine))
        profile = roundhouse.extract_param_profile(self.unit.exec_start.engine_argv)
        self.assertEqual(roundhouse.probe_port(self.unit.exec_start.engine, profile), 8094)

    def test_no_declared_port_means_no_probe(self):
        """extract_param_profile defaults to 8080 — another engine's port on this fleet.
        An unclassified unit must not go READY on somebody else's answer."""
        path = self.path.parent / 'marker-no-port.service'
        unit = roundhouse.parse_unit(str(path), path.read_bytes())
        self.assertEqual(unit.exec_start.engine, {})
        profile = roundhouse.extract_param_profile(unit.exec_start.engine_argv)
        self.assertEqual(profile['port_source'], 'default', 'precondition: the profile invents a port')
        self.assertIsNone(roundhouse.probe_port(unit.exec_start.engine, profile))

    def test_classified_engines_keep_their_default_port(self):
        raw = b"[Service]\nExecStart=/usr/bin/arcint --model /models/ov/x\n"
        unit = roundhouse.parse_unit('/tmp/arcint.service', raw)
        profile = roundhouse.extract_param_profile(unit.exec_start.engine_argv)
        self.assertEqual(roundhouse.probe_port(unit.exec_start.engine, profile), 8090)

    def test_journal_driven_engines_stay_off_the_probe(self):
        """llama-server and llamafile keep their journal markers (BUSY needs them)."""
        self.assertFalse(roundhouse.probes_models({'kind': 'llama-server'}))
        self.assertFalse(roundhouse.probes_models({'kind': 'llamafile'}))
        for kind in roundhouse.OPENAI_PROBE_ENGINES:
            self.assertTrue(roundhouse.probes_models({'kind': kind}), kind)

    def test_the_poll_loop_asks_the_helper(self):
        src = (Path(__file__).resolve().parents[1] / 'roundhouse.py').read_text()
        loop = src[src.index('probe_targets = []'):src.index('for unit_name, unit_port in probe_targets')]
        self.assertIn('probes_models(u.exec_start.engine)', loop)
        self.assertIn('probe_port(', loop)
        self.assertNotIn('not in OPENAI_PROBE_ENGINES', loop)


class TestWhisperEngine(unittest.TestCase):
    """whisper.cpp's server (upstream `whisper-server`, renamed `arcint-whisper`
    by the Arc package): a kind with --port/--host but no alias flag and no
    /v1/models. Readiness is GET /health (see TestWhisperReadiness)."""

    def setUp(self):
        self.fixtures_extra = Path(__file__).resolve().parents[0] / "fixtures-extra"

    def _parse(self, raw, name='/tmp/stt.service'):
        return roundhouse.parse_unit(name, raw)

    def test_arcint_whisper_basename(self):
        unit = self._parse(b"[Service]\nExecStart=/opt/fixture/bin/arcint-whisper "
                           b"--model /opt/fixture/models/ggml-base.en.bin --port 8096\n")
        self.assertEqual(unit.exec_start.engine.get('kind'), 'whisper')
        self.assertEqual(unit.exec_start.engine.get('variant'), 'whisper.cpp')

    def test_upstream_whisper_server_basename(self):
        unit = self._parse(b"[Service]\nExecStart=/opt/fixture/bin/whisper-server "
                           b"--model /opt/fixture/models/ggml-base.en.bin\n")
        self.assertEqual(unit.exec_start.engine.get('kind'), 'whisper')

    def test_port_from_flag(self):
        unit = self._parse(b"[Service]\nExecStart=/opt/fixture/bin/arcint-whisper "
                           b"--model /opt/fixture/m.bin --host 127.0.0.1 --port 8096\n")
        profile = roundhouse.extract_param_profile(unit.exec_start.engine_argv)
        self.assertEqual(profile['port'], 8096)
        self.assertEqual(profile['port_source'], 'flag')
        self.assertEqual(profile['host_bind'], '127.0.0.1')

    def test_default_port_is_whisper_cpps_own(self):
        unit = self._parse(b"[Service]\nExecStart=/usr/bin/whisper-server "
                           b"--model /opt/fixture/models/ggml-base.en.bin\n")
        profile = roundhouse.extract_param_profile(unit.exec_start.engine_argv)
        self.assertEqual(profile['port'], 8080, 'whisper.cpp serves on 8080 when --port is absent')
        self.assertEqual(profile['port_source'], 'default')

    def test_probed_like_the_other_classified_engines(self):
        """A classified whisper unit is probed (via /health) on its own default,
        the same way arcint is probed on its own default."""
        unit = self._parse(b"[Service]\nExecStart=/usr/bin/whisper-server "
                           b"--model /opt/fixture/models/ggml-base.en.bin\n")
        profile = roundhouse.extract_param_profile(unit.exec_start.engine_argv)
        self.assertTrue(roundhouse.probes_models(unit.exec_start.engine))
        self.assertEqual(roundhouse.probe_port(unit.exec_start.engine, profile), 8080)

    def test_fixture_is_selected(self):
        names = [os.path.basename(p) for p in
                 roundhouse.select_units(str(self.fixtures_extra))]
        self.assertIn('whisper-transcribe.service', names)


class TestAliasDirective(unittest.TestCase):
    """`# roundhouse: alias <name>` (also `;`) names a unit whose engine has no
    alias flag. A flag alias wins over the directive; an invalid name is refused."""

    def _parse(self, directive, exec_start):
        raw = ("[Service]\n" + directive + "\nExecStart=" + exec_start + "\n").encode()
        return roundhouse.parse_unit('/tmp/stt.service', raw)

    def _whisper(self, directive='# roundhouse: alias stt-base'):
        return self._parse(directive, '/opt/fixture/bin/arcint-whisper '
                           '--model /opt/fixture/m.bin --port 8096')

    def test_hash_form_sets_the_alias(self):
        unit = self._whisper('# roundhouse: alias stt-base')
        self.assertEqual(unit.alias_directive, 'stt-base')
        profile = roundhouse.extract_param_profile(unit.exec_start.engine_argv)
        self.assertEqual(profile['alias'], None, 'precondition: the engine has no alias flag')
        self.assertEqual(roundhouse.unit_alias(unit, profile), 'stt-base')
        self.assertEqual(unit.warnings, [])

    def test_semicolon_form_sets_the_alias(self):
        unit = self._whisper('; roundhouse: alias stt-base')
        self.assertEqual(unit.alias_directive, 'stt-base')
        profile = roundhouse.extract_param_profile(unit.exec_start.engine_argv)
        self.assertEqual(roundhouse.unit_alias(unit, profile), 'stt-base')

    def test_directive_absent_falls_back_to_the_unit_name(self):
        unit = self._parse('[Service]', '/usr/bin/whisper-server --model /opt/fixture/m.bin')
        self.assertIsNone(unit.alias_directive)
        profile = roundhouse.extract_param_profile(unit.exec_start.engine_argv)
        self.assertEqual(roundhouse.unit_alias(unit, profile), 'stt.service')

    def test_flag_beats_the_directive_with_a_warning(self):
        unit = self._parse('# roundhouse: alias from-directive',
                           '/usr/bin/llama-server --model /opt/fixture/m.gguf '
                           '--alias from-flag --port 8080')
        self.assertEqual(unit.alias_directive, 'from-directive', 'read, but not used')
        profile = roundhouse.extract_param_profile(unit.exec_start.engine_argv)
        self.assertEqual(roundhouse.unit_alias(unit, profile), 'from-flag',
                         'one source of truth: the flag')
        self.assertTrue(any('alias directive ignored' in w for w in unit.warnings),
                        unit.warnings)

    def test_invalid_name_is_rejected(self):
        unit = self._whisper("# roundhouse: alias 'stt base'")
        self.assertIsNone(unit.alias_directive)
        profile = roundhouse.extract_param_profile(unit.exec_start.engine_argv)
        self.assertEqual(roundhouse.unit_alias(unit, profile), 'stt.service')
        self.assertTrue(any('invalid alias' in w for w in unit.warnings), unit.warnings)


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


class TestOpenArcEngine(unittest.TestCase):
    """OpenArc (OpenVINO) engine recognition — MVP10."""

    def setUp(self):
        self.fixtures_extra = Path(__file__).resolve().parents[0] / "fixtures-extra"

    def _parse_fixture(self):
        fpath = str(self.fixtures_extra / "openarc-coder.service")
        with open(fpath, 'rb') as f:
            raw = f.read()
        return roundhouse.parse_unit(fpath, raw)

    def test_classified_as_openarc(self):
        unit = self._parse_fixture()
        self.assertIsNotNone(unit.exec_start)
        self.assertEqual(unit.exec_start.engine.get('kind'), 'openarc')
        self.assertEqual(unit.exec_start.engine.get('variant'), 'openvino')
        self.assertEqual(unit.exec_start.engine.get('binary'),
                         '/home/mfritsche/openarc-venv/bin/openarc')

    def test_port_from_flag(self):
        unit = self._parse_fixture()
        profile = roundhouse.extract_param_profile(unit.exec_start.engine_argv)
        self.assertEqual(profile['port'], 8080)
        self.assertEqual(profile['port_source'], 'flag')

    def test_alias_from_load_models(self):
        unit = self._parse_fixture()
        profile = roundhouse.extract_param_profile(unit.exec_start.engine_argv)
        self.assertEqual(profile['alias'], 'qwen3.6-coder')

    def test_default_port_8000(self):
        """Without --port, OpenArc serves on 8000 (not llama.cpp's 8080)."""
        raw = b"""[Unit]
Description=Test
[Service]
ExecStart=/home/mfritsche/openarc-venv/bin/openarc serve start --load-models m1
"""
        unit = roundhouse.parse_unit('/tmp/openarc-test.service', raw)
        profile = roundhouse.extract_param_profile(unit.exec_start.engine_argv)
        self.assertEqual(profile['port'], 8000)
        self.assertEqual(profile['port_source'], 'default')

    def test_non_serve_subcommand_not_classified(self):
        """`openarc download` is a CLI action, not a server: no engine."""
        raw = b"""[Unit]
Description=Test
[Service]
ExecStart=/home/mfritsche/openarc-venv/bin/openarc download --model foo
"""
        unit = roundhouse.parse_unit('/tmp/openarc-dl.service', raw)
        self.assertEqual(unit.exec_start.engine, {})

    def test_select_units_picks_up_openarc(self):
        result = roundhouse.select_units(str(self.fixtures_extra))
        names = [os.path.basename(p) for p in result]
        self.assertIn('openarc-coder.service', names)

    def test_conflicts_directive_in_known(self):
        unit = self._parse_fixture()
        self.assertEqual(unit.known.get('conflicts'),
                         'llama-coder.service llama-agent.service llama-qwen38.service')


class TestArcintEngine(unittest.TestCase):
    """arcint recognition, alias from --model-id, ctx from --n-ctx (dirac swap)."""

    def setUp(self):
        self.fixtures_extra = Path(__file__).resolve().parents[0] / "fixtures-extra"

    def _parse(self, name):
        fpath = str(self.fixtures_extra / name)
        with open(fpath, 'rb') as f:
            return roundhouse.parse_unit(fpath, f.read())

    def test_classification_and_profile(self):
        unit = self._parse('arcint.service')
        self.assertEqual(unit.exec_start.engine.get('kind'), 'arcint')
        self.assertEqual(unit.exec_start.engine.get('variant'), 'openvino')
        profile = roundhouse.extract_param_profile(unit.exec_start.engine_argv)
        self.assertEqual(profile['port'], 8080)
        self.assertEqual(profile['port_source'], 'flag')
        self.assertEqual(profile['ctx'], 262144, '--n-ctx is arcint\'s context flag')
        self.assertEqual(profile['model_path'], '/models/ov/qwen36-coder-b5-ov')

    def test_alias_is_the_served_name(self):
        # The deployed unit carries both alias sources: --model-id asserts the
        # artifact, --served-model-name (arcint 0.2.1) names the endpoint. The
        # roster must show what a client sends, not the allowlist id.
        unit = self._parse('arcint.service')
        profile = roundhouse.extract_param_profile(unit.exec_start.engine_argv)
        self.assertEqual(profile['alias'], 'qwen3.6-coder')

    def test_served_model_name_outranks_model_id_either_order(self):
        # Both flags map to 'alias'. Plain last-wins would make the roster alias
        # depend on flag order in the unit; the served name must always win, and
        # the edit span must land on the flag that names the endpoint.
        for argv in ("--model-id qwen3.6-27b-a3b-coder --served-model-name qwen3.6-coder",
                     "--served-model-name qwen3.6-coder --model-id qwen3.6-27b-a3b-coder"):
            raw = ("[Service]\nExecStart=/usr/bin/arcint --model /models/ov/x "
                   + argv + " --port 8080\n").encode()
            unit = roundhouse.parse_unit('/tmp/arcint.service', raw)
            profile = roundhouse.extract_param_profile(unit.exec_start.engine_argv)
            self.assertEqual(profile['alias'], 'qwen3.6-coder', argv)
            flag_span = profile['spans']['alias']['flag']
            self.assertEqual(raw[flag_span[0]:flag_span[1]], b'--served-model-name', argv)

    def test_model_id_alone_is_still_the_alias(self):
        raw = (b"[Service]\nExecStart=/usr/bin/arcint --model /models/ov/x "
               b"--model-id qwen3.6-27b-a3b-coder --port 8080\n")
        unit = roundhouse.parse_unit('/tmp/arcint.service', raw)
        profile = roundhouse.extract_param_profile(unit.exec_start.engine_argv)
        self.assertEqual(profile['alias'], 'qwen3.6-27b-a3b-coder')

    def test_default_port_is_arcints_own(self):
        raw = b"[Service]\nExecStart=/usr/bin/arcint --model /models/ov/x\n"
        unit = roundhouse.parse_unit('/tmp/arcint.service', raw)
        profile = roundhouse.extract_param_profile(unit.exec_start.engine_argv)
        self.assertEqual(profile['port'], 8090, "arcint's own default, not 8080")
        self.assertEqual(profile['port_source'], 'default')

    def test_selected_and_probe_gated(self):
        names = [os.path.basename(p) for p in
                 roundhouse.select_units(str(self.fixtures_extra))]
        self.assertIn('arcint.service', names)
        self.assertIn('arcint', roundhouse.OPENAI_PROBE_ENGINES)

    def test_conflicts_and_mem_estimate_are_read(self):
        unit = self._parse('arcint.service')
        self.assertEqual(unit.mem_estimate, 8 << 30, "measured peak 7.80 GiB, rounded up")
        self.assertIn('openarc-coder.service', unit.known.get('conflicts', ''))


class TestBoschEngines(unittest.TestCase):
    """ds4-server and docker-wrapped vLLM recognition + mem-estimate marker (MVP10)."""

    def setUp(self):
        self.fixtures_extra = Path(__file__).resolve().parents[0] / "fixtures-extra"

    def _parse(self, name):
        fpath = str(self.fixtures_extra / name)
        with open(fpath, 'rb') as f:
            return roundhouse.parse_unit(fpath, f.read())

    def test_ds4_classification_and_profile(self):
        unit = self._parse('ds4-server.service')
        self.assertEqual(unit.exec_start.engine.get('kind'), 'ds4')
        self.assertEqual(unit.exec_start.engine.get('variant'), 'ds4')
        profile = roundhouse.extract_param_profile(unit.exec_start.engine_argv)
        self.assertEqual(profile['port'], 8085)
        self.assertEqual(profile['port_source'], 'flag')
        self.assertEqual(profile['ctx'], 655360)
        self.assertTrue(profile['model_path'].endswith('.gguf'))

    def test_vllm_docker_classification_and_profile(self):
        unit = self._parse('qwen-vllm.service')
        self.assertEqual(unit.exec_start.engine.get('kind'), 'vllm')
        self.assertEqual(unit.exec_start.engine.get('variant'), 'docker')
        profile = roundhouse.extract_param_profile(unit.exec_start.engine_argv)
        self.assertEqual(profile['port'], 8086, 'host half of -p 8086:8000')
        self.assertEqual(profile['port_source'], 'flag')
        self.assertEqual(profile['alias'], 'qwen38', 'first --served-model-name value')
        self.assertEqual(profile['ctx'], 262144)
        self.assertEqual(profile['model_path'], '/home/mfritsche/models/nvfp4/qwen38-nvfp4')

    def test_plain_docker_unit_not_classified(self):
        raw = b"""[Unit]
Description=Some container
[Service]
ExecStart=/usr/bin/docker run --rm -p 8086:8000 nginx:latest
"""
        unit = roundhouse.parse_unit('/tmp/nginx.service', raw)
        self.assertEqual(unit.exec_start.engine, {})

    def test_mem_estimate_marker(self):
        self.assertEqual(self._parse('ds4-server.service').mem_estimate, 100 << 30)
        self.assertEqual(self._parse('qwen-vllm.service').mem_estimate, 106 << 30)
        self.assertIsNone(self._parse('openarc-coder.service').mem_estimate)

    def test_parse_mem_size(self):
        self.assertEqual(roundhouse.parse_mem_size('100G'), 100 << 30)
        self.assertEqual(roundhouse.parse_mem_size('30g'), 30 << 30)
        self.assertEqual(roundhouse.parse_mem_size('512M'), 512 << 20)
        self.assertEqual(roundhouse.parse_mem_size('1048576'), 1048576)
        self.assertIsNone(roundhouse.parse_mem_size('12GB'))
        self.assertIsNone(roundhouse.parse_mem_size('viel'))

    def test_select_units_picks_up_both(self):
        result = roundhouse.select_units(str(self.fixtures_extra))
        names = [os.path.basename(p) for p in result]
        self.assertIn('ds4-server.service', names)
        self.assertIn('qwen-vllm.service', names)

    def test_estimate_prefers_declared_marker(self):
        est, src = roundhouse._estimate_start_bytes('x.service', {}, None,
                                                    mem_estimate=100 << 30)
        self.assertEqual((est, src), (100 << 30, 'declared'))

    def test_freed_bytes_prefers_declared_marker(self):
        row = {'rung': 'READY', 'mem_estimate': 106 << 30, 'mem': {}}
        freed, src = roundhouse._freed_bytes('q.service', row,
                                             {'q.service': {'current': 800 << 20}})
        self.assertEqual(freed, 106 << 30)
        self.assertEqual(src, 'declared mem-estimate')
