import os
import re
from typing import Dict, List, Any

def parse_unit(path: str) -> dict:
    """
    Parse a systemd unit file and extract sections, tokens, comments, parameters, and unknown directives.
    
    Args:
        path: Path to the systemd unit file
        
    Returns:
        dict: Parsed unit data with sections and manifest
    """
    with open(path, 'rb') as f:
        source_bytes = f.read()
    
    source_text = source_bytes.decode('utf-8', errors='replace')
    
    # Read manifest file
    manifest_path = os.path.join(os.path.dirname(path), 'MANIFEST.txt')
    manifest_content = None
    if os.path.exists(manifest_path):
        with open(manifest_path, 'r') as f:
            manifest_content = f.read()
    
    sections = []
    lines = source_text.split('\n')
    current_section = None
    section_start = 0
    line_start = 0
    
    for i, line in enumerate(lines):
        line_bytes = (line + '\n').encode('utf-8')
        line_end = line_start + len(line_bytes)
        
        # Check for section header
        if line.strip().startswith('[') and line.strip().endswith(']'):
            # Save previous section if exists
            if current_section:
                section_end = line_start
                section_text = source_bytes[section_start:section_end].decode('utf-8')
                parsed_section = _parse_section(section_text, section_start)
                parsed_section['name'] = current_section
                _enrich(parsed_section, source_bytes, section_start, section_end)
                sections.append(parsed_section)
            
            # Start new section
            current_section = line.strip()[1:-1]
            section_start = line_start
        
        line_start = line_end
    
    # Handle last section
    if current_section:
        section_text = source_bytes[section_start:].decode('utf-8')
        parsed_section = _parse_section(section_text, section_start)
        parsed_section['name'] = current_section
        _enrich(parsed_section, source_bytes, section_start, len(source_bytes))
        sections.append(parsed_section)
    
    return {
        "sections": sections,
        "manifest": manifest_content
    }

def _parse_section(section_text: str, base_offset: int) -> dict:
    """
    Parse a section's content and extract tokens, comments, parameters, and unknown directives.
    """
    tokens = []
    comments = []
    unknown = []
    params = {}
    
    lines = section_text.split('\n')
    line_offset = 0
    
    for line in lines:
        line_bytes = (line + '\n').encode('utf-8') if line else b''
        line_end = line_offset + len(line_bytes)
        
        stripped = line.lstrip()
        if not stripped:
            line_offset += len(line_bytes)
            continue
            
        # Handle comments
        if stripped.startswith('#'):
            comment_start = line_offset + (len(line) - len(line.lstrip()))
            comment_end = line_offset + len(line.rstrip())
            comments.append({
                'text': line.rstrip(),
                'start_byte': base_offset + comment_start,
                'end_byte': base_offset + comment_end
            })
        # Handle directives
        elif '=' in stripped and not stripped.startswith(';'):
            # Parse key=value directive
            key_part = stripped.split('=', 1)[0].strip()
            value_part = stripped.split('=', 1)[1] if '=' in stripped else ''
            
            # Handle continuation lines (backslash at end)
            full_line = line
            full_value = value_part
            
            # Extract parameters from ExecStart and other directives
            if key_part == 'ExecStart':
                params.update(_extract_execstart_params(full_value))
            
            # Add to unknown for now - in a real implementation we'd parse properly
            unknown.append({
                'text': line.rstrip(),
                'start_byte': base_offset + line_offset,
                'end_byte': base_offset + line_offset + len(line.rstrip().encode('utf-8'))
            })
        else:
            # Unknown directive or empty line
            if line.strip():
                unknown.append({
                    'text': line.rstrip(),
                    'start_byte': base_offset + line_offset,
                    'end_byte': base_offset + line_offset + len(line.rstrip().encode('utf-8'))
                })
        
        line_offset += len(line_bytes)
    
    return {
        'tokens': tokens,
        'comments': comments,
        'params': params,
        'unknown': unknown
    }

def _unquote(value: str) -> str:
    """Strip matching outer quotes.

    The value comes from the assembled ExecStart, where
    --chat-template-kwargs '{"enable_thinking":false}' still has its single
    quotes. The test requires the CONTENT -- same rule as for tokens,
    just one level further.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _extract_execstart_params(execstart_value: str) -> dict:
    """
    Extract parameters from ExecStart value.
    """
    params = {}
    
    # Simple parameter extraction for common patterns
    # This is a simplified version - a full implementation would be more robust
    parts = execstart_value.split()
    
    i = 0
    while i < len(parts):
        part = parts[i]
        if part.startswith('-') or part.startswith('--'):
            if '=' in part:
                # Handle --param=value format
                key, value = part.split('=', 1)
                params[key] = _unquote(value)
            elif i + 1 < len(parts) and not parts[i + 1].startswith('-'):
                # Handle -param value format
                params[part] = _unquote(parts[i + 1])
                i += 1
            else:
                # Handle flag format (no value)
                params[part] = True
        i += 1
    
    return params


# ---------------------------------------------------------------------------
# Byte-level parser. ALL positions are offsets in `data` -- never slices.
# The first attempt mixed both and ran backwards.
# ---------------------------------------------------------------------------

_WS = (b" ", b"\t")


def _line_end(data: bytes, i: int, limit: int) -> int:
    e = data.find(b"\n", i)
    return limit if e == -1 or e > limit else e


def _logical_value(data: bytes, start: int, limit: int):
    """Value from `start` (absolute), across continuation lines.

    Returns (pieces, end_absolute). `pieces` are (offset, bytes) pairs, each
    a contiguous range in the FILE -- so token boundaries point to the file
    not to an assembled copy. A backslash immediately before a newline
    continues and belongs to no token.
    """
    pieces, i = [], start
    while i <= limit:
        e = _line_end(data, i, limit)
        line = data[i:e]
        if line.endswith(b"\\"):
            pieces.append((i, line[:-1]))
            i = e + 1
            continue
        pieces.append((i, line))
        return pieces, e
    return pieces, limit


def _scan_tokens(data: bytes, pieces):
    """Shell-like tokens. A run in ' or " is ONE token; the quotes belong
    neither to text nor to the range -- the boundaries point to the
    CONTENT, as test_quotes_single_token_json requires."""
    tokens = []
    for base, raw in pieces:
        i, n = 0, len(raw)
        while i < n:
            c = raw[i:i + 1]
            if c in _WS:
                i += 1
                continue
            if c in (b"'", b'"'):
                end = raw.find(c, i + 1)
                if end == -1:
                    tokens.append({"text": raw[i:n].decode("utf-8", "replace"),
                                   "start_byte": base + i, "end_byte": base + n})
                    break
                tokens.append({"text": raw[i + 1:end].decode("utf-8", "replace"),
                               "start_byte": base + i + 1, "end_byte": base + end})
                i = end + 1
                continue
            j = i
            while j < n and raw[j:j + 1] not in _WS:
                j += 1
            tokens.append({"text": raw[i:j].decode("utf-8", "replace"),
                           "start_byte": base + i, "end_byte": base + j})
            i = j
    return tokens


def _tokenize_section(data: bytes, sec_start: int, sec_end: int):
    """(tokens, values) of a section. `values` maps directive names to
    the LOGICAL value, with continuations already attached."""
    tokens, values = [], {}
    i = sec_start
    while i < sec_end:
        e = _line_end(data, i, sec_end)
        line = data[i:e]
        core = line.strip()
        if not core or core[:1] in (b"#", b";") or (core[:1] == b"[" and core[-1:] == b"]"):
            i = e + 1
            continue
        rel = line.find(b"=")
        if rel == -1:
            i = e + 1
            continue
        equals = i + rel                       # ABSOLUTE -- here was the error
        name = data[i:equals].strip().decode("utf-8", "replace")
        pieces, end = _logical_value(data, equals + 1, sec_end)
        tokens.extend(_scan_tokens(data, pieces))
        values[name] = b" ".join(s for _, s in pieces).decode("utf-8", "replace")
        i = max(end + 1, i + 1)               # never backwards
    return tokens, values


def _enrich(section: dict, data: bytes, start: int, end: int) -> None:
    """Attach tokens and extract parameters from the logical ExecStart.

    Previously, parameter extraction only received the first line. `-c 65536` comes
    after a continuation, `taskset -c 4-7` on the same line before -- thus gaining
    the CPU binding. This is the common reason five models were stuck.
    """
    tokens, values = _tokenize_section(data, start, end)
    section["tokens"] = tokens
    if "ExecStart" in values:
        section.setdefault("params", {}).update(_extract_execstart_params(values["ExecStart"]))
