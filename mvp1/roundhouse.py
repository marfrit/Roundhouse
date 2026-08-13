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
from datetime import datetime, timezone
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


# Any URL scheme except file: means the thing being launched or loaded is not on this box.
REMOTE_SCHEME_RE = re.compile(r'(?!file://)\b[a-zA-Z][a-zA-Z0-9+.\-]*://')


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
    ]

    # Check exec_start: the argv is what actually launches something, so a *remote* scheme
    # here would mean the artifact or engine is not local. file:// is local by definition.
    if 'exec_start' in dep and dep['exec_start']:
        for token in dep['exec_start'].tokens:
            for domain in paid_domains:
                assert domain not in token.text, f"Paid offloading detected: {domain}"
            m = REMOTE_SCHEME_RE.search(token.text)
            assert m is None, f"Non-local artifact/engine detected: {m.group(0) if m else ''}"

    # Check known fields for paid endpoints only. A bare '://' must NOT be rejected here:
    # Documentation=file:///... is a real directive on two of this host's units, and an
    # operator's doc link is not an offload path.
    if 'known' in dep:
        known_str = str(dep['known'])
        for domain in paid_domains:
            assert domain not in known_str, f"Paid offloading detected: {domain}"


# ===== SECTION B: WATCHER + MEMSTORE (no threads inside; run_ro is the only subprocess gate) =====

import subprocess
import threading

READONLY_SYSTEMCTL_VERBS = {"show", "cat", "list-units", "list-unit-files"}

# Journal regex constants per §3.4 (AMENDED per orchestrator)
# llama-server patterns
LS_READY = [r"model loaded", r"update_slots: all slots are idle"]
LS_LISTENING = [r"listening on http"]
LS_BUSY_START = [r"launch_slot_", r"update_slots: .*new prompt", r"processing task"]
LS_BUSY_END = [r"slot\s+release:", r"update_slots: all slots are idle"]
LS_REQ_DONE = [r"request: (GET|POST) [^ ]+ [0-9.]+ 200"]

# llamafile patterns
LF_READY = [r"ll?ama server listening at", r"all slots are idle", r"model loaded"]
LF_BUSY_START = [r"slot \d+ is processing", r"processing task"]
LF_BUSY_END = [r"slot \d+ released", r"all slots are idle"]


def run_ro(argv: List[str], timeout=10) -> str:
    """Run a read-only subprocess (systemctl/journalctl only).

    Args:
        argv: command and arguments
        timeout: timeout in seconds (default 10)

    Returns:
        stdout as string

    Raises:
        ValueError: if argv[0] is not systemctl/journalctl, or if systemctl
                    verb is not in READONLY_SYSTEMCTL_VERBS
    """
    if argv[0] not in {"systemctl", "journalctl"}:
        raise ValueError(f"Only systemctl and journalctl allowed; got {argv[0]}")

    if argv[0] == "systemctl":
        # Extract verb (first non-flag argument after systemctl)
        verb = None
        for arg in argv[1:]:
            if not arg.startswith('-'):
                verb = arg
                break
        if verb and verb not in READONLY_SYSTEMCTL_VERBS:
            raise ValueError(f"systemctl verb '{verb}' not in read-only set {READONLY_SYSTEMCTL_VERBS}")

    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return result.stdout
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"Command {argv} timed out after {timeout}s")


def spawn_ro_stream(argv: List[str]) -> subprocess.Popen:
    """Spawn a read-only stream subprocess (systemctl/journalctl only).

    Args:
        argv: command and arguments

    Returns:
        Popen object for the subprocess

    Raises:
        ValueError: if argv[0] is not systemctl/journalctl, or if systemctl
                    verb is not in READONLY_SYSTEMCTL_VERBS
    """
    if argv[0] not in {"systemctl", "journalctl"}:
        raise ValueError(f"Only systemctl and journalctl allowed; got {argv[0]}")

    if argv[0] == "systemctl":
        # Extract verb (first non-flag argument after systemctl)
        verb = None
        for arg in argv[1:]:
            if not arg.startswith('-'):
                verb = arg
                break
        if verb and verb not in READONLY_SYSTEMCTL_VERBS:
            raise ValueError(f"systemctl verb '{verb}' not in read-only set {READONLY_SYSTEMCTL_VERBS}")

    return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def parse_show_blocks(text: str, unit_order: List[str]) -> Dict[str, Dict[str, str]]:
    """Parse systemctl --user show output into blocks per unit.

    Args:
        text: output from systemctl --user show (blocks separated by blank lines)
        unit_order: expected unit names in order (corresponds to arguments passed to systemctl)

    Returns:
        dict mapping unit name to dict of {property: value}
    """
    blocks = text.strip().split('\n\n')
    result = {}

    for i, block in enumerate(blocks):
        if i >= len(unit_order):
            break

        unit_name = unit_order[i]
        props = {}

        for line in block.strip().split('\n'):
            if '=' in line:
                key, value = line.split('=', 1)
                props[key] = value

        result[unit_name] = props

    return result


def classify_port_claims(claims: List[Dict]) -> tuple:
    """Classify a port's claim list per §4.4(c). Returns (class, note).

    Single claim -> (None, None). Otherwise:
      active = >=2 claimants actually occupying the port (STARTING/LOADING/READY/BUSY)
      armed  = >=2 claimants that are enabled, or held back only by an unsatisfied gate
      latent = anything else (e.g. the retired mixperten claim on :8085)
    """
    if len(claims) < 2:
        return (None, None)

    ACTIVE_RUNGS = {'STARTING', 'LOADING', 'READY', 'BUSY'}
    if sum(1 for c in claims if c.get('rung') in ACTIVE_RUNGS) >= 2:
        return ('active', 'two claimants are live on this port right now')

    armed = sum(1 for c in claims
                if (c.get('enabled') and not c.get('retired')) or c.get('gate'))
    if armed >= 2:
        return ('armed', 'harmless only while BOTH the disable and the kernel gate hold')

    return ('latent', None)


@dataclass
class Watcher:
    """State machine for tracking llama-server/llamafile units.

    Processes systemctl show output, journal lines, and cgroup samples
    to maintain per-unit state (rung, badges, timings).
    """
    units: Dict[str, UnitFile]
    running_kernel: str
    mem_store: Optional['MemStore']
    now: callable = field(default_factory=lambda: time.time)

    # Per-unit state (internal)
    _state: Dict[str, Dict] = field(default_factory=dict)
    _cgroup_cache: Dict[str, Dict] = field(default_factory=dict)
    # Set by cmd_serve: the port this Roundhouse instance is bound to, and the
    # live health of the two sensing sources ("ok" or "down since <epoch>").
    self_port: int = 8090
    sources: Dict[str, str] = field(default_factory=lambda: {'journal': 'ok', 'systemctl': 'ok'})

    def __post_init__(self):
        """Initialize per-unit state."""
        for unit_name in self.units:
            self._state[unit_name] = {
                'active_state': None,
                'sub_state': None,
                'result': None,
                'n_restarts': 0,
                'exec_main_pid': None,
                'exec_main_start_ts': None,
                'exec_main_start_ts_mono': None,
                'condition_result': None,
                'control_group': None,
                'ready': False,
                'busy': False,
                'busy_since': None,
                'last_marker': None,
                'unit_file_state': None,
                'ready_at': None,
                'mem_recorded': False,
                'sensed_at': self.now()
            }
            self._cgroup_cache[unit_name] = {
                'peak': None,
                'current': None,
                'last_peak': None
            }

    def apply_systemctl_show(self, props: Dict[str, Dict[str, str]]) -> List[Dict]:
        """Apply systemctl show output.

        Args:
            props: dict[unit_name, dict[property, value]] from parse_show_blocks

        Returns:
            list of event dicts (empty if no rung changes)
        """
        events = []

        for unit_name, unit_props in props.items():
            if unit_name not in self._state:
                continue

            old_ts_mono = self._state[unit_name]['exec_main_start_ts_mono']
            new_ts_mono = unit_props.get('ExecMainStartTimestampMonotonic', '0')

            # Check for process restart (timestamp changed)
            if old_ts_mono and old_ts_mono != new_ts_mono:
                # Process restarted; reset journal state
                self._state[unit_name]['ready'] = False
                self._state[unit_name]['busy'] = False
                self._state[unit_name]['busy_since'] = None
                self._state[unit_name]['last_marker'] = None
                self._state[unit_name]['ready_at'] = None
                self._state[unit_name]['mem_recorded'] = False

            # Parse timestamp
            ts_str = unit_props.get('ExecMainStartTimestamp', '')
            parsed_ts = None
            if ts_str and ts_str.strip():
                try:
                    # Format: "Wed 2026-08-12 13:24:45 CEST"
                    # Strip weekday and timezone
                    parts = ts_str.split()
                    if len(parts) >= 4:
                        date_time_str = f"{parts[1]} {parts[2]}"  # "2026-08-12 13:24:45"
                        parsed_ts = time.mktime(time.strptime(date_time_str, "%Y-%m-%d %H:%M:%S"))
                except Exception:
                    pass

            # Check if leaving active state
            old_active = self._state[unit_name]['active_state']
            new_active = unit_props.get('ActiveState', '')
            if old_active == 'active' and new_active != 'active':
                # Leaving active: record the 'exit' peak row (§6 write moment 2) from the
                # last cached tick sample -- the cgroup itself may already be gone -- then
                # reset journal state.
                self._record_mem(unit_name, 'exit')
                self._state[unit_name]['ready'] = False
                self._state[unit_name]['busy'] = False
                self._state[unit_name]['busy_since'] = None
                self._state[unit_name]['last_marker'] = None
                self._state[unit_name]['ready_at'] = None
                self._state[unit_name]['mem_recorded'] = False

            # Update state
            self._state[unit_name]['active_state'] = new_active
            self._state[unit_name]['sub_state'] = unit_props.get('SubState', '')
            self._state[unit_name]['result'] = unit_props.get('Result', '')
            self._state[unit_name]['n_restarts'] = int(unit_props.get('NRestarts', '0'))
            self._state[unit_name]['exec_main_pid'] = unit_props.get('ExecMainPID', '0')
            self._state[unit_name]['exec_main_start_ts'] = parsed_ts
            self._state[unit_name]['exec_main_start_ts_mono'] = new_ts_mono
            self._state[unit_name]['condition_result'] = unit_props.get('ConditionResult', '')
            self._state[unit_name]['control_group'] = unit_props.get('ControlGroup', '')
            self._state[unit_name]['unit_file_state'] = unit_props.get('UnitFileState', '')
            self._state[unit_name]['sensed_at'] = self.now()

            # Compute rung and check for changes
            old_rung = self._get_rung(unit_name)
            new_rung = self._compute_rung(unit_name)

            if old_rung != new_rung:
                events.append(self._make_rung_event(unit_name, new_rung))

        return events

    def apply_journal_line(self, rec: Dict) -> List[Dict]:
        """Apply a journal record line.

        Args:
            rec: dict from json.loads of a journal line

        Returns:
            list of event dicts
        """
        events = []

        # Extract unit name from _SYSTEMD_USER_UNIT
        unit_name = rec.get('_SYSTEMD_USER_UNIT', '')
        if not unit_name or unit_name not in self._state:
            return events

        # Extract message
        message = rec.get('MESSAGE', '')
        if not message:
            return events

        # When the line was written, not when we read it. Backfilled lines can be minutes
        # old at startup; timing a load from self.now() there invents a load_seconds that
        # measures Roundhouse's own start-up delay instead of the model's load.
        try:
            line_ts = int(rec['__REALTIME_TIMESTAMP']) / 1e6
        except (KeyError, TypeError, ValueError):
            line_ts = self.now()

        unit = self.units.get(unit_name)
        if not unit or not unit.exec_start:
            return events

        # Determine engine kind
        engine_kind = unit.exec_start.engine.get('kind')
        if engine_kind == 'llama-server':
            ready_patterns = LS_READY
            busy_start_patterns = LS_BUSY_START
            busy_end_patterns = LS_BUSY_END
            req_done_patterns = LS_REQ_DONE
        elif engine_kind == 'llamafile':
            ready_patterns = LF_READY
            busy_start_patterns = LF_BUSY_START
            busy_end_patterns = LF_BUSY_END
            req_done_patterns = []
        else:
            return events

        # Apply transition logic per §3.4
        old_rung = self._get_rung(unit_name)

        # Rule 1: BUSY_END match
        busy_end_match = any(re.search(p, message) for p in busy_end_patterns)
        if busy_end_match:
            self._state[unit_name]['busy'] = False
            # Also a READY marker?
            ready_match = any(re.search(p, message) for p in ready_patterns)
            if ready_match:
                self._state[unit_name]['ready'] = True
            self._state[unit_name]['last_marker'] = message
        # Rule 2: READY match
        elif any(re.search(p, message) for p in ready_patterns):
            self._state[unit_name]['ready'] = True
            self._state[unit_name]['busy'] = False
            self._state[unit_name]['last_marker'] = message
        # Rule 3: BUSY_START match
        elif any(re.search(p, message) for p in busy_start_patterns):
            self._state[unit_name]['busy'] = True
            self._state[unit_name]['ready'] = True
            self._state[unit_name]['busy_since'] = line_ts
            self._state[unit_name]['last_marker'] = message
        # Rule 4: REQ_DONE match
        elif req_done_patterns and any(re.search(p, message) for p in req_done_patterns):
            self._state[unit_name]['ready'] = True
            self._state[unit_name]['busy'] = False
            self._state[unit_name]['last_marker'] = message
        # Rule 5: no match - no state change
        else:
            return events

        # Stamp the moment the model first became ready; load_seconds (§6) is measured
        # from ExecMainStartTimestamp to here, NOT to the next cgroup tick.
        if self._state[unit_name]['ready'] and not self._state[unit_name].get('ready_at'):
            self._state[unit_name]['ready_at'] = line_ts

        self._state[unit_name]['sensed_at'] = self.now()

        # Check for rung change
        new_rung = self._compute_rung(unit_name)
        if old_rung != new_rung:
            events.append(self._make_rung_event(unit_name, new_rung))

        return events

    def apply_cgroup_sample(self, unit_name: str, peak: Optional[int], current: Optional[int]) -> List[Dict]:
        """Apply a cgroup memory sample.

        Args:
            unit_name: name of the unit
            peak: peak memory in bytes or None
            current: current memory in bytes or None

        Returns:
            list of event dicts
        """
        if unit_name not in self._cgroup_cache:
            return []

        # Cache the sample
        if peak is not None:
            self._cgroup_cache[unit_name]['peak'] = peak
            self._cgroup_cache[unit_name]['last_peak'] = peak
        if current is not None:
            self._cgroup_cache[unit_name]['current'] = current

        # Record the measured peak once per process lifetime, on the first tick that
        # observes the unit at READY.
        #
        # This deliberately does NOT test for a rung *transition* here: READY is reached
        # by the journal thread (apply_journal_line caches _rung='READY'), so by the time
        # the next 3 s tick arrives the transition is already spent and an
        # old_rung != 'READY' test never fires -- which is why no row was ever written.
        # The 'mem_recorded' latch (reset on restart and on leaving active) gives the
        # once-per-lifecycle guarantee instead.
        events = []
        if self._compute_rung(unit_name) == 'READY' and not self._state[unit_name].get('mem_recorded'):
            ev = self._record_mem(unit_name, 'ready')
            if ev:
                events.append(ev)

        return events

    def _record_mem(self, unit_name: str, phase: str) -> Optional[Dict]:
        """Record a measured cgroup peak to sqlite (§6 write moments).

        Uses the last cached tick sample, so it still works at 'exit' time when the
        cgroup has already been torn down. Returns the `mem` SSE payload, or None when
        nothing was recorded (no store, no sample, no model path).
        """
        if not self.mem_store or unit_name not in self.units:
            return None

        peak = self._cgroup_cache.get(unit_name, {}).get('last_peak')
        if not peak:
            return None

        unit = self.units[unit_name]
        if not unit.exec_start:
            return None

        profile = extract_param_profile(unit.exec_start.engine_argv)
        model_path = profile.get('model_path')
        if not model_path:
            return None

        state = self._state[unit_name]
        load_seconds = None
        if phase == 'ready' and state.get('exec_main_start_ts'):
            ready_at = state.get('ready_at') or self.now()
            load_seconds = ready_at - state['exec_main_start_ts']

        self.mem_store.record(
            unit=unit_name,
            model_path=model_path,
            file_id=self._compute_file_id(model_path),
            ctx=profile.get('ctx'),
            ctk=profile.get('cache_type_k'),
            ctv=profile.get('cache_type_v'),
            phase=phase,
            peak_bytes=peak,
            load_seconds=load_seconds,
        )
        if phase == 'ready':
            state['mem_recorded'] = True

        return {
            'unit': unit_name,
            'peak_bytes': peak,
            'phase': phase,
            'source': 'measured',
        }

    def snapshot(self) -> Dict:
        """Return a complete snapshot of the current state.

        Returns:
            dict shaped per spec §4.4(a)
        """
        # Get memory info
        mem_total = None
        mem_available = None
        try:
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    if line.startswith('MemTotal:'):
                        mem_total = int(line.split()[1]) * 1024
                    elif line.startswith('MemAvailable:'):
                        mem_available = int(line.split()[1]) * 1024
        except Exception:
            pass

        # Build units list
        units_list = []
        for unit_name, unit in self.units.items():
            rung = self._compute_rung(unit_name)

            if unit.exec_start:
                profile = extract_param_profile(unit.exec_start.engine_argv)
            else:
                profile = {}

            mem_info = self._compute_memory(unit_name, profile)

            port = profile.get('port', 8080)
            port_source = profile.get('port_source', 'default')
            alias = profile.get('alias', unit_name)
            quant = quant_hint(profile.get('model_path', ''))

            unit_dict = {
                'unit': unit_name,
                'description': unit.description or '',
                'retired': unit.retired,
                'rung': rung,
                'roster': self._rung_to_roster(rung),
                'since': self._state[unit_name].get('exec_main_start_ts') or self.now(),
                'detail': self._compute_detail(unit_name, rung),
                'badges': self._compute_badges(unit_name, rung),
                'stale': False,
                'sensed_at': self._state[unit_name]['sensed_at'],
                'enabled': self._state[unit_name].get('unit_file_state') == 'enabled',
                'active_state': self._state[unit_name]['active_state'],
                'sub_state': self._state[unit_name]['sub_state'],
                'n_restarts': self._state[unit_name]['n_restarts'],
                'port': port,
                'port_source': port_source,
                'alias': alias,
                'gate': unit.gate,
                'model_file': os.path.basename(profile.get('model_path', '')),
                'quant_hint': quant,
                'ctx': profile.get('ctx'),
                'mem': mem_info,
                'port_conflict': None  # filled in below, once every claim is known
            }

            units_list.append(unit_dict)

        # Second pass: a unit cannot know it shares a port until every unit is rendered.
        claims_by_port = {}
        for row in units_list:
            if row['port']:
                claims_by_port.setdefault(row['port'], []).append(row)
        for port, rows in claims_by_port.items():
            if len(rows) < 2:
                continue
            cls, note = classify_port_claims(rows)
            for row in rows:
                row['port_conflict'] = {
                    'class': cls,
                    'note': note,
                    'with': [o['unit'] for o in rows if o['unit'] != row['unit']],
                }

        return {
            'host': os.uname()[1],  # hostname
            'kernel': os.uname()[2],  # kernel release
            'now': self.now(),
            'mem': {
                'total_bytes': mem_total,
                'available_bytes': mem_available
            },
            'sources': dict(self.sources),
            'self_port': self.self_port,
            'units': units_list
        }

    def _get_rung(self, unit_name: str) -> Optional[str]:
        """Get the current rung for a unit (or None if never computed)."""
        state = self._state.get(unit_name, {})
        return state.get('_rung')

    def _compute_rung(self, unit_name: str) -> str:
        """Compute the rung for a unit according to the 8-rung table (§3.3)."""
        unit = self.units.get(unit_name)
        if not unit:
            return 'OFF'

        state = self._state[unit_name]
        active_state = state.get('active_state', '')
        sub_state = state.get('sub_state', '')

        # Rule 1: RETIRED
        if unit.retired:
            state['_rung'] = 'RETIRED'
            return 'RETIRED'

        # Rule 2: FAILED
        if active_state == 'failed':
            state['_rung'] = 'FAILED'
            return 'FAILED'

        if active_state == 'activating' and sub_state == 'auto-restart':
            state['_rung'] = 'FAILED'
            return 'FAILED'

        # Rule 3: STARTING
        if active_state == 'activating':
            state['_rung'] = 'STARTING'
            return 'STARTING'

        # Rule 4: BUSY
        if active_state == 'active' and state.get('busy'):
            state['_rung'] = 'BUSY'
            return 'BUSY'

        # Rule 5: READY
        if active_state == 'active' and state.get('ready'):
            state['_rung'] = 'READY'
            return 'READY'

        # Rule 6: LOADING
        if active_state == 'active' and not state.get('ready'):
            state['_rung'] = 'LOADING'
            return 'LOADING'

        # Rule 7: STANDBY
        if active_state in ('inactive', 'dead'):
            if unit.gate:
                gate = unit.gate
                if gate['kind'] == 'kernel':
                    if gate.get('wants') != self.running_kernel:
                        state['_rung'] = 'STANDBY'
                        return 'STANDBY'
                elif gate['kind'] == 'opaque':
                    if state.get('condition_result') == 'no':
                        state['_rung'] = 'STANDBY'
                        return 'STANDBY'

        # Rule 8: OFF
        state['_rung'] = 'OFF'
        return 'OFF'

    def _rung_to_roster(self, rung: str) -> str:
        """Map rung to roster state."""
        if rung in ('READY', 'BUSY'):
            return 'hot'
        elif rung in ('STARTING', 'LOADING'):
            return 'loading'
        elif rung in ('OFF', 'STANDBY'):
            return 'configured'
        elif rung == 'FAILED':
            return 'load-failed'
        elif rung == 'RETIRED':
            return None
        return 'configured'

    def _make_rung_event(self, unit_name: str, rung: str) -> Dict:
        """Create a rung event dict."""
        state = self._state[unit_name]
        state['_rung'] = rung

        return {
            'unit': unit_name,
            'rung': rung,
            'roster': self._rung_to_roster(rung),
            'since': state.get('exec_main_start_ts') or self.now(),
            'detail': self._compute_detail(unit_name, rung),
            'badges': self._compute_badges(unit_name, rung),
            'stale': False
        }

    def _compute_detail(self, unit_name: str, rung: str) -> str:
        """Compute the detail text for a rung."""
        state = self._state[unit_name]

        if rung == 'FAILED':
            n_restarts = state.get('n_restarts', 0)
            sub_state = state.get('sub_state', '')
            if 'auto-restart' in sub_state:
                return f"restart-looping, NRestarts={n_restarts}"
        elif rung == 'LOADING':
            ts = state.get('exec_main_start_ts')
            if ts:
                elapsed = int(self.now() - ts)
                detail = f"elapsed {elapsed}s"
                last = self._last_load_seconds(unit_name)
                if last:
                    detail += f" (last load: {int(last)}s)"
                return detail
        elif rung == 'BUSY':
            busy_since = state.get('busy_since')
            if busy_since:
                elapsed = int(self.now() - busy_since)
                return f"since {elapsed}s"
        elif rung == 'STANDBY':
            # The whole point of the STANDBY rung: say what it is waiting for, neutrally.
            gate = (self.units.get(unit_name).gate if self.units.get(unit_name) else None) or {}
            if gate.get('kind') == 'kernel':
                return (f"waiting for kernel {gate.get('wants')} "
                        f"(running: {self.running_kernel})")
            return f"gated (condition unverified): {gate.get('raw', '')}"
        elif rung == 'OFF' and state.get('active_state') == 'deactivating':
            return 'stopping'

        return ''

    def _compute_badges(self, unit_name: str, rung: str) -> List[str]:
        """Compute badges for a unit."""
        badges = []
        state = self._state[unit_name]

        if rung == 'BUSY':
            busy_since = state.get('busy_since')
            if busy_since:
                elapsed = self.now() - busy_since
                if elapsed > 30 * 60:  # 30 minutes
                    badges.append('long_running')

        return badges

    def _compute_memory(self, unit_name: str, profile: Dict) -> Dict:
        """Compute memory info for a unit."""
        if self.mem_store and profile.get('model_path'):
            file_id = self._compute_file_id(profile['model_path'])
            ctx = profile.get('ctx')
            mem = self.mem_store.lookup(unit_name, file_id, ctx)
            if mem:
                return mem

        # Fallback: estimate
        return estimate_memory(
            {'artifact': {'path': profile.get('model_path')}},
            self.mem_store
        )

    def _last_load_seconds(self, unit_name: str) -> Optional[float]:
        """Newest recorded load_seconds for this unit, for the LOADING detail line."""
        if not self.mem_store:
            return None
        for row in self.mem_store.history(unit_name):
            if row.get('load_seconds'):
                return row['load_seconds']
        return None

    def _compute_file_id(self, model_path: str) -> str:
        """Compute file_id (sz<size>:mt<mtime>) for a model file."""
        try:
            st = os.stat(model_path)
            return f"sz{st.st_size}:mt{int(st.st_mtime)}"
        except Exception:
            return ""


@dataclass
class MemStore:
    """SQLite-backed memory peak storage.

    Stores (ready, exit) measurements indexed by unit + model + context.
    If db_path is None, all operations are no-ops (inert mode).
    """
    db_path: Optional[str] = None
    _conn: Optional[sqlite3.Connection] = field(default=None, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self):
        """Initialize database connection."""
        if self.db_path:
            self._init_db()

    def _init_db(self):
        """Initialize the sqlite database and schema."""
        if not self.db_path:
            return

        # Create directory if needed
        os.makedirs(os.path.dirname(self.db_path) or '.', exist_ok=True)

        try:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.execute('''
                CREATE TABLE IF NOT EXISTS mem_peak (
                    unit TEXT NOT NULL,
                    model_path TEXT NOT NULL,
                    model_file_id TEXT NOT NULL,
                    ctx INTEGER,
                    ctk TEXT,
                    ctv TEXT,
                    phase TEXT NOT NULL CHECK (phase IN ('ready', 'exit')),
                    peak_bytes INTEGER NOT NULL,
                    load_seconds REAL,
                    boot_id TEXT NOT NULL,
                    sampled_at TEXT NOT NULL,
                    PRIMARY KEY (unit, model_file_id, ctx, boot_id, phase)
                )
            ''')
            self._conn.commit()
        except Exception as e:
            print(f"Error initializing MemStore: {e}", file=sys.stderr)
            self._conn = None

    def record(self, *, unit: str, model_path: str, file_id: str, ctx: Optional[int],
               ctk: Optional[str], ctv: Optional[str], phase: str, peak_bytes: int,
               load_seconds: Optional[float] = None):
        """Record a memory peak measurement.

        Args:
            unit: unit name
            model_path: path to model file
            file_id: file ID (sz<size>:mt<mtime>)
            ctx: context size (may be None)
            ctk: KV cache type K
            ctv: KV cache type V
            phase: 'ready' or 'exit'
            peak_bytes: peak memory in bytes
            load_seconds: load time in seconds (for ready phase)
        """
        if not self._conn:
            return

        with self._lock:
            try:
                # Get boot_id
                boot_id = self._get_boot_id()

                # ISO 8601 timestamp
                now_ts = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

                self._conn.execute('''
                    INSERT OR REPLACE INTO mem_peak
                    (unit, model_path, model_file_id, ctx, ctk, ctv, phase, peak_bytes,
                     load_seconds, boot_id, sampled_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (unit, model_path, file_id, ctx, ctk, ctv, phase, peak_bytes,
                      load_seconds, boot_id, now_ts))

                self._conn.commit()
            except Exception as e:
                print(f"Error recording to MemStore: {e}", file=sys.stderr)

    def lookup(self, unit: str, file_id: str, ctx: Optional[int]) -> Optional[Dict]:
        """Look up memory info for a unit/model/context.

        Args:
            unit: unit name
            file_id: file ID
            ctx: context size (may be None)

        Returns:
            dict with keys: peak_bytes, load_seconds, source, label
            or None if not found
        """
        if not self._conn:
            return None

        with self._lock:
            try:
                cursor = self._conn.execute('''
                    SELECT peak_bytes, load_seconds, phase
                    FROM mem_peak
                    WHERE unit = ? AND model_file_id = ? AND ctx = ?
                    ORDER BY phase DESC, sampled_at DESC
                    LIMIT 1
                ''', (unit, file_id, ctx))

                row = cursor.fetchone()
                if row:
                    peak_bytes, load_seconds, phase = row
                    return {
                        'bytes': peak_bytes,
                        'load_seconds': load_seconds,
                        'source': 'measured',
                        'label': 'measured peak, this (unit, model, ctx)'
                    }
            except Exception as e:
                print(f"Error looking up MemStore: {e}", file=sys.stderr)

        return None

    def history(self, unit: str) -> List[Dict]:
        """Get memory history for a unit.

        Args:
            unit: unit name

        Returns:
            list of measurement dicts
        """
        if not self._conn:
            return []

        with self._lock:
            try:
                cursor = self._conn.execute('''
                    SELECT ctx, peak_bytes, sampled_at, phase, load_seconds
                    FROM mem_peak
                    WHERE unit = ?
                    ORDER BY sampled_at DESC
                ''', (unit,))

                rows = cursor.fetchall()
                return [
                    {
                        'ctx': row[0],
                        'peak_bytes': row[1],
                        'sampled_at': row[2],
                        'phase': row[3],
                        'load_seconds': row[4],
                        'source': 'measured'
                    }
                    for row in rows
                ]
            except Exception as e:
                print(f"Error querying MemStore history: {e}", file=sys.stderr)

        return []

    def _get_boot_id(self) -> str:
        """Get the system boot ID."""
        try:
            with open('/proc/sys/kernel/random/boot_id', 'r') as f:
                return f.read().strip()
        except Exception:
            return 'unknown'


def estimate_memory(dep: Dict, store: Optional[MemStore]) -> Dict:
    """Estimate memory usage for a deployment.

    Args:
        dep: deployment dict with artifact.path
        store: MemStore (may be None)

    Returns:
        dict with keys: bytes, source, label
    """
    model_path = dep.get('artifact', {}).get('path')

    # Try sqlite lookup
    if store and model_path:
        try:
            file_id = f"sz{os.stat(model_path).st_size}:mt{int(os.stat(model_path).st_mtime)}"
            mem = store.lookup(dep.get('unit', ''), file_id, None)
            if mem:
                return mem
        except Exception:
            pass

    # Check if model file exists
    if model_path and os.path.isfile(model_path):
        try:
            size = os.path.getsize(model_path)
            estimated = int(size * 1.10 + 1.5 * 2**30)
            return {
                'bytes': estimated,
                'source': 'estimate',
                'label': 'estimate (file size + 10% + 1.5 GiB overhead; no measured peak, no KV model)'
            }
        except Exception:
            pass

    # Model not found
    return {
        'bytes': None,
        'source': 'unknown',
        'label': 'model file not found'
    }


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


def _wrapper_json(unit: 'UnitFile') -> Optional[Dict]:
    """JSON-safe view of exec_start.wrapper (drops the Token objects it carries)."""
    if not unit.exec_start or not unit.exec_start.wrapper:
        return None
    w = unit.exec_start.wrapper
    out = {k: v for k, v in w.items() if k != 'tokens'}
    out['text'] = ' '.join(t.text for t in w.get('tokens', []))
    return out


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
        """Serve /api/units/<name>: the list row (a) plus the parsed-file detail (b)."""
        watcher = self.server.watcher
        snapshot = watcher.snapshot()

        row = next((u for u in snapshot.get('units', []) if u['unit'] == unit_name), None)
        if row is None:
            self.error_404()
            return

        unit = getattr(watcher, 'units', {}).get(unit_name)
        if unit is None:
            # Watcher without parsed files (stub / degraded): the list row is all there is.
            self.send_json(row)
            return

        profile = extract_param_profile(unit.exec_start.engine_argv) if unit.exec_start else {}
        store = getattr(watcher, 'mem_store', None)

        self.send_json({
            **row,
            'path': unit.path,
            'param_profile': profile,
            'engine': unit.exec_start.engine if unit.exec_start else {},
            'wrapper': _wrapper_json(unit),
            # Verbatim, byte-faithful: the UI assigns these via textContent, never innerHTML.
            'comments': unit.comments,
            'other_directives': [
                {'section': d.section, 'key': d.key, 'value': _decode_value(d.value_raw),
                 'span': list(d.value_span)}
                for d in unit.other_directives
            ],
            'lines': [
                {'kind': l.kind, 'start': l.start, 'end': l.end, 'lineno': l.lineno}
                for l in unit.lines
            ],
            'warnings': list(unit.warnings),
            'raw_size': len(unit.raw),
            'known': dict(unit.known),
            'history_mem': store.history(unit_name) if store else [],
        })

    def serve_ports(self):
        """Serve /api/ports (port board)."""
        self.send_json(_build_port_board(self.server.watcher.snapshot()))

    def serve_deployments(self):
        """Serve /api/deployments (§4.4d).

        The record body comes from build_deployment() -- the parser owns the spine shape.
        Only the live half (enable state, roster, memory) is layered on from the snapshot.
        RETIRED units still emit a record with retired:true and roster.state null;
        consumers filter on `retired` (they are never placement targets).
        """
        watcher = self.server.watcher
        snapshot = watcher.snapshot()
        host = snapshot.get('host', '?')

        deployments = []
        parsed = getattr(watcher, 'units', {})
        for row in snapshot.get('units', []):
            unit = parsed.get(row['unit'])
            if unit is None:
                continue
            dep = build_deployment(unit, host)
            dep['load_strategy'] = {
                'kind': 'on-boot' if row.get('enabled') else 'manual',
                'enabled': row.get('enabled', False),
                'gate': unit.gate,
            }
            dep['roster'] = {
                'rung': row.get('rung', 'OFF'),
                'state': row.get('roster'),
                'since': row.get('since'),
            }
            dep['memory'] = row.get('mem') or dep['memory']
            deployments.append(dep)

        self.send_json({'host': host, 'deployments': deployments})

    def serve_mem(self):
        """Serve /api/mem: measured peak rows (§6 schema) plus the current per-unit
        number the UI shows, so a caller can tell measurement from estimate."""
        watcher = self.server.watcher
        snapshot = watcher.snapshot()
        store = getattr(watcher, 'mem_store', None)

        rows = []
        for unit in snapshot.get('units', []):
            for row in (store.history(unit['unit']) if store else []):
                rows.append(dict(row, unit=unit['unit']))

        self.send_json({
            'rows': rows,
            'current': [
                {'unit': u['unit'], **(u.get('mem') or {})}
                for u in snapshot.get('units', [])
            ],
        })

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
    selected = {os.path.basename(p) for p in unit_paths}
    considered = sorted(f for f in os.listdir(unit_dir) if f.endswith('.service'))

    print(f"Unit dir: {unit_dir}")
    print(f"{len(considered)} .service files, {len(selected)} selected as ours "
          f"(D1: ExecStart basename matches ^llama-server or contains llamafile)")

    # Parse and report
    units = {}
    port_claims = {}

    for fpath in unit_paths:
        try:
            with open(fpath, 'rb') as f:
                raw = f.read()
            unit = parse_unit(fpath, raw)
            units[unit.name] = unit
            flags = []
            if unit.retired:
                flags.append('RETIRED')
            if unit.gate:
                flags.append(f"gated:{unit.gate.get('wants') or unit.gate.get('kind')}")
            if unit.warnings:
                flags.append(f"{len(unit.warnings)} warning(s)")
            print(f"  {unit.name}" + (f"   [{', '.join(flags)}]" if flags else ''))

            # Track port claims
            if unit.exec_start and unit.exec_start.engine_argv:
                profile = extract_param_profile(unit.exec_start.engine_argv)
                port = profile.get('port', 8080)
                port_claims.setdefault(port, []).append({
                    'unit': unit.name,
                    'alias': profile.get('alias') or unit.name,
                    # A static scan cannot see enable state; treat every non-retired unit
                    # as a live claimant so nothing is quietly downgraded to 'latent'.
                    'enabled': not unit.retired,
                    'rung': None,          # nothing is running from the file's point of view
                    'retired': unit.retired,
                    'gate': unit.gate,
                })
        except Exception as e:
            print(f"  Error parsing {fpath}: {e}", file=sys.stderr)
            return 1

    # Not-ours files are listed explicitly: a count that differs from the known fleet size
    # must be explainable from this output alone, not investigated by hand.
    skipped = [f for f in considered if f not in selected]
    if skipped:
        print(f"\nNot ours ({len(skipped)}):")
        for name in skipped:
            print(f"  {name}   [no llama-server/llamafile in ExecStart]")

    print("\nPort board:")
    for port in sorted(port_claims.keys()):
        claims = port_claims[port]
        cls, note = classify_port_claims(claims)
        icon = {'active': '✗', 'armed': '⚠', 'latent': '◌'}.get(cls, '✓')
        names = ', '.join(c['alias'] + (' [RETIRED]' if c['retired'] else '')
                          + (' [gated]' if c['gate'] else '') for c in claims)
        label = f"{cls}: " if cls else ''
        print(f"  {port} {icon} {label}{names}")

    print("\nDeployment records: (not printed in --scan mode)")
    print("Exit: 0")
    return 0


def cmd_serve(args):
    """Run the server with polling threads and event streaming."""
    import time
    from datetime import timezone

    unit_dir = args.unit_dir
    port = args.port
    use_db = not args.no_db
    db_path = args.db

    # Determine MemStore path
    if use_db:
        if not db_path:
            xdg_state = os.environ.get('XDG_STATE_HOME', os.path.expanduser('~/.local/state'))
            db_path = os.path.join(xdg_state, 'roundhouse', 'roundhouse.sqlite')
    else:
        db_path = None

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

    selected_unit_names = sorted(units.keys())

    # Create MemStore
    mem_store = MemStore(db_path) if use_db else MemStore(None)

    # Create Watcher
    running_kernel = os.uname().release
    watcher = Watcher(units, running_kernel, mem_store)
    watcher.self_port = port

    # Create event bus
    event_bus = EventBus()

    # Shared lock for all apply_* calls
    watcher_lock = threading.Lock()

    # Shutdown event
    shutdown_event = threading.Event()

    # Track journal source state
    journal_state = {'down_since': None, 'proc': None}
    last_board = {'board': None}
    systemctl_state = {'down_since': None}
    backoff_journal = 1

    def poll_systemctl():
        """Poll systemctl every 3 seconds."""
        nonlocal backoff_journal

        while not shutdown_event.is_set():
            try:
                # Run systemctl show
                properties = [
                    'ActiveState', 'SubState', 'UnitFileState', 'Result', 'NRestarts',
                    'ExecMainPID', 'ExecMainStartTimestamp', 'ExecMainStartTimestampMonotonic',
                    'ConditionResult', 'ControlGroup'
                ]
                prop_args = ','.join(properties)

                try:
                    output = run_ro([
                        'systemctl', '--user', 'show', '-p', prop_args, '--'
                    ] + selected_unit_names)
                    props = parse_show_blocks(output, selected_unit_names)
                    systemctl_state['down_since'] = None
                    with watcher_lock:
                        watcher.sources['systemctl'] = 'ok'
                except Exception as e:
                    # Mark systemctl down; units render stale
                    if systemctl_state['down_since'] is None:
                        systemctl_state['down_since'] = time.time()
                    with watcher_lock:
                        watcher.sources['systemctl'] = 'down since %d' % systemctl_state['down_since']
                    props = {}

                # Apply to watcher
                with watcher_lock:
                    events = watcher.apply_systemctl_show(props)
                    for event in events:
                        event_bus.publish('rung', event)

                    # Read cgroup memory samples for active units
                    for unit_name in selected_unit_names:
                        state = watcher._state.get(unit_name, {})
                        control_group = state.get('control_group')
                        if control_group:
                            peak = None
                            current = None
                            try:
                                peak_path = f"/sys/fs/cgroup{control_group}/memory.peak"
                                with open(peak_path, 'r') as f:
                                    peak = int(f.read().strip())
                            except Exception:
                                pass
                            try:
                                current_path = f"/sys/fs/cgroup{control_group}/memory.current"
                                with open(current_path, 'r') as f:
                                    current = int(f.read().strip())
                            except Exception:
                                pass

                            if peak is not None or current is not None:
                                events = watcher.apply_cgroup_sample(unit_name, peak, current)
                                for event in events:
                                    event_bus.publish('mem', event)

                    # Rebuild the port board; emit only when a claim's class actually
                    # changed (§4). Re-emitting every 3 s makes every connected browser
                    # rebuild the board for nothing.
                    board = _build_port_board(watcher.snapshot())
                    if board != last_board['board']:
                        last_board['board'] = board
                        event_bus.publish('ports', board)

                # Sleep 3 seconds
                shutdown_event.wait(3)
            except Exception as e:
                print(f"Error in poll_systemctl: {e}", file=sys.stderr)
                shutdown_event.wait(1)

    def journal_tail():
        """Tail journal and apply events; respawn with backoff on error."""
        nonlocal backoff_journal, journal_state
        backoff = 1

        while not shutdown_event.is_set():
            try:
                # Spawn journalctl tail
                proc = spawn_ro_stream([
                    'journalctl', '--user', '-f', '-o', 'json', '-n', '0', '--no-pager'
                ])
                journal_state['proc'] = proc
                journal_state['down_since'] = None
                with watcher_lock:
                    watcher.sources['journal'] = 'ok'
                backoff = 1

                # Read lines
                while not shutdown_event.is_set():
                    line = proc.stdout.readline()
                    if not line:
                        # Stream ended
                        break

                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue

                    # Filter to selected units
                    unit_name = rec.get('_SYSTEMD_USER_UNIT')
                    if unit_name not in selected_unit_names:
                        continue

                    # Apply to watcher
                    with watcher_lock:
                        events = watcher.apply_journal_line(rec)
                        for event in events:
                            event_bus.publish('rung', event)

                # Process exited
                proc.terminate()
                journal_state['proc'] = None

            except Exception as e:
                print(f"Journal tail error: {e}", file=sys.stderr)
                if journal_state['proc']:
                    journal_state['proc'].terminate()
                    journal_state['proc'] = None

            # Mark down and backoff
            if journal_state['down_since'] is None:
                journal_state['down_since'] = time.time()
            with watcher_lock:
                watcher.sources['journal'] = 'down since %d' % journal_state['down_since']

            if shutdown_event.is_set():
                break

            sleep_time = min(backoff, 30)
            shutdown_event.wait(sleep_time)
            backoff = min(backoff * 2, 30)

    def backfill_journal():
        """Backfill journal at startup for currently active units."""
        try:
            with watcher_lock:
                # Get current snapshot
                snap = watcher.snapshot()
                active_rungs = {'STARTING', 'LOADING', 'READY', 'BUSY', 'STANDBY'}
                active_units = [u['unit'] for u in snap.get('units', []) if u.get('rung') in active_rungs]

                for unit_name in active_units:
                    unit = units.get(unit_name)
                    if not unit or not unit.exec_start:
                        continue

                    state = watcher._state.get(unit_name, {})
                    exec_ts = state.get('exec_main_start_ts')
                    if not exec_ts:
                        continue

                    # Get last 300 lines
                    try:
                        output = run_ro([
                            'journalctl', '--user', '-u', unit_name, '-o', 'json', '-n', '300', '--no-pager'
                        ])

                        for line in output.strip().split('\n'):
                            if not line:
                                continue
                            try:
                                rec = json.loads(line)
                            except Exception:
                                continue

                            # Filter by timestamp if available
                            rt = rec.get('__REALTIME_TIMESTAMP')
                            if rt:
                                try:
                                    rt_us = int(rt)
                                    exec_ts_us = int(exec_ts * 1e6)
                                    if rt_us < exec_ts_us:
                                        continue
                                except Exception:
                                    pass

                            # Apply
                            watcher.apply_journal_line(rec)
                    except Exception:
                        pass
        except Exception as e:
            print(f"Backfill error: {e}", file=sys.stderr)

    # Do initial systemctl poll synchronously
    try:
        properties = [
            'ActiveState', 'SubState', 'UnitFileState', 'Result', 'NRestarts',
            'ExecMainPID', 'ExecMainStartTimestamp', 'ExecMainStartTimestampMonotonic',
            'ConditionResult', 'ControlGroup'
        ]
        prop_args = ','.join(properties)

        try:
            output = run_ro([
                'systemctl', '--user', 'show', '-p', prop_args, '--'
            ] + selected_unit_names)
            props = parse_show_blocks(output, selected_unit_names)
        except Exception as e:
            print(f"Warning: initial systemctl poll failed: {e}", file=sys.stderr)
            props = {}

        with watcher_lock:
            watcher.apply_systemctl_show(props)
    except Exception as e:
        print(f"Error in initial poll: {e}", file=sys.stderr)

    # Start threads
    poll_thread = threading.Thread(target=poll_systemctl, daemon=True)
    poll_thread.start()

    # Start journal thread (with backfill first)
    def journal_with_backfill():
        backfill_journal()
        journal_tail()

    journal_thread = threading.Thread(target=journal_with_backfill, daemon=True)
    journal_thread.start()

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
            shutdown_event.set()
            if journal_state['proc']:
                try:
                    journal_state['proc'].terminate()
                except Exception:
                    pass
            # server.shutdown() blocks until serve_forever() returns -- and serve_forever()
            # runs in THIS thread, so calling it from the handler deadlocks the process:
            # it stops accepting but never exits, and systemd has to SIGKILL it at the stop
            # timeout. Ask for shutdown from a helper thread and let serve_forever() unwind.
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        # Serve forever
        server.serve_forever()
        server.server_close()
    except Exception as e:
        print(f"Error starting server: {e}", file=sys.stderr)
        shutdown_event.set()
        return 1

    return 0


def _build_port_board(snapshot: Dict) -> Dict:
    """Build the port board (§4.4c) from a snapshot. The single implementation:
    /api/ports and the SSE `ports` event both go through here."""
    self_port = snapshot.get('self_port', 8090)

    ports = {}
    for unit in snapshot.get('units', []):
        port = unit.get('port')
        if port:
            ports.setdefault(port, []).append({
                'unit': unit['unit'],
                'enabled': unit.get('enabled', False),
                'rung': unit.get('rung', 'OFF'),
                'retired': unit.get('retired', False),
                'gate': unit.get('gate')
            })

    # Roundhouse's own port is a claim too -- but it must be MERGED, never assigned:
    # overwriting the entry would erase a real unit's claim on the same port and hide
    # exactly the collision this board exists to show.
    ports.setdefault(self_port, [])

    port_list = []
    for port in sorted(ports.keys()):
        claims = ports[port]
        cls, note = classify_port_claims(claims)
        if port == self_port:
            note = 'roundhouse (self)' + (f' — also claimed by {len(claims)} unit(s)' if claims else '')
        port_list.append({
            'port': port,
            'claims': claims,
            'class': cls,
            'note': note,
            'self': port == self_port,
        })

    return {
        'ports': port_list,
        'self': {
            'port': self_port,
            'claims_by_units': [c['unit'] for c in ports.get(self_port, [])],
        },
    }


if __name__ == '__main__':
    sys.exit(main())
