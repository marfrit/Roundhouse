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
import hmac
import hashlib
import secrets
import stat
import difflib

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
    """Tokenize value pieces into Tokens with absolute byte offsets.

    Tokens are scanned per piece in BYTES, each piece carrying its own
    absolute base offset, so continuations cannot skew spans and there is
    no char-vs-byte drift. A backslash-newline continuation acts as a word
    separator, so no token ever spans two pieces.
    """
    tokens = []

    def _is_ws(c: bytes) -> bool:
        return c in (b' ', b'\t')

    for base, raw in pieces:
        n = len(raw)
        i = 0
        while i < n:
            c = raw[i:i + 1]
            if _is_ws(c):
                i += 1
                continue
            start = i

            if c == b"'":
                # Single-quoted: content is literal, no escapes, no specifiers.
                j = raw.find(b"'", i + 1)
                if j == -1:
                    end_tok = n
                    text = raw[i + 1:n].decode('utf-8', errors='replace')
                else:
                    end_tok = j + 1
                    text = raw[i + 1:j].decode('utf-8', errors='replace')
                tokens.append(Token(
                    text=text,
                    raw=raw[start:end_tok],
                    start=base + start,
                    end=base + end_tok,
                    has_specifier=False
                ))
                i = end_tok

            elif c == b'"':
                # Double-quoted: resolve \" \\ \n \t; %% -> %; lone % = specifier.
                j = i + 1
                text_parts = []
                has_spec = False
                closed = False
                while j < n:
                    cj = raw[j:j + 1]
                    if cj == b'"':
                        closed = True
                        break
                    if cj == b'\\' and j + 1 < n:
                        nxt = raw[j + 1:j + 2]
                        text_parts.append({b'n': b'\n', b't': b'\t',
                                           b'\\': b'\\', b'"': b'"'}.get(nxt, nxt))
                        j += 2
                        continue
                    if cj == b'%':
                        if j + 1 < n and raw[j + 1:j + 2] == b'%':
                            text_parts.append(b'%')
                            j += 2
                            continue
                        text_parts.append(b'%')
                        has_spec = True
                        j += 1
                        continue
                    text_parts.append(cj)
                    j += 1
                end_tok = (j + 1) if closed else n
                tokens.append(Token(
                    text=b''.join(text_parts).decode('utf-8', errors='replace'),
                    raw=raw[start:end_tok],
                    start=base + start,
                    end=base + end_tok,
                    has_specifier=has_spec
                ))
                i = end_tok

            else:
                # Unquoted word: runs to whitespace; %% -> %; lone % = specifier.
                j = i
                text_parts = []
                has_spec = False
                while j < n and not _is_ws(raw[j:j + 1]):
                    cj = raw[j:j + 1]
                    if cj == b'%':
                        if j + 1 < n and raw[j + 1:j + 2] == b'%':
                            text_parts.append(b'%')
                            j += 2
                            continue
                        text_parts.append(b'%')
                        has_spec = True
                        j += 1
                        continue
                    text_parts.append(cj)
                    j += 1
                tokens.append(Token(
                    text=b''.join(text_parts).decode('utf-8', errors='replace'),
                    raw=raw[start:j],
                    start=base + start,
                    end=base + j,
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

        # D1: tokenized-basename match, not a substring scan of the physical
        # line — a binary on a continuation line must count, and a path merely
        # CONTAINING 'llama-server' must not.
        is_ours = False
        try:
            unit = parse_unit(fpath, raw)
            if unit.exec_start:
                for tok in unit.exec_start.tokens:
                    base = os.path.basename(tok.text)
                    if base.startswith('llama-server') or 'llamafile' in base:
                        is_ours = True
                        break
        except Exception:
            is_ours = False

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
        self._badges = {}  # Cache of badges per unit
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
            self._badges[unit_name] = []

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
            self._state[unit_name]['_rung'] = new_rung

            # Emit rung-change event
            if old_rung != new_rung:
                events.append(self._make_rung_event(unit_name, new_rung))

            # Compute badges and emit badge-change event (even if rung unchanged)
            old_badges = self._badges.get(unit_name, [])
            new_badges = self._compute_badges(unit_name, new_rung)
            self._badges[unit_name] = new_badges

            if old_badges != new_badges:
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
        self._state[unit_name]['_rung'] = new_rung
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
                'stale': self._sources_degraded(),
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
        """Compute the rung per the 8-rung table (SPEC §3.3). PURE: no state
        writes — the _rung cache is committed only inside apply_* under the
        caller's lock, so unlocked snapshot() reads cannot swallow events."""
        unit = self.units.get(unit_name)
        if not unit:
            return 'OFF'

        state = self._state[unit_name]
        active_state = state.get('active_state', '')
        sub_state = state.get('sub_state', '')

        # Rule 1: RETIRED
        if unit.retired:
            return 'RETIRED'

        # Rule 2: FAILED
        if active_state == 'failed':
            return 'FAILED'

        if active_state == 'activating' and sub_state == 'auto-restart':
            return 'FAILED'

        # Rule 3: STARTING
        if active_state == 'activating':
            return 'STARTING'

        # Rule 4: BUSY
        if active_state == 'active' and state.get('busy'):
            return 'BUSY'

        # Rule 5: READY
        if active_state == 'active' and state.get('ready'):
            return 'READY'

        # Rule 6: LOADING
        if active_state == 'active' and not state.get('ready'):
            return 'LOADING'

        # Rule 7: STANDBY
        if active_state in ('inactive', 'dead'):
            if unit.gate:
                gate = unit.gate
                if gate['kind'] == 'kernel':
                    if gate.get('wants') != self.running_kernel:
                        return 'STANDBY'
                elif gate['kind'] == 'opaque':
                    if state.get('condition_result') == 'no':
                        return 'STANDBY'

        # Rule 8: OFF
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
            'stale': self._sources_degraded()
        }

    def _sources_degraded(self) -> bool:
        """True when either sensing source is down: every rung is then a
        best-effort guess and renders dimmed per SPEC.md section 3.4."""
        return any(v != 'ok' for v in self.sources.values())

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

        # Check for no_ready_marker: LOADING but past TimeoutStartSec without seeing ready marker
        if rung == 'LOADING':
            exec_main_start_ts = state.get('exec_main_start_ts')
            if exec_main_start_ts:
                unit = self.units.get(unit_name)
                if unit:
                    timeout_sec = parse_timeout_start_sec(unit.known.get('timeoutstartsec'))
                    elapsed = self.now() - exec_main_start_ts
                    if elapsed > timeout_sec:
                        badges.append('no_ready_marker')

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
        elif route.startswith('/api/rollouts/'):
            rollout_id = urllib.parse.unquote(route[len('/api/rollouts/'):])
            self.serve_rollout(rollout_id)
        else:
            self.error_404()

    def do_POST(self):
        """Handle POST requests with authentication."""
        # Parse URL
        path = self.path
        parsed = urllib.parse.urlparse(path)
        route = parsed.path

        # Determine if this is a known POST route
        is_post_route = (
            (route.startswith('/api/units/') and route.endswith('/edit')) or
            (route.startswith('/api/units/') and route.endswith('/rollout')) or
            (route.startswith('/api/rollouts/') and route.endswith('/rollback')) or
            (route.startswith('/api/rollouts/') and route.endswith('/dismiss'))
        )

        # Determine if this is a known GET-only route
        is_get_route = (
            route == '/' or
            route == '/api/units' or
            route.startswith('/api/units/') or
            route == '/api/ports' or
            route == '/api/deployments' or
            route == '/api/mem' or
            route == '/api/events' or
            route.startswith('/api/rollouts/')   # GET-only unless /rollback|/dismiss
        )

        # If it's not a POST route but is a known GET route, return 405 immediately
        if not is_post_route and is_get_route:
            self.error_405()
            return

        # Step 1: Check bearer token (E8) — only for POST routes
        auth_status = check_bearer(self)
        if auth_status is not None:
            if auth_status == 403:
                self.send_json_error(403, 'read_only_mode',
                                   'launch with --actuate to enable rollouts')
            else:
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Bearer')
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'unauthorized'}).encode('utf-8'))
            return

        # Step 2: Route and dispatch POST routes
        if route.startswith('/api/units/') and route.endswith('/edit'):
            unit_name = urllib.parse.unquote(route[len('/api/units/'):-len('/edit')])
            self.handle_edit(unit_name)
        elif route.startswith('/api/units/') and route.endswith('/rollout'):
            unit_name = urllib.parse.unquote(route[len('/api/units/'):-len('/rollout')])
            self.handle_rollout(unit_name)
        elif route.startswith('/api/rollouts/') and route.endswith('/rollback'):
            rollout_id = urllib.parse.unquote(route[len('/api/rollouts/'):-len('/rollback')])
            self.handle_rollback(rollout_id)
        elif route.startswith('/api/rollouts/') and route.endswith('/dismiss'):
            rollout_id = urllib.parse.unquote(route[len('/api/rollouts/'):-len('/dismiss')])
            self.handle_dismiss(rollout_id)
        else:
            # Unknown POST route
            self.send_response(404)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'not_found'}).encode('utf-8'))

    def handle_edit(self, unit_name: str):
        """Handle POST /api/units/<name>/edit (preview mode)."""
        # Read body
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)
        except (ValueError, json.JSONDecodeError):
            self.send_json_error(400, 'bad_json', 'malformed JSON body')
            return

        if not isinstance(data.get('edits'), dict):
            self.send_json_error(400, 'bad_json', 'edits must be a dict')
            return

        # Find unit
        watcher = self.server.watcher
        if unit_name not in watcher.units:
            self.send_json_error(404, 'not_found', f'unit {unit_name} not found')
            return

        unit = watcher.units[unit_name]

        # Plan edits
        try:
            edits = plan_edits(unit, data.get('edits', {}))
        except EditError as e:
            self.send_json_error(400, e.reason, e.detail or e.reason)
            return

        # Run preflight
        pf = preflight(unit, edits, watcher, self.server.port, unit.path.rsplit('/', 1)[0])
        if not pf['ok']:
            self.send_response(422)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            prov_line = provenance_line(edits, datetime.now(timezone.utc))
            prov_bytes = splice(unit.raw, edits, prov_line)
            diff_text = unified_diff_text(unit.raw, prov_bytes, unit.name)
            response = {
                'error': 'preflight_failed',
                'checks': pf['checks'],
                'diff': diff_text
            }
            self.wfile.write(json.dumps(response).encode('utf-8'))
            return

        # Compute confirm hash
        confirm = compute_confirm(unit_name, unit.raw, edits)

        # Build diff
        prov_line = provenance_line(edits, datetime.now(timezone.utc))
        prov_bytes = splice(unit.raw, edits, prov_line)
        diff_text = unified_diff_text(unit.raw, prov_bytes, unit.name)

        # Return preview
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        response = {
            'unit': unit_name,
            'edits': [{'field': e.field, 'flag': e.flag, 'old': e.old_text, 'new': e.new_text}
                     for e in edits],
            'diff': diff_text,
            'confirm': confirm,
            'preflight': pf,
            'notices': [],
            'provenance_preview': f"# roundhouse: {prov_line}"
        }
        self.wfile.write(json.dumps(response).encode('utf-8'))

    def handle_rollout(self, unit_name: str):
        """Handle POST /api/units/<name>/rollout (apply)."""
        # Read body
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)
        except (ValueError, json.JSONDecodeError):
            self.send_json_error(400, 'bad_json', 'malformed JSON body')
            return

        # Get engine
        engine = self.server.rollout_engine
        if not engine:
            self.send_json_error(500, 'no_engine', 'rollout engine not initialized')
            return

        # Find unit
        watcher = self.server.watcher
        if unit_name not in watcher.units:
            self.send_json_error(404, 'not_found', f'unit {unit_name} not found')
            return

        unit = watcher.units[unit_name]

        # [RETIRED] is a structural exclusion (§9.5): it must answer 422 here, before the
        # staleness check, or a fabricated `confirm` masks it as 409 preview_stale.
        retired_check = preflight_retired(unit)
        if not retired_check['ok']:
            self.send_response(422)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({
                'error': 'preflight_failed',
                'checks': [retired_check],
            }).encode('utf-8'))
            return

        # Plan edits
        try:
            edits = plan_edits(unit, data.get('edits', {}))
        except EditError as e:
            self.send_json_error(400, e.reason, e.detail or e.reason)
            return

        # Check confirm matches (E5)
        confirm = data.get('confirm', '')
        computed = compute_confirm(unit_name, unit.raw, edits)
        if confirm != computed:
            self.send_response(409)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            response = {
                'error': 'preview_stale',
                'detail': 'unit file or edits changed since preview; re-preview'
            }
            self.wfile.write(json.dumps(response).encode('utf-8'))
            return

        # Start rollout
        try:
            rollout = engine.start_rollout(unit_name, edits, confirm)
            self.send_response(202)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            response = {'rollout_id': rollout['rollout_id']}
            self.wfile.write(json.dumps(response).encode('utf-8'))
        except ActuationError as e:
            if 'rollout_in_progress' in str(e):
                self.send_response(409)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                response = {
                    'error': 'rollout_in_progress',
                    'rollout_id': engine.current.get('rollout_id') if engine.current else None
                }
                self.wfile.write(json.dumps(response).encode('utf-8'))
            else:
                self.send_json_error(400, 'rollout_error', str(e))

    def handle_rollback(self, rollout_id: str):
        """Handle POST /api/rollouts/<id>/rollback."""
        engine = self.server.rollout_engine
        if not engine:
            self.send_json_error(500, 'no_engine', 'rollout engine not initialized')
            return

        rollout = engine.rollouts.get(rollout_id)
        if not rollout:
            self.send_json_error(404, 'not_found', f'rollout {rollout_id} not found')
            return

        if rollout.get('phase') != 'failed' or not rollout.get('rollback', {}).get('offered'):
            self.send_response(409)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            response = {'error': 'not_rollbackable'}
            self.wfile.write(json.dumps(response).encode('utf-8'))
            return

        try:
            engine.rollback(rollout_id)
            self.send_response(202)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({'rollout_id': rollout_id}).encode('utf-8'))
        except ActuationError as e:
            self.send_json_error(400, 'rollback_error', str(e))

    def handle_dismiss(self, rollout_id: str):
        """Handle POST /api/rollouts/<id>/dismiss."""
        engine = self.server.rollout_engine
        if not engine:
            self.send_json_error(500, 'no_engine', 'rollout engine not initialized')
            return

        rollout = engine.rollouts.get(rollout_id)
        if not rollout:
            self.send_json_error(404, 'not_found', f'rollout {rollout_id} not found')
            return

        if rollout.get('phase') != 'failed' or not rollout.get('rollback', {}).get('offered'):
            self.send_response(409)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            response = {'error': 'not_dismissable'}
            self.wfile.write(json.dumps(response).encode('utf-8'))
            return

        engine.dismiss(rollout_id)
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps({'ok': True}).encode('utf-8'))

    def send_json_error(self, status: int, error: str, detail: str = None):
        """Send a JSON error response."""
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        response = {'error': error}
        if detail:
            response['detail'] = detail
        self.wfile.write(json.dumps(response).encode('utf-8'))

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
        snapshot = self.server.take_snapshot()
        self.send_json(snapshot)

    def serve_unit_detail(self, unit_name):
        """Serve /api/units/<name>: the list row (a) plus the parsed-file detail (b)."""
        watcher = self.server.watcher
        snapshot = self.server.take_snapshot()

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
        self.send_json(_build_port_board(self.server.take_snapshot()))

    def serve_deployments(self):
        """Serve /api/deployments (§4.4d).

        The record body comes from build_deployment() -- the parser owns the spine shape.
        Only the live half (enable state, roster, memory) is layered on from the snapshot.
        RETIRED units still emit a record with retired:true and roster.state null;
        consumers filter on `retired` (they are never placement targets).
        """
        watcher = self.server.watcher
        snapshot = self.server.take_snapshot()
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

    def serve_rollout(self, rollout_id: str):
        """Serve GET /api/rollouts/<id> (§6): the rollout record, or 404.

        Read route, so unauthenticated like every other GET. `/rollback` and `/dismiss`
        are POST-only and never reach here (do_GET routes the whole suffix as an id).
        """
        if rollout_id.endswith('/rollback') or rollout_id.endswith('/dismiss'):
            self.error_405()          # POST-only routes (§6 status doctrine)
            return
        engine = getattr(self.server, 'rollout_engine', None)
        rollout = engine.rollouts.get(rollout_id) if engine else None
        if not rollout:
            self.send_json_error(404, 'not_found', f'no rollout {rollout_id}')
            return
        self.send_json(rollout_public_record(rollout))

    def serve_mem(self):
        """Serve /api/mem: measured peak rows (§6 schema) plus the current per-unit
        number the UI shows, so a caller can tell measurement from estimate."""
        watcher = self.server.watcher
        snapshot = self.server.take_snapshot()
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
            snapshot = self.server.take_snapshot()
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

    def __init__(self, host_port, handler_class, watcher, event_bus, port,
                 watcher_lock=None, rollout_engine=None):
        self.watcher = watcher
        self.event_bus = event_bus
        self.port = port
        self.rollout_engine = rollout_engine
        # snapshot() reads AND writes watcher state (rung cache commit is
        # elsewhere, but _state mutates under the sensing threads), so every
        # handler read must take the same lock the threads use. Sockets are
        # written OUTSIDE the lock: take_snapshot returns a plain dict.
        self.watcher_lock = watcher_lock or threading.Lock()
        super().__init__(host_port, handler_class)

    def take_snapshot(self):
        with self.watcher_lock:
            snapshot = self.watcher.snapshot()
            snapshot['mode'] = 'actuate' if ACTUATE_ARMED else 'read-only'
            if self.rollout_engine and self.rollout_engine.current:
                snapshot['rollout'] = rollout_public_record(self.rollout_engine.current)
            else:
                snapshot['rollout'] = None
            return snapshot


# ===== SECTION E: ACTUATION (armed only by --actuate; run_actuate + run_git are the only mutation gateways) =====

def rollout_public_record(rollout: dict) -> dict:
    """The §4.3 wire shape of a rollout record.

    One implementation for both consumers (snapshot merge and GET /api/rollouts/<id>) so
    they cannot drift; `old_raw` (the in-memory pre-edit bytes) is never serialized.
    """
    return {
        'rollout_id': rollout['rollout_id'],
        'unit': rollout['unit'],
        'phase': rollout['phase'],
        'detail': rollout['detail'],
        'edits': rollout['edits'],
        'was_active': rollout['was_active'],
        'commit': rollout['commit'],
        'restored': rollout['restored'],
        'failure': rollout['failure'],
        'rollback': rollout['rollback'],
        'started_at': rollout['started_at'],
        'updated_at': rollout['updated_at'],
    }


# Module globals
ACTUATE_ARMED = False
ACTUATE_SYSTEMCTL_VERBS = {"stop", "start", "daemon-reload"}
GIT_VERBS = {"version", "rev-parse", "status", "ls-files", "log", "show",
             "diff", "add", "commit", "revert"}
GIT_MUTATING_VERBS = {"add", "commit", "revert"}
GIT_FORBIDDEN_TOKENS = {"push", "pull", "fetch", "remote", "clone", "init",
                        "checkout", "reset", "clean", "submodule"}
TOKEN_PATH = os.path.expanduser("~/.config/roundhouse/token")
TOKEN = None
HEADROOM_BYTES = 1 * 1024 * 1024 * 1024  # 1 GiB

# Exceptions
class ActuationError(Exception):
    """Raised when actuation is not armed or a command fails."""
    pass

class EditError(Exception):
    """Raised when edit validation fails."""
    def __init__(self, reason: str, detail: str = None):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)

class VerifyError(Exception):
    """Raised when splice verification fails."""
    def __init__(self, reason: str, detail: str = None):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)

# Gateway functions
def run_actuate(argv: List[str], units: Dict[str, 'UnitFile'], timeout: float = 90) -> str:
    """Run a systemctl command via subprocess. Raises ActuationError unless armed and conditions met."""
    if not ACTUATE_ARMED:
        raise ActuationError("actuation not armed (--actuate not passed)")

    # Check exact shape
    if argv == ["systemctl", "--user", "daemon-reload"]:
        pass  # Valid standalone command
    elif len(argv) == 5 and argv[0] == "systemctl" and argv[1] == "--user" and argv[2] in ACTUATE_SYSTEMCTL_VERBS and argv[3] == "--":
        unit = argv[4]
        if unit not in units:
            raise ActuationError(f"{unit} not in selected units")
        if units[unit].retired:
            raise ActuationError(f"{unit} is [RETIRED]")
    else:
        raise ActuationError(f"run_actuate: invalid shape {argv}")

    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            raise ActuationError(f"{' '.join(argv)} rc={result.returncode}: {result.stderr.strip()}")
        return result.stdout
    except subprocess.TimeoutExpired:
        raise ActuationError(f"{' '.join(argv)} timeout after {timeout}s")

def run_git(args: List[str], unit_dir: str, timeout: float = 30, bootstrap: bool = False) -> subprocess.CompletedProcess:
    """Run a git command. Raises ActuationError unless armed and conditions met."""
    if not ACTUATE_ARMED and not bootstrap:
        raise ActuationError("actuation not armed")

    if bootstrap:
        if args[0] not in ("version", "rev-parse", "status"):
            raise ActuationError(f"bootstrap: only read-only verbs allowed, got {args}")
    else:
        if not args or args[0] not in GIT_VERBS:
            raise ActuationError(f"git verb {args[0] if args else 'none'} not in allowlist")
        if any(t in args for t in GIT_FORBIDDEN_TOKENS):
            raise ActuationError(f"git forbidden token found in {args}")

        if args[0] == "add":
            if not (len(args) == 3 and args[1] == "--" and "/" not in args[2]):
                raise ActuationError(f"git add must be exactly ['add', '--', 'basename'], got {args}")
        elif args[0] == "revert":
            if not ((len(args) == 3 and args[1:3] == ["--no-edit", args[2]]) or args == ["revert", "--abort"]):
                raise ActuationError(f"git revert must be ['revert', '--no-edit', sha] or ['revert', '--abort'], got {args}")

    # Build command. `git version` answers "is git on PATH?" and must not depend on the
    # unit dir existing — with -C a missing/!dir unit_dir makes git exit 128, which the
    # startup check would misread as "git is not installed" (§2.4 step 1 vs step 2).
    if args == ["version"]:
        cmd = ["git", "version"]
    else:
        cmd = ["git", "-C", unit_dir] + args

    # Set author env for mutating verbs
    env = os.environ.copy()
    if args and args[0] in GIT_MUTATING_VERBS:
        hostname = socket.gethostname()
        env["GIT_AUTHOR_NAME"] = "roundhouse"
        env["GIT_AUTHOR_EMAIL"] = f"roundhouse@{hostname}"
        env["GIT_COMMITTER_NAME"] = "roundhouse"
        env["GIT_COMMITTER_EMAIL"] = f"roundhouse@{hostname}"

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return result
    except subprocess.TimeoutExpired:
        raise ActuationError(f"git {args[0]} timeout after {timeout}s")
    except FileNotFoundError:
        # git binary absent from PATH (E11): a gateway-level refusal, never a traceback.
        raise ActuationError("git not found on PATH")

def git_startup_check(unit_dir: str, unit_names: Optional[List[str]] = None):
    """Check git is available and repo is valid. Raises ActuationError on failure."""
    # Check git --version (bootstrap mode)
    try:
        result = run_git(["version"], unit_dir, timeout=5, bootstrap=True)
        if result.returncode != 0:
            print("--actuate requires git on PATH (read-only mode does not); install git and relaunch", file=sys.stderr)
            sys.exit(2)
    except ActuationError:
        print("--actuate requires git on PATH (read-only mode does not); install git and relaunch", file=sys.stderr)
        sys.exit(2)

    # Check repo present
    result = run_git(["rev-parse", "--show-toplevel"], unit_dir, bootstrap=True)
    if result.returncode != 0:
        print_git_init_instructions(unit_dir, unit_names)
        sys.exit(2)

    repo_root = result.stdout.strip()
    if os.path.realpath(repo_root) != os.path.realpath(unit_dir):
        print_git_init_instructions(unit_dir, unit_names)
        sys.exit(2)

    # Check worktree clean
    result = run_git(["status", "--porcelain", "--untracked-files=no"], unit_dir, bootstrap=True)
    if result.stdout.strip():
        print(f"""--actuate refused: the unit-dir git worktree has uncommitted changes to tracked files
(a previous rollout may have died mid-apply):
  {result.stdout.strip()}
Inspect:   git -C {unit_dir} diff
Resolve:   commit the change, or discard it with
           git -C {unit_dir} restore -- <file>
Then run:  systemctl --user daemon-reload
Relaunch with --actuate when the worktree is clean.""", file=sys.stderr)
        sys.exit(2)

    # Step 4: warn (never fail) about a missing/incomplete .gitignore (E3).
    gitignore = os.path.join(unit_dir, '.gitignore')
    try:
        with open(gitignore, 'r') as f:
            ignore_text = f.read()
    except OSError:
        ignore_text = None
    if ignore_text is None:
        print(f"warning: {gitignore} is missing; add '*.bak*' and '*.roundhouse-tmp' to it",
              file=sys.stderr)
    elif '*.bak*' not in ignore_text:
        print(f"warning: {gitignore} does not cover '*.bak*'", file=sys.stderr)

def print_git_init_instructions(unit_dir: str, unit_names: Optional[List[str]] = None):
    """Print the git init instructions message.

    `unit_names` is the SELECTED unit set (E3: scoped tracking, never `git add -A`).
    Falling back to every .service file in the dir would tell the operator to commit
    100+ unrelated desktop units on a real host.
    """
    if unit_names is not None:
        units = sorted(unit_names)
    else:
        units = []
        if os.path.isdir(unit_dir):
            for f in os.listdir(unit_dir):
                if f.endswith('.service'):
                    units.append(f)
        units.sort()

    unit_names = " ".join(units) if units else "<unit1>.service <unit2>.service ... <unitN>.service"

    print(f"""--actuate refused: {unit_dir} is not a git repository (contract §git).
Roundhouse never runs `git init` itself. Initialize it as the operator, once:

  cd {unit_dir}
  git init
  printf '%s\\n' '*.bak*' '*.roundhouse-tmp' > .gitignore
  git add .gitignore {unit_names}
  git commit -m "roundhouse baseline: {len(units)} managed units"

Then relaunch with --actuate.""", file=sys.stderr)

def ensure_token() -> str:
    """Ensure token exists and is properly permissioned. Returns the token."""
    global TOKEN, TOKEN_PATH

    # Create directory if needed
    token_dir = os.path.dirname(TOKEN_PATH)
    os.makedirs(token_dir, mode=0o700, exist_ok=True)

    # Check if file exists and is readable
    if os.path.exists(TOKEN_PATH):
        # Check permissions
        mode = os.stat(TOKEN_PATH).st_mode
        if mode & 0o077:
            print(f"token file {TOKEN_PATH} is group/world-readable; chmod 600 it and relaunch", file=sys.stderr)
            sys.exit(2)

        # Read token
        with open(TOKEN_PATH, 'r') as f:
            token_content = f.read().strip()

        if not token_content:
            # Empty file, regenerate
            token = secrets.token_urlsafe(32)
            _atomic_write(TOKEN_PATH, token.encode() + b'\n')
            os.chmod(TOKEN_PATH, 0o600)
            print(f"generated bearer token at {TOKEN_PATH} — paste its contents into the UI", file=sys.stderr)
            TOKEN = token
            return token

        TOKEN = token_content
        return token_content
    else:
        # Generate new token
        token = secrets.token_urlsafe(32)
        _atomic_write(TOKEN_PATH, token.encode() + b'\n')
        os.chmod(TOKEN_PATH, 0o600)
        print(f"generated bearer token at {TOKEN_PATH} — paste its contents into the UI", file=sys.stderr)
        TOKEN = token
        return token

def check_bearer(handler) -> Optional[int]:
    """Check authorization header. Returns HTTP status to fail with, or None if ok."""
    if not ACTUATE_ARMED:
        return 403

    auth_header = handler.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return 401

    token = auth_header[7:]  # Strip "Bearer "
    if not hmac.compare_digest(token, TOKEN or ''):
        return 401

    return None

def parse_timeout_start_sec(val: Optional[str]) -> int:
    """Parse TimeoutStartSec value to seconds."""
    if not val or not val.strip():
        return 90

    val = val.strip()

    # Try plain seconds
    if val.isdigit():
        return int(val)

    # Try with unit: <digits><unit>
    m = re.match(r'^(\d+)\s*(s|sec|min)?$', val)
    if m:
        num = int(m.group(1))
        unit = m.group(2)
        if unit in ('min',):
            return num * 60
        return num

    # Default
    return 90

# Edit and splice engine
@dataclass
class Edit:
    """A field edit to be spliced into a file."""
    field: str              # canonical name or "unknown:<flag-text>"
    flag: str               # flag as written (e.g. "-c")
    old_text: str           # current decoded value
    new_text: str           # submitted value
    span: tuple             # (start, end) of value bytes in raw
    quote: str              # '' or "'"

def plan_edits(unit: UnitFile, changes: Dict[str, str]) -> List[Edit]:
    """Validate and plan edits from user changes."""
    if not unit.exec_start:
        raise EditError("no_exec_start", "unit has no ExecStart")

    profile = extract_param_profile(unit.exec_start.engine_argv)
    edits = []

    for key, new_value in changes.items():
        # Resolve the span
        span_info = None
        flag_text = None
        old_text = None
        quote = ''

        # Known field
        if key in profile['spans']:
            span_info = profile['spans'][key]
            if 'value' not in span_info:
                raise EditError("field_not_editable", f"field {key} has no value span")
            value_span = span_info['value']
            # E12/§3.1: Edit.flag is the flag AS WRITTEN (`-c`), because commit_message
            # uses the flag spelling while provenance_line uses the canonical field name.
            # `key` here is the canonical name, so read the flag back from its own span.
            flag_span = span_info.get('flag')
            if flag_span:
                flag_text = unit.raw[flag_span[0]:flag_span[1]].decode('utf-8', errors='replace')
            else:
                flag_text = key
        else:
            # Unknown field
            if not key.startswith("unknown:"):
                raise EditError("field_not_editable", f"unknown field {key}")
            flag_text = key[8:]  # Remove "unknown:" prefix

            # Find matching unknown flag
            found = False
            for unk in profile['unknown_flags']:
                if unk['flag'] == flag_text and unk['value_span']:
                    span_info = unk
                    value_span = unk['value_span']
                    found = True
                    break

            if not found:
                raise EditError("field_not_editable", f"flag {flag_text} not found or has no value")

        # Get old text
        start, end = value_span
        old_raw = unit.raw[start:end]

        # Determine quoting style
        if old_raw.startswith(b"'"):
            quote = "'"
            old_bytes = old_raw[1:-1] if old_raw.endswith(b"'") else old_raw[1:]
        else:
            quote = ''
            old_bytes = old_raw

        old_text = old_bytes.decode('utf-8', errors='replace')

        # Byte safety validate
        if quote == '':
            # Unquoted: must match alphanumeric + safe chars
            if not re.match(r'^[A-Za-z0-9._:/=,+\-]*$', new_value):
                raise EditError("invalid_value", f"unquoted value contains invalid chars: {new_value}")
        elif quote == "'":
            # Single-quoted: no ' or \ or newline
            if "'" in new_value or "\\" in new_value or "\n" in new_value:
                raise EditError("invalid_value", "single-quoted value cannot contain quotes, backslashes, or newlines")

        # Remote scheme check
        if "://" in new_value:
            raise EditError("remote_scheme", f"value contains remote scheme: {new_value}")

        # No-op check
        if new_value == old_text:
            continue  # Skip this edit

        edits.append(Edit(
            field=key,
            flag=flag_text,
            old_text=old_text,
            new_text=new_value,
            span=value_span,
            quote=quote
        ))

    if not edits:
        raise EditError("no_change", "no edits after filtering")

    return edits

def render_value_bytes(edit: Edit) -> bytes:
    """Render an edit as raw bytes (quoted as needed)."""
    return (edit.quote + edit.new_text + edit.quote).encode('utf-8')

def splice(raw: bytes, edits: List[Edit], provenance: str) -> bytes:
    """Splice edits into raw bytes at the recorded spans."""
    # Sort edits by span start, descending (so earlier offsets stay valid)
    sorted_edits = sorted(edits, key=lambda e: e.span[0], reverse=True)

    # Assert spans are disjoint
    for i, e1 in enumerate(sorted_edits):
        for e2 in sorted_edits[i+1:]:
            if not (e1.span[1] <= e2.span[0] or e2.span[1] <= e1.span[0]):
                raise EditError("overlapping_spans", f"spans {e1.span} and {e2.span} overlap")

    # Apply edits
    new_raw = raw
    for edit in sorted_edits:
        start, end = edit.span
        new_raw = new_raw[:start] + render_value_bytes(edit) + new_raw[end:]

    # Append provenance
    if new_raw and not new_raw.endswith(b'\n'):
        new_raw += b'\n'
    new_raw += b'# roundhouse: ' + provenance.encode('utf-8') + b'\n'

    return new_raw

def assert_span_invariants(unit: UnitFile) -> None:
    """Assert MVP1 span invariants plus the splice-only-affects-spans invariant."""
    raw = unit.raw

    # (1) Lines reassemble to raw
    line_bytes = b''.join(raw[l.start:l.end] for l in unit.lines)
    assert line_bytes == raw, "line invariant failed"

    # (2) ExecStart tokens are accurate
    if unit.exec_start:
        for token in unit.exec_start.tokens:
            assert raw[token.start:token.end] == token.raw, f"token invariant failed for {token.text}"

    # (3) Profile spans are accurate (simplified check)
    if unit.exec_start:
        profile = extract_param_profile(unit.exec_start.engine_argv)
        for field, span_dict in profile['spans'].items():
            if 'value' in span_dict:
                start, end = span_dict['value']
                assert start < end and end <= len(raw), f"span {field} out of bounds"

def verify_splice(old_unit: UnitFile, new_raw: bytes, edits: List[Edit], provenance: str) -> UnitFile:
    """Verify a splice is correct and return the new parsed unit."""
    # Parse the new file
    path = old_unit.path
    new_unit = parse_unit(path, new_raw)

    # Check (a): Profile equality except edited fields and spans
    if old_unit.exec_start and new_unit.exec_start:
        old_profile = extract_param_profile(old_unit.exec_start.engine_argv)
        new_profile = extract_param_profile(new_unit.exec_start.engine_argv)

        # Build normalized profile dicts (removing spans and raw_argv)
        def normalize_profile(p):
            d = {k: v for k, v in p.items() if k not in ('spans', 'raw_argv')}
            d['unknown_flags'] = [(f['flag'], f['value']) for f in p['unknown_flags']]
            return d

        old_norm = normalize_profile(old_profile)
        new_norm = normalize_profile(new_profile)

        # Check edited fields are present, unedited fields are identical
        edited_fields = {e.field for e in edits}
        for key in old_norm:
            if key not in edited_fields:
                if old_norm.get(key) != new_norm.get(key):
                    raise VerifyError("profile_changed", f"unedited field {key} changed")

    # Check (b): Comments unchanged except for provenance
    old_comments = [c['text'] for c in old_unit.comments]
    new_comments = [c['text'] for c in new_unit.comments]
    expected_prov = '# roundhouse: ' + provenance
    if new_comments != old_comments + [expected_prov]:
        raise VerifyError("comments_changed", "comments don't match expected")

    # Check (c): Span invariants
    assert_span_invariants(new_unit)

    return new_unit

def unified_diff_text(old: bytes, new: bytes, name: str) -> str:
    """Generate unified diff text."""
    old_lines = old.decode('utf-8', errors='replace').splitlines(keepends=True)
    new_lines = new.decode('utf-8', errors='replace').splitlines(keepends=True)

    diff = difflib.unified_diff(old_lines, new_lines, fromfile=f"{name} (old)", tofile=f"{name} (new)")
    return ''.join(diff)

def provenance_line(edits: List[Edit], now_utc: datetime) -> str:
    """Generate a provenance line."""
    iso_time = now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')
    # Use canonical field names, in file order (order by span start)
    sorted_edits = sorted(edits, key=lambda e: e.span[0])
    edits_text = ', '.join(f"{e.field} {e.old_text} -> {e.new_text}" for e in sorted_edits)
    return f"{iso_time} {edits_text} via UI"

def commit_message(unit_name: str, edits: List[Edit]) -> str:
    """Generate a git commit message."""
    unit_stem = unit_name.replace('.service', '')
    # Use flag spelling, in file order
    sorted_edits = sorted(edits, key=lambda e: e.span[0])
    edits_text = '; '.join(f"{e.flag} {e.old_text} -> {e.new_text}" for e in sorted_edits)
    return f"roundhouse: {unit_stem} {edits_text}"

def compute_confirm(unit_name: str, old_raw: bytes, edits: List[Edit]) -> str:
    """Compute the confirmation hash per §E5."""
    old_hash = hashlib.sha256(old_raw).hexdigest()

    edits_list = [
        [e.field, e.old_text, e.new_text]
        for e in sorted(edits, key=lambda x: x.field)
    ]

    data = {
        "unit": unit_name,
        "base": old_hash,
        "edits": edits_list
    }

    canonical_json = json.dumps(data, separators=(',', ':'), sort_keys=True)
    return hashlib.sha256(canonical_json.encode()).hexdigest()

def _atomic_write(path: str, data: bytes) -> None:
    """Atomically write data to path via tmp+fsync+replace."""
    dir_path = os.path.dirname(path) or '.'

    # Write to tmp file in same directory
    tmp_path = path + '.roundhouse-tmp'
    with open(tmp_path, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())

    # Atomic replace
    os.replace(tmp_path, path)

    # Fsync directory (best effort)
    try:
        fd = os.open(dir_path, os.O_RDONLY)
        os.fsync(fd)
        os.close(fd)
    except Exception:
        pass


# ===== SECTION E PART 2: PREFLIGHT & ROLLOUT ENGINE =====

# Rollout phases
ROLLOUT_PHASES = ("preflight", "applying", "reloading", "starting", "watching",
                  "done", "failed", "rolling_back", "rolled_back", "rollback_failed")


def preflight_retired(unit: UnitFile) -> Dict:
    """Check if unit is retired."""
    if unit.retired:
        return {
            "ok": False,
            "check": "retired",
            "detail": f"unit is {unit.retired_note or '[RETIRED]'} — structurally excluded from every actuation path"
        }
    return {"ok": True, "check": "retired"}


def preflight_git(unit: UnitFile, unit_dir: str) -> Dict:
    """Check if unit file is tracked in git."""
    try:
        result = run_git(["ls-files", "--error-unmatch", "--", unit.name], unit_dir)
        if result.returncode == 0:
            # Also check clean worktree for this file
            result2 = run_git(["status", "--porcelain", "--", unit.name], unit_dir)
            if result2.stdout.strip():
                return {
                    "ok": False,
                    "check": "git",
                    "detail": f"unit file {unit.name} has uncommitted changes"
                }
            return {"ok": True, "check": "git"}
        else:
            return {
                "ok": False,
                "check": "git",
                "detail": f"unit file is not tracked in the unit-dir git repo; run: git -C {unit_dir} add {unit.name} && git commit -m 'track {unit.name}'"
            }
    except Exception as e:
        return {"ok": False, "check": "git", "detail": str(e)}


def preflight_port(unit: UnitFile, edits: List[Edit], watcher: 'Watcher', self_port: int) -> Dict:
    """Check if new port collides with existing claims."""
    # Find if port is being edited
    port_edit = None
    for edit in edits:
        if edit.field == "port":
            port_edit = edit
            break

    if not port_edit:
        return {"ok": True, "check": "port"}

    new_port = int(port_edit.new_text)

    # Check against all other units and self
    if new_port == self_port:
        return {
            "ok": False,
            "check": "port",
            "detail": f"port {new_port} is claimed by roundhouse (self)"
        }

    # Build list of other claimants
    claimants = []
    snapshot = watcher.snapshot()

    for u in snapshot.get('units', []):
        if u['unit'] == unit.name:
            continue  # Skip self
        if u.get('port') == new_port and not u.get('retired'):
            enabled_str = "enabled" if u.get('enabled') else "disabled"
            rung_str = u.get('rung', 'OFF')
            claimants.append(f"{u['unit']} ({enabled_str}, {rung_str})")

    if claimants:
        detail = f"port {new_port} already declared by {', '.join(claimants)}"
        return {"ok": False, "check": "port", "detail": detail}

    return {"ok": True, "check": "port"}


def preflight_memory(unit: UnitFile, edits: List[Edit], watcher: 'Watcher',
                     meminfo_reader=None) -> Dict:
    """Check if memory estimate fits within budget."""
    # Determine if any memory-relevant field is being edited
    memory_fields = {"ctx", "cache_type_k", "cache_type_v", "model_path"}
    has_memory_edit = any(e.field in memory_fields for e in edits)

    if not has_memory_edit:
        return {"ok": True, "check": "memory"}

    if not unit.exec_start:
        return {"ok": True, "check": "memory"}

    # Build new profile with edits applied
    old_profile = extract_param_profile(unit.exec_start.engine_argv)
    new_profile = dict(old_profile)

    for edit in edits:
        # §5: the estimate runs on the profile OVERLAID with the edits — model_path very
        # much included, or a swap to a bigger GGUF is sized against the old model and the
        # budget check the swap exists for never fires.
        if edit.field == "ctx":
            try:
                new_profile["ctx"] = int(edit.new_text)
            except ValueError:
                pass
        elif edit.field in ("cache_type_k", "cache_type_v", "model_path"):
            new_profile[edit.field] = edit.new_text

    # Estimate memory
    store = getattr(watcher, 'mem_store', None)
    estimate_bytes = None
    estimate_source = None

    # Try exact measurement first
    model_path = new_profile.get('model_path')
    if model_path and unit.exec_start:
        try:
            file_id = f"sz{os.stat(model_path).st_size}:mt{int(os.stat(model_path).st_mtime)}"
            mem = store.lookup(unit.name, file_id, new_profile.get('ctx')) if store else None
            if mem:
                estimate_bytes = mem['bytes']
                estimate_source = "measured"
        except Exception:
            pass

    # Fallback to formula
    if estimate_bytes is None:
        if model_path:
            try:
                size = os.path.getsize(model_path)
                estimate_bytes = int(size * 1.10 + 1.5 * 1024**3)
                estimate_source = "formula"
            except Exception:
                pass

        if estimate_bytes is None:
            estimate_bytes = int(9 * 1024**3)  # Default 9 GiB
            estimate_source = "default"

    # Check model_path exists if being edited
    for edit in edits:
        if edit.field == "model_path":
            if not os.path.exists(edit.new_text):
                return {
                    "ok": False,
                    "check": "memory",
                    "detail": f"model file not found: {edit.new_text}"
                }

    # Read MemAvailable
    mem_available = None
    if meminfo_reader:
        info = meminfo_reader()
        mem_available = info.get('MemAvailable', 0) * 1024
    else:
        try:
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    if line.startswith('MemAvailable:'):
                        mem_available = int(line.split()[1]) * 1024
                        break
        except Exception:
            pass

    if mem_available is None:
        mem_available = 0

    # Freed memory from stopping this unit
    freed_bytes = 0
    snapshot = watcher.snapshot()
    for u in snapshot.get('units', []):
        if u['unit'] == unit.name:
            mem = u.get('mem', {})
            if mem and 'bytes' in mem:
                freed_bytes = mem['bytes']
            break

    budget = mem_available + freed_bytes
    headroom = HEADROOM_BYTES

    if estimate_bytes + headroom > budget:
        return {
            "ok": False,
            "check": "memory",
            "detail": f"estimated {estimate_bytes/(1024**3):.1f} GiB ({estimate_source}), "
                     f"+ {headroom/(1024**3):.1f} GiB headroom exceeds budget {budget/(1024**3):.1f} GiB "
                     f"(MemAvailable {mem_available/(1024**3):.1f} GiB + freed {freed_bytes/(1024**3):.1f} GiB)",
            "estimate_bytes": estimate_bytes,
            "estimate_source": estimate_source,
            "mem_available_bytes": mem_available,
            "freed_bytes": freed_bytes,
            "headroom_bytes": headroom,
            "budget_bytes": budget
        }

    return {"ok": True, "check": "memory"}


def preflight(unit: UnitFile, edits: List[Edit], watcher: 'Watcher',
              self_port: int, unit_dir: str, meminfo_reader=None) -> Dict:
    """Run all preflight checks."""
    checks = []

    # Order: retired, git, port, memory
    retired_check = preflight_retired(unit)
    checks.append(retired_check)
    if not retired_check["ok"]:
        return {"ok": False, "checks": checks}

    git_check = preflight_git(unit, unit_dir)
    checks.append(git_check)
    if not git_check["ok"]:
        return {"ok": False, "checks": checks}

    port_check = preflight_port(unit, edits, watcher, self_port)
    checks.append(port_check)
    if not port_check["ok"]:
        return {"ok": False, "checks": checks}

    memory_check = preflight_memory(unit, edits, watcher, meminfo_reader)
    checks.append(memory_check)

    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "checks": checks}


class RolloutEngine:
    """Manages rollout state machine and worker thread."""

    def __init__(self, watcher: 'Watcher', units: Dict[str, UnitFile],
                 unit_dir: str, self_port: int, event_bus: EventBus,
                 watcher_lock: threading.Lock):
        self.watcher = watcher
        self.units = units
        self.unit_dir = unit_dir
        self.self_port = self_port
        self.event_bus = event_bus
        self.watcher_lock = watcher_lock

        self.current = None
        self.rollouts = {}
        self.counter = 0

    def start_rollout(self, unit_name: str, edits: List[Edit], confirm: str) -> Dict:
        """Start a new rollout. Returns the rollout record."""
        with self.watcher_lock:
            if self.current and self.current.get('phase') not in ('done', 'rolled_back', 'rollback_failed'):
                if self.current.get('phase') == 'failed' and self.current.get('rollback', {}).get('offered'):
                    raise ActuationError("rollout_in_progress: rollback offered")
                raise ActuationError("rollout_in_progress")

            unit = self.units.get(unit_name)
            if not unit:
                raise ActuationError(f"unit {unit_name} not found")

            # Create rollout record
            self.counter += 1
            rollout_id = f"ro-{int(time.time())}-{self.counter}"
            now = time.time()

            rollout = {
                "rollout_id": rollout_id,
                "unit": unit_name,
                "phase": "preflight",
                "detail": "checking prerequisites",
                "edits": [{"field": e.field, "flag": e.flag, "old": e.old_text, "new": e.new_text} for e in edits],
                "was_active": None,
                "commit": None,
                "restored": False,
                "failure": None,
                "rollback": None,
                "started_at": now,
                "updated_at": now,
                "old_raw": unit.raw,
            }

            self.current = rollout
            self.rollouts[rollout_id] = rollout

            # Spawn worker thread
            threading.Thread(
                target=self._run_rollout,
                args=(rollout_id, unit_name, edits, confirm),
                name="rollout",
                daemon=True
            ).start()

            return rollout

    def _run_rollout(self, rollout_id: str, unit_name: str, edits: List[Edit], confirm: str):
        """Worker thread for rollout execution."""
        rollout = self.rollouts.get(rollout_id)
        if not rollout:
            return

        try:
            unit = self.units[unit_name]

            # Preflight phase
            self._update_phase(rollout_id, "preflight", "checking prerequisites")
            pf = preflight(unit, edits, self.watcher, self.self_port, self.unit_dir)
            if not pf["ok"]:
                self._fail_rollout(rollout_id, "preflight", "preflight", detail="pre-flight checks failed")
                return

            # Recompute confirm
            computed_confirm = compute_confirm(unit_name, unit.raw, edits)
            if computed_confirm != confirm:
                self._fail_rollout(rollout_id, "preflight", "preview_stale", detail="confirm mismatch (stale preview)")
                return

            # Capture was_active and the OLD deployment's ExecMainStartTimestamp — the
            # `watching` phase uses it to tell the new process from a stale sample of the
            # one this rollout is about to stop.
            was_active = False
            prior_start_ts = None
            with self.watcher_lock:
                snapshot = self.watcher.snapshot()
            for u in snapshot.get('units', []):
                if u['unit'] == unit_name:
                    rung = u.get('rung', 'OFF')
                    was_active = rung in ('STARTING', 'LOADING', 'READY', 'BUSY')
                    prior_start_ts = u.get('since')
                    break

            with self.watcher_lock:
                rollout['was_active'] = was_active
                rollout['updated_at'] = time.time()

            # Applying phase
            self._update_phase(rollout_id, "applying", "stopping unit")

            try:
                if was_active:
                    try:
                        self._stop_unit(unit_name)
                    except Exception as e:
                        self._fail_rollout(rollout_id, "applying", "stop_error", detail=f"stop failed: {e}")
                        if was_active:
                            try:
                                self._start_unit(unit_name)
                            except Exception:
                                pass
                        return

                # Splice
                self._update_phase(rollout_id, "applying", "splicing")
                prov_line = provenance_line(edits, datetime.now(timezone.utc))
                new_raw = splice(unit.raw, edits, prov_line)

                # Write
                self._update_phase(rollout_id, "applying", "writing")
                _atomic_write(unit.path, new_raw)

                # Verify
                self._update_phase(rollout_id, "applying", "verifying")
                new_unit = verify_splice(unit, new_raw, edits, prov_line)

                # Commit
                self._update_phase(rollout_id, "applying", "committing")
                try:
                    run_git(["add", "--", unit_name], self.unit_dir)
                    run_git(["commit", "-m", commit_message(unit_name, edits)], self.unit_dir)
                    result = run_git(["rev-parse", "HEAD"], self.unit_dir)
                    commit_sha = result.stdout.strip()
                    with self.watcher_lock:
                        rollout['commit'] = commit_sha
                        rollout['updated_at'] = time.time()
                except Exception as e:
                    # Restore and fail
                    _atomic_write(unit.path, unit.raw)
                    if was_active:
                        try:
                            self._start_unit(unit_name)
                        except Exception:
                            pass
                    self._fail_rollout(rollout_id, "applying", "commit_error", detail=f"commit failed: {e}")
                    return

                # Update watcher with new unit (S4)
                with self.watcher_lock:
                    self.units[unit_name] = new_unit
                    self.watcher.units[unit_name] = new_unit

            except Exception as e:
                # Restore
                try:
                    _atomic_write(unit.path, unit.raw)
                except Exception:
                    pass
                if was_active:
                    try:
                        self._start_unit(unit_name)
                    except Exception:
                        pass
                self._fail_rollout(rollout_id, "applying", "apply_error", detail=str(e))
                return

            # Reloading phase
            self._update_phase(rollout_id, "reloading", "reloading daemon")
            try:
                self._daemon_reload()
            except Exception as e:
                self._fail_rollout(rollout_id, "reloading", "daemon_reload", detail=str(e), offer_rollback=True)
                return

            # Starting phase (if was_active)
            if was_active:
                self._update_phase(rollout_id, "starting", "starting unit")
                try:
                    self._start_unit(unit_name)
                except Exception as e:
                    self._fail_rollout(rollout_id, "starting", "start_error", detail=f"start failed: {e}", offer_rollback=True)
                    return

                # Watching phase
                self._update_phase(rollout_id, "watching", "waiting for unit to be ready")
                self._watch_unit(rollout_id, unit_name, prior_start_ts=prior_start_ts)
            else:
                # Not starting
                detail = "applied; unit was not running — not started"
                if self._check_gate(unit_name):
                    detail = "applied; kernel gate unsatisfied — not started"
                self._update_phase(rollout_id, "done", detail)
                with self.watcher_lock:
                    rollout['restored'] = False
                    rollout['updated_at'] = time.time()

        except Exception as e:
            self._fail_rollout(rollout_id, "preflight", "engine_error", detail=str(e))

    def _update_phase(self, rollout_id: str, phase: str, detail: str):
        """Update phase and publish SSE event."""
        with self.watcher_lock:
            rollout = self.rollouts.get(rollout_id)
            if rollout:
                rollout['phase'] = phase
                rollout['detail'] = detail
                rollout['updated_at'] = time.time()

        self.event_bus.publish('rollout', {
            'rollout_id': rollout_id,
            'phase': phase,
            'detail': detail,
            'ok': True,
            'ts': time.time()
        })

    def _fail_rollout(self, rollout_id: str, phase: str, reason: str,
                      offer_rollback: bool = False, detail: str = None):
        """Mark rollout as failed.

        `reason` is the machine code of §4.2 (`unit_failed`, `no_ready_marker`,
        `watch_timeout`, `daemon_reload`, `start_error`, `preflight`, ...); `detail` is the
        human text. The record's own `detail` is updated too, so a page refresh rebuilding
        the stepper from the snapshot does not show the last in-flight sub-step.
        """
        text = detail or reason
        with self.watcher_lock:
            rollout = self.rollouts.get(rollout_id)
            if rollout:
                rollout['phase'] = 'failed'
                rollout['detail'] = text
                rollout['failure'] = {'reason': reason, 'detail': text}
                if offer_rollback and rollout['commit']:
                    rollout['rollback'] = {'offered': True}
                rollout['updated_at'] = time.time()

        self.event_bus.publish('rollout', {
            'rollout_id': rollout_id,
            'phase': 'failed',
            'detail': text,
            'ok': False,
            'ts': time.time()
        })

    def _stop_unit(self, unit_name: str):
        """Stop a unit."""
        run_actuate(["systemctl", "--user", "stop", "--", unit_name], self.units)

    def _start_unit(self, unit_name: str):
        """Start a unit."""
        run_actuate(["systemctl", "--user", "start", "--", unit_name], self.units)

    def _daemon_reload(self):
        """Reload systemd daemon."""
        run_actuate(["systemctl", "--user", "daemon-reload"], self.units)

    def _watch_unit(self, rollout_id: str, unit_name: str, rollback_mode: bool = False,
                    prior_start_ts: Optional[float] = None):
        """Watch a started unit to a terminal rung (§4.2 `watching`, 900 s cap).

        The rung is SAMPLED under `watcher_lock`; every state change happens after the
        lock is released. `_update_phase`/`_fail_rollout` acquire the same non-reentrant
        lock, so calling them from inside the `with` block deadlocks this thread while it
        still holds the lock — which freezes `take_snapshot()` and with it every /api GET
        and the 3 s systemctl tick, so the rung can never change either.

        `rollback_mode` watches the restored config: its terminal states are
        `rolled_back` / `rollback_failed`, never `done` / a second rollback offer.
        """
        start = time.time()
        timeout = 900

        while time.time() - start < timeout:
            rung = None
            badges = []
            since = None
            with self.watcher_lock:
                snapshot = self.watcher.snapshot()
                for u in snapshot.get('units', []):
                    if u['unit'] == unit_name:
                        rung = u.get('rung', 'OFF')
                        badges = u.get('badges') or []
                        since = u.get('since')
                        break

            # Freshness gate: the roster is refreshed by a 3 s tick, so the first samples
            # after `start` still describe the deployment that was just stopped. Acting on
            # them declares "done, loaded in 0.0s" against the OLD process (or fails the
            # rollout on the old process's FAILED). `since` is ExecMainStartTimestamp, so
            # it changes exactly when systemd reports the new main process.
            if prior_start_ts is not None and since == prior_start_ts:
                time.sleep(1)
                continue

            elapsed = time.time() - start
            if rung in ('READY', 'BUSY'):
                if rollback_mode:
                    self._finish_rollback(rollout_id, f"rolled back; old config ready in {elapsed:.1f}s")
                else:
                    self._update_phase(rollout_id, 'done', f"loaded in {elapsed:.1f}s")
                    with self.watcher_lock:
                        rollout = self.rollouts.get(rollout_id)
                        if rollout:
                            rollout['phase'] = 'done'
                            rollout['restored'] = False
                            rollout['updated_at'] = time.time()
                return
            if rung == 'FAILED':
                self._watch_failed(rollout_id, rollback_mode, 'unit_failed', 'unit reached FAILED state')
                return
            if 'no_ready_marker' in badges:
                self._watch_failed(rollout_id, rollback_mode, 'no_ready_marker',
                                   'active but no ready marker seen before TimeoutStartSec')
                return

            time.sleep(1)

        self._watch_failed(rollout_id, rollback_mode, 'watch_timeout', 'watch timeout (900s)')

    def _finish_rollback(self, rollout_id: str, detail: str):
        """Terminal `rolled_back` state (§4.3 rollback record)."""
        self._update_phase(rollout_id, 'rolled_back', detail)
        with self.watcher_lock:
            rollout = self.rollouts.get(rollout_id)
            if rollout:
                rollout['phase'] = 'rolled_back'
                rollout['rollback'] = {'offered': False, 'phase': 'rolled_back',
                                       'revert_commit': rollout.get('revert_commit')}
                rollout['updated_at'] = time.time()

    def _watch_failed(self, rollout_id: str, rollback_mode: bool, reason: str, detail: str):
        """A watch that ended badly: `rollback_failed` when watching a rollback (terminal,
        no second offer), else `failed` with the rollback offer."""
        if not rollback_mode:
            self._fail_rollout(rollout_id, 'watching', reason, offer_rollback=True, detail=detail)
            return
        self._update_phase(rollout_id, 'rollback_failed', detail)
        with self.watcher_lock:
            rollout = self.rollouts.get(rollout_id)
            if rollout:
                rollout['phase'] = 'rollback_failed'
                rollout['failure'] = {'reason': f'rollback_{reason}', 'detail': detail}
                rollout['updated_at'] = time.time()

    def _check_gate(self, unit_name: str) -> bool:
        """Check if unit has an unsatisfied kernel gate."""
        unit = self.units.get(unit_name)
        if not unit or not unit.gate:
            return False
        return True

    def rollback(self, rollout_id: str):
        """Start rollback of a failed rollout."""
        rollout = self.rollouts.get(rollout_id)
        if not rollout or not rollout.get('commit'):
            raise ActuationError("not_rollbackable")

        if rollout.get('phase') != 'failed' or not rollout.get('rollback', {}).get('offered'):
            raise ActuationError("not_rollbackable")

        rollout['phase'] = 'rolling_back'
        rollout['updated_at'] = time.time()

        # Spawn rollback worker
        threading.Thread(
            target=self._run_rollback,
            args=(rollout_id,),
            daemon=True
        ).start()

    def _run_rollback(self, rollout_id: str):
        """Worker thread for rollback."""
        rollout = self.rollouts.get(rollout_id)
        if not rollout:
            return

        unit_name = rollout['unit']
        was_active = rollout.get('was_active', False)

        try:
            # ExecMainStartTimestamp of the failed deployment, so the watch below can tell
            # the restored process from a stale sample of the one being torn down.
            prior_start_ts = None
            with self.watcher_lock:
                snapshot = self.watcher.snapshot()
            for u in snapshot.get('units', []):
                if u['unit'] == unit_name:
                    prior_start_ts = u.get('since')
                    break

            # Stop unit
            self._update_phase(rollout_id, 'rolling_back', 'stopping unit')
            try:
                self._stop_unit(unit_name)
            except Exception:
                pass

            # Revert commit
            self._update_phase(rollout_id, 'rolling_back', 'reverting commit')
            try:
                run_git(["revert", "--no-edit", rollout['commit']], self.unit_dir)
                head = run_git(["rev-parse", "HEAD"], self.unit_dir)
                if head.returncode == 0:
                    rollout['revert_commit'] = head.stdout.strip()
            except Exception as e:
                try:
                    run_git(["revert", "--abort"], self.unit_dir)
                except Exception:
                    pass
                _atomic_write(rollout.get('unit_path') or self.units[unit_name].path, rollout['old_raw'])
                try:
                    run_git(["add", "--", unit_name], self.unit_dir)
                    run_git(["commit", "-m", f"roundhouse: rollback {unit_name} (byte restore; revert failed)"], self.unit_dir)
                except Exception:
                    pass

            # S4 in reverse (§3.6/§4.4): the file is back to old_raw, so the in-memory
            # UnitFile must be too — otherwise the next edit splices at the spans of a
            # config that no longer exists on disk.
            try:
                old_unit = parse_unit(self.units[unit_name].path, rollout['old_raw'])
                with self.watcher_lock:
                    self.units[unit_name] = old_unit
                    self.watcher.units[unit_name] = old_unit
            except Exception:
                pass

            # Reload daemon
            self._update_phase(rollout_id, 'rolling_back', 'reloading daemon')
            try:
                self._daemon_reload()
            except Exception as e:
                self._update_phase(rollout_id, 'rollback_failed', f"daemon reload failed: {e}")
                with self.watcher_lock:
                    rollout['phase'] = 'rollback_failed'
                    rollout['failure'] = {'reason': 'daemon reload', 'detail': str(e)}
                    rollout['updated_at'] = time.time()
                return

            # Start unit if it was active
            if was_active:
                self._update_phase(rollout_id, 'rolling_back', 'starting unit')
                try:
                    self._start_unit(unit_name)
                except Exception as e:
                    self._update_phase(rollout_id, 'rollback_failed', f"start failed: {e}")
                    with self.watcher_lock:
                        rollout['phase'] = 'rollback_failed'
                        rollout['failure'] = {'reason': 'start', 'detail': str(e)}
                        rollout['updated_at'] = time.time()
                    return

                # Watch old config back up
                self._watch_unit(rollout_id, unit_name, rollback_mode=True,
                                 prior_start_ts=prior_start_ts)
            else:
                self._finish_rollback(rollout_id, 'rollback complete; unit was not running')

        except Exception as e:
            self._update_phase(rollout_id, 'rollback_failed', str(e))
            with self.watcher_lock:
                rollout['phase'] = 'rollback_failed'
                rollout['updated_at'] = time.time()

    def dismiss(self, rollout_id: str):
        """Dismiss a failed rollout."""
        rollout = self.rollouts.get(rollout_id)
        if rollout and rollout.get('rollback', {}).get('offered'):
            with self.watcher_lock:
                rollout['rollback'] = {'offered': False}
                rollout['updated_at'] = time.time()


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
    parser.add_argument('--actuate', action='store_true', help='Enable rollouts (requires git)')

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

    # Arming sequence (§2.4) — MUST be before any thread starts
    global ACTUATE_ARMED
    if args.actuate:
        git_startup_check(unit_dir, selected_unit_names)
        ensure_token()
        ACTUATE_ARMED = True

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

    # Create RolloutEngine if armed
    rollout_engine = None
    if ACTUATE_ARMED:
        rollout_engine = RolloutEngine(watcher, units, unit_dir, port, event_bus, watcher_lock)

    # Start HTTP server
    try:
        server = ThreadingHTTPServer(
            ('0.0.0.0', port),
            RoundhouseRequestHandler,
            watcher,
            event_bus,
            port,
            watcher_lock=watcher_lock,
            rollout_engine=rollout_engine
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
