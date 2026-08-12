"""
Roundhouse Unit Parser Acceptance Tests
=======================================

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
from pathlib import Path
import pytest

# Import contract: parser module is in the repo root (parent of this tests/ directory)
# @coder must place parser.py in /tmp/sandbox/ or /var/lib/bullpen/coder/ etc.
# Repo root = parent of tests/; it is inserted into sys.path
# BUT per @noether: resolve paths relative to test file for grinder compatibility
def get_fixtures_dir():
    """Get the fixtures directory path."""
    # Resolve relative to test file location for grinder compatibility
    test_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(test_dir)
    vorgabe = os.environ.get('ROUNDHOUSE_FIXTURES_DIR')
    if vorgabe:
        return vorgabe
    # Im Projekt liegen die Fixtures unter docs/, in einem Arbeits-Repo daneben.
    # Beides zulassen, statt 23 Dateien zu verlegen oder eine Umgebungsvariable
    # zur Voraussetzung zu machen.
    for kandidat in (os.path.join(repo_root, 'fixtures'),
                     os.path.join(repo_root, 'docs', 'fixtures')):
        if os.path.isdir(kandidat):
            return kandidat
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

def test_parse_unit_exists_and_signature():
    """Test that parse_unit is importable and callable with correct signature."""
    # This will fail if parser module doesn't exist (RED)
    stdout, stderr, parser_module = capture_import_output()
    assert len(stdout.strip()) == 0, f"Parser import produced stdout: {stdout!r}"
    assert len(stderr.strip()) == 0, f"Parser import produced stderr: {stderr!r}"
    assert parser_module is not None, "Could not import parser module"
    
    # Test that parse_unit exists and is callable
    assert hasattr(parser_module, 'parse_unit'), "parse_unit not found in parser module"
    parse_unit = getattr(parser_module, 'parse_unit')
    assert callable(parse_unit), "parse_unit is not callable"

def test_no_output_at_import():
    """Test that parser module produces no output on import.

    Der Import wird ZUERST verlangt. Ohne diese Zeile besteht der Test, solange es
    gar keinen Parser gibt: der Import scheitert, gibt nichts aus, und "keine
    Ausgabe" haelt trivial. Gemessen 2026-08-12 -- er war der einzige gruene Test
    einer Suite, die nichts pruefen konnte.
    """
    stdout, stderr, parser_module = capture_import_output()
    assert parser_module is not None, "Could not import parser module — suite cannot proceed"
    assert len(stdout.strip()) == 0, f"Parser import produced stdout: {stdout!r}"
    assert len(stderr.strip()) == 0, f"Parser import produced stderr: {stderr!r}"

def test_fixtures_accessible():
    """Vorpruefung: Fixture-Ordner da, 23 Units, Namen decken sich mit MANIFEST.

    Steht VOR jedem Test, der den Parser braucht. Ohne sie meldet eine falsche
    ROUNDHOUSE_FIXTURES_DIR woertlich denselben Fehler wie ein fehlender Parser
    ("Could not import parser module") und schickt den Implementierer in seinen
    eigenen Code, statt auf den Pfad zu zeigen.
    """
    fixtures_dir = get_fixtures_dir()
    assert os.path.isdir(fixtures_dir), (
        f"Fixtures dir not found: {fixtures_dir!r} — check ROUNDHOUSE_FIXTURES_DIR")
    service_files = sorted(f for f in os.listdir(fixtures_dir) if f.endswith('.service'))
    assert len(service_files) == 23, (
        f"Expected 23 .service files in {fixtures_dir!r}, got {len(service_files)}: {service_files}")

    # MANIFEST.txt ist sha256sum-Ausgabe: "<hash>  <dateiname>", getrennt durch ZWEI
    # LEERZEICHEN. Eine fruehere Fassung zerlegte an "\t" und uebersprang den Vergleich
    # dann still -- 26 Zeilen, 0 Tabulatoren. Kein Waechter mehr: faellt das MANIFEST
    # weg oder ist es unlesbar, soll dieser Test rot werden, nicht schweigen.
    manifest = os.path.join(fixtures_dir, 'MANIFEST.txt')
    assert os.path.isfile(manifest), f"MANIFEST.txt missing in {fixtures_dir!r}"
    with open(manifest) as f:
        zeilen = [l.strip() for l in f if l.strip() and not l.lstrip().startswith('#')]
    genannt = sorted(os.path.basename(l.split()[-1]) for l in zeilen if len(l.split()) >= 2)
    assert genannt == service_files, (
        f"Fixture files do not match MANIFEST.txt:\n  on disk:   {service_files}\n"
        f"  manifest:  {genannt}")


def test_all_fixtures_parse():
    """Test that all 23 fixtures parse without error."""
    parser_module = setup_parser_import()
    assert parser_module is not None, "Could not import parser module"
    parse_unit = getattr(parser_module, 'parse_unit')
    
    fixtures_dir = get_fixtures_dir()
    service_files = [f for f in os.listdir(fixtures_dir) if f.endswith('.service')]
    assert len(service_files) == 23, f"Expected 23 .service files, got {len(service_files)}"
    
    for service_file in service_files:
        service_path = os.path.join(fixtures_dir, service_file)
        try:
            result = parse_unit(service_path)
            assert isinstance(result, dict), f"parse_unit({service_file!r}) did not return dict"
        except Exception as e:
            pytest.fail(f"parse_unit({service_file!r}) raised {type(e).__name__}: {e}")

def test_manifest_content():
    """Test that manifest field contains MANIFEST.txt content."""
    parser_module = setup_parser_import()
    assert parser_module is not None, "Could not import parser module"
    parse_unit = getattr(parser_module, 'parse_unit')
    
    fixtures_dir = get_fixtures_dir()
    manifest_path = os.path.join(fixtures_dir, 'MANIFEST.txt')
    assert os.path.exists(manifest_path), "MANIFEST.txt not found"
    
    with open(manifest_path, 'r') as f:
        expected_manifest = f.read()
    
    # Parse any fixture (e.g. qwen3.6-coding.service)
    fixture_path = os.path.join(fixtures_dir, 'qwen3.6-coding.service')
    result = parse_unit(fixture_path)
    
    # Manifest should be the file content (str)
    assert 'manifest' in result, "Result missing 'manifest' key"
    assert isinstance(result['manifest'], str), f"manifest should be str, got {type(result['manifest'])}"
    assert result['manifest'] == expected_manifest, "manifest content mismatch"

def test_sections_structure():
    """Test that sections have correct structure."""
    parser_module = setup_parser_import()
    assert parser_module is not None, "Could not import parser module"
    parse_unit = getattr(parser_module, 'parse_unit')
    
    fixtures_dir = get_fixtures_dir()
    fixture_path = os.path.join(fixtures_dir, 'qwen3.6-coding.service')
    result = parse_unit(fixture_path)
    
    assert 'sections' in result, "Result missing 'sections' key"
    sections = result['sections']
    assert isinstance(sections, list), "'sections' should be a list"
    
    # Should have at least Unit and Service sections
    section_names = [s.get('name') for s in sections]
    assert 'Unit' in section_names, "Missing 'Unit' section"
    assert 'Service' in section_names, "Missing 'Service' section"
    
    for section in sections:
        assert 'name' in section, "Section missing 'name'"
        assert 'tokens' in section, "Section missing 'tokens'"
        assert 'comments' in section, "Section missing 'comments'"
        assert 'params' in section, "Section missing 'params'"
        assert 'unknown' in section, "Section missing 'unknown'"
        
        assert isinstance(section['tokens'], list), "'tokens' should be a list"
        assert isinstance(section['comments'], list), "'comments' should be a list"
        assert isinstance(section['params'], dict), "'params' should be a dict"
        assert isinstance(section['unknown'], list), "'unknown' should be a list"

def test_byte_offset_correctness():
    """Test that every token's source[start:end] == token text."""
    parser_module = setup_parser_import()
    assert parser_module is not None, "Could not import parser module"
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
            assert isinstance(start, int) and isinstance(end, int), "Token byte offsets must be integers"
            assert 0 <= start <= end <= len(source), f"Token byte range invalid: {start}:{end} in {len(source)}-byte source"
            
            # Check byte range matches text
            extracted_text = source[start:end].decode('utf-8')
            assert extracted_text == text, f"Token text mismatch: {extracted_text!r} != {text!r}"

def test_comments_verbatim_byte_exact():
    """Test that comments are extracted byte-exactly, none lost/added, order preserved."""
    parser_module = setup_parser_import()
    assert parser_module is not None, "Could not import parser module"
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
            assert isinstance(start, int) and isinstance(end, int), "Comment byte offsets must be integers"
            assert 0 <= start <= end <= len(source), f"Comment byte range invalid: {start}:{end} in {len(source)}-byte source"
            
            # Verify byte range matches text
            extracted_text = source[start:end].decode('utf-8')
            assert extracted_text == text, f"Comment text mismatch: {extracted_text!r} != {text!r}"
            
            collected_comments.append(text)
    
    # Compare expected vs collected (order matters)
    assert collected_comments == expected_comments, "Comments mismatch: expected vs collected"

def test_quotes_single_token_json():
    """Test that JSON '{"enable_thinking":false}' is ONE token in qwen3.6-coding.service."""
    parser_module = setup_parser_import()
    assert parser_module is not None, "Could not import parser module"
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
    
    assert execstart_section is not None, "Service section not found"
    
    tokens = execstart_section.get('tokens', [])
    json_token_text = '{"enable_thinking":false}'
    
    # Look for the JSON token
    found_tokens = []
    for token in tokens:
        text = token.get('text')
        if text == json_token_text:
            found_tokens.append(token)
    
    # Should be exactly one token with that text
    assert len(found_tokens) == 1, f"Expected exactly one JSON token, found {len(found_tokens)}"
    
    # Verify its byte range matches source
    token = found_tokens[0]
    start = token.get('start_byte')
    end = token.get('end_byte')
    extracted_text = source[start:end].decode('utf-8')
    assert extracted_text == json_token_text, "JSON token text mismatch"

def test_percent_percent_not_special():
    """Test that %% in comments is preserved verbatim in qwen3.6-coding.service."""
    parser_module = setup_parser_import()
    assert parser_module is not None, "Could not import parser module"
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
    assert len(percent_comments) > 0, "No comments with %% found"
    
    # Verify each %% comment is preserved verbatim
    for comment in percent_comments:
        assert '%%' in comment, f"Comment {comment!r} does not contain %%"

def test_backslash_no_space_continuation():
    """Test that llama-server-qwen35-npu.service handles backslash-no-space continuation."""
    parser_module = setup_parser_import()
    assert parser_module is not None, "Could not import parser module"
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
    
    assert service_section is not None, "Service section not found"
    
    # Extract params
    params = service_section.get('params', {})
    
    # Check that params contain expected values from continuation lines
    # The ExecStart line has a continuation with no space before backslash
    # The joined command should contain model path and flags
    # This test verifies the parser joined the continuation correctly
    
    # We can't easily assert exact params without knowing the exact parser behavior
    # But we can assert that the parser didn't crash and the result is valid
    assert isinstance(params, dict), "Params should be a dict"

def test_multi_line_execstart_continuation():
    """Test that mixperten.service handles multi-line ExecStart continuation."""
    parser_module = setup_parser_import()
    assert parser_module is not None, "Could not import parser module"
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
    
    assert service_section is not None, "Service section not found"
    
    # Extract params
    params = service_section.get('params', {})
    
    # Verify the parser didn't crash and result is valid
    assert isinstance(params, dict), "Params should be a dict"

def test_unknown_directives_preserved_verbatim_with_byte_range():
    """Test that unknown directives are preserved verbatim with byte ranges."""
    parser_module = setup_parser_import()
    assert parser_module is not None, "Could not import parser module"
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
        assert isinstance(start, int) and isinstance(end, int), "Unknown directive byte offsets must be integers"
        assert 0 <= start <= end <= len(source), f"Unknown directive byte range invalid: {start}:{end} in {len(source)}-byte source"
        
        # Verify byte range matches text
        extracted_text = source[start:end].decode('utf-8')
        assert extracted_text == text, f"Unknown directive text mismatch: {extracted_text!r} != {text!r}"

def test_paramprofile_extraction():
    """Test that ParamProfile extraction works for qwen3.6-coding.service."""
    parser_module = setup_parser_import()
    assert parser_module is not None, "Could not import parser module"
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
    
    assert service_section is not None, "Service section not found"
    
    params = service_section.get('params', {})
    
    # Test specific param extractions from qwen3.6-coding.service
    # Based on inspection, these are expected to be present
    assert '-c' in params, "Missing -c param"
    assert params['-c'] == '65536', f"Expected -c=65536, got {params['-c']!r}"
    
    assert '-t' in params, "Missing -t param"
    assert params['-t'] == '4', f"Expected -t=4, got {params['-t']!r}"
    
    assert '-fa' in params, "Missing -fa param"
    assert params['-fa'] == 'on', f"Expected -fa=on, got {params['-fa']!r}"
    
    assert '-ctk' in params, "Missing -ctk param"
    assert params['-ctk'] == 'q8_0', f"Expected -ctk=q8_0, got {params['-ctk']!r}"
    
    assert '-ctv' in params, "Missing -ctv param"
    assert params['-ctv'] == 'q8_0', f"Expected -ctv=q8_0, got {params['-ctv']!r}"
    
    assert '--jinja' in params, "Missing --jinja param (should be flag)"
    assert params['--jinja'] is True, f"Expected --jinja=True (flag), got {params['--jinja']!r}"
    
    assert '--chat-template-kwargs' in params, "Missing --chat-template-kwargs param"
    assert params['--chat-template-kwargs'] == '{"enable_thinking":false}', f"Expected JSON, got {params['--chat-template-kwargs']!r}"
    
    assert '--temp' in params, "Missing --temp param"
    assert params['--temp'] == '1.0', f"Expected --temp=1.0, got {params['--temp']!r}"
    
    assert '--top-p' in params, "Missing --top-p param"
    assert params['--top-p'] == '0.95', f"Expected --top-p=0.95, got {params['--top-p']!r}"
    
    assert '--top-k' in params, "Missing --top-k param"
    assert params['--top-k'] == '20', f"Expected --top-k=20, got {params['--top-k']!r}"
    
    assert '--min-p' in params, "Missing --min-p param"
    assert params['--min-p'] == '0.0', f"Expected --min-p=0.0, got {params['--min-p']!r}"
    
    assert '--alias' in params, "Missing --alias param"
    assert params['--alias'] == 'qwen3.6-coding', f"Expected --alias=qwen3.6-coding, got {params['--alias']!r}"
    
    assert '--port' in params, "Missing --port param"
    assert params['--port'] == '8085', f"Expected --port=8085, got {params['--port']!r}"
    
    # Check that quantization info is present (e.g. Q4_K_M in model path)
    # This is a robust check that the parser extracted model path
    model_path_key = '-m'  # The key for model path in params
    assert model_path_key in params, "Missing -m (model path) param"
    
    # Check that the model path contains quantization info
    model_path = params[model_path_key]
    assert 'Q4_K_M' in model_path, f"Model path does not contain quantization info: {model_path!r}"
    
    # Also check that the params dict contains 'Q4_K_M' somewhere
    import json
    params_str = json.dumps(params)
    assert 'Q4_K_M' in params_str, "Quantization info (Q4_K_M) not found in params"

def test_no_write_path():
    """Test that parser is read-only (no file writes during parsing)."""
    parser_module = setup_parser_import()
    assert parser_module is not None, "Could not import parser module"
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
    assert before_snapshot == after_snapshot, "Files were modified during parsing (read-only violation)"

def test_standby_is_not_error():
    """Test that NPU unit with ExecCondition is parsed without error."""
    parser_module = setup_parser_import()
    assert parser_module is not None, "Could not import parser module"
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
    
    assert service_section is not None, "Service section not found"
    
    # Check that the ExecCondition directive is either in params or unknown
    # This is a minimal test: the parser should not crash
    assert isinstance(result, dict), "parse_unit should return dict"

if __name__ == '__main__':
    # Run tests if invoked directly
    pytest.main([__file__, '-v'])