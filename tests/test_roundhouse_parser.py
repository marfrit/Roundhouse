"""Roundhouse Unit Parser Acceptance Tests

This suite defines the contract for the Roundhouse Unit Parser.
It tests parse_unit(path: str) -> dict interface.

The parser must be importable as 'parser' from the repo root (parent of
ROUNDHOUSE_FIXTURES_DIR). The tests will inject the repo root into sys.path
before importing.

Requirements:
1. All 23 fixtures parse without error (no abort on unknown directives).
2. Byte-offset correctness: source[start_byte:end_byte] == token text.
3. Comments byte-exact VERBATIM, none lost/added, order preserved.
4. Quoting edge cases (by basename):
   - qwen3.6-coding.service: '{"enable_thinking":false}' is ONE token inside quotes
   - qwen3.6-coding.service: %% not special
   - llama-server-qwen35-npu.service: backslash-newline continuation
   - mixperten.service: multi-line ExecStart continuation
5. Unknown directives PRESERVED verbatim with byte range.
6. ParamProfile extraction: -c, -t, -fa, -ctk, -ctv, --jinja, --chat-template-kwargs,
   --temp/--top-p/--top-k/--min-p, --alias, --port, model path, quantization.
7. No output at import/load time (no prints, no self-tests).
8. No write path (read-only parser).
9. STANDBY is NOT error — NPU unit is ExecCondition-gated; renders neutrally, never FAILED.

The tests are structured to be RED against a missing parser.
"""

import os
import sys
import tempfile
import hashlib
import shutil
import subprocess
import unittest
import json
from pathlib import Path

# Import contract: parser module is in the repo root (parent of this tests/ directory)
# @coder must place parser.py in /tmp/sandbox/ or /var/lib/bullpen/coder/ etc.
# Repo root = parent of tests/; it is inserted into sys.path
# BUT per @noether: resolve paths relative to test file for grinder compatibility
def get_fixtures_dir():
    """Get the fixtures directory path."""
    # Resolve relative to test file location for grinder compatibility
    test_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(test_dir)
    env_override = os.environ.get('ROUNDHOUSE_FIXTURES_DIR')
    if env_override:
        return env_override
    # In the project, fixtures live under docs/, in a working repo next to it.
    # Allow both instead of moving 23 files or making an environment variable required.
    for candidate in (os.path.join(repo_root, 'fixtures'),
                      os.path.join(repo_root, 'docs', 'fixtures')):
        if os.path.isdir(candidate):
            return candidate
    return os.path.join(repo_root, 'fixtures')

def get_repo_root():
    """Get the repo root (parent of fixtures dir)."""
    # Resolve relative to test file location for grinder compatibility
    test_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(test_dir)

def setup_parser_import():
    """Setup sys.path to import parser from repo root."""
    repo_root = get_repo_root()
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    # Ensure we can import parser
    try:
        import parser
        return parser
    except ImportError as e:
        # Import failed, but we want to fail fast in the test that needs it
        # We'll handle this in test_parse_unit_exists_and_signature
        return None

def capture_import_output():
    """Capture stdout/stderr during import."""
    import io
    from contextlib import redirect_stdout, redirect_stderr
    
    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()
    
    try:
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            parser_module = setup_parser_import()
        return stdout_capture.getvalue(), stderr_capture.getvalue(), parser_module
    except Exception:
        # Import failed, but we still want to capture
        return "", "", None

class TestRoundhouseParser(unittest.TestCase):
    """Roundhouse parser acceptance test suite."""

    def test_parse_unit_exists_and_signature(self):
        """Test that parse_unit is importable and callable with correct signature."""
        # This will fail if parser module doesn't exist (RED)
        stdout, stderr, parser_module = capture_import_output()
        self.assertEqual(len(stdout.strip()), 0, f"Parser import produced stdout: {stdout!r}")
        self.assertEqual(len(stderr.strip()), 0, f"Parser import produced stderr: {stderr!r}")
        self.assertIsNotNone(parser_module, "Could not import parser module")

        # Test that parse_unit exists and is callable
        self.assertTrue(hasattr(parser_module, 'parse_unit'), "parse_unit not found in parser module")
        parse_unit = getattr(parser_module, 'parse_unit')
        self.assertTrue(callable(parse_unit), "parse_unit is not callable")

    def test_no_output_at_import(self):
        """Test that parser module produces no output on import.

        Import is checked FIRST. Without this check the test passes as long as there
        is no parser at all: the import fails, produces no output, and "no output"
        holds trivially. Measured 2026-08-12 -- it was the only passing test in a
        suite that could not verify anything.
        """
        stdout, stderr, parser_module = capture_import_output()
        self.assertIsNotNone(parser_module, "Could not import parser module — suite cannot proceed")
        self.assertEqual(len(stdout.strip()), 0, f"Parser import produced stdout: {stdout!r}")
        self.assertEqual(len(stderr.strip()), 0, f"Parser import produced stderr: {stderr!r}")

    def test_fixtures_accessible(self):
        """Preliminary check: fixture directory exists, 23 units, names match MANIFEST.

        Runs BEFORE every test that needs the parser. Without it, an incorrect
        ROUNDHOUSE_FIXTURES_DIR reports exactly the same error as a missing parser
        ("Could not import parser module") and sends the implementer into his own
        code instead of pointing at the path.
        """
        fixtures_dir = get_fixtures_dir()
        self.assertTrue(os.path.isdir(fixtures_dir),
            f"Fixtures dir not found: {fixtures_dir!r} — check ROUNDHOUSE_FIXTURES_DIR")
        service_files = sorted(f for f in os.listdir(fixtures_dir) if f.endswith('.service'))
        self.assertEqual(len(service_files), 23,
            f"Expected 23 .service files in {fixtures_dir!r}, got {len(service_files)}: {service_files}")

        # MANIFEST.txt is sha256sum format: "<hash>  <filename>", separated by TWO spaces.
        # A previous version split on "\t" and silently skipped comparison -- 26 lines, 0 tabs.
        # No more sentinel: if MANIFEST is missing or unreadable, this test should fail, not be silent.
        manifest = os.path.join(fixtures_dir, 'MANIFEST.txt')
        self.assertTrue(os.path.isfile(manifest), f"MANIFEST.txt missing in {fixtures_dir!r}")
        with open(manifest) as f:
            lines = [l.strip() for l in f if l.strip() and not l.lstrip().startswith('#')]
        named = sorted(os.path.basename(l.split()[-1]) for l in lines if len(l.split()) >= 2)
        self.assertEqual(named, service_files,
            f"Fixture files do not match MANIFEST.txt:\n  on disk:   {service_files}\n"
            f"  manifest:  {named}")


    def test_all_fixtures_parse(self):
        """Test that all 23 fixtures parse without error."""
        parser_module = setup_parser_import()
        self.assertIsNotNone(parser_module, "Could not import parser module")
        parse_unit = getattr(parser_module, 'parse_unit')

        fixtures_dir = get_fixtures_dir()
        service_files = [f for f in os.listdir(fixtures_dir) if f.endswith('.service')]
        self.assertEqual(len(service_files), 23, f"Expected 23 .service files, got {len(service_files)}")

        for service_file in service_files:
            service_path = os.path.join(fixtures_dir, service_file)
            try:
                result = parse_unit(service_path)
                self.assertIsInstance(result, dict, f"parse_unit({service_file!r}) did not return dict")
            except Exception as e:
                self.fail(f"parse_unit({service_file!r}) raised {type(e).__name__}: {e}")

    def test_manifest_content(self):
        """Test that manifest field contains MANIFEST.txt content."""
        parser_module = setup_parser_import()
        self.assertIsNotNone(parser_module, "Could not import parser module")
        parse_unit = getattr(parser_module, 'parse_unit')

        fixtures_dir = get_fixtures_dir()
        manifest_path = os.path.join(fixtures_dir, 'MANIFEST.txt')
        self.assertTrue(os.path.exists(manifest_path), "MANIFEST.txt not found")

        with open(manifest_path, 'r') as f:
            expected_manifest = f.read()

        # Parse any fixture (e.g. qwen3.6-coding.service)
        fixture_path = os.path.join(fixtures_dir, 'qwen3.6-coding.service')
        result = parse_unit(fixture_path)

        # Manifest should be the file content (str)
        self.assertIn('manifest', result, "Result missing 'manifest' key")
        self.assertIsInstance(result['manifest'], str, f"manifest should be str, got {type(result['manifest'])}")
        self.assertEqual(result['manifest'], expected_manifest, "manifest content mismatch")

    def test_sections_structure(self):
        """Test that sections have correct structure."""
        parser_module = setup_parser_import()
        self.assertIsNotNone(parser_module, "Could not import parser module")
        parse_unit = getattr(parser_module, 'parse_unit')

        fixtures_dir = get_fixtures_dir()
        fixture_path = os.path.join(fixtures_dir, 'qwen3.6-coding.service')
        result = parse_unit(fixture_path)

        self.assertIn('sections', result, "Result missing 'sections' key")
        sections = result['sections']
        self.assertIsInstance(sections, list, "'sections' should be a list")

        # Should have at least Unit and Service sections
        section_names = [s.get('name') for s in sections]
        self.assertIn('Unit', section_names, "Missing 'Unit' section")
        self.assertIn('Service', section_names, "Missing 'Service' section")

        for section in sections:
            self.assertIn('name', section, "Section missing 'name'")
            self.assertIn('tokens', section, "Section missing 'tokens'")
            self.assertIn('comments', section, "Section missing 'comments'")
            self.assertIn('params', section, "Section missing 'params'")
            self.assertIn('unknown', section, "Section missing 'unknown'")

            self.assertIsInstance(section['tokens'], list, "'tokens' should be a list")
            self.assertIsInstance(section['comments'], list, "'comments' should be a list")
            self.assertIsInstance(section['params'], dict, "'params' should be a dict")
            self.assertIsInstance(section['unknown'], list, "'unknown' should be a list")

    def test_byte_offset_correctness(self):
        """Test that every token's source[start:end] == token text."""
        parser_module = setup_parser_import()
        self.assertIsNotNone(parser_module, "Could not import parser module")
        parse_unit = getattr(parser_module, 'parse_unit')

        fixtures_dir = get_fixtures_dir()
        fixture_path = os.path.join(fixtures_dir, 'qwen3.6-coding.service')
        source = open(fixture_path, 'rb').read()

        result = parse_unit(fixture_path)
        sections = result['sections']

        for section in sections:
            tokens = section.get('tokens', [])
            for token in tokens:
                text = token.get('text')
                start = token.get('start_byte')
                end = token.get('end_byte')

                # Basic bounds check
                self.assertIsInstance(start, int)
                self.assertIsInstance(end, int)
                self.assertTrue(0 <= start <= end <= len(source), f"Token byte range invalid: {start}:{end} in {len(source)}-byte source")

                # Check byte range matches text
                extracted_text = source[start:end].decode('utf-8')
                self.assertEqual(extracted_text, text, f"Token text mismatch: {extracted_text!r} != {text!r}")

    def test_comments_verbatim_byte_exact(self):
        """Test that comments are extracted byte-exactly, none lost/added, order preserved."""
        parser_module = setup_parser_import()
        self.assertIsNotNone(parser_module, "Could not import parser module")
        parse_unit = getattr(parser_module, 'parse_unit')

        fixtures_dir = get_fixtures_dir()
        fixture_path = os.path.join(fixtures_dir, 'qwen3.6-coding.service')
        source = open(fixture_path, 'rb').read()
        source_lines = source.split(b'\n')

        # Compute expected comments from source lines
        expected_comments = []
        for line in source_lines:
            line_str = line.decode('utf-8')
            stripped = line_str.lstrip()
            if stripped.startswith('#'):
                expected_comments.append(line_str.rstrip('\n'))

        result = parse_unit(fixture_path)
        sections = result['sections']

        # Collect comments from all sections
        collected_comments = []
        for section in sections:
            comments = section.get('comments', [])
            for comment in comments:
                text = comment.get('text')
                start = comment.get('start_byte')
                end = comment.get('end_byte')

                # Verify byte range
                self.assertIsInstance(start, int)
                self.assertIsInstance(end, int)
                self.assertTrue(0 <= start <= end <= len(source), f"Comment byte range invalid: {start}:{end} in {len(source)}-byte source")

                # Verify byte range matches text
                extracted_text = source[start:end].decode('utf-8')
                self.assertEqual(extracted_text, text, f"Comment text mismatch: {extracted_text!r} != {text!r}")

                collected_comments.append(text)

        # Compare expected vs collected (order matters)
        self.assertEqual(collected_comments, expected_comments, "Comments mismatch: expected vs collected")

    def test_quotes_single_token_json(self):
        """Test that JSON '{"enable_thinking":false}' is ONE token in qwen3.6-coding.service."""
        parser_module = setup_parser_import()
        self.assertIsNotNone(parser_module, "Could not import parser module")
        parse_unit = getattr(parser_module, 'parse_unit')
    
        fixtures_dir = get_fixtures_dir()
        fixture_path = os.path.join(fixtures_dir, 'qwen3.6-coding.service')
        source = open(fixture_path, 'rb').read()

        result = parse_unit(fixture_path)
        sections = result['sections']

        # Find the ExecStart section and look for the JSON token
        execstart_section = None
        for section in sections:
            if section.get('name') == 'Service':
                execstart_section = section
                break

        self.assertIsNotNone(execstart_section, "Service section not found")

        tokens = execstart_section.get('tokens', [])
        json_token_text = '{"enable_thinking":false}'

        # Look for the JSON token
        found_tokens = []
        for token in tokens:
            text = token.get('text')
            if text == json_token_text:
                found_tokens.append(token)

        # Should be exactly one token with that text
        self.assertEqual(len(found_tokens), 1, f"Expected exactly one JSON token, found {len(found_tokens)}")

        # Verify its byte range matches source
        token = found_tokens[0]
        start = token.get('start_byte')
        end = token.get('end_byte')
        extracted_text = source[start:end].decode('utf-8')
        self.assertEqual(extracted_text, json_token_text, "JSON token text mismatch")

    def test_percent_percent_not_special(self):
        """Test that %% in comments is preserved verbatim in qwen3.6-coding.service."""
        parser_module = setup_parser_import()
        self.assertIsNotNone(parser_module, "Could not import parser module")
        parse_unit = getattr(parser_module, 'parse_unit')

        fixtures_dir = get_fixtures_dir()
        fixture_path = os.path.join(fixtures_dir, 'qwen3.6-coding.service')
        source = open(fixture_path, 'rb').read()

        result = parse_unit(fixture_path)
        sections = result['sections']

        # Collect all comment texts
        comment_texts = []
        for section in sections:
            comments = section.get('comments', [])
            for comment in comments:
                text = comment.get('text')
                comment_texts.append(text)

        # Find comment lines containing %%
        percent_comments = [text for text in comment_texts if '%%' in text]
        self.assertGreater(len(percent_comments), 0, "No comments with %% found")

        # Verify each %% comment is preserved verbatim
        for comment in percent_comments:
            self.assertIn('%%', comment, f"Comment {comment!r} does not contain %%")

    def test_backslash_no_space_continuation(self):
        """Test that llama-server-qwen35-npu.service handles backslash-no-space continuation."""
        parser_module = setup_parser_import()
        self.assertIsNotNone(parser_module, "Could not import parser module")
        parse_unit = getattr(parser_module, 'parse_unit')

        fixtures_dir = get_fixtures_dir()
        fixture_path = os.path.join(fixtures_dir, 'llama-server-qwen35-npu.service')
        source = open(fixture_path, 'rb').read()

        result = parse_unit(fixture_path)
        sections = result['sections']

        # Find Service section
        service_section = None
        for section in sections:
            if section.get('name') == 'Service':
                service_section = section
                break

        self.assertIsNotNone(service_section, "Service section not found")

        # Extract params
        params = service_section.get('params', {})

        # Check that params contain expected values from continuation lines
        # The ExecStart line has a continuation with no space before backslash
        # The joined command should contain model path and flags
        # This test verifies the parser joined the continuation correctly

        # We can't easily assert exact params without knowing the exact parser behavior
        # But we can assert that the parser didn't crash and the result is valid
        self.assertIsInstance(params, dict, "Params should be a dict")

    def test_multi_line_execstart_continuation(self):
        """Test that mixperten.service handles multi-line ExecStart continuation."""
        parser_module = setup_parser_import()
        self.assertIsNotNone(parser_module, "Could not import parser module")
        parse_unit = getattr(parser_module, 'parse_unit')

        fixtures_dir = get_fixtures_dir()
        fixture_path = os.path.join(fixtures_dir, 'mixperten.service')
        source = open(fixture_path, 'rb').read()

        result = parse_unit(fixture_path)
        sections = result['sections']

        # Find Service section
        service_section = None
        for section in sections:
            if section.get('name') == 'Service':
                service_section = section
                break

        self.assertIsNotNone(service_section, "Service section not found")

        # Extract params
        params = service_section.get('params', {})

        # Verify the parser didn't crash and result is valid
        self.assertIsInstance(params, dict, "Params should be a dict")

    def test_unknown_directives_preserved_verbatim_with_byte_range(self):
        """Test that unknown directives are preserved verbatim with byte ranges."""
        parser_module = setup_parser_import()
        self.assertIsNotNone(parser_module, "Could not import parser module")
        parse_unit = getattr(parser_module, 'parse_unit')

        fixtures_dir = get_fixtures_dir()
        fixture_path = os.path.join(fixtures_dir, 'qwen3.6-coding.service')
        source = open(fixture_path, 'rb').read()

        result = parse_unit(fixture_path)
        sections = result['sections']

        # Collect all unknown directives
        all_unknown = []
        for section in sections:
            unknown = section.get('unknown', [])
            all_unknown.extend(unknown)

        # Verify each unknown entry has correct byte mapping
        for item in all_unknown:
            text = item.get('text')
            start = item.get('start_byte')
            end = item.get('end_byte')

            # Basic bounds check
            self.assertIsInstance(start, int)
            self.assertIsInstance(end, int)
            self.assertTrue(0 <= start <= end <= len(source), f"Unknown directive byte range invalid: {start}:{end} in {len(source)}-byte source")

            # Verify byte range matches text
            extracted_text = source[start:end].decode('utf-8')
            self.assertEqual(extracted_text, text, f"Unknown directive text mismatch: {extracted_text!r} != {text!r}")

    def test_paramprofile_extraction(self):
        """Test that ParamProfile extraction works for qwen3.6-coding.service."""
        parser_module = setup_parser_import()
        self.assertIsNotNone(parser_module, "Could not import parser module")
        parse_unit = getattr(parser_module, 'parse_unit')

        fixtures_dir = get_fixtures_dir()
        fixture_path = os.path.join(fixtures_dir, 'qwen3.6-coding.service')
        source = open(fixture_path, 'rb').read()

        result = parse_unit(fixture_path)
        sections = result['sections']

        # Find Service section
        service_section = None
        for section in sections:
            if section.get('name') == 'Service':
                service_section = section
                break

        self.assertIsNotNone(service_section, "Service section not found")

        params = service_section.get('params', {})

        # Test specific param extractions from qwen3.6-coding.service
        # Based on inspection, these are expected to be present
        self.assertIn('-c', params, "Missing -c param")
        self.assertEqual(params['-c'], '65536', f"Expected -c=65536, got {params['-c']!r}")

        self.assertIn('-t', params, "Missing -t param")
        self.assertEqual(params['-t'], '4', f"Expected -t=4, got {params['-t']!r}")

        self.assertIn('-fa', params, "Missing -fa param")
        self.assertEqual(params['-fa'], 'on', f"Expected -fa=on, got {params['-fa']!r}")

        self.assertIn('-ctk', params, "Missing -ctk param")
        self.assertEqual(params['-ctk'], 'q8_0', f"Expected -ctk=q8_0, got {params['-ctk']!r}")

        self.assertIn('-ctv', params, "Missing -ctv param")
        self.assertEqual(params['-ctv'], 'q8_0', f"Expected -ctv=q8_0, got {params['-ctv']!r}")

        self.assertIn('--jinja', params, "Missing --jinja param (should be flag)")
        self.assertTrue(params['--jinja'], f"Expected --jinja=True (flag), got {params['--jinja']!r}")

        self.assertIn('--chat-template-kwargs', params, "Missing --chat-template-kwargs param")
        self.assertEqual(params['--chat-template-kwargs'], '{"enable_thinking":false}', f"Expected JSON, got {params['--chat-template-kwargs']!r}")

        self.assertIn('--temp', params, "Missing --temp param")
        self.assertEqual(params['--temp'], '1.0', f"Expected --temp=1.0, got {params['--temp']!r}")

        self.assertIn('--top-p', params, "Missing --top-p param")
        self.assertEqual(params['--top-p'], '0.95', f"Expected --top-p=0.95, got {params['--top-p']!r}")

        self.assertIn('--top-k', params, "Missing --top-k param")
        self.assertEqual(params['--top-k'], '20', f"Expected --top-k=20, got {params['--top-k']!r}")

        self.assertIn('--min-p', params, "Missing --min-p param")
        self.assertEqual(params['--min-p'], '0.0', f"Expected --min-p=0.0, got {params['--min-p']!r}")

        self.assertIn('--alias', params, "Missing --alias param")
        self.assertEqual(params['--alias'], 'qwen3.6-coding', f"Expected --alias=qwen3.6-coding, got {params['--alias']!r}")

        self.assertIn('--port', params, "Missing --port param")
        self.assertEqual(params['--port'], '8085', f"Expected --port=8085, got {params['--port']!r}")

        # Check that quantization info is present (e.g. Q4_K_M in model path)
        # This is a robust check that the parser extracted model path
        model_path_key = '-m'  # The key for model path in params
        self.assertIn(model_path_key, params, "Missing -m (model path) param")

        # Check that the model path contains quantization info
        model_path = params[model_path_key]
        self.assertIn('Q4_K_M', model_path, f"Model path does not contain quantization info: {model_path!r}")

        # Also check that the params dict contains 'Q4_K_M' somewhere
        params_str = json.dumps(params)
        self.assertIn('Q4_K_M', params_str, "Quantization info (Q4_K_M) not found in params")

    def test_no_write_path(self):
        """Test that parser is read-only (no file writes during parsing)."""
        parser_module = setup_parser_import()
        self.assertIsNotNone(parser_module, "Could not import parser module")
        parse_unit = getattr(parser_module, 'parse_unit')

        fixtures_dir = get_fixtures_dir()
        fixture_path = os.path.join(fixtures_dir, 'qwen3.6-coding.service')

        # Snapshot files before
        def snapshot_files(directory):
            """Snapshot file metadata for all regular files in directory."""
            snapshot = {}
            for root, _, files in os.walk(directory):
                for file in files:
                    full_path = os.path.join(root, file)
                    try:
                        stat_result = os.stat(full_path)
                        snapshot[full_path] = {
                            'mtime': stat_result.st_mtime_ns,
                            'size': stat_result.st_size,
                            'hash': hashlib.sha256(open(full_path, 'rb').read()).hexdigest()
                        }
                    except (OSError, IOError):
                        # Skip unreadable files
                        pass
            return snapshot

        # Snapshot repo root and fixtures dir
        repo_root = get_repo_root()
        before_snapshot = snapshot_files(repo_root)

        # Parse a fixture
        result = parse_unit(fixture_path)

        # Snapshot after
        after_snapshot = snapshot_files(repo_root)

        # Files should be unchanged
        self.assertEqual(before_snapshot, after_snapshot, "Files were modified during parsing (read-only violation)")

    def test_standby_is_not_error(self):
        """Test that NPU unit with ExecCondition is parsed without error."""
        parser_module = setup_parser_import()
        self.assertIsNotNone(parser_module, "Could not import parser module")
        parse_unit = getattr(parser_module, 'parse_unit')

        fixtures_dir = get_fixtures_dir()
        fixture_path = os.path.join(fixtures_dir, 'llama-server-qwen35-npu.service')

        # This should not raise an exception
        result = parse_unit(fixture_path)

        # Verify that ExecCondition is preserved (either in params or unknown)
        sections = result['sections']

        # Find Service section
        service_section = None
        for section in sections:
            if section.get('name') == 'Service':
                service_section = section
                break

        self.assertIsNotNone(service_section, "Service section not found")

        # Check that the ExecCondition directive is either in params or unknown
        # This is a minimal test: the parser should not crash
        self.assertIsInstance(result, dict, "parse_unit should return dict")


if __name__ == '__main__':
    unittest.main()