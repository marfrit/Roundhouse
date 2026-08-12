#!/usr/bin/env python3
"""Roundhouse MVP1 — driver + roster for boltzmann.

Parser + watcher + server for the llama-server fleet on boltzmann.
Reads systemd units, journal, cgroup memory; renders a thermal roster
(8-rung ladder) via HTTP+SSE. Read-only: no actuation, zero write path.

Sections:
  A: Parser (pure; bytes in, structured data out)
  B: Watcher + MemStore (state machine fed by journal/systemctl)
  C: Server + SSE + static page
  D: Main / CLI entry points
"""

import os
import sys
import re
import json
import argparse
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime
import socket
import time
import threading
import queue
import http.server
import urllib.parse
import subprocess
import signal

# Hard rail: never implemented
PAID_OFFLOAD = None


# ===== SECTION A: PARSER (pure; no I/O beyond bytes in) =====


@dataclass
class Line:
    """A physical line in the unit file.

    kind: 'comment' | 'section' | 'directive' | 'continuation' | 'blank'
    start, end: absolute byte offsets (end is exclusive, includes trailing \n if present)
    lineno: 1-based physical line number
    """
    kind: str
    start: int
    end: int
    lineno: int


@dataclass
class Token:
    """A shell-like token from ExecStart (or other directive value).

    text: decoded, quotes stripped, %% -> %, escapes resolved
    raw: exact source bytes (quotes and all)
    start, end: absolute byte offsets of raw in the file
    has_specifier: True if a lone %x (x != %) was left literal in text
    """
    text: str
    raw: bytes
    start: int
    end: int
    has_specifier: bool = False


@dataclass
class Directive:
    """A key=value pair in a section (assembled across continuations).

    section: 'Unit', 'Service', 'Install', ...
    key: decoded, exact case as written
    key_span: (start, end) byte offsets of the key
    value_raw: everything after '=', verbatim, incl. '\' and '\n'
    value_span: (start, end) byte offsets of the value
    lineno: 1-based line number where the directive starts
    """
    section: str
    key: str
    key_span: tuple
    value_raw: bytes
    value_span: tuple
    lineno: int


@dataclass
class ExecStart:
    """Parsed ExecStart directive: tokens, wrapper prefix, engine details.

    directive: the Directive it came from
    tokens: full argv including wrapper prefix (taskset, nice, etc.)
    wrapper: dict like {"kind":"taskset","cpus":"4-7","tokens":[...]} or None
    engine_argv: tokens from the engine binary onward (after wrapper)
    engine: {"kind": 'llama-server'|'llamafile', "binary": str, "variant": str}
      variant: 'rk-llama.cpp' if '/rk-llama.cpp/' in binary else
               'llama.cpp' for llama-server; 'llamafile' otherwise
    """
    directive: Directive
    tokens: List[Token]
    wrapper: Optional[Dict] = None
    engine_argv: List[Token] = field(default_factory=list)
    engine: Dict = field(default_factory=dict)


@dataclass
class UnitFile:
    """Complete parsed systemd unit file.

    path: file path
    name: basename (e.g. 'qwen3.6-coding.service')
    raw: raw bytes of the file (the single source of truth)
    lines: all lines (each belongs to exactly one Line)
    directives: all directives in file order
    comments: list of dicts {lineno, start, end, text}
    warnings: list of warning strings

    Semantic extracts (each also in .directives):
      description: decoded Description field or None
      retired: True if Description matches r'^\\[RETIRED'
      retired_note: full '[RETIRED ...]' bracket content or None
      exec_start: ExecStart or None
      exec_condition: Directive or None
      gate: dict from parse_gate() or None
      install_wanted_by: decoded WantedBy field or None
      known: dict of {field: value} for standard keys
      other_directives: list of Directive not in the known set
    """
    path: str
    name: str
    raw: bytes
    lines: List[Line]
    directives: List[Directive]
    comments: List[Dict]
    warnings: List[str]
    description: Optional[str] = None
    retired: bool = False
    retired_note: Optional[str] = None
    exec_start: Optional[ExecStart] = None
    exec_condition: Optional[Directive] = None
    gate: Optional[Dict] = None
    install_wanted_by: Optional[str] = None
    known: Dict = field(default_factory=dict)
    other_directives: List[Directive] = field(default_factory=list)


def parse_unit(path: str, raw: bytes) -> UnitFile:
    """Parse a systemd unit file from raw bytes.

    Args:
        path: file path (for .name and .path)
        raw: complete file as bytes

    Returns:
        UnitFile with all lines, directives, comments, and semantic extracts

    Invariant: b"".join(raw[l.start:l.end] for l in lines) == raw
    """
    lines = _parse_lines(raw)
    directives = _parse_directives(raw, lines)
    comments = _extract_comments(raw, lines)

    warnings = []
    known = {}
    other_directives = []

    # Extract semantic fields
    description = None
    retired = False
    retired_note = None
    exec_start = None
    exec_condition = None
    gate = None
    install_wanted_by = None

    # Known directive keys
    KNOWN_KEYS = {
        "Description", "Documentation", "After", "Wants",
        "ExecStart", "ExecCondition", "Type", "Restart", "RestartSec",
        "TimeoutStartSec", "WorkingDirectory", "LimitNOFILE", "WantedBy"
    }

    for directive in directives:
        if directive.key == "Description":
            description = _decode_value(directive.value_raw)
            if description.startswith("[RETIRED"):
                retired = True
                # Extract the bracketed content
                m = re.match(r'(\[RETIRED[^\]]*\])', description)
                if m:
                    retired_note = m.group(1)
            known[directive.key] = description
        elif directive.key == "ExecStart":
            exec_start = tokenize_execstart(directive, directive.value_raw)
        elif directive.key == "ExecCondition":
            exec_condition = directive
        elif directive.key in KNOWN_KEYS:
            val = _decode_value(directive.value_raw)
            known[directive.key.lower()] = val
            if directive.key == "WantedBy":
                install_wanted_by = val
        else:
            other_directives.append(directive)

    # Parse gate from ExecCondition or ConditionKernelVersion
    gate = parse_gate_impl(directives)

    unit = UnitFile(
        path=path,
        name=os.path.basename(path),
        raw=raw,
        lines=lines,
        directives=directives,
        comments=comments,
        warnings=warnings,
        description=description,
        retired=retired,
        retired_note=retired_note,
        exec_start=exec_start,
        exec_condition=exec_condition,
        gate=gate,
        install_wanted_by=install_wanted_by,
        known=known,
        other_directives=other_directives
    )

    return unit


def _parse_lines(raw: bytes) -> List[Line]:
    """Parse raw bytes into Line objects.

    Each byte belongs to exactly one Line. Lines are classified as:
    comment, section, directive, continuation, or blank.
    """
    lines = []
    i = 0
    lineno = 1

    while i < len(raw):
        start = i

        # Find end of line (next \n or end of file)
        end = raw.find(b'\n', i)
        if end == -1:
            end = len(raw)
            include_newline = False
        else:
            include_newline = True
            end += 1

        line_content = raw[start:end]
        if include_newline:
            line_bytes = line_content[:-1]  # without \n
        else:
            line_bytes = line_content

        # Classify the line
        stripped = line_bytes.lstrip()

        if not stripped:
            kind = 'blank'
        elif stripped[:1] in (b'#', b';'):
            kind = 'comment'
        elif stripped[:1] == b'[' and stripped[-1:] == b']':
            kind = 'section'
        elif b'=' in line_bytes:
            kind = 'directive'
        else:
            # Likely a continuation
            kind = 'continuation'

        lines.append(Line(
            kind=kind,
            start=start,
            end=end,
            lineno=lineno
        ))

        i = end
        lineno += 1

    return lines


def _parse_directives(raw: bytes, lines: List[Line]) -> List[Directive]:
    """Parse directive lines (key=value with continuation support)."""
    directives = []
    i = 0

    while i < len(lines):
        line = lines[i]

        if line.kind not in ('directive', 'section', 'comment', 'blank'):
            i += 1
            continue

        if line.kind != 'directive':
            i += 1
            continue

        # Extract key from this line
        line_bytes = raw[line.start:line.end]
        if line_bytes.endswith(b'\n'):
            line_bytes = line_bytes[:-1]

        eq_pos = line_bytes.find(b'=')
        if eq_pos == -1:
            i += 1
            continue

        key_bytes = line_bytes[:eq_pos].strip()
        key = key_bytes.decode('utf-8', errors='replace')

        key_span = (line.start + len(line_bytes) - len(line_bytes.lstrip()),
                    line.start + eq_pos)
        value_start = line.start + eq_pos + 1

        # Collect value across continuation lines
        value_end = line.end
        current_line = i

        while current_line < len(lines):
            current = lines[current_line]
            current_bytes = raw[current.start:current.end]
            if current_bytes.endswith(b'\n'):
                current_bytes = current_bytes[:-1]

            # Check if this line ends with backslash (continuation)
            if current_bytes.rstrip() and current_bytes.rstrip()[-1:] == b'\\':
                value_end = current.end
                current_line += 1
                # Skip comment lines in continuation
                while current_line < len(lines) and lines[current_line].kind == 'comment':
                    current_line += 1
            else:
                value_end = current.end
                break

        value_raw = raw[value_start:value_end]
        if value_raw.endswith(b'\n'):
            value_raw = value_raw[:-1]

        value_span = (value_start, value_end)

        directives.append(Directive(
            section='',  # Will be filled in later
            key=key,
            key_span=key_span,
            value_raw=value_raw,
            value_span=value_span,
            lineno=line.lineno
        ))

        i = current_line + 1

    return directives


def _extract_comments(raw: bytes, lines: List[Line]) -> List[Dict]:
    """Extract all comments as dicts with lineno, start, end, text."""
    comments = []

    for line in lines:
        if line.kind != 'comment':
            continue

        line_bytes = raw[line.start:line.end]
        if line_bytes.endswith(b'\n'):
            line_bytes = line_bytes[:-1]

        text = line_bytes.decode('utf-8', errors='replace')
        comments.append({
            'lineno': line.lineno,
            'start': line.start,
            'end': line.end - (1 if raw[line.end - 1:line.end] == b'\n' else 0),
            'text': text
        })

    return comments


def _decode_value(value_raw: bytes) -> str:
    """Decode a directive value (raw bytes)."""
    # Handle line continuations: replace \<newline> with space
    assembled = value_raw.replace(b'\\\n', b' ').decode('utf-8', errors='replace')
    return assembled.strip()


def tokenize_execstart(directive: Directive, raw: bytes) -> ExecStart:
    """Parse ExecStart into tokens, detect wrapper and engine.

    Args:
        directive: the ExecStart Directive
        raw: the value_raw bytes

    Returns:
        ExecStart with tokens, wrapper, engine_argv, engine details
    """
    # Collect pieces (handling continuations)
    pieces = _collect_pieces(raw, directive.value_span[0])

    # Tokenize using pieces (preserves byte offsets)
    tokens = _tokenize_pieces(pieces)

    # Detect wrapper and engine
    wrapper = None
    engine_argv = tokens
    engine = {}

    if tokens:
        # Check for wrapper (taskset, nice, env, ionice)
        wrapper_kinds = {'taskset', 'nice', 'env', 'ionice'}
        first_token_text = tokens[0].text
        first_basename = os.path.basename(first_token_text)

        if first_basename in wrapper_kinds:
            # Parse wrapper options
            wrapper_tokens = [tokens[0]]
            i = 1

            if first_basename == 'taskset':
                # taskset -c <cpus>
                if i < len(tokens) and tokens[i].text == '-c':
                    wrapper_tokens.append(tokens[i])
                    i += 1
                    if i < len(tokens):
                        wrapper_tokens.append(tokens[i])
                        cpus = tokens[i].text
                        i += 1
                        wrapper = {
                            'kind': 'taskset',
                            'cpus': cpus,
                            'tokens': wrapper_tokens
                        }
            elif first_basename == 'nice':
                # nice [-n increment] <command>
                if i < len(tokens) and tokens[i].text == '-n':
                    wrapper_tokens.append(tokens[i])
                    i += 1
                    if i < len(tokens):
                        wrapper_tokens.append(tokens[i])
                        i += 1
                wrapper = {'kind': 'nice', 'tokens': wrapper_tokens}

            engine_argv = tokens[i:]

        # Detect engine
        if engine_argv:
            engine_binary = engine_argv[0].text
            engine_basename = os.path.basename(engine_binary)

            if engine_basename.startswith('llama-server'):
                if '/rk-llama.cpp/' in engine_binary:
                    variant = 'rk-llama.cpp'
                else:
                    variant = 'llama.cpp'
                engine = {
                    'kind': 'llama-server',
                    'binary': engine_binary,
                    'variant': variant
                }
            elif 'llamafile' in engine_basename:
                engine = {
                    'kind': 'llamafile',
                    'binary': engine_binary,
                    'variant': 'llamafile'
                }

    return ExecStart(
        directive=directive,
        tokens=tokens,
        wrapper=wrapper,
        engine_argv=engine_argv,
        engine=engine
    )


def _collect_pieces(raw: bytes, base_offset: int) -> List[tuple]:
    """Collect pieces of the value, handling continuations.

    Returns list of (offset, bytes) pairs where each is a contiguous range.
    Backslash+newline continuations are represented as separate pieces.
    """
    pieces = []
    i = 0

    while i < len(raw):
        end = raw.find(b'\n', i)
        if end == -1:
            end = len(raw)
            line = raw[i:]
        else:
            line = raw[i:end]

        # Check if line ends with backslash (continuation)
        if line.rstrip(b'\r').endswith(b'\\'):
            # Remove the backslash for assembly
            pieces.append((base_offset + i, line[:-1]))
            i = end + 1
        else:
            pieces.append((base_offset + i, line))
            break

    return pieces


def _tokenize_pieces(pieces: List[tuple]) -> List[Token]:
    """Tokenize value from pieces, preserving byte offsets.

    Each piece is (base_offset, raw_bytes).
    """
    tokens = []

    # First, assemble the value for parsing
    assembled_parts = []
    for offset, raw_bytes in pieces:
        assembled_parts.append(raw_bytes.decode('utf-8', errors='replace'))
    assembled = ' '.join(assembled_parts)

    # Now tokenize with offset tracking
    i = 0
    piece_idx = 0
    pos_in_piece = 0
    base_offset, current_bytes = pieces[piece_idx]

    while i < len(assembled):
        # Skip whitespace
        if assembled[i] in ' \t\n':
            i += 1
            continue

        # Single quote: no escapes
        if assembled[i] == "'":
            j = i + 1
            while j < len(assembled) and assembled[j] != "'":
                j += 1

            if j < len(assembled):
                # Found closing quote
                text = assembled[i+1:j]
                # Reconstruct raw from assembled (simplified - use the assembled bytes)
                tokens.append(Token(
                    text=text,
                    raw=assembled[i:j+1].encode('utf-8'),
                    start=base_offset + pos_in_piece + i,
                    end=base_offset + pos_in_piece + j + 1,
                    has_specifier=False
                ))
                i = j + 1
            else:
                # Unterminated quote
                text = assembled[i+1:]
                tokens.append(Token(
                    text=text,
                    raw=assembled[i:].encode('utf-8'),
                    start=base_offset + pos_in_piece + i,
                    end=base_offset + pos_in_piece + len(assembled),
                    has_specifier=False
                ))
                break

        # Double quote: handle escapes
        elif assembled[i] == '"':
            j = i + 1
            text_parts = []
            has_spec = False

            while j < len(assembled):
                if assembled[j] == '"':
                    break
                elif assembled[j] == '\\' and j + 1 < len(assembled):
                    next_char = assembled[j + 1]
                    if next_char == 'n':
                        text_parts.append('\n')
                    elif next_char == 't':
                        text_parts.append('\t')
                    elif next_char == '\\':
                        text_parts.append('\\')
                    elif next_char == '"':
                        text_parts.append('"')
                    else:
                        text_parts.append(next_char)
                    j += 2
                elif assembled[j] == '%' and j + 1 < len(assembled):
                    next_char = assembled[j + 1]
                    if next_char == '%':
                        text_parts.append('%')
                        j += 2
                    else:
                        text_parts.append(assembled[j])
                        has_spec = True
                        j += 1
                else:
                    text_parts.append(assembled[j])
                    j += 1

            text = ''.join(text_parts)
            if j < len(assembled):
                # Found closing quote
                tokens.append(Token(
                    text=text,
                    raw=assembled[i:j+1].encode('utf-8'),
                    start=base_offset + pos_in_piece + i,
                    end=base_offset + pos_in_piece + j + 1,
                    has_specifier=has_spec
                ))
                i = j + 1
            else:
                # Unterminated quote
                tokens.append(Token(
                    text=text,
                    raw=assembled[i:].encode('utf-8'),
                    start=base_offset + pos_in_piece + i,
                    end=base_offset + pos_in_piece + len(assembled),
                    has_specifier=has_spec
                ))
                break

        # Unquoted token
        else:
            j = i
            text_parts = []
            has_spec = False

            while j < len(assembled) and assembled[j] not in ' \t\n':
                if assembled[j] == '%':
                    next_char = assembled[j + 1] if j + 1 < len(assembled) else ''
                    if next_char == '%':
                        text_parts.append('%')
                        j += 2
                    else:
                        text_parts.append(assembled[j])
                        has_spec = True
                        j += 1
                else:
                    text_parts.append(assembled[j])
                    j += 1

            text = ''.join(text_parts)
            tokens.append(Token(
                text=text,
                raw=assembled[i:j].encode('utf-8'),
                start=base_offset + pos_in_piece + i,
                end=base_offset + pos_in_piece + j,
                has_specifier=has_spec
            ))
            i = j

    return tokens


def extract_param_profile(engine_argv: List[Token]) -> Dict:
    """Extract parameter profile from engine argv.

    Returns a dict with fields for all known flags, plus spans and unknown_flags.
    """
    result = {
        'model_path': None,
        'ctx': None,
        'threads': None,
        'threads_batch': None,
        'flash_attn': None,
        'cache_type_k': None,
        'cache_type_v': None,
        'jinja': False,
        'chat_template_kwargs': None,
        'chat_template_kwargs_json': None,
        'sampling': {},
        'n_predict': None,
        'reasoning_budget': None,
        'reasoning': None,
        'cpu_range': None,
        'cpu_range_batch': None,
        'cpu_strict': None,
        'cpu_strict_batch': None,
        'alias': None,
        'host_bind': None,
        'port': None,
        'port_source': 'default',
        'pinning': None,
        'unknown_flags': [],
        'raw_argv': [],
        'spans': {}
    }

    # Known flags with their arities and target fields
    flag_map = {
        '-m': ('model_path', 1, 'str'),
        '--model': ('model_path', 1, 'str'),
        '-c': ('ctx', 1, 'int'),
        '--ctx-size': ('ctx', 1, 'int'),
        '-t': ('threads', 1, 'int'),
        '--threads': ('threads', 1, 'int'),
        '-tb': ('threads_batch', 1, 'int'),
        '--threads-batch': ('threads_batch', 1, 'int'),
        '-fa': ('flash_attn', 1, 'str'),
        '--flash-attn': ('flash_attn', 1, 'str'),
        '-ctk': ('cache_type_k', 1, 'str'),
        '--cache-type-k': ('cache_type_k', 1, 'str'),
        '-ctv': ('cache_type_v', 1, 'str'),
        '--cache-type-v': ('cache_type_v', 1, 'str'),
        '--jinja': (None, 0, 'bool'),
        '--chat-template-kwargs': ('chat_template_kwargs', 1, 'str'),
        '--temp': ('sampling.temp', 1, 'float'),
        '--top-p': ('sampling.top_p', 1, 'float'),
        '--top-k': ('sampling.top_k', 1, 'int'),
        '--min-p': ('sampling.min_p', 1, 'float'),
        '--presence-penalty': ('sampling.presence_penalty', 1, 'float'),
        '--repeat-penalty': ('sampling.repeat_penalty', 1, 'float'),
        '-n': ('n_predict', 1, 'int'),
        '--predict': ('n_predict', 1, 'int'),
        '--reasoning-budget': ('reasoning_budget', 1, 'int'),
        '--reasoning': ('reasoning', 1, 'str'),
        '-Cr': ('cpu_range', 1, 'str'),
        '-Crb': ('cpu_range_batch', 1, 'str'),
        '--cpu-strict': ('cpu_strict', 1, 'int'),
        '--cpu-strict-batch': ('cpu_strict_batch', 1, 'int'),
        '--alias': ('alias', 1, 'str'),
        '--host': ('host_bind', 1, 'str'),
        '--port': ('port', 1, 'int'),
    }

    # Build raw_argv
    result['raw_argv'] = [tok.text for tok in engine_argv]

    i = 1  # Skip the binary
    while i < len(engine_argv):
        token = engine_argv[i]
        text = token.text

        if text.startswith('-'):
            # It's a flag
            if text in flag_map:
                field_name, arity, type_hint = flag_map[text]

                if type_hint == 'bool':
                    # No value flag
                    if field_name is None and text == '--jinja':
                        result['jinja'] = True
                    result['spans'][text] = {'flag': (token.start, token.end)}
                    i += 1
                elif arity == 0:
                    i += 1
                elif arity == 1:
                    # Expects next token as value
                    if i + 1 < len(engine_argv):
                        value_token = engine_argv[i + 1]
                        value_text = value_token.text

                        # Parse value by type
                        try:
                            if type_hint == 'int':
                                parsed_value = int(value_text)
                            elif type_hint == 'float':
                                parsed_value = float(value_text)
                            else:
                                parsed_value = value_text
                        except (ValueError, TypeError):
                            parsed_value = value_text

                        # Store in result
                        if '.' in field_name:
                            # Nested field like 'sampling.temp'
                            parts = field_name.split('.')
                            if parts[0] not in result:
                                result[parts[0]] = {}
                            result[parts[0]][parts[1]] = parsed_value
                        else:
                            result[field_name] = parsed_value

                        # Store spans
                        if field_name not in result['spans']:
                            result['spans'][field_name] = {}
                        result['spans'][field_name]['flag'] = (token.start, token.end)
                        result['spans'][field_name]['value'] = (value_token.start, value_token.end)

                        # Handle special JSON parsing for chat_template_kwargs
                        if field_name == 'chat_template_kwargs':
                            try:
                                json_val = json.loads(value_text)
                                result['chat_template_kwargs_json'] = json_val
                            except (json.JSONDecodeError, ValueError):
                                result['chat_template_kwargs_json'] = None

                        # Handle port_source
                        if field_name == 'port':
                            result['port_source'] = 'flag'

                        i += 2
                    else:
                        # No value available
                        result['unknown_flags'].append({
                            'flag': text,
                            'value': None,
                            'flag_span': (token.start, token.end),
                            'value_span': None
                        })
                        i += 1
            else:
                # Unknown flag
                value = None
                value_span = None
                if i + 1 < len(engine_argv) and not engine_argv[i + 1].text.startswith('-'):
                    value = engine_argv[i + 1].text
                    value_span = (engine_argv[i + 1].start, engine_argv[i + 1].end)
                    i += 2
                else:
                    i += 1

                result['unknown_flags'].append({
                    'flag': text,
                    'value': value,
                    'flag_span': (token.start, token.end),
                    'value_span': value_span
                })
        else:
            i += 1

    # Set default port if not specified
    if result['port'] is None:
        result['port'] = 8080
        result['port_source'] = 'default'

    return result


def parse_gate(unit: UnitFile) -> Optional[Dict]:
    """Parse kernel gate from ExecCondition or ConditionKernelVersion."""
    return parse_gate_impl(unit.directives)


def parse_gate_impl(directives: List[Directive]) -> Optional[Dict]:
    """Implementation of gate parsing."""
    for directive in directives:
        if directive.key == 'ExecCondition':
            value = _decode_value(directive.value_raw)
            # Look for uname -r pattern
            m = re.search(r'uname -r[^=]*=\s*"?([0-9A-Za-z._-]+)"?', value)
            if m:
                return {
                    'kind': 'kernel',
                    'wants': m.group(1),
                    'raw': value
                }
            return {
                'kind': 'opaque',
                'wants': None,
                'raw': value
            }
        elif directive.key == 'ConditionKernelVersion':
            value = _decode_value(directive.value_raw)
            m = re.search(r'([0-9A-Za-z._-]+)', value)
            if m:
                return {
                    'kind': 'kernel',
                    'wants': m.group(1),
                    'raw': value
                }

    return None


def select_units(unit_dir: str) -> List[str]:
    """Find all managed units in unit_dir.

    A unit is managed if:
    1. It's a .service file (not .bak, .service.bak-*, etc.)
    2. It contains llama-server or llamafile in its ExecStart
    3. No override comment like '# roundhouse: ignore'

    Returns sorted list of full file paths.
    """
    managed = []

    if not os.path.isdir(unit_dir):
        return []

    for fname in os.listdir(unit_dir):
        if not fname.endswith('.service'):
            continue

        fpath = os.path.join(unit_dir, fname)
        if not os.path.isfile(fpath):
            continue

        # Read file and check for roundhouse directives
        try:
            with open(fpath, 'rb') as f:
                raw = f.read()
        except Exception:
            continue

        # Check for ignore override
        raw_str = raw.decode('utf-8', errors='replace')
        if '# roundhouse: ignore' in raw_str or '; roundhouse: ignore' in raw_str:
            continue

        # Check for manage override
        has_manage = '# roundhouse: manage' in raw_str or '; roundhouse: manage' in raw_str

        # Look for llama-server or llamafile in ExecStart
        is_ours = False
        for line in raw_str.split('\n'):
            if line.strip().startswith('ExecStart='):
                if 'llama-server' in line or 'llamafile' in line:
                    is_ours = True
                    break

        if has_manage or is_ours:
            managed.append(fpath)

    return sorted(managed)


def build_deployment(unit: UnitFile, host: str, statf=os.stat) -> Dict:
    """Build a deployment record for a unit.

    Returns a dict shaped per §4.4d of the spec.
    """
    # Assertion: no paid offloading
    assert_no_paid_offload({'unit': unit.name, 'exec_start': unit.exec_start, 'known': unit.known})

    # Extract artifact info
    model_path = None
    model_file = None
    if unit.exec_start and unit.exec_start.engine_argv:
        profile = extract_param_profile(unit.exec_start.engine_argv)
        model_path = profile.get('model_path')

    if model_path:
        model_file = os.path.basename(model_path)

    # Get file stats
    artifact = {
        'model': None,
        'path': model_path,
        'filename': model_file,
        'format': 'gguf' if model_file and model_file.endswith('.gguf') else None,
        'quant_hint': quant_hint(model_file) if model_file else None,
        'sha256': None
    }

    host_artifact = {
        'host': host,
        'path': model_path,
        'exists': False,
        'size_bytes': None,
        'mtime': None
    }

    if model_path and os.path.isfile(model_path):
        try:
            st = statf(model_path)
            host_artifact['exists'] = True
            host_artifact['size_bytes'] = st.st_size
            host_artifact['mtime'] = int(st.st_mtime)
            artifact['file_id'] = f"sz{st.st_size}:mt{int(st.st_mtime)}"
        except Exception:
            pass

    # Engine info
    engine = unit.exec_start.engine if unit.exec_start else {}

    # Param profile
    param_profile = {}
    if unit.exec_start:
        param_profile = extract_param_profile(unit.exec_start.engine_argv)

    # Load strategy
    enabled = unit.known.get('unitfilestate') == 'enabled'
    load_strategy = {
        'kind': 'on-boot' if enabled else 'manual',
        'enabled': enabled,
        'gate': unit.gate
    }

    # Build deployment dict
    deployment = {
        'deployment_id': f"{host}/{unit.name}",
        'unit': unit.name,
        'artifact': artifact,
        'host_artifact': host_artifact,
        'engine': engine,
        'param_profile': param_profile,
        'load_strategy': load_strategy,
        'roster': {
            'rung': 'OFF',
            'state': None,
            'since': None
        },
        'memory': {
            'bytes': None,
            'source': 'unknown',
            'label': 'model file not found'
        },
        'retired': unit.retired
    }

    return deployment


def quant_hint(filename: str) -> Optional[str]:
    """Extract quantization hint from filename.

    Looks for patterns like Q4_K_M, IQ3_XXS, etc.
    """
    if not filename:
        return None

    m = re.search(r'(?i)(IQ\d_[A-Z]+|Q\d_K_[MSL]|Q\d_K|Q\d_0|UD-Q\d_K_XL|BF16|F16)', filename)
    if m:
        return m.group(1)
    return None


def assert_no_paid_offload(dep: Dict) -> None:
    """Assert that no paid offloading is configured.

    Raises AssertionError if model_path is not a local absolute path, or if
    any binary/argv contains paid API endpoints.
    """
    paid_domains = [
        "api.openai.com",
        "openrouter.ai",
        "api.anthropic.com",
        "googleapis.com",
        "://"
    ]

    # Check exec_start
    if 'exec_start' in dep and dep['exec_start']:
        for token in dep['exec_start'].tokens:
            for domain in paid_domains:
                assert domain not in token.text, f"Paid offloading detected: {domain}"

    # Check known fields
    if 'known' in dep:
        known_str = str(dep['known'])
        for domain in paid_domains:
            assert domain not in known_str, f"Paid offloading detected: {domain}"


# ===== SECTION B: WATCHER + MEMSTORE (no threads inside; run_ro is the only subprocess gate) =====
# implemented by T2


# ===== SECTION C: SERVER + SSE + STATIC =====

class EventBus:
    """Publish-subscribe event bus for SSE clients."""

    def __init__(self):
        self.subscribers = []
        self.event_counter = 0

    def subscribe(self) -> queue.Queue:
        """Subscribe to events; returns a queue.Queue(maxsize=256)."""
        q = queue.Queue(maxsize=256)
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        """Unsubscribe a queue."""
        if q in self.subscribers:
            self.subscribers.remove(q)

    def publish(self, event: str, data: dict):
        """Publish an event to all subscribers; drop client if queue is full."""
        self.event_counter += 1
        msg = (event, data, self.event_counter)

        dead = []
        for q in self.subscribers:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)

        for q in dead:
            self.unsubscribe(q)


class RoundhouseRequestHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler for Roundhouse server."""

    def do_GET(self):
        """Handle GET requests; return 405 for anything else."""
        path = self.path

        # Parse URL path and query
        parsed = urllib.parse.urlparse(path)
        route = parsed.path

        if route == '/':
            self.serve_static()
        elif route == '/api/units':
            self.serve_units()
        elif route.startswith('/api/units/'):
            unit_name = urllib.parse.unquote(route[len('/api/units/'):])
            self.serve_unit_detail(unit_name)
        elif route == '/api/ports':
            self.serve_ports()
        elif route == '/api/deployments':
            self.serve_deployments()
        elif route == '/api/mem':
            self.serve_mem()
        elif route == '/api/events':
            self.serve_events()
        else:
            self.error_404()

    def do_POST(self):
        self.error_405()

    def do_PUT(self):
        self.error_405()

    def do_DELETE(self):
        self.error_405()

    def do_HEAD(self):
        self.error_405()

    def error_405(self):
        """Return 405 Method Not Allowed."""
        self.send_response(405)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'405 Method Not Allowed')

    def error_404(self):
        """Return 404 Not Found."""
        self.send_response(404)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'404 Not Found')

    def serve_static(self):
        """Serve static/index.html."""
        html_path = Path(__file__).parent / 'static' / 'index.html'
        try:
            with open(html_path, 'r') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(content.encode('utf-8'))
        except FileNotFoundError:
            self.error_404()

    def serve_units(self):
        """Serve /api/units (snapshot)."""
        snapshot = self.server.watcher.snapshot()
        self.send_json(snapshot)

    def serve_unit_detail(self, unit_name):
        """Serve /api/units/<name> detail."""
        snapshot = self.server.watcher.snapshot()

        # Find unit in snapshot
        for unit in snapshot.get('units', []):
            if unit['unit'] == unit_name:
                # Fetch full detail (in real impl, this would include extra fields)
                self.send_json(unit)
                return

        self.error_404()

    def serve_ports(self):
        """Serve /api/ports (port board)."""
        snapshot = self.server.watcher.snapshot()
        units = snapshot.get('units', [])

        port_claims = {}
        for unit in units:
            port = unit.get('port')
            if port:
                if port not in port_claims:
                    port_claims[port] = []
                port_claims[port].append({
                    'unit': unit['unit'],
                    'enabled': unit.get('enabled', False),
                    'rung': unit.get('rung', 'OFF'),
                    'retired': unit.get('retired', False),
                    'gate': unit.get('gate')
                })

        # Add self port
        port_claims[self.server.port] = []

        # Build port board response
        ports = []
        for port in sorted(port_claims.keys()):
            claims = port_claims[port]
            if port == self.server.port:
                # Self
                ports.append({
                    'port': port,
                    'claims': [],
                    'class': None,
                    'note': 'roundhouse (self)'
                })
            elif len(claims) == 0:
                # No claims (shouldn't happen)
                pass
            elif len(claims) == 1:
                # Single claim
                claim = claims[0]
                ports.append({
                    'port': port,
                    'claims': [claim],
                    'class': None,
                    'note': None
                })
            else:
                # Multiple claims - determine class
                active_rungs = {'STARTING', 'LOADING', 'READY', 'BUSY'}
                active_count = sum(1 for c in claims if c['rung'] in active_rungs)

                if active_count >= 2:
                    port_class = 'active'
                else:
                    # Check if armed: >=2 claims that are enabled OR whose only blocker is unsatisfied gate
                    enabled_or_gated = sum(1 for c in claims if c['enabled'] or (c['gate'] and not c['enabled']))
                    if enabled_or_gated >= 2:
                        port_class = 'armed'
                        note = 'harmless only while BOTH the disable and the kernel gate hold'
                    else:
                        port_class = 'latent'
                        note = None

                ports.append({
                    'port': port,
                    'claims': claims,
                    'class': port_class if active_count < 2 else 'active',
                    'note': note if active_count < 2 else None
                })

        response = {
            'ports': ports,
            'self': {'port': self.server.port, 'claims_by_units': []}
        }
        self.send_json(response)

    def serve_deployments(self):
        """Serve /api/deployments."""
        snapshot = self.server.watcher.snapshot()
        units = snapshot.get('units', [])

        deployments = []
        for unit in units:
            if not unit.get('retired', False):
                dep = {
                    'deployment_id': f"{snapshot.get('host', '?')}/{unit['unit']}",
                    'unit': unit['unit'],
                    'artifact': {
                        'model': None,
                        'path': unit.get('model_path'),
                        'filename': unit.get('model_file'),
                        'format': 'gguf',
                        'quant_hint': unit.get('quant_hint'),
                        'sha256': None,
                        'file_id': None
                    },
                    'host_artifact': {
                        'host': snapshot.get('host', '?'),
                        'path': unit.get('model_path'),
                        'exists': False,
                        'size_bytes': None,
                        'mtime': None
                    },
                    'engine': unit.get('engine', {}),
                    'param_profile': unit.get('param_profile', {}),
                    'load_strategy': {
                        'kind': 'on-boot' if unit.get('enabled') else 'manual',
                        'enabled': unit.get('enabled', False),
                        'gate': unit.get('gate')
                    },
                    'roster': {
                        'rung': unit.get('rung', 'OFF'),
                        'state': unit.get('roster', None),
                        'since': unit.get('since')
                    },
                    'memory': unit.get('mem', {'bytes': None, 'source': 'unknown'}),
                    'retired': False
                }
                deployments.append(dep)

        response = {
            'host': snapshot.get('host', '?'),
            'deployments': deployments
        }
        self.send_json(response)

    def serve_mem(self):
        """Serve /api/mem (memory history)."""
        # Simplified: return empty list for now
        self.send_json({'rows': []})

    def serve_events(self):
        """Serve /api/events (Server-Sent Events)."""
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()

        # Send retry
        self.wfile.write(b'retry: 3000\n')
        self.wfile.flush()

        # Subscribe to events
        event_queue = self.server.event_bus.subscribe()

        try:
            # Send initial snapshot
            snapshot = self.server.watcher.snapshot()
            self.send_sse_event(0, 'snapshot', snapshot)

            # Heartbeat timer
            last_heartbeat = time.time()

            while True:
                try:
                    # Wait for event with timeout (for heartbeat)
                    event, data, event_id = event_queue.get(timeout=5)
                    self.send_sse_event(event_id, event, data)
                    last_heartbeat = time.time()
                except queue.Empty:
                    # Send heartbeat
                    now = time.time()
                    if now - last_heartbeat > 15:
                        self.wfile.write(b': ping\n\n')
                        self.wfile.flush()
                        last_heartbeat = now
        except Exception:
            pass
        finally:
            self.server.event_bus.unsubscribe(event_queue)

    def send_sse_event(self, event_id: int, event: str, data: dict):
        """Send a single SSE event."""
        self.wfile.write(f'id: {event_id}\n'.encode('utf-8'))
        self.wfile.write(f'event: {event}\n'.encode('utf-8'))
        self.wfile.write(f'data: {json.dumps(data)}\n\n'.encode('utf-8'))
        self.wfile.flush()

    def send_json(self, data: dict):
        """Send JSON response."""
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))

    def log_message(self, format, *args):
        """Suppress log messages."""
        pass


class ThreadingHTTPServer(http.server.ThreadingHTTPServer):
    """HTTP server with references to watcher, event_bus, and port."""

    def __init__(self, host_port, handler_class, watcher, event_bus, port):
        self.watcher = watcher
        self.event_bus = event_bus
        self.port = port
        super().__init__(host_port, handler_class)


# ===== SECTION D: MAIN / CLI =====


def main():
    parser = argparse.ArgumentParser(description='Roundhouse MVP1 — fleet driver for boltzmann')
    parser.add_argument('--serve', action='store_true', help='Run server (default)')
    parser.add_argument('--scan', metavar='DIR', help='Scan unit directory and print report')
    parser.add_argument('--unit-dir', default=os.path.expanduser('~/.config/systemd/user'),
                        help='Unit directory (default: ~/.config/systemd/user)')
    parser.add_argument('--port', type=int, default=8090, help='HTTP port (default: 8090)')
    parser.add_argument('--db', help='SQLite database path')
    parser.add_argument('--no-db', action='store_true', help='Skip database')

    args = parser.parse_args()

    # Default to --serve if nothing specified
    if not args.scan and not args.serve:
        args.serve = True

    if args.scan:
        return cmd_scan(args)
    elif args.serve:
        return cmd_serve(args)

    return 0


def cmd_scan(args):
    """Scan a directory and print unit information."""
    unit_dir = args.scan

    if not os.path.isdir(unit_dir):
        print(f"Error: {unit_dir} not a directory", file=sys.stderr)
        return 1

    # Select units
    unit_paths = select_units(unit_dir)
    print(f"Selected {len(unit_paths)} units:")

    # Parse and report
    units = {}
    port_claims = {}

    for fpath in unit_paths:
        try:
            with open(fpath, 'rb') as f:
                raw = f.read()
            unit = parse_unit(fpath, raw)
            units[unit.name] = unit
            print(f"  {unit.name}")

            # Track port claims
            if unit.exec_start and unit.exec_start.engine_argv:
                profile = extract_param_profile(unit.exec_start.engine_argv)
                port = profile.get('port', 8080)
                alias = profile.get('alias', unit.name)

                if port not in port_claims:
                    port_claims[port] = []
                port_claims[port].append({
                    'unit': unit.name,
                    'alias': alias,
                    'enabled': True,  # We don't have systemd state here
                    'retired': unit.retired,
                    'gate': unit.gate
                })
        except Exception as e:
            print(f"  Error parsing {fpath}: {e}", file=sys.stderr)
            return 1

    # Print port board
    print("\nPort board:")
    for port in sorted(port_claims.keys()):
        claims = port_claims[port]
        if len(claims) == 1:
            claim = claims[0]
            status = '✓' if not claim['retired'] else '◌'
            print(f"  {port} {status} {claim['alias']}")
        else:
            # Collision
            enabled_active = sum(1 for c in claims if c['enabled'] and not c['retired'])
            if enabled_active >= 2:
                cls = 'active'
                icon = '✗'
            else:
                # Check if gated
                has_gate = any(c['gate'] for c in claims)
                if has_gate and not enabled_active:
                    cls = 'armed'
                    icon = '⚠'
                else:
                    cls = 'latent'
                    icon = '◌'

            claim_names = ', '.join(f"{c['alias']}" for c in claims)
            print(f"  {port} {icon} {cls}: {claim_names}")

    print("\nDeployment records: (not printed in --scan mode)")
    print(f"Exit: 0")
    return 0


def cmd_serve(args):
    """Run the server."""
    unit_dir = args.unit_dir
    port = args.port
    use_db = not args.no_db
    db_path = args.db

    # Select units
    unit_paths = select_units(unit_dir)
    if not unit_paths:
        print("Warning: no units selected", file=sys.stderr)

    # Parse units
    units = {}
    for fpath in unit_paths:
        try:
            with open(fpath, 'rb') as f:
                raw = f.read()
            unit = parse_unit(fpath, raw)
            units[unit.name] = unit
        except Exception as e:
            print(f"Error parsing {fpath}: {e}", file=sys.stderr)
            return 1

    # Create watcher stub or real implementation
    try:
        # Try to use real Watcher if available (from Section B)
        running_kernel = os.uname().release
        watcher = Watcher(units, running_kernel, None)
    except NameError:
        # Fallback to stub
        watcher = _StubWatcher(units)

    # Create event bus
    event_bus = EventBus()

    # Start HTTP server
    try:
        server = ThreadingHTTPServer(
            ('0.0.0.0', port),
            RoundhouseRequestHandler,
            watcher,
            event_bus,
            port
        )
        print(f"Roundhouse listening on http://0.0.0.0:{port}")

        # Handle shutdown signals
        def signal_handler(sig, frame):
            print("\nShutting down...")
            server.shutdown()
            sys.exit(0)

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        # Serve forever
        server.serve_forever()
    except Exception as e:
        print(f"Error starting server: {e}", file=sys.stderr)
        return 1

    return 0


class _StubWatcher:
    """Stub Watcher for testing when Section B is not available."""

    def __init__(self, units: dict):
        self.units = units
        self.host = os.uname().nodename
        self.kernel = os.uname().release
        self.now = time.time()
        self.mem_total = 32840000000  # 32 GiB stub
        self.mem_available = 14000000000  # 14 GiB stub

    def snapshot(self) -> dict:
        """Return a stub snapshot."""
        units_list = []
        for name, unit in self.units.items():
            param_profile = {}
            if unit.exec_start:
                param_profile = extract_param_profile(unit.exec_start.engine_argv)

            units_list.append({
                'unit': name,
                'description': unit.description or '',
                'retired': unit.retired,
                'rung': 'OFF',
                'roster': None,
                'since': self.now,
                'detail': '',
                'badges': [],
                'stale': False,
                'sensed_at': self.now,
                'enabled': True,
                'active_state': 'inactive',
                'sub_state': 'dead',
                'n_restarts': 0,
                'port': param_profile.get('port', 8080),
                'port_source': param_profile.get('port_source', 'default'),
                'alias': param_profile.get('alias', name),
                'gate': unit.gate,
                'model_file': param_profile.get('model_path', ''),
                'model_path': param_profile.get('model_path', ''),
                'quant_hint': None,
                'ctx': param_profile.get('ctx'),
                'engine': unit.exec_start.engine if unit.exec_start else {},
                'param_profile': param_profile,
                'mem': {'bytes': None, 'source': 'unknown', 'label': 'unknown'},
                'port_conflict': None
            })

        return {
            'host': self.host,
            'kernel': self.kernel,
            'now': self.now,
            'mem': {
                'total_bytes': self.mem_total,
                'available_bytes': self.mem_available
            },
            'sources': {
                'journal': 'ok',
                'systemctl': 'ok'
            },
            'self_port': 8090,
            'units': units_list
        }


if __name__ == '__main__':
    sys.exit(main())
