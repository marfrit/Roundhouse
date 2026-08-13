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
      on_demand: True if '# roundhouse: on-demand' or '; roundhouse: on-demand' found in raw
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
    on_demand: bool = False
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

    # Check for on-demand marker (same substring mechanism as manage/ignore)
    raw_str = raw.decode('utf-8', errors='replace')
    on_demand = ('# roundhouse: on-demand' in raw_str or '; roundhouse: on-demand' in raw_str)

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
        on_demand=on_demand,
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


# Known-flag table (SPEC §2.5): flag -> (canonical field, arity, type).
# Module level so the splice engine (§3.2.2 type validation) and verify (§3.5(a) typed
# re-parse) share ONE table with the extractor instead of re-deriving it.
KNOWN_FLAG_MAP = {
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

# canonical field name -> type, derived from the one table above.
FIELD_TYPES = {field: type_hint
               for (field, _arity, type_hint) in KNOWN_FLAG_MAP.values()
               if field}


def coerce_field_value(field: str, text: str):
    """Coerce a value string the way `extract_param_profile` would for this field.

    Same contract as the extractor: on a type mismatch the raw text is kept (the
    extractor never raises; `plan_edits` is where bad input is refused).
    """
    type_hint = FIELD_TYPES.get(field, 'str')
    try:
        if type_hint == 'int':
            return int(text)
        if type_hint == 'float':
            return float(text)
    except (ValueError, TypeError):
        return text
    return text


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

    # Known flags with their arities and target fields (module-level table)
    flag_map = KNOWN_FLAG_MAP

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
        'gate': unit.gate,
        'on_demand': unit.on_demand
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
ACTIVE_RUNGS = {'STARTING', 'LOADING', 'READY', 'BUSY'}

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


def strategy_note(enabled: bool, rung: str, retired: bool) -> Optional[str]:
    """Compute the drift note for a unit per G6.

    Args:
        enabled: UnitFileState == 'enabled'
        rung: current rung from snapshot
        retired: unit is retired

    Returns:
        str note, or None if no note applies
    """
    if retired:
        return None
    if enabled and rung in ('OFF', 'STANDBY', 'FAILED'):
        return "returns at boot"
    if not enabled and rung in ACTIVE_RUNGS:
        return "manual — will not survive reboot"
    return None


def classify_port_claims(claims: List[Dict]) -> tuple:
    """Classify a port's claim list per §4.4(c). Returns (class, note).

    Single claim -> (None, None). Otherwise:
      active = >=2 claimants actually occupying the port (STARTING/LOADING/READY/BUSY)
      armed  = >=2 claimants that are enabled, or held back only by an unsatisfied gate
      latent = anything else (e.g. the retired mixperten claim on :8085)
    """
    if len(claims) < 2:
        return (None, None)

    if sum(1 for c in claims if c.get('rung') in ACTIVE_RUNGS) >= 2:
        return ('active', 'two claimants are live on this port right now')

    armed = sum(1 for c in claims
                if (c.get('enabled') and not c.get('retired')) or c.get('gate'))
    if armed >= 2:
        return ('armed', 'harmless only while BOTH the disable and the kernel gate hold')

    return ('latent', None)


def locked_snapshot(watcher: 'Watcher') -> Dict:
    """Take a snapshot under the watcher's lock.

    The only sanctioned way to take a snapshot from an unlocked context; NEVER call
    while holding the lock (non-reentrant — deadlock).
    """
    with watcher.lock:
        return watcher.snapshot()


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
    lock: threading.Lock = field(default_factory=threading.Lock)
    self_unit_file_state: str = ''

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

        # Pop roundhouse.service props into self_unit_file_state (G8)
        if 'roundhouse.service' in props:
            self_props = props.pop('roundhouse.service')
            self.self_unit_file_state = self_props.get('UnitFileState', '')

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

    def apply_unit_file_state(self, unit_name: str, value: str) -> None:
        """Set the unit_file_state for a unit without updating sensed_at.

        This method updates ONLY _state[unit]['unit_file_state'] — it MUST NOT stamp
        sensed_at, as that would launder a stale ActiveState sample as fresh past
        _confirm_off's freshness gate.

        Args:
            unit_name: name of the unit
            value: new UnitFileState value (e.g., 'enabled', 'disabled')
        """
        if unit_name in self._state:
            self._state[unit_name]['unit_file_state'] = value

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

            enabled = self._state[unit_name].get('unit_file_state') == 'enabled'
            unit_dict = {
                'unit': unit_name,
                'description': unit.description or '',
                'retired': unit.retired,
                'rung': rung,
                'roster': self._rung_to_roster(rung),
                'since': self._state[unit_name].get('exec_main_start_ts') or self.now(),
                'start_ts_mono': self._state[unit_name].get('exec_main_start_ts_mono') or '0',
                'detail': self._compute_detail(unit_name, rung),
                'badges': self._compute_badges(unit_name, rung),
                'stale': self._sources_degraded(),
                'sensed_at': self._state[unit_name]['sensed_at'],
                'enabled': enabled,
                'active_state': self._state[unit_name]['active_state'],
                'sub_state': self._state[unit_name]['sub_state'],
                'n_restarts': self._state[unit_name]['n_restarts'],
                'port': port,
                'port_source': port_source,
                'alias': alias,
                'on_demand': unit.on_demand,
                'gate': unit.gate,
                'model_file': os.path.basename(profile.get('model_path', '')),
                'quant_hint': quant,
                'ctx': profile.get('ctx'),
                'mem': mem_info,
                'port_conflict': None,  # filled in below, once every claim is known
                'strategy_note': strategy_note(enabled, rung, unit.retired)
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
            'self_unit': {
                'unit': 'roundhouse.service',
                'unit_file_state': self.self_unit_file_state,
                'enabled': self.self_unit_file_state == 'enabled'
            },
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
        elif route == '/api/routing-config':
            self.serve_routing_config()
        elif route == '/api/routing-config.json':
            self.serve_routing_config_json()
        elif route == '/api/warm':
            self.serve_warm_state()
        elif route.startswith('/api/rollouts/'):
            rollout_id = urllib.parse.unquote(route[len('/api/rollouts/'):])
            self.serve_rollout(rollout_id)
        elif route == '/api/switch/preview' or route == '/api/switch' or route == '/api/warm/cancel':
            # POST-only routes
            self.error_405()
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
            (route.startswith('/api/units/') and route.endswith('/enablement')) or
            (route.startswith('/api/rollouts/') and route.endswith('/rollback')) or
            (route.startswith('/api/rollouts/') and route.endswith('/dismiss')) or
            route == '/api/switch/preview' or
            route == '/api/switch' or
            route == '/api/warm' or
            route == '/api/warm/cancel'
        )

        # Determine if this is a known GET-only or POST-only route
        is_get_route = (
            route == '/' or
            route == '/api/units' or
            route.startswith('/api/units/') or
            route == '/api/ports' or
            route == '/api/deployments' or
            route == '/api/mem' or
            route == '/api/events' or
            route == '/api/routing-config' or
            route == '/api/routing-config.json' or
            route == '/api/warm' or
            route.startswith('/api/rollouts/') or   # GET-only unless /rollback|/dismiss
            route == '/api/switch/preview' or
            route == '/api/switch'
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
        elif route.startswith('/api/units/') and route.endswith('/enablement'):
            unit_name = urllib.parse.unquote(route[len('/api/units/'):-len('/enablement')])
            self.handle_enablement(unit_name)
        elif route.startswith('/api/rollouts/') and route.endswith('/rollback'):
            rollout_id = urllib.parse.unquote(route[len('/api/rollouts/'):-len('/rollback')])
            self.handle_rollback(rollout_id)
        elif route.startswith('/api/rollouts/') and route.endswith('/dismiss'):
            rollout_id = urllib.parse.unquote(route[len('/api/rollouts/'):-len('/dismiss')])
            self.handle_dismiss(rollout_id)
        elif route == '/api/switch/preview':
            self.handle_switch_preview()
        elif route == '/api/switch':
            self.handle_switch()
        elif route == '/api/warm':
            self.handle_warm()
        elif route == '/api/warm/cancel':
            self.handle_warm_cancel()
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

        # E5 companion: preview against the bytes ON DISK. Unit files are parsed once at
        # startup and refreshed only by a successful apply (S4), so after an external hand
        # edit the in-memory spans are dead — and since apply now refuses on a disk/memory
        # mismatch, without this refresh the unit would be un-appliable until a restart.
        # §7.5's "re-preview once on preview_stale" recovery depends on this.
        try:
            disk_raw = _read_file_bytes(unit.path)
        except OSError as exc:
            self.send_json_error(404, 'not_found', f'cannot read {unit.path}: {exc}')
            return
        if disk_raw != unit.raw:
            engine = self.server.rollout_engine
            fresh = None
            if engine is None or _slot_free(getattr(engine, 'current', None)):
                # Never swap the file's parse out from under a live rollout.
                try:
                    fresh = parse_unit(unit.path, disk_raw)
                except Exception:
                    fresh = None
            if fresh is None:
                self.send_response(409)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps({
                    'error': 'preview_stale',
                    'detail': 'unit file or edits changed since preview; re-preview',
                }).encode('utf-8'))
                return
            with self.server.watcher_lock:
                watcher.units[unit_name] = fresh
                if engine is not None:
                    engine.units[unit_name] = fresh
            unit = fresh

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

        # E5 staleness FIRST, and against the bytes ON DISK (not the in-memory copy).
        # A hash of `unit.raw` cannot notice an external edit made after the preview: it
        # matches, the splice runs at the old spans and silently clobbers the change.
        # It precedes plan_edits because staleness is a property of the base bytes — if
        # the hand edit removed the field being edited, "re-preview" is the honest answer,
        # not "field_not_editable".
        try:
            disk_raw = _read_file_bytes(unit.path)
        except OSError as exc:
            self.send_json_error(404, 'not_found', f'cannot read {unit.path}: {exc}')
            return
        if disk_raw != unit.raw:
            self.send_response(409)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({
                'error': 'preview_stale',
                'detail': 'unit file or edits changed since preview; re-preview'
            }).encode('utf-8'))
            return

        # Plan edits
        try:
            edits = plan_edits(unit, data.get('edits', {}))
        except EditError as e:
            self.send_json_error(400, e.reason, e.detail or e.reason)
            return

        # Check confirm matches (E5), computed from the verified disk bytes.
        confirm = data.get('confirm', '')
        computed = compute_confirm(unit_name, disk_raw, edits)
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

    def handle_enablement(self, unit_name: str):
        """Handle POST /api/units/<name>/enablement (enable/disable boot strategy)."""
        # Step 1: Parse body; enabled must be True or False (strict bool)
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)
        except (ValueError, json.JSONDecodeError):
            self.send_json_error(400, 'bad_json', 'malformed JSON body')
            return

        enabled = data.get('enabled')
        if not isinstance(enabled, bool):
            self.send_json_error(400, 'bad_body', 'enabled must be true or false')
            return

        # Step 2: Find unit
        watcher = self.server.watcher
        if unit_name not in watcher.units:
            self.send_json_error(404, 'not_found', f'unit {unit_name} not found')
            return

        # Step 3: Get engine
        engine = self.server.rollout_engine
        if not engine:
            self.send_json_error(500, 'no_engine', 'rollout engine not initialized')
            return

        # Step 4: Preflight checks
        snap = locked_snapshot(watcher)
        pf = enablement_preflight(unit_name, enabled, snap, watcher.units, self.server.port)
        if not pf['ok']:
            # G3 puts the frozen fail string on the failing CHECK ROW (the edit route's
            # shape), not at the top level — reading only pf['detail'] shipped an empty
            # `detail` to the client and an explanation-less toast to the UI.
            detail = pf.get('detail') or next(
                (c.get('detail', '') for c in pf.get('checks', []) if not c.get('ok')), '')
            self.send_response(422)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            if pf.get('claimants'):
                # enable_collision
                response = {
                    'error': 'enable_collision',
                    'port': pf.get('port'),
                    'claimants': pf['claimants'],
                    'detail': detail
                }
            else:
                # preflight_failed (e.g., retired)
                response = {
                    'error': 'preflight_failed',
                    'detail': detail
                }
            self.wfile.write(json.dumps(response).encode('utf-8'))
            return

        # Step 5: Get the 'was' value from snapshot
        target_row = next((u for u in snap.get('units', []) if u['unit'] == unit_name), {})
        was = target_row.get('enabled', False)

        # Step 6: Execute enablement via engine gateway
        try:
            engine._set_enablement(unit_name, enabled)
        except ActuationError as e:
            self.send_json_error(500, 'enablement_error', str(e))
            return

        # Step 7: Read back via run_ro and apply under lock
        enabled_after = enabled  # default to requested value if read fails
        try:
            output = run_ro(['systemctl', '--user', 'show', '-p', 'UnitFileState', '--', unit_name])
            for line in output.split('\n'):
                if line.startswith('UnitFileState='):
                    state_value = line.split('=', 1)[1]
                    with watcher.lock:
                        watcher.apply_unit_file_state(unit_name, state_value)
                    enabled_after = (state_value == 'enabled')
                    break
        except Exception:
            # Read-back failure: continue with requested value
            pass

        # Step 8: Compute strategy note
        note = strategy_note(enabled_after, target_row.get('rung', 'OFF'), unit.retired if (unit := watcher.units.get(unit_name)) else False)

        # Step 9: Publish SSE event
        self.server.event_bus.publish('enablement', {
            'unit': unit_name,
            'enabled': enabled_after,
            'strategy_note': note,
            'ts': time.time()
        })

        # Step 10: Send 200 response
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        response = {
            'unit': unit_name,
            'enabled': enabled_after,
            'was': was,
            'changed': (was != enabled_after),
            'strategy_note': note
        }
        self.wfile.write(json.dumps(response).encode('utf-8'))

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

        # `rollout['rollback']` is None until an offer is made, so `.get('rollback', {})`
        # returned None here and `.get('offered')` raised AttributeError -> 500.
        if rollout.get('phase') != 'failed' or not _rollback_offered(rollout):
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
            # Lost the race against a concurrent rollback/dismiss: concurrency, so 409.
            if 'not_rollbackable' in str(e):
                self.send_response(409)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'not_rollbackable'}).encode('utf-8'))
                return
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

        if rollout.get('phase') != 'failed' or not _rollback_offered(rollout):
            self.send_response(409)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            response = {'error': 'not_dismissable'}
            self.wfile.write(json.dumps(response).encode('utf-8'))
            return

        try:
            engine.dismiss(rollout_id)
        except ActuationError:
            # Lost the race against a concurrent rollback/dismiss.
            self.send_response(409)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'not_dismissable'}).encode('utf-8'))
            return

        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps({'ok': True}).encode('utf-8'))

    def handle_switch_preview(self):
        """Handle POST /api/switch/preview (preview mode)."""
        # Read body
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)
        except (ValueError, json.JSONDecodeError):
            self.send_json_error(400, 'bad_json', 'malformed JSON body')
            return

        # Validate target
        target = data.get('target')
        if not isinstance(target, str):
            self.send_json_error(400, 'bad_body', 'target must be a string')
            return

        # Validate stops (optional, defaults to [])
        stops = data.get('stops', [])
        if not isinstance(stops, list) or not all(isinstance(s, str) for s in stops):
            self.send_json_error(400, 'bad_body', 'stops must be a list of strings')
            return

        # Get engine
        engine = self.server.rollout_engine
        if not engine:
            self.send_json_error(500, 'no_engine', 'rollout engine not initialized')
            return

        # NO slot check here. Preview is STATELESS: it takes no slot and answers while an
        # operation runs (§4's route table lists no 409 for this route; the MVP2 edit
        # preview behaves the same). Only /api/switch competes for the slot.

        # Find units
        watcher = self.server.watcher
        if target not in watcher.units:
            self.send_json_error(404, 'not_found', f'unit {target} not found')
            return

        for stop_name in stops:
            if stop_name not in watcher.units:
                self.send_json_error(404, 'not_found', f'unit {stop_name} not found')
                return

        # Run preflight (one snapshot per request lives inside switch_preflight, §3.3)
        pf = switch_preflight(target, stops, watcher, watcher.units,
                             self.server.port)

        # Build response based on preflight result
        if pf['ok']:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            response = {
                'target': pf['target'],
                'stop_candidates': pf['stop_candidates'],
                'checks': pf['checks'],
                'fit': pf['fit'],
                'port': pf['port'],
                'suggested_stops': pf['suggested_stops'],
                'notices': pf['notices'],
                'confirm': pf['confirm'],
            }
            self.wfile.write(json.dumps(response).encode('utf-8'))
        else:
            self.send_response(422)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            response = {
                'error': 'preflight_failed',
                'target': pf['target'],
                'stop_candidates': pf['stop_candidates'],
                'checks': pf['checks'],
                'fit': pf['fit'],
                'port': pf['port'],
                'suggested_stops': pf['suggested_stops'],
                'notices': pf['notices'],
            }
            self.wfile.write(json.dumps(response).encode('utf-8'))

    def handle_switch(self):
        """Handle POST /api/switch (execute)."""
        # Read body
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)
        except (ValueError, json.JSONDecodeError):
            self.send_json_error(400, 'bad_json', 'malformed JSON body')
            return

        # Validate target
        target = data.get('target')
        if not isinstance(target, str):
            self.send_json_error(400, 'bad_body', 'target must be a string')
            return

        # Validate stops (optional, defaults to [])
        stops = data.get('stops', [])
        if not isinstance(stops, list) or not all(isinstance(s, str) for s in stops):
            self.send_json_error(400, 'bad_body', 'stops must be a list of strings')
            return

        # Validate confirm
        confirm = data.get('confirm')
        if not isinstance(confirm, str):
            self.send_json_error(400, 'bad_body', 'confirm must be a string')
            return

        # Get engine
        engine = self.server.rollout_engine
        if not engine:
            self.send_json_error(500, 'no_engine', 'rollout engine not initialized')
            return

        # Find units
        watcher = self.server.watcher
        if target not in watcher.units:
            self.send_json_error(404, 'not_found', f'unit {target} not found')
            return

        for stop_name in stops:
            if stop_name not in watcher.units:
                self.send_json_error(404, 'not_found', f'unit {stop_name} not found')
                return

        # Slot check (F11) BEFORE preflight/staleness: while another operation holds the
        # slot the world is moving under us, so the fingerprint almost always mismatches
        # and the honest answer would be masked as `preview_stale` ("re-preview" — which
        # would loop forever). `start_switch` re-checks under the lock; this is the honest
        # message, that one is the race-proof gate.
        if not _slot_free(engine.current):
            self.send_response(409)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            response = {
                'error': 'operation_in_progress',
                'rollout_id': engine.current.get('rollout_id'),
                'kind': engine.current.get('kind', 'rollout'),
            }
            self.wfile.write(json.dumps(response).encode('utf-8'))
            return

        # Re-check preflight (run switch_preflight to validate everything is still good)
        pf = switch_preflight(target, stops, watcher, watcher.units,
                             self.server.port)
        if not pf['ok']:
            self.send_response(422)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            response = {
                'error': 'preflight_failed',
                'target': pf['target'],
                'stop_candidates': pf['stop_candidates'],
                'checks': pf['checks'],
                'fit': pf['fit'],
                'port': pf['port'],
                'suggested_stops': pf['suggested_stops'],
                'notices': pf['notices'],
            }
            self.wfile.write(json.dumps(response).encode('utf-8'))
            return

        # Verify confirm hash matches (F3)
        fingerprint = fleet_fingerprint(locked_snapshot(watcher))
        computed_confirm = compute_switch_confirm(target, stops, fingerprint)
        if confirm != computed_confirm:
            self.send_response(409)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            response = {
                'error': 'preview_stale',
                'detail': 'fleet state changed since preview (a unit started or stopped); re-preview'
            }
            self.wfile.write(json.dumps(response).encode('utf-8'))
            return

        # Start switch
        try:
            switch = engine.start_switch(target, stops, computed_confirm)
            self.send_response(202)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            response = {'rollout_id': switch['rollout_id']}
            self.wfile.write(json.dumps(response).encode('utf-8'))
        except ActuationError as e:
            error_str = str(e)
            if 'operation_in_progress' in error_str:
                self.send_response(409)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                response = {
                    'error': 'operation_in_progress',
                    'rollout_id': engine.current.get('rollout_id') if engine.current else None,
                    'kind': engine.current.get('kind', 'rollout') if engine.current else None
                }
                self.wfile.write(json.dumps(response).encode('utf-8'))
            else:
                self.send_json_error(400, 'switch_error', error_str)

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
                'on_demand': unit.on_demand,
                'strategy_note': row.get('strategy_note')
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

    def serve_routing_config(self):
        """Serve GET /api/routing-config (YAML routing config)."""
        snapshot = self.server.take_snapshot()
        advertise_host = getattr(self.server, 'advertise_host', None) or snapshot.get('host', '?')
        now_utc = datetime.now(timezone.utc)

        entries = build_routing_entries(snapshot, advertise_host)
        meta = routing_meta(snapshot, advertise_host, self.server.port, now_utc)

        yaml_text = emit_routing_yaml(meta, entries)

        self.send_response(200)
        self.send_header('Content-Type', 'text/yaml; charset=utf-8')
        self.end_headers()
        self.wfile.write(yaml_text.encode('utf-8'))

    def serve_routing_config_json(self):
        """Serve GET /api/routing-config.json (JSON routing config)."""
        snapshot = self.server.take_snapshot()
        advertise_host = getattr(self.server, 'advertise_host', None) or snapshot.get('host', '?')
        now_utc = datetime.now(timezone.utc)

        entries = build_routing_entries(snapshot, advertise_host)
        meta = routing_meta(snapshot, advertise_host, self.server.port, now_utc)

        response = {**meta, 'model_list': entries}

        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps(response).encode('utf-8'))

    def serve_warm_state(self):
        """Serve GET /api/warm (warm queue state)."""
        engine = self.server.rollout_engine
        if engine:
            state = engine.warm_state()
        else:
            state = {'pending': None, 'last': None}

        self.send_json(state)

    def handle_warm(self):
        """Handle POST /api/warm (warm request)."""
        # Step 1: Parse body
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)
        except (ValueError, json.JSONDecodeError):
            self.send_json_error(400, 'bad_json', 'malformed JSON body')
            return

        # Extract logical and unit (exactly one must be given)
        logical = data.get('logical')
        unit = data.get('unit')

        if not isinstance(logical, (str, type(None))) or not isinstance(unit, (str, type(None))):
            self.send_json_error(400, 'bad_body', 'logical and unit must be strings or null')
            return

        # Count non-None values
        given_count = sum(1 for x in [logical, unit] if x is not None)
        if given_count != 1:
            self.send_json_error(400, 'bad_body', 'give exactly one of logical or unit')
            return

        # Step 2: Get engine
        engine = self.server.rollout_engine
        if not engine:
            self.send_json_error(403, 'read_only_mode', 'launch with --actuate to enable rollouts')
            return

        # Step 3: Resolve target
        watcher = self.server.watcher
        snap = locked_snapshot(watcher)

        result = resolve_warm_target(logical, unit, snap, watcher.units)
        if result[0] == 'error':
            status, error_code, extra = result[1], result[2], result[3]
            if error_code == 'ambiguous_alias':
                self.send_response(422)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps({
                    'error': error_code,
                    'units': extra.get('units', [])
                }).encode('utf-8'))
            else:
                self.send_json_error(status, error_code, f'{error_code}')
            return

        target = result[1]

        # Step 4: Sanitize requester
        # H7: strip, keep only [A-Za-z0-9._@ -], truncate to 64, empty -> 'token'
        requester_header = self.headers.get('X-Roundhouse-Requester', '').strip()
        requester = ''.join(c for c in requester_header if c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._@ -')
        # Truncate to 64 chars
        requester = requester[:64]
        # If empty, use 'token' as fallback
        if not requester:
            requester = 'token'

        # Step 5: Check if retired
        target_row = None
        for row in snap.get('units', []):
            if row['unit'] == target:
                target_row = row
                break

        if target_row and target_row.get('retired'):
            self.send_response(422)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({
                'error': 'preflight_failed',
                'checks': [preflight_retired(watcher.units[target])]
            }).encode('utf-8'))
            return

        # Step 6: Check if already warm
        if target_row:
            rung = target_row.get('rung', 'OFF')
            if rung in ('READY', 'BUSY', 'STARTING', 'LOADING'):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps({
                    'status': 'already_warm',
                    'unit': target,
                    'rung': rung
                }).encode('utf-8'))
                return

        # Step 7: Check if on-demand
        unit_obj = watcher.units.get(target)
        if not unit_obj or not unit_obj.on_demand:
            self.send_response(422)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({
                'error': 'not_on_demand',
                'unit': target,
                'detail': "unit is not marked '# roundhouse: on-demand' — a warm request may neither start nor stop it (add the marker and restart roundhouse)"
            }).encode('utf-8'))
            return

        # Step 8: Queue gate — state transitions under the lock, RESPONSE
        # WRITES OUTSIDE IT (MVP5 review blocker 1: wfile.write can block on a
        # slow client; blocking while holding the global lock wedges the server).
        queue_response = None  # (status, body) decided under the lock
        with watcher.lock:
            if engine.pending_warm is not None:
                pending_unit = engine.pending_warm.get('unit')
                if pending_unit == target:
                    queue_response = (200, {
                        'status': 'already_queued',
                        'unit': target,
                        'pending': dict(engine.pending_warm)
                    })
                else:
                    queue_response = (409, {
                        'error': 'warm_queue_full',
                        'pending': dict(engine.pending_warm)
                    })
            elif not _slot_free(engine.current):
                # Park the request
                engine.warm_seq += 1
                engine.pending_warm = {
                    'seq': engine.warm_seq,
                    'unit': target,
                    'logical': logical,
                    'requester': requester,
                    'requested_at': time.time()
                }
                queue_response = (202, {'queued': True, 'unit': target})
        if queue_response is not None:
            status, body = queue_response
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(body).encode('utf-8'))
            return

        # Step 9: Plan warm (slot is free, no pending)
        cgroup_cache = getattr(watcher, '_cgroup_cache', None) or {}
        mem_store = watcher.mem_store
        plan = warm_plan(target, snap, watcher.units, cgroup_cache, mem_store)

        if not plan['fits']:
            self.send_response(422)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            response = {
                'error': 'consent_unfittable',
                'unit': target,
                'detail': f"estimated {plan['estimate_bytes']} bytes + headroom {plan['headroom_bytes']} bytes exceeds available + freed by consenting stops",
                'estimate_bytes': plan['estimate_bytes'],
                'estimate_source': plan['estimate_source'],
                'mem_available_bytes': plan['mem_available_bytes'],
                'headroom_bytes': plan['headroom_bytes'],
                'freed_by': plan['freed_by'],
                'shortfall_bytes': plan['shortfall_bytes'],
                'consenting': plan['consenting'],
                'excluded_unmarked': plan['excluded_unmarked']
            }
            self.wfile.write(json.dumps(response).encode('utf-8'))
            return

        # Step 10: Preflight
        pf = switch_preflight(target, plan['stops'], watcher, watcher.units, self.server.port)
        if not pf['ok']:
            self.send_response(422)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            response = {
                'error': 'preflight_failed',
                'target': pf.get('target'),
                'stop_candidates': pf.get('stop_candidates'),
                'checks': pf.get('checks', []),
                'fit': pf.get('fit'),
                'port': pf.get('port'),
                'suggested_stops': pf.get('suggested_stops', []),
                'notices': pf.get('notices', [])
            }
            self.wfile.write(json.dumps(response).encode('utf-8'))
            return

        # Step 11: Start switch.
        #
        # NEVER hold `watcher.lock` across this call: `start_switch` claims the slot
        # inside its own `with self.watcher_lock:` block (H4 — that block is where the
        # consent re-check, the warm_seq handshake and the slot claim are made atomic),
        # and the lock is a plain non-reentrant `threading.Lock`. A caller that already
        # holds it deadlocks the request thread WITH the lock held, wedging every
        # snapshot, poll and route in the process.
        try:
            result = engine.start_switch(target, plan['stops'], pf['confirm'],
                                         origin='warm', requester=requester)

            self.send_response(202)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            response = {
                'rollout_id': result.get('rollout_id'),
                'stops': plan['stops']
            }
            self.wfile.write(json.dumps(response).encode('utf-8'))

        except ActuationError as e:
            error_str = str(e)
            if 'operation_in_progress' in error_str:
                # A human claimed the slot between step 8 and here: re-enter step 8's
                # locked block exactly once (park / dup / full).
                # State transition under the lock; RESPONSE WRITE OUTSIDE
                # (MVP5 review blocker 1 — same rule as step 8).
                reentry = None
                with watcher.lock:
                    if engine.pending_warm is not None:
                        pending = dict(engine.pending_warm)
                        if pending.get('unit') == target:
                            reentry = (200, {'status': 'already_queued',
                                             'unit': target, 'pending': pending})
                        else:
                            reentry = (409, {'error': 'warm_queue_full',
                                             'pending': pending})
                    else:
                        engine.warm_seq += 1
                        engine.pending_warm = {
                            'seq': engine.warm_seq,
                            'unit': target,
                            'logical': logical,
                            'requester': requester,
                            'requested_at': time.time()
                        }
                        reentry = (202, {'queued': True, 'unit': target})
                status, body = reentry
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps(body).encode('utf-8'))
            elif 'warm_consent' in error_str:
                self.send_json_error(422, 'not_on_demand', str(e))
            else:
                self.send_json_error(400, 'warm_error', error_str)

    def handle_warm_cancel(self):
        """Handle POST /api/warm/cancel (cancel pending warm)."""
        # Get engine
        engine = self.server.rollout_engine
        if not engine:
            self.send_json_error(403, 'read_only_mode', 'launch with --actuate to enable rollouts')
            return

        watcher = self.server.watcher

        # Cancel: state transition under the lock, RESPONSE WRITE OUTSIDE
        # (MVP5 review blocker 1 — a blocked write here held the global lock).
        cancelled_unit = None
        had_pending = False
        with watcher.lock:
            if engine.pending_warm is not None:
                had_pending = True
                cancelled_unit = engine.pending_warm.get('unit')
                requester = engine.pending_warm.get('requester')
                engine.pending_warm = None
                engine.last_warm = {
                    'unit': cancelled_unit,
                    'requester': requester,
                    'disposition': 'cancelled',
                    'at': time.time()
                }
        if not had_pending:
            self.send_json_error(404, 'no_pending', 'no pending warm request')
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps({
            'cancelled': True,
            'unit': cancelled_unit
        }).encode('utf-8'))

    def log_message(self, format, *args):
        """Suppress log messages."""
        pass


class ThreadingHTTPServer(http.server.ThreadingHTTPServer):
    """HTTP server with references to watcher, event_bus, and port."""

    def __init__(self, host_port, handler_class, watcher, event_bus, port,
                 watcher_lock=None, rollout_engine=None, advertise_host=None):
        self.watcher = watcher
        self.event_bus = event_bus
        self.port = port
        self.rollout_engine = rollout_engine
        self.advertise_host = advertise_host
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
    """The §4.3 wire shape of a rollout or switch record.

    One implementation for both consumers (snapshot merge and GET /api/rollouts/<id>) so
    they cannot drift; `old_raw` (the in-memory pre-edit bytes) is never serialized.
    """
    kind = rollout.get('kind', 'rollout')

    # Common fields for both kinds
    common = {
        'rollout_id': rollout['rollout_id'],
        'kind': kind,
        'unit': rollout['unit'],
        'phase': rollout['phase'],
        'detail': rollout['detail'],
        'failure': rollout['failure'],
        'rollback': rollout['rollback'],
        'restored': rollout['restored'],
        'started_at': rollout['started_at'],
        'updated_at': rollout['updated_at'],
    }

    # Kind-specific fields
    if kind == 'switch':
        # Switch record: add switch-specific fields
        common.update({
            'target': rollout.get('target'),
            'stops': rollout.get('stops', []),
            'stopped': rollout.get('stopped', []),
            'target_started': rollout.get('target_started', False),
            'origin': rollout.get('origin', 'human'),
            'requester': rollout.get('requester'),
        })
    else:
        # Rollout record: add rollout-specific fields
        common.update({
            'edits': rollout['edits'],
            'was_active': rollout['was_active'],
            'commit': rollout['commit'],
        })

    return common


# Module globals
ACTUATE_ARMED = False
ACTUATE_SYSTEMCTL_VERBS = {"stop", "start", "daemon-reload"}
ENABLEMENT_VERBS = {"enable", "disable"}
GIT_VERBS = {"version", "rev-parse", "status", "ls-files", "log", "show",
             "diff", "add", "commit", "revert"}
GIT_MUTATING_VERBS = {"add", "commit", "revert"}
GIT_FORBIDDEN_TOKENS = {"push", "pull", "fetch", "remote", "clone", "init",
                        "checkout", "reset", "clean", "submodule"}
FROZEN_POST_ROUTES = (
    "/api/units/<name>/edit", "/api/units/<name>/rollout",
    "/api/rollouts/<id>/rollback", "/api/rollouts/<id>/dismiss",
    "/api/switch/preview", "/api/switch", "/api/units/<name>/enablement",
    "/api/warm", "/api/warm/cancel"
)
TOKEN_PATH = os.path.expanduser("~/.config/roundhouse/token")
TOKEN = None
HEADROOM_BYTES = 1 * 1024 * 1024 * 1024  # 1 GiB

# Timeout constants (section E)
STOP_TIMEOUT_SEC = 150
CONFIRM_OFF_TIMEOUT_SEC = 30
START_TIMEOUT_SEC = 30
WATCH_TIMEOUT_SEC = 900

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

    # Check exact shape per G2 per-verb table
    if argv == ["systemctl", "--user", "daemon-reload"]:
        pass  # Valid standalone command (len 3 only)
    elif len(argv) == 5 and argv[0] == "systemctl" and argv[1] == "--user" and argv[3] == "--":
        # len 5: stop|start|enable|disable with unit; daemon-reload rejects here
        if argv[2] not in (ACTUATE_SYSTEMCTL_VERBS | ENABLEMENT_VERBS) or argv[2] == "daemon-reload":
            raise ActuationError(f"run_actuate: invalid verb {argv[2]}")
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

def run_git(args: List[str], unit_dir: str, timeout: float = 30, bootstrap: bool = False,
            units: Optional[Dict[str, 'UnitFile']] = None) -> subprocess.CompletedProcess:
    """Run a git command. Raises ActuationError unless armed and conditions met.

    `units` is the selected unit set; when supplied, `git add -- <basename>` must name a
    member of it (§2.2). Callers that mutate always pass it — staging an arbitrary
    basename is how a unit Roundhouse does not manage ends up in a Roundhouse commit.
    """
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
            if not (len(args) == 3 and args[1] == "--"):
                raise ActuationError(f"git add must be exactly ['add', '--', 'basename'], got {args}")
            basename = args[2]
            # A basename, not a path and not an option: no separators, no `..`, no leading
            # dash, and it must look like a unit file.
            if ("/" in basename or os.sep in basename or ".." in basename
                    or basename.startswith("-") or not basename.endswith(".service")):
                raise ActuationError(f"git add basename is not a unit filename: {basename!r}")
            if units is not None and basename not in units:
                raise ActuationError(f"git add {basename!r} is not a selected unit")
        elif args[0] == "revert":
            # `args[1:3] == ["--no-edit", args[2]]` was a tautology — it only constrained
            # args[1], so 'HEAD~5..HEAD', '--all' and 'main' all passed as the "sha".
            if args == ["revert", "--abort"]:
                pass
            elif (len(args) == 3 and args[1] == "--no-edit"
                    and re.fullmatch(r'[0-9a-f]{7,64}', args[2])):
                pass
            else:
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
    if ignore_text is not None and '*.roundhouse-tmp' not in ignore_text:
        print(f"warning: {gitignore} does not cover '*.roundhouse-tmp'", file=sys.stderr)

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
  git -c user.name=roundhouse -c user.email="roundhouse@$(hostname)" \\
      commit -m "roundhouse baseline: {len(units)} managed units"

(The -c identity flags matter on a fresh host: without a configured user.email
git refuses to commit, and --actuate can never arm. Roundhouse's own commits
carry the same identity via GIT_AUTHOR_*/GIT_COMMITTER_*.)

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
            # mode=0o600 at CREATE time: no world-readable window (chmod-after-replace had one)
            _atomic_write(TOKEN_PATH, token.encode() + b'\n', mode=0o600)
            os.chmod(TOKEN_PATH, 0o600)
            print(f"generated bearer token at {TOKEN_PATH} — paste its contents into the UI", file=sys.stderr)
            TOKEN = token
            return token

        TOKEN = token_content
        return token_content
    else:
        # Generate new token
        token = secrets.token_urlsafe(32)
        # mode=0o600 at CREATE time: no world-readable window (chmod-after-replace had one)
        _atomic_write(TOKEN_PATH, token.encode() + b'\n', mode=0o600)
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

def validate_field_value(field: str, new_value: str) -> None:
    """§3.2.2 type/range validation for a canonical field. Raises EditError invalid_value.

    Unknown flags (`unknown:<flag>`) carry no declared type and are only byte-safety
    checked — the parser never typed them either.
    """
    if field.startswith('unknown:'):
        return

    if field == 'chat_template_kwargs':
        try:
            json.loads(new_value)
        except (ValueError, json.JSONDecodeError):
            raise EditError("invalid_value",
                            f"{field} must be valid JSON: {new_value!r}")
        return

    type_hint = FIELD_TYPES.get(field)
    if type_hint == 'int':
        try:
            parsed = int(new_value)
        except (ValueError, TypeError):
            raise EditError("invalid_value", f"{field} must be an integer: {new_value!r}")
        if field == 'ctx' and parsed <= 0:
            raise EditError("invalid_value", f"ctx must be > 0: {new_value!r}")
        if field == 'port' and not (1 <= parsed <= 65535):
            raise EditError("invalid_value", f"port must be 1..65535: {new_value!r}")
    elif type_hint == 'float':
        try:
            float(new_value)
        except (ValueError, TypeError):
            raise EditError("invalid_value", f"{field} must be a number: {new_value!r}")


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

        # Type/range validate (§3.2.2) before byte safety: 'abc' for an int field is a
        # type error, not a tokenization hazard, and must never reach the splice.
        validate_field_value(key, new_value)

        # Byte safety validate
        if quote == '':
            # Unquoted: must match alphanumeric + safe chars. `fullmatch` + `+` (§3.2.3):
            # non-empty, and no `$`-before-trailing-newline hole (a value ending in \n
            # would otherwise pass and split the token).
            if not re.fullmatch(r'[A-Za-z0-9._:/=,+\-]+', new_value):
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

def _expected_profile(old_norm: Dict, edits: List[Edit]) -> Dict:
    """The profile the splice is SUPPOSED to produce: old, with each edit applied (§3.5(a)).

    Building the expectation (instead of skipping edited keys) is what makes the check
    strong AND correct for the two shapes where the key name is not the field name:

    * `unknown:<flag>` edits land in the `unknown_flags` list, never in a key called
      `unknown:--foo` — comparing `unknown_flags` as an *unedited* field failed every
      unknown-flag edit after the service was already stopped and spliced.
    * `sampling.temp` and friends land inside the `sampling` sub-dict, same trap.
    """
    expected = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
                for k, v in old_norm.items()}

    for e in edits:
        if e.field.startswith('unknown:'):
            pairs = list(expected.get('unknown_flags') or [])
            # plan_edits binds the first entry with this flag text that HAS a value span;
            # `value == old_text` reproduces that choice on the normalized list. The
            # flag-only fallback covers tokens whose decoded text differs from their raw
            # bytes (escapes, quoting) — never fail verify on a bookkeeping mismatch after
            # the file has already been written.
            index = None
            for i, (flag, value) in enumerate(pairs):
                if flag == e.flag and value == e.old_text:
                    index = i
                    break
            if index is None:
                for i, (flag, value) in enumerate(pairs):
                    if flag == e.flag and value is not None:
                        index = i
                        break
            if index is None:
                raise VerifyError("profile_changed",
                                  f"edited unknown flag {e.flag} not found in the old profile")
            pairs[index] = (pairs[index][0], e.new_text)
            expected['unknown_flags'] = pairs
            continue

        typed = coerce_field_value(e.field, e.new_text)
        if '.' in e.field:
            parent, sub = e.field.split('.', 1)
            sub_dict = dict(expected.get(parent) or {})
            sub_dict[sub] = typed
            expected[parent] = sub_dict
            continue

        expected[e.field] = typed
        # Derived keys that follow their source field.
        if e.field == 'chat_template_kwargs':
            try:
                expected['chat_template_kwargs_json'] = json.loads(e.new_text)
            except (ValueError, json.JSONDecodeError):
                expected['chat_template_kwargs_json'] = None
        elif e.field == 'port':
            expected['port_source'] = 'flag'

    return expected


def assert_outside_spans_unchanged(old_raw: bytes, new_raw: bytes,
                                   edits: List[Edit], provenance: str) -> None:
    """§3.5(c) MVP2 addition, as a RUNTIME assertion.

    Replays the splice arithmetic forward — descending-order splicing is equivalent to
    shifting every later offset by the cumulative length delta — and asserts every byte of
    `new_raw` outside the replaced spans and the appended EOF provenance region equals the
    corresponding old byte. Deliberately independent of `assert_span_invariants` (which
    only re-checks the NEW file against itself and would pass a wholesale rewrite).
    """
    ordered = sorted(edits, key=lambda e: e.span[0])

    cursor_old = 0        # old-file offset of the next unedited run
    delta = 0             # cumulative length change of all preceding edits
    for e in ordered:
        start, end = e.span
        if start < cursor_old:
            raise VerifyError("overlapping_spans", f"span {e.span} overlaps a previous edit")
        run = old_raw[cursor_old:start]
        got = new_raw[cursor_old + delta:start + delta]
        if got != run:
            raise VerifyError(
                "outside_span_mutation",
                f"bytes {cursor_old}..{start} outside the edited spans changed")
        delta += len(render_value_bytes(e)) - (end - start)
        cursor_old = end

    tail = old_raw[cursor_old:]
    tail_start = cursor_old + delta
    if new_raw[tail_start:tail_start + len(tail)] != tail:
        raise VerifyError(
            "outside_span_mutation",
            f"bytes after {cursor_old} outside the edited spans changed")

    # Everything past the replayed body is the appended EOF region, and nothing else.
    body = new_raw[:tail_start + len(tail)]
    eof_region = new_raw[tail_start + len(tail):]
    expected_eof = (b'' if (not body or body.endswith(b'\n')) else b'\n')
    expected_eof += b'# roundhouse: ' + provenance.encode('utf-8') + b'\n'
    if eof_region != expected_eof:
        raise VerifyError("eof_region_changed",
                          "appended EOF region is not exactly the provenance line")


def verify_splice(old_unit: UnitFile, new_raw: bytes, edits: List[Edit], provenance: str) -> UnitFile:
    """Verify a splice is correct and return the new parsed unit."""
    # Parse the new file
    path = old_unit.path
    new_unit = parse_unit(path, new_raw)

    # A spliced file whose ExecStart no longer parses must never pass verify: check (a)
    # used to be guarded by `and new_unit.exec_start`, so the single worst outcome the
    # splice can produce — a destroyed ExecStart — silently SKIPPED the whole check.
    if new_unit.exec_start is None:
        raise VerifyError("execstart_unparseable",
                          "spliced file has no parseable ExecStart")

    # Check (a): Profile equality except edited fields and spans
    if old_unit.exec_start:
        old_profile = extract_param_profile(old_unit.exec_start.engine_argv)
        new_profile = extract_param_profile(new_unit.exec_start.engine_argv)

        # Build normalized profile dicts (removing spans and raw_argv)
        def normalize_profile(p):
            d = {k: v for k, v in p.items() if k not in ('spans', 'raw_argv')}
            d['unknown_flags'] = [(f['flag'], f['value']) for f in p['unknown_flags']]
            return d

        old_norm = normalize_profile(old_profile)
        new_norm = normalize_profile(new_profile)
        expected_norm = _expected_profile(old_norm, edits)

        edited_fields = {e.field for e in edits}
        for key in expected_norm:
            if expected_norm[key] != new_norm.get(key):
                touched = (key in edited_fields
                           or key == 'unknown_flags'
                           or any(e.field.startswith(key + '.') for e in edits))
                if touched:
                    raise VerifyError(
                        "profile_changed",
                        f"edited field {key} did not take the expected value")
                raise VerifyError("profile_changed", f"unedited field {key} changed")
        for key in new_norm:
            if key not in expected_norm:
                raise VerifyError("profile_changed", f"unexpected new field {key}")

    # Check (b): Comments unchanged except for provenance
    old_comments = [c['text'] for c in old_unit.comments]
    new_comments = [c['text'] for c in new_unit.comments]
    expected_prov = '# roundhouse: ' + provenance
    if new_comments != old_comments + [expected_prov]:
        raise VerifyError("comments_changed", "comments don't match expected")

    # Check (c): Span invariants, plus the MVP2 addition — the splice touched nothing
    # outside the replaced spans and the appended EOF region.
    assert_span_invariants(new_unit)
    assert_outside_spans_unchanged(old_unit.raw, new_raw, edits, provenance)

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

def _read_file_bytes(path: str) -> bytes:
    """Read a file's bytes. The disk side of the E5 staleness check (§4.2)."""
    with open(path, 'rb') as f:
        return f.read()


def _atomic_write(path: str, data: bytes, mode: Optional[int] = None) -> None:
    """Atomically write data to path via tmp+fsync+replace.

    `mode` (used by `ensure_token`) creates the tmp file with those permissions at
    open() time. Chmod-after-replace leaves a window in which a freshly generated bearer
    token is on disk under the process umask — world-readable on a default umask — and
    `os.replace` carries the TMP file's mode onto the target, so the mode has to be right
    before a single byte is written.
    """
    dir_path = os.path.dirname(path) or '.'

    # Write to tmp file in same directory
    tmp_path = path + '.roundhouse-tmp'
    if mode is None:
        opener = None
    else:
        def opener(p, flags):
            # O_EXCL: never write through a tmp file we did not create (and thus never
            # inherit a mode someone else chose). A leftover tmp from a crash is cleared
            # once, deliberately, rather than bricking every future write.
            try:
                return os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            except FileExistsError:
                os.unlink(p)
                return os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)

    with open(tmp_path, 'wb', opener=opener) as f:
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

# Terminal phases: the slot is unconditionally free in these.
ROLLOUT_TERMINAL_PHASES = ("done", "rolled_back", "rollback_failed")

# Switch phases
SWITCH_PHASES = ("preflight", "stopping", "starting", "watching",
                 "done", "failed", "restoring", "restored", "restore_failed")

# Terminal phases for both kinds (rollout + switch)
OPERATION_TERMINAL_PHASES = ROLLOUT_TERMINAL_PHASES + ("restored", "restore_failed")


def _slot_free(record: Optional[Dict]) -> bool:
    """§4.1: is the global rollout slot idle?

    Free iff no record, or a terminal phase, or `failed` that has been settled — either
    the bytes were restored, or no rollback was ever offered / the offer was dismissed.

    Treating ONLY the three terminal phases as free wedged the slot forever on every
    `failed` that carries no offer (preflight drift, stop/apply/commit errors, stale
    confirm, engine_error) and on every dismissed offer. `record.get('rollback') or {}`
    matters just as much: the key exists with value None from record creation, so
    `.get('rollback', {})` returns None and `.get('offered')` on it raises
    AttributeError — a 500 out of the route, not a 409.
    """
    if record is None:
        return True
    phase = record.get('phase')
    if phase in OPERATION_TERMINAL_PHASES:
        return True
    if phase == 'failed':
        if record.get('restored'):
            return True
        if not (record.get('rollback') or {}).get('offered'):
            return True
    return False


def _rollback_offered(record: Optional[Dict]) -> bool:
    """True iff this record currently carries a live rollback offer."""
    if not record:
        return False
    return bool((record.get('rollback') or {}).get('offered'))


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

    # plan_edits refuses a non-integer port, but preflight is also called with edits built
    # by hand (tests, engine re-runs): a bare int() here turned bad input into a 500.
    try:
        new_port = int(port_edit.new_text)
    except (ValueError, TypeError):
        return {
            "ok": False,
            "check": "port",
            "detail": f"port value is not an integer: {port_edit.new_text!r}"
        }

    # Check against all other units and self
    if new_port == self_port:
        return {
            "ok": False,
            "check": "port",
            "detail": f"port {new_port} is claimed by roundhouse (self)"
        }

    # Build list of other claimants
    claimants = []
    snapshot = locked_snapshot(watcher)

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

    # Check model_path exists if being edited
    for edit in edits:
        if edit.field == "model_path":
            if not os.path.exists(edit.new_text):
                return {
                    "ok": False,
                    "check": "memory",
                    "detail": f"model file not found: {edit.new_text}"
                }

    # Estimate memory using extracted helper
    store = getattr(watcher, 'mem_store', None)
    estimate_bytes, estimate_source = _estimate_start_bytes(unit.name, new_profile, store)

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

    # Freed memory from stopping this unit (E9), using extracted helper.
    #
    # Only a unit that is actually RESIDENT frees anything. The snapshot's `mem` row is a
    # measured peak or an estimate FORMULA and is reported for units that are OFF too, so
    # reading it unconditionally credited the budget with memory nobody is holding.
    freed_bytes = 0
    freed_source = 'none (unit not active)'
    snapshot = locked_snapshot(watcher)
    unit_row = None
    for u in snapshot.get('units', []):
        if u['unit'] == unit.name:
            unit_row = u
            break

    if unit_row:
        cgroup_cache = getattr(watcher, '_cgroup_cache', None) or {}
        freed_bytes, freed_source = _freed_bytes(unit.name, unit_row, cgroup_cache)

    budget = mem_available + freed_bytes
    headroom = HEADROOM_BYTES

    if estimate_bytes + headroom > budget:
        return {
            "ok": False,
            "check": "memory",
            "detail": f"estimated {estimate_bytes/(1024**3):.1f} GiB ({estimate_source}), "
                     f"+ {headroom/(1024**3):.1f} GiB headroom exceeds budget {budget/(1024**3):.1f} GiB "
                     f"(MemAvailable {mem_available/(1024**3):.1f} GiB + "
                     f"{freed_bytes/(1024**3):.1f} GiB freed by stopping {unit.name} "
                     f"[{freed_source}])",
            "estimate_bytes": estimate_bytes,
            "estimate_source": estimate_source,
            "mem_available_bytes": mem_available,
            "freed_bytes": freed_bytes,
            "freed_source": freed_source,
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

        # Warm-up queue and state (mutated only under watcher_lock)
        self.pending_warm = None
        self.last_warm = None
        self.warm_seq = 0

    def start_rollout(self, unit_name: str, edits: List[Edit], confirm: str) -> Dict:
        """Start a new rollout. Returns the rollout record."""
        with self.watcher_lock:
            if not _slot_free(self.current):
                if self.current.get('phase') == 'failed' and _rollback_offered(self.current):
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
                "kind": "rollout",
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
                self._fail_rollout(rollout_id, "preflight", "preflight",
                                   detail="pre-flight checks failed", restored=True)
                return

            # E5 staleness against DISK, not against memory (§4.2 preflight row).
            # The confirm hash was recomputed from the in-memory `unit.raw`, which the
            # watcher only refreshes on its own terms — an external edit between preview
            # and apply hashed identically and was silently clobbered by the splice.
            try:
                disk_raw = _read_file_bytes(unit.path)
            except OSError as exc:
                self._fail_rollout(rollout_id, "preflight", "preflight",
                                   detail=f"cannot read {unit.path}: {exc}", restored=True)
                return
            if disk_raw != unit.raw:
                self._fail_rollout(
                    rollout_id, "preflight", "preview_stale",
                    detail="unit file changed on disk since preview; nothing was touched — re-preview",
                    restored=True)
                return

            # Recompute confirm from the verified disk bytes (identical to unit.raw here,
            # by the check above — the point is that the hash is disk-derived).
            computed_confirm = compute_confirm(unit_name, disk_raw, edits)
            if computed_confirm != confirm:
                self._fail_rollout(rollout_id, "preflight", "preview_stale",
                                   detail="confirm mismatch (stale preview)", restored=True)
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
                        self._fail_rollout(rollout_id, "applying", "stop_error",
                                           detail=f"stop failed: {e}", restored=True)
                        if was_active:
                            try:
                                self._start_unit(unit_name)
                            except Exception:
                                pass
                        return

                # Splice — from the disk bytes verified in preflight (§4.2/E5)
                self._update_phase(rollout_id, "applying", "splicing")
                prov_line = provenance_line(edits, datetime.now(timezone.utc))
                new_raw = splice(disk_raw, edits, prov_line)

                # Write
                self._update_phase(rollout_id, "applying", "writing")
                _atomic_write(unit.path, new_raw)

                # Verify
                self._update_phase(rollout_id, "applying", "verifying")
                new_unit = verify_splice(unit, new_raw, edits, prov_line)

                # Commit
                self._update_phase(rollout_id, "applying", "committing")
                try:
                    run_git(["add", "--", unit_name], self.unit_dir, units=self.units)
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
                    self._fail_rollout(rollout_id, "applying", "commit_error",
                                       detail=f"commit failed: {e}", restored=True)
                    return

                # Update watcher with new unit (S4)
                with self.watcher_lock:
                    self.units[unit_name] = new_unit
                    self.watcher.units[unit_name] = new_unit

            except Exception as e:
                # Restore. `restored` reports what actually happened — claiming a restore
                # that raised would free the slot over a half-written file.
                restored_ok = True
                try:
                    _atomic_write(unit.path, unit.raw)
                except Exception:
                    restored_ok = False
                if was_active:
                    try:
                        self._start_unit(unit_name)
                    except Exception:
                        pass
                self._fail_rollout(rollout_id, "applying", "apply_error", detail=str(e),
                                   restored=restored_ok)
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
        kind = None
        unit = None
        with self.watcher_lock:
            rollout = self.rollouts.get(rollout_id)
            if rollout:
                rollout['phase'] = phase
                rollout['detail'] = detail
                rollout['updated_at'] = time.time()
                kind = rollout.get('kind', 'rollout')
                unit = rollout.get('unit')

        self.event_bus.publish('rollout', {
            'rollout_id': rollout_id,
            'kind': kind,
            'unit': unit,
            'phase': phase,
            'detail': detail,
            'ok': True,
            'ts': time.time()
        })

    def _fail_rollout(self, rollout_id: str, phase: str, reason: str,
                      offer_rollback: bool = False, detail: str = None,
                      restored: bool = False):
        """Mark rollout as failed.

        `reason` is the machine code of §4.2 (`unit_failed`, `no_ready_marker`,
        `watch_timeout`, `daemon_reload`, `start_error`, `preflight`, ...); `detail` is the
        human text. The record's own `detail` is updated too, so a page refresh rebuilding
        the stepper from the snapshot does not show the last in-flight sub-step.

        `restored=True` on every path that put the old bytes back (or never touched them):
        §4.1 reads it to free the slot, and the record has to say so out loud.
        """
        text = detail or reason
        kind = None
        unit = None
        with self.watcher_lock:
            rollout = self.rollouts.get(rollout_id)
            if rollout:
                rollout['phase'] = 'failed'
                rollout['detail'] = text
                rollout['failure'] = {'reason': reason, 'detail': text, 'phase': phase}
                if restored:
                    rollout['restored'] = True
                kind = rollout.get('kind', 'rollout')
                unit = rollout.get('unit')
                # Offer reversibility per §2.6: rollouts require commit; switches need stopped/target_started
                if offer_rollback:
                    if kind == 'rollout':
                        if rollout.get('commit'):
                            rollout['rollback'] = {'offered': True}
                    else:  # switch
                        if rollout.get('stopped') or rollout.get('target_started'):
                            rollout['rollback'] = {'offered': True}
                rollout['updated_at'] = time.time()

        self.event_bus.publish('rollout', {
            'rollout_id': rollout_id,
            'kind': kind,
            'unit': unit,
            'phase': 'failed',
            'detail': text,
            'ok': False,
            'ts': time.time()
        })

    def _stop_unit(self, unit_name: str, timeout: float = STOP_TIMEOUT_SEC):
        """Stop a unit."""
        run_actuate(["systemctl", "--user", "stop", "--", unit_name], self.units, timeout=timeout)

    def _start_unit(self, unit_name: str, timeout: float = START_TIMEOUT_SEC):
        """Start a unit."""
        run_actuate(["systemctl", "--user", "start", "--", unit_name], self.units, timeout=timeout)

    def _daemon_reload(self):
        """Reload systemd daemon."""
        run_actuate(["systemctl", "--user", "daemon-reload"], self.units)

    def _set_enablement(self, unit_name: str, enable: bool) -> None:
        """Enable or disable a unit."""
        verb = "enable" if enable else "disable"
        run_actuate(["systemctl", "--user", verb, "--", unit_name], self.units)

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
        timeout = WATCH_TIMEOUT_SEC

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

    def _watch_to_ready(self, unit_name: str, prior_start_ts: Optional[float], deadline_ts: float) -> tuple:
        """Watch a unit to READY/BUSY, or return failure reason (extracted from _watch_unit).

        Returns: ('ready', elapsed) | ('failed', reason, detail) | ('timeout',)
        """
        start = time.time()

        while time.time() < deadline_ts:
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

            # Freshness gate: check if we're looking at a stale sample
            if prior_start_ts is not None and since == prior_start_ts:
                time.sleep(1)
                continue

            elapsed = time.time() - start
            if rung in ('READY', 'BUSY'):
                return ('ready', elapsed)
            if rung == 'FAILED':
                return ('failed', 'unit_failed', 'unit reached FAILED state')
            if 'no_ready_marker' in badges:
                return ('failed', 'no_ready_marker', 'active but no ready marker seen before TimeoutStartSec')

            time.sleep(1)

        return ('timeout',)

    def start_switch(self, target: str, stops: List[str], confirm: str,
                     origin: str = 'human', requester: Optional[str] = None,
                     warm_seq: Optional[int] = None) -> Dict:
        """Start a switch operation. Returns the switch record.

        Args:
            target: target unit name
            stops: list of unit names to stop
            confirm: confirmation token
            origin: 'human' (default) or 'warm' (autonomous)
            requester: optional string identifying the requester
            warm_seq: if origin='warm', the sequence number of the parked warm

        When origin='warm', this performs consent re-check inside the lock:
        target and stops must all be on-demand-marked, and warm_seq must
        match the pending warm being fired (else warm_cancelled error).
        """
        with self.watcher_lock:
            # SLOT CHECK FIRST (SPEC §4.4, MVP5 review blocker 2): if the warm
            # branch ran first, its pending_warm pop would execute before an
            # operation_in_progress raise — a human slot-claim during the fire
            # window would silently destroy the parked warm with no disposition.
            # Order here: slot, then consent, then pop — one lock hold.
            if not _slot_free(self.current):
                if self.current and self.current.get('kind') == 'switch':
                    raise ActuationError("operation_in_progress")
                elif self.current and self.current.get('phase') == 'failed' and _rollback_offered(self.current):
                    raise ActuationError("operation_in_progress")
                raise ActuationError("operation_in_progress")

            # Consent fence layer 2 (engine): re-validate when origin='warm'
            if origin == 'warm':
                unit = self.units.get(target)
                if not unit or not unit.on_demand:
                    raise ActuationError(f"warm_consent: target {target} is not marked on-demand")

                for stop_unit in stops:
                    stop_unit_obj = self.units.get(stop_unit)
                    if not stop_unit_obj or not stop_unit_obj.on_demand:
                        raise ActuationError(f"warm_consent: stop {stop_unit} is not marked on-demand")

                # Verify warm_seq matches (or the park was cancelled meanwhile), then
                # pop it in the SAME lock hold as the slot claim (H4). The pop belongs
                # to the fire path only: a direct route-driven warm (warm_seq=None)
                # must never silently discard somebody else's parked request.
                if warm_seq is not None:
                    if self.pending_warm is None or self.pending_warm.get('seq') != warm_seq:
                        raise ActuationError("warm_cancelled")
                    self.pending_warm = None

            unit = self.units.get(target)
            if not unit:
                raise ActuationError(f"unit {target} not found")

            # Create switch record
            self.counter += 1
            switch_id = f"sw-{int(time.time())}-{self.counter}"
            now = time.time()

            switch = {
                "rollout_id": switch_id,
                "kind": "switch",
                "unit": target,
                "target": target,
                "stops": stops,
                "stopped": [],
                "target_started": False,
                "phase": "preflight",
                "detail": "checking prerequisites",
                "failure": None,
                "rollback": None,
                "restored": False,
                "started_at": now,
                "updated_at": now,
                "origin": origin,
                "requester": requester,
            }

            self.current = switch
            self.rollouts[switch_id] = switch

            # Spawn worker thread
            threading.Thread(
                target=self._run_switch,
                args=(switch_id, target, stops, confirm),
                name="switch",
                daemon=True
            ).start()

            return switch

    def _run_switch(self, switch_id: str, target: str, stops: List[str], confirm: str):
        """Worker thread for switch execution (§2.1 phase table)."""
        switch = self.rollouts.get(switch_id)
        if not switch:
            return

        try:
            # Preflight: rerun switch_preflight and recompute confirm (§2.1 preflight row)
            self._update_phase(switch_id, "preflight", "checking prerequisites")
            pf = switch_preflight(target, stops, self.watcher, self.units, self.self_port)
            if not pf["ok"]:
                self._fail_rollout(switch_id, "preflight", "preflight",
                                 detail="pre-flight checks failed", restored=True)
                return

            # Recompute confirm from the current snapshot and compare
            with self.watcher_lock:
                snapshot = self.watcher.snapshot()
            fingerprint = fleet_fingerprint(snapshot)
            computed_confirm = compute_switch_confirm(target, stops, fingerprint)
            if computed_confirm != confirm:
                self._fail_rollout(switch_id, "preflight", "preview_stale",
                                 detail="fleet state changed since preview (a unit started or stopped); re-preview", restored=True)
                return

            # Stopping phase: sequential stops with confirmation
            stop_count = len(stops)
            for i, stop_unit in enumerate(stops):
                detail = f"stopping {stop_unit} ({i+1}/{stop_count})"
                self._update_phase(switch_id, "stopping", detail)

                try:
                    self._stop_unit(stop_unit, timeout=STOP_TIMEOUT_SEC)
                except Exception as e:
                    self._fail_rollout(switch_id, "stopping", "stop_error",
                                     offer_rollback=bool(switch.get('stopped')), detail=f"stop failed: {e}")
                    return

                # Confirm OFF
                try:
                    status = self._confirm_off(stop_unit)
                except Exception:
                    status = ''
                if not status:
                    self._fail_rollout(switch_id, "stopping", "stop_unconfirmed",
                                     offer_rollback=bool(switch.get('stopped')),
                                     detail=f"unit {stop_unit} did not confirm OFF within {CONFIRM_OFF_TIMEOUT_SEC}s")
                    return

                # Record the stopped unit.
                with self.watcher_lock:
                    switch = self.rollouts.get(switch_id)
                    if switch:
                        switch['stopped'].append(stop_unit)
                        switch['updated_at'] = time.time()

                # A FAILED-after-stop counts as stopped (the process is dead and its
                # memory is freed) but must say so out loud — on the record AND on the
                # stream, or the operator never learns the unit did not exit cleanly (F2).
                if status == 'failed':
                    self._update_phase(switch_id, "stopping",
                                       f"{detail} (unit FAILED; considered stopped)")

            # Starting phase
            self._update_phase(switch_id, "starting", f"starting {target}")

            # Capture prior_start_ts before starting
            prior_start_ts = None
            with self.watcher_lock:
                snapshot = self.watcher.snapshot()
                for u in snapshot.get('units', []):
                    if u['unit'] == target:
                        prior_start_ts = u.get('since')
                        break

            try:
                self._start_unit(target)
            except Exception as e:
                self._fail_rollout(switch_id, "starting", "start_error",
                                 offer_rollback=True, detail=f"start failed: {e}")
                return

            # Mark target as started BEFORE watching (per §2.1)
            with self.watcher_lock:
                switch = self.rollouts.get(switch_id)
                if switch:
                    switch['target_started'] = True
                    switch['updated_at'] = time.time()

            # Watching phase
            self._update_phase(switch_id, "watching", f"watching {target}")
            deadline = time.time() + WATCH_TIMEOUT_SEC
            result = self._watch_to_ready(target, prior_start_ts, deadline)

            if result[0] == 'ready':
                elapsed = result[1]
                self._update_phase(switch_id, "done", f"switched: {target} ready in {elapsed:.1f}s")
                with self.watcher_lock:
                    switch = self.rollouts.get(switch_id)
                    if switch:
                        switch['phase'] = 'done'
                        switch['restored'] = False
                        switch['updated_at'] = time.time()
            elif result[0] == 'failed':
                reason, detail = result[1], result[2]
                self._fail_rollout(switch_id, "watching", reason, offer_rollback=True, detail=detail)
            else:  # timeout
                self._fail_rollout(switch_id, "watching", "watch_timeout",
                                 offer_rollback=True, detail=f"watch timeout ({WATCH_TIMEOUT_SEC}s)")

        except Exception as e:
            self._fail_rollout(switch_id, "failed", "engine_error", detail=str(e))

    def _confirm_off(self, unit_name: str) -> str:
        """Poll the roster until `unit_name` is confirmed no longer running (F2).

        Returns a STATUS STRING, falsy only on timeout, so callers can both gate on
        truthiness and tell the two confirmed outcomes apart:
          `'off'`     — rung left ACTIVE_RUNGS in a sample sensed after the stop returned
          `'failed'`  — rung is FAILED: the process is dead and its memory is freed, which
                        is all a switch needs, but the caller appends a notice (F2)
          `''`        — not confirmed within CONFIRM_OFF_TIMEOUT_SEC
        """
        start = time.time()
        timeout = CONFIRM_OFF_TIMEOUT_SEC

        while time.time() - start < timeout:
            with self.watcher_lock:
                snapshot = self.watcher.snapshot()
                for u in snapshot.get('units', []):
                    if u['unit'] == unit_name:
                        rung = u.get('rung', 'OFF')
                        sensed_at = u.get('sensed_at', 0)
                        # FAILED means the main process is gone; no freshness gate is
                        # needed (a stop candidate was ACTIVE at preflight by construction).
                        if rung == 'FAILED':
                            return 'failed'
                        # Otherwise require a sample sensed AFTER the stop command returned:
                        # the 3 s tick means the first samples still describe the live unit.
                        if rung not in ACTIVE_RUNGS and sensed_at > start:
                            return 'off'
                        break

            time.sleep(1)

        return ''

    def _run_restore(self, switch_id: str):
        """Worker thread for restore after a failed switch (§2.1 restoring phase)."""
        switch = self.rollouts.get(switch_id)
        if not switch:
            return

        try:
            target = switch.get('target')
            stopped_units = list(switch.get('stopped', []))
            # §2.1: 900 s TOTAL for the restore, one shared clock — not per unit.
            deadline = time.time() + WATCH_TIMEOUT_SEC

            # Stop target if active and non-fatal
            if target:
                self._update_phase(switch_id, "restoring", f"stopping {target}")
                try:
                    # Check if target is active
                    with self.watcher_lock:
                        snapshot = self.watcher.snapshot()
                        target_active = False
                        for u in snapshot.get('units', []):
                            if u['unit'] == target and u.get('rung') in ACTIVE_RUNGS:
                                target_active = True
                                break

                    if target_active:
                        try:
                            self._stop_unit(target)
                            # Wait for it to actually die before restarting the units it
                            # displaced — otherwise the restored unit races the target for
                            # the port. Non-fatal: the target may already be gone.
                            self._confirm_off(target)
                        except Exception:
                            pass  # Log but don't fail
                except Exception:
                    pass

            # Restart stopped units in original order
            for idx, unit in enumerate(stopped_units):
                self._update_phase(switch_id, "restoring", f"starting {unit}")
                try:
                    self._start_unit(unit)
                except Exception as e:
                    # Everything from this unit onward is still down: name exactly those.
                    manual_cmd = f"systemctl --user start {' '.join(stopped_units[idx:])}"
                    self._update_phase(switch_id, "restore_failed",
                                       f"restore failed; manual recovery: {manual_cmd}")
                    with self.watcher_lock:
                        switch = self.rollouts.get(switch_id)
                        if switch:
                            switch['phase'] = 'restore_failed'
                            switch['failure'] = {'reason': 'restore_start_failed', 'detail': str(e)}
                            switch['detail'] = f"restore failed; manual recovery: {manual_cmd}"
                            switch['updated_at'] = time.time()
                    return

                # Watch each unit back to READY against the shared restore deadline
                result = self._watch_to_ready(unit, None, deadline)

                if result[0] != 'ready':
                    reason = result[1] if result[0] == 'failed' else 'watch_timeout'
                    # `unit` was started but never came up, so it stays in the list too.
                    manual_cmd = f"systemctl --user start {' '.join(stopped_units[idx:])}"
                    self._update_phase(switch_id, "restore_failed",
                                       f"restore incomplete ({unit}: {reason}); manually start: {manual_cmd}")
                    with self.watcher_lock:
                        switch = self.rollouts.get(switch_id)
                        if switch:
                            switch['phase'] = 'restore_failed'
                            switch['failure'] = {'reason': 'restore_watch_failed', 'detail': reason}
                            switch['detail'] = (
                                f"restore incomplete ({unit}: {reason}); manually start: {manual_cmd}")
                            switch['updated_at'] = time.time()
                    return

            # Success: all units back to READY
            self._update_phase(switch_id, "restored", f"restored: {len(stopped_units)} unit(s) back to READY")
            with self.watcher_lock:
                switch = self.rollouts.get(switch_id)
                if switch:
                    switch['phase'] = 'restored'
                    switch['rollback'] = {'offered': False, 'phase': 'restored'}
                    switch['updated_at'] = time.time()

        except Exception as e:
            self._update_phase(switch_id, "restore_failed", str(e))
            with self.watcher_lock:
                switch = self.rollouts.get(switch_id)
                if switch:
                    switch['phase'] = 'restore_failed'
                    switch['updated_at'] = time.time()

    def rollback(self, rollout_id: str):
        """Start rollback/restore of a failed operation (rollout or switch).

        Eligibility check AND the transition happen under `watcher_lock`:
        check-and-set outside it let a double-click through twice.
        """
        with self.watcher_lock:
            rollout = self.rollouts.get(rollout_id)
            if not rollout:
                raise ActuationError("not_rollbackable")

            kind = rollout.get('kind', 'rollout')

            # Eligibility: rollouts require commit; switches only require a live offer
            if kind == 'rollout':
                if not rollout.get('commit'):
                    raise ActuationError("not_rollbackable")
            # switches have no commit requirement - just checked by offer

            if rollout.get('phase') != 'failed' or not _rollback_offered(rollout):
                raise ActuationError("not_rollbackable")

            # Claim the offer inside the lock: the loser of a race (second click, or a
            # dismiss arriving concurrently) now sees phase != 'failed' / no offer.
            if kind == 'switch':
                rollout['phase'] = 'restoring'
                rollout['rollback'] = {'offered': False, 'phase': 'restoring'}
            else:
                rollout['phase'] = 'rolling_back'
                rollout['rollback'] = {'offered': False, 'phase': 'rolling_back'}
            rollout['updated_at'] = time.time()

            # Spawn appropriate worker
            if kind == 'switch':
                worker_target = self._run_restore
            else:
                worker_target = self._run_rollback

        # Spawn worker (outside lock)
        threading.Thread(
            target=worker_target,
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
                    run_git(["add", "--", unit_name], self.unit_dir, units=self.units)
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
        """Dismiss a failed rollout's rollback offer, freeing the slot (§6).

        Same lock as `rollback()` and the same first-one-wins rule: whichever of
        dismiss/rollback takes the lock first settles the offer; the loser raises
        `not_dismissable` / `not_rollbackable` and the route answers 409.
        """
        with self.watcher_lock:
            rollout = self.rollouts.get(rollout_id)
            if not rollout or rollout.get('phase') != 'failed' or not _rollback_offered(rollout):
                raise ActuationError("not_dismissable")

            # The record is SET (not just read): `_slot_free` reads `rollback.offered`, so
            # a dismissed offer has to leave a record that says so.
            rollout['rollback'] = {'offered': False, 'dismissed': True}
            rollout['updated_at'] = time.time()

    def warm_state(self) -> Dict:
        """Return the current warm-up state for GET /api/warm (read route).

        Returns a dict with 'pending' (record or None) and 'last' (record or None),
        captured under the lock.
        """
        with self.watcher_lock:
            pending = None
            if self.pending_warm:
                pending = dict(self.pending_warm)

            last = None
            if self.last_warm:
                last = dict(self.last_warm)

            return {'pending': pending, 'last': last}

    def tick_pending_warm(self):
        """Fire the pending warm if the slot is free (called from poll_systemctl).

        Runs outside the lock; takes the lock only to check state.
        """
        with self.watcher_lock:
            p = self.pending_warm
            if p is None or not _slot_free(self.current):
                return

            seq = p['seq']
            target = p['unit']
            requester = p['requester']

        # Fire outside the lock (fresh snapshot + re-plan + re-preflight)
        self._fire_warm(target, requester, seq)

    def _fire_warm(self, target: str, requester: Optional[str], seq: int):
        """Fire a parked warm request with fresh snapshot and re-preflight.

        Called from tick_pending_warm; drops to last_warm on failure.
        """
        try:
            # Fresh snapshot + re-plan + re-preflight
            with self.watcher_lock:
                snap = self.watcher.snapshot()

            unit = self.units.get(target)

            # Check 1: Target retired
            target_row = None
            for row in snap.get('units', []):
                if row['unit'] == target:
                    target_row = row
                    break

            if not target_row or target_row.get('retired'):
                # Drop: retired
                with self.watcher_lock:
                    if self.pending_warm and self.pending_warm.get('seq') == seq:
                        self.pending_warm = None
                        self.last_warm = {
                            'unit': target,
                            'requester': requester,
                            'disposition': 'already_warm',
                            'detail': 'unit is retired',
                            'at': time.time()
                        }
                return

            # Check 2: Target on-demand
            if not unit or not unit.on_demand:
                # Drop: not marked
                with self.watcher_lock:
                    if self.pending_warm and self.pending_warm.get('seq') == seq:
                        self.pending_warm = None
                        self.last_warm = {
                            'unit': target,
                            'requester': requester,
                            'disposition': 'not_on_demand',
                            'detail': 'unit is not marked on-demand',
                            'at': time.time()
                        }
                return

            # Check 3: Target already warm
            rung = target_row.get('rung')
            if rung in ('READY', 'BUSY', 'STARTING', 'LOADING'):
                # Drop: already warm
                with self.watcher_lock:
                    if self.pending_warm and self.pending_warm.get('seq') == seq:
                        self.pending_warm = None
                        self.last_warm = {
                            'unit': target,
                            'requester': requester,
                            'disposition': 'already_warm',
                            'detail': f'unit is {rung}',
                            'at': time.time()
                        }
                return

            # Warm plan
            cgroup_cache = getattr(self.watcher, '_cgroup_cache', None) or {}
            mem_store = self.watcher.mem_store
            plan = warm_plan(target, snap, self.units, cgroup_cache, mem_store)

            if not plan['fits']:
                # Drop: unfittable
                with self.watcher_lock:
                    if self.pending_warm and self.pending_warm.get('seq') == seq:
                        self.pending_warm = None
                        self.last_warm = {
                            'unit': target,
                            'requester': requester,
                            'disposition': 'consent_unfittable',
                            'detail': f"estimated {plan['estimate_bytes']} bytes + headroom {plan['headroom_bytes']} bytes exceeds available + freed by consenting stops",
                            'at': time.time()
                        }
                return

            # Switch preflight
            pf = switch_preflight(target, plan['stops'], self.watcher, self.units, self.self_port)
            if not pf['ok']:
                # Drop: preflight failed
                with self.watcher_lock:
                    if self.pending_warm and self.pending_warm.get('seq') == seq:
                        self.pending_warm = None
                        self.last_warm = {
                            'unit': target,
                            'requester': requester,
                            'disposition': 'preflight_failed',
                            'detail': 'preflight checks failed',
                            'at': time.time()
                        }
                return

            # Start switch
            try:
                result = self.start_switch(target, plan['stops'], pf['confirm'],
                                           origin='warm', requester=requester, warm_seq=seq)

                # Record success
                with self.watcher_lock:
                    self.last_warm = {
                        'unit': target,
                        'requester': requester,
                        'disposition': 'started',
                        'rollout_id': result.get('rollout_id'),
                        'at': time.time()
                    }

            except ActuationError as e:
                err_str = str(e)
                if 'operation_in_progress' in err_str:
                    # Slot stolen by human: leave parked and retry next tick
                    return
                elif 'warm_cancelled' in err_str:
                    # Already cleared by cancel handler
                    return
                else:
                    # Other error: drop
                    with self.watcher_lock:
                        if self.pending_warm and self.pending_warm.get('seq') == seq:
                            self.pending_warm = None
                            self.last_warm = {
                                'unit': target,
                                'requester': requester,
                                'disposition': 'error',
                                'detail': str(e),
                                'at': time.time()
                            }

        except Exception as e:
            # Catch-all for unexpected errors
            with self.watcher_lock:
                if self.pending_warm and self.pending_warm.get('seq') == seq:
                    self.pending_warm = None
                    self.last_warm = {
                        'unit': target,
                        'requester': requester,
                        'disposition': 'error',
                        'detail': str(e),
                        'at': time.time()
                    }


# ===== SECTION E PART 3: SWITCH (lifecycle verbs only; no file writes, no git, no daemon-reload) =====

def fleet_fingerprint(snapshot: Dict) -> Dict[str, str]:
    """Compute a fingerprint of the fleet's lifecycle state (F3).

    Returns {unit: ts_mono_str} for every non-retired selected unit.
    ts_mono_str = the unit's ExecMainStartTimestampMonotonic as a string,
    with None/''/'0' canonicalized to '0'.
    """
    result = {}
    for u in snapshot.get('units', []):
        if not u.get('retired'):
            ts_mono = u.get('start_ts_mono') or '0'
            result[u['unit']] = str(ts_mono)
    return result


def compute_switch_confirm(target: str, stops: List[str], fingerprint: Dict[str, str]) -> str:
    """Compute the switch confirm hash (F3 canonicalization).

    confirm = sha256(canonical_json({
        "kind": "switch",
        "target": name,
        "stops": sorted(ticked names),
        "fingerprint": {unit: ts_mono_str for every non-retired selected unit}
    })).hexdigest()

    Fingerprinting all units (not just target+stops) is deliberate: any unit
    starting or stopping between preview and execute invalidates the memory
    arithmetic and the runtime port picture.
    """
    obj = {
        "kind": "switch",
        "target": target,
        "stops": sorted(stops),
        "fingerprint": fingerprint
    }
    canonical_json = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode()).hexdigest()


def _estimate_start_bytes(unit_name: str, profile: Dict, mem_store) -> tuple:
    """Estimate memory needed to start a unit (bytes, source_label).

    Order: exact measured (unit, file_id, ctx) -> "measured"
           newest measured (unit, file_id, any ctx) -> "measured at ctx <c>; target ctx unproven"
           formula int(size*1.10 + 1.5 GiB) -> "formula"
           9 GiB default -> "default"
    """
    model_path = profile.get('model_path')
    ctx = profile.get('ctx')

    # Try exact measurement first
    if model_path:
        try:
            file_id = f"sz{os.stat(model_path).st_size}:mt{int(os.stat(model_path).st_mtime)}"
            if mem_store:
                mem = mem_store.lookup(unit_name, file_id, ctx)
                if mem:
                    return (mem['bytes'], "measured")
        except Exception:
            pass

    # Fallback to formula
    if model_path:
        try:
            size = os.path.getsize(model_path)
            return (int(size * 1.10 + 1.5 * 1024**3), "formula")
        except Exception:
            pass

    # Default fallback
    return (int(9 * 1024**3), "default")


def _freed_bytes(unit_name: str, unit_row: Dict, cgroup_cache: Dict) -> tuple:
    """Estimate memory freed by stopping a unit (bytes, source_label).

    Only a unit that is actually RESIDENT frees anything.
    Order: cgroup memory.current -> last_peak -> measured row -> 0
    """
    rung = unit_row.get('rung')
    if rung not in ACTIVE_RUNGS:
        return (0, 'none (unit not active)')

    # Check cgroup
    cgroup = cgroup_cache.get(unit_name) or {}
    current = cgroup.get('current')
    if current:
        return (current, 'cgroup memory.current')

    last_peak = cgroup.get('last_peak')
    if last_peak:
        return (last_peak, 'cgroup memory.peak (last sample)')

    # Check measured row
    row_mem = unit_row.get('mem') or {}
    if row_mem.get('source') == 'measured' and row_mem.get('bytes'):
        return (row_mem['bytes'], 'measured peak row')

    # Active but no sample yet
    return (0, 'active, but no cgroup sample yet')


def switch_preflight(target: str, stops: List[str], watcher: 'Watcher', units: Dict[str, 'UnitFile'],
                    self_port: int, meminfo_reader=None) -> Dict:
    """Run preflight checks for a switch operation (§3.2 five checks exactly).

    Returns {
        "ok": bool,
        "checks": [...],
        "target": {...},
        "stop_candidates": [...],
        "fit": {...},
        "port": {...},
        "suggested_stops": [...],
        "notices": [...],
        "confirm": "..." (only if all checks pass)
    }
    """
    checks = []
    notices = []

    # One snapshot for everything (§3.3 one-snapshot rule)
    snapshot = locked_snapshot(watcher)
    cgroup_cache = getattr(watcher, '_cgroup_cache', None) or {}
    mem_store = getattr(watcher, 'mem_store', None)

    # 1. RETIRED CHECK
    target_unit = units.get(target)
    if not target_unit:
        return {
            "ok": False,
            "checks": [{"ok": False, "check": "retired",
                       "detail": f"unit {target} not found"}],
            "target": {},
            "stop_candidates": [],
            "fit": {},
            "port": {},
            "suggested_stops": [],
            "notices": []
        }

    if target_unit.retired:
        retired_detail = target_unit.retired_note or '[RETIRED]'
        return {
            "ok": False,
            "checks": [{"ok": False, "check": "retired",
                       "detail": f"unit is {retired_detail} — structurally excluded from every actuation path"}],
            "target": {},
            "stop_candidates": [],
            "fit": {},
            "port": {},
            "suggested_stops": [],
            "notices": []
        }

    # The retired check passed: record it, so a successful preview carries all five
    # §3.2 rows (the failing paths above return the single offending row).
    checks.append({"ok": True, "check": "retired"})

    # 2. TARGET RUNG CHECK (F9: must be OFF with per-rung details)
    target_row = None
    for u in snapshot.get('units', []):
        if u['unit'] == target:
            target_row = u
            break

    target_rung = target_row.get('rung', 'OFF') if target_row else 'OFF'
    target_check_ok = (target_rung == 'OFF')
    target_check_detail = None

    if not target_check_ok:
        if target_rung == 'STANDBY':
            # The gate dict is `parse_gate`'s shape: {'kind', 'wants', 'raw'} — it carries
            # neither a 'kernel' nor a 'running' key, so reading those rendered
            # "waiting for kernel None (running: None)". The running release lives on the
            # snapshot (os.uname()[2]); `wants` is None for an opaque gate, where the raw
            # ExecCondition is the only honest thing to show.
            gate = (target_row or {}).get('gate') or {}
            wants = gate.get('wants') or gate.get('raw') or 'an unknown kernel'
            running = (snapshot.get('kernel')
                       or getattr(watcher, 'running_kernel', None) or 'unknown')
            target_check_detail = f"waiting for kernel {wants} (running: {running})"
        elif target_rung == 'RETIRED':
            target_check_detail = "unit is retired"
        elif target_rung in ACTIVE_RUNGS:
            target_check_detail = "already active"
        elif target_rung == 'FAILED':
            target_check_detail = "unit is FAILED — clear the failure by hand first"
        else:
            target_check_detail = f"target rung is {target_rung}"

        return {
            "ok": False,
            "checks": [{"ok": False, "check": "target", "detail": target_check_detail}],
            "target": target_row or {},
            "stop_candidates": [],
            "fit": {},
            "port": {},
            "suggested_stops": [],
            "notices": []
        }

    target_check = {"ok": True, "check": "target"}
    checks.append(target_check)

    # Build stop candidates list for checks and response
    stop_candidates = []
    for u in snapshot.get('units', []):
        if u['unit'] != target and u.get('rung') in ACTIVE_RUNGS and not u.get('retired'):
            stop_candidates.append(u)

    # 3. STOPS CHECK
    stops_check_ok = True
    stop_offenders = []

    # A duplicated tick is not harmless: the freed-memory sum below iterates `stops`, so
    # the same unit's residency would be counted twice — inflating the budget until a
    # switch that does NOT fit passes the fit check — and the worker would append it to
    # `stopped` twice, making the restore start it twice. F9 lists duplicates as an
    # ineligible-stop case; enforce it here, once, before any arithmetic runs.
    seen = set()
    for stop_unit in stops:
        if stop_unit in seen:
            stop_offenders.append(f"{stop_unit} (listed twice)")
            continue
        seen.add(stop_unit)
        if stop_unit == target:
            stop_offenders.append(f"{stop_unit} (cannot stop target)")
        elif stop_unit not in units:
            stop_offenders.append(f"{stop_unit} (not found)")
        elif units[stop_unit].retired:
            stop_offenders.append(f"{stop_unit} (retired)")
        else:
            # Check if in active candidates
            found_active = False
            for u in stop_candidates:
                if u['unit'] == stop_unit:
                    found_active = True
                    break
            if not found_active:
                stop_offenders.append(f"{stop_unit} (not active)")

    if stop_offenders:
        stops_check_ok = False

    stops_check = {
        "ok": stops_check_ok,
        "check": "stops",
        "detail": f"ineligible stops: {', '.join(stop_offenders)}" if stop_offenders else ""
    }
    checks.append(stops_check)

    if not stops_check_ok:
        return {
            "ok": False,
            "checks": checks,
            "target": target_row or {},
            "stop_candidates": [
                {
                    "unit": u['unit'],
                    "rung": u.get('rung', 'OFF'),
                    "resident_bytes": _freed_bytes(u['unit'], u, cgroup_cache)[0],
                    "resident_source": _freed_bytes(u['unit'], u, cgroup_cache)[1],
                    "port": u.get('port'),
                    "alias": u.get('alias', u['unit']),
                    "ticked": u['unit'] in stops
                }
                for u in stop_candidates
            ],
            "fit": {},
            "port": {},
            "suggested_stops": [],
            "notices": notices
        }

    # 4. MEMORY CHECK
    target_profile = {}
    if target_unit.exec_start:
        target_profile = extract_param_profile(target_unit.exec_start.engine_argv)

    estimate_bytes, estimate_source = _estimate_start_bytes(target, target_profile, mem_store)

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

    # Compute freed bytes for ticked stops (multi-stop with per-unit breakdown)
    freed_by = []
    freed_bytes = 0
    for stop_unit in stops:
        stop_row = None
        for u in snapshot.get('units', []):
            if u['unit'] == stop_unit:
                stop_row = u
                break

        if stop_row:
            bytes_val, source_label = _freed_bytes(stop_unit, stop_row, cgroup_cache)
            freed_bytes += bytes_val
            freed_by.append({"unit": stop_unit, "bytes": bytes_val, "source": source_label})

    budget = mem_available + freed_bytes
    headroom = HEADROOM_BYTES
    memory_ok = (estimate_bytes + headroom <= budget)

    memory_check = {
        "ok": memory_ok,
        "check": "memory",
        "estimate_bytes": estimate_bytes,
        "estimate_source": estimate_source,
        "mem_available_bytes": mem_available,
        "freed_bytes": freed_bytes,
        "freed_by": freed_by,
        "headroom_bytes": headroom,
        "budget_bytes": budget
    }

    if not memory_ok:
        memory_check['detail'] = (
            f"estimated {estimate_bytes/(1024**3):.1f} GiB ({estimate_source}), "
            f"+ {headroom/(1024**3):.1f} GiB headroom exceeds budget {budget/(1024**3):.1f} GiB "
            f"(MemAvailable {mem_available/(1024**3):.1f} GiB + "
            f"{freed_bytes/(1024**3):.1f} GiB freed by stopping {len(stops)} unit(s))"
        )

    checks.append(memory_check)

    # 5. PORT CHECK (F4 runtime rule)
    target_port = (target_row or {}).get('port', 8080)
    port_blockers = []
    port_notices = []

    for u in snapshot.get('units', []):
        if u['unit'] != target and u.get('port') == target_port:
            u_rung = u.get('rung', 'OFF')
            if u_rung in ACTIVE_RUNGS and u['unit'] not in stops:
                # Blocker: active and not ticked to stop
                port_blockers.append({
                    "unit": u['unit'],
                    "rung": u_rung,
                    "detail": f"port {target_port} will still be bound after the plan: {u['unit']} ({u_rung}, not ticked)"
                })
            else:
                # Notice: ticked/STANDBY/RETIRED claimant
                port_notices.append({
                    "unit": u['unit'],
                    "rung": u_rung,
                    "detail": f"port {target_port} also declared by {u['unit']} ({u_rung})"
                })

    # Self-port check
    if target_port == self_port:
        port_blockers.append({
            "unit": "roundhouse",
            "rung": "N/A",
            "detail": f"port {target_port} is in use by roundhouse itself"
        })

    port_check = {
        "ok": len(port_blockers) == 0,
        "check": "port",
        "port": target_port,
        "blockers": port_blockers,
        "notices": port_notices
    }

    checks.append(port_check)

    # Suggested stops (F7)
    suggested_stops = []
    if not memory_ok:
        suggested_stops = suggest_stops(target, stops, stop_candidates,
                                       estimate_bytes, budget, freed_by,
                                       cgroup_cache, mem_store)

    # Compute confirm hash only if all checks pass
    confirm = None
    ok = all(c["ok"] for c in checks)
    if ok:
        fingerprint = fleet_fingerprint(snapshot)
        confirm = compute_switch_confirm(target, stops, fingerprint)

    return {
        "ok": ok,
        "checks": checks,
        "target": target_row or {},
        "stop_candidates": [
            {
                "unit": u['unit'],
                "rung": u.get('rung', 'OFF'),
                "resident_bytes": _freed_bytes(u['unit'], u, cgroup_cache)[0],
                "resident_source": _freed_bytes(u['unit'], u, cgroup_cache)[1],
                "port": u.get('port'),
                "alias": u.get('alias', u['unit']),
                "ticked": u['unit'] in stops
            }
            for u in stop_candidates
        ],
        "fit": memory_check,
        "port": port_check,
        "suggested_stops": suggested_stops,
        "notices": notices,
        "confirm": confirm
    }


def suggest_stops(target: str, stops: List[str], stop_candidates: List[Dict],
                 estimate: int, budget: int, freed_by: List[Dict],
                 cgroup_cache: Dict, mem_store) -> List[str]:
    """Compute suggested stops per F7 greedy-by-residency rule.

    Only when fit fails with submitted ticks: walk active, un-ticked, non-target
    candidates in order (resident_bytes descending, name ascending tie-break),
    hypothetically adding each to freed sum until estimate + HEADROOM <= budget
    or candidates exhausted. Return added names in walk order; empty list if fit passes.
    """
    if estimate + HEADROOM_BYTES <= budget:
        return []

    # Build candidate list: active, un-ticked, not target
    candidates_for_suggestion = []
    for u in stop_candidates:
        if u['unit'] not in stops and u['unit'] != target:
            resident_bytes, _ = _freed_bytes(u['unit'], u, cgroup_cache)
            candidates_for_suggestion.append((u['unit'], resident_bytes))

    # Sort by resident_bytes descending, name ascending (tie-break)
    candidates_for_suggestion.sort(key=lambda x: (-x[1], x[0]))

    # Greedily add until fit or exhaustion
    suggested = []
    current_budget = budget
    for unit_name, resident_bytes in candidates_for_suggestion:
        if estimate + HEADROOM_BYTES <= current_budget:
            break
        suggested.append(unit_name)
        current_budget += resident_bytes

    return suggested


# ===== SECTION E PART 4: ENABLEMENT (boot strategy; enable/disable only; slotless; no file writes, no git, no daemon-reload) =====

def enablement_preflight(unit_name: str, enable: bool, snapshot: Dict, units: Dict[str, 'UnitFile'],
                         self_port: int) -> Dict:
    """Preflight check for enablement toggle per G3.

    Args:
        unit_name: name of the unit to enable/disable
        enable: True to enable, False to disable
        snapshot: locked snapshot from watcher
        units: selected units dict
        self_port: Roundhouse's own port

    Returns:
        dict shaped {
            "ok": bool,
            "checks": [...],
            "port": int,
            "claimants": [...]  # only if ok=False and enable=True
        }
    """
    # Find target unit row in snapshot
    target_row = None
    for u in snapshot.get('units', []):
        if u['unit'] == unit_name:
            target_row = u
            break

    if not target_row:
        return {
            "ok": False,
            "checks": [{"ok": False, "check": "retired", "detail": f"unit {unit_name} not found"}]
        }

    # Check 1: RETIRED (both directions)
    if target_row.get('retired'):
        return {
            "ok": False,
            "checks": [{"ok": False, "check": "retired",
                       "detail": f"unit is [RETIRED] — structurally excluded from every actuation path"}]
        }

    # Check 2: Disable always passes
    if not enable:
        return {
            "ok": True,
            "checks": [{"ok": True, "check": "retired"}]
        }

    # Check 3: Enable collision preflight (rung-blind, defaults count, gates don't exempt)
    target_port = target_row.get('port')
    claimants = []

    # Collect claimants: other rows, not retired, enabled true, same port
    for u in snapshot.get('units', []):
        if u['unit'] == unit_name:
            continue  # Skip self
        if u.get('retired'):
            continue  # Skip retired
        if not u.get('enabled'):
            continue  # Skip disabled
        if u.get('port') != target_port:
            continue  # Skip different port

        # This is a claimant
        claimant = {
            'unit': u['unit'],
            'alias': u.get('alias', u['unit']),
            'port': u.get('port'),
            'rung': u.get('rung', 'OFF'),
            'enabled': True,
            'gate': u.get('gate')
        }
        claimants.append(claimant)

    # Check self_port pseudo-claimant
    if target_port == self_port:
        claimants.append({
            'unit': 'roundhouse (self)',
            'alias': 'roundhouse (self)',
            'port': self_port,
            'rung': None,
            'enabled': True,
            'gate': None
        })

    if claimants:
        # Format detail string per G3
        claimant_strs = []
        for c in claimants:
            if c['unit'] == 'roundhouse (self)':
                claimant_strs.append('roundhouse (self)')
            else:
                gate_suffix = ', kernel-gated' if c.get('gate') else ''
                claimant_strs.append(f"{c['unit']} (enabled, {c['rung']}{gate_suffix})")

        detail = f"port {target_port} is already a boot claim of: {', '.join(claimant_strs)}"
        if target_port == self_port:
            detail = f"port {target_port} is roundhouse's own port"

        return {
            "ok": False,
            "checks": [{"ok": False, "check": "retired", "detail": detail}],
            "port": target_port,
            "claimants": claimants
        }

    return {
        "ok": True,
        "checks": [{"ok": True, "check": "retired"}],
        "port": target_port,
        "claimants": []
    }


# ===== SECTION E PART 5: ROUTING-CONFIG + WARM (generation is a pure read; warm reuses start_switch; no new verbs, no file writes, no git) =====

# YAML keyword ambiguity set
YAML_AMBIGUOUS = {'true', 'false', 'yes', 'no', 'on', 'off', 'null', 'none', '~'}

# Safe bare string pattern: alphanumeric, dot, underscore, slash, dash.
# Matched with fullmatch(), NOT match(): in a `...$` pattern, `$` also matches just
# before a trailing newline, so 'alias\n' would qualify as bare and inject a raw line
# break into the document (§3.3 spells the rule as re.fullmatch for exactly this).
SAFE_BARE_RE = re.compile(r'[A-Za-z0-9._/-]+')


def _yaml_str(s: str) -> str:
    """Quote a string value per H2/§3 quoting rules exactly.

    Bare iff matches [A-Za-z0-9._/-]+ AND not numeric-looking AND
    not leading dash AND not in YAML_AMBIGUOUS set.
    Everything else → double-quoted with exact escape rules.
    """
    if not isinstance(s, str):
        s = str(s)

    # Check bare class conditions
    # Numeric-looking: integers, floats, scientific notation, etc.
    is_numeric_looking = re.fullmatch(r'[0-9.+-]*[0-9](?:[eE][+-]?[0-9]+)?|[0-9.+-]+', s)

    is_safe_bare = (
        SAFE_BARE_RE.fullmatch(s) is not None and
        not s.startswith('-') and
        not is_numeric_looking and
        s.lower() not in YAML_AMBIGUOUS
    )

    if is_safe_bare:
        return s

    # Double-quote and escape
    escaped = ''
    for c in s:
        if c == '\\':
            escaped += '\\\\'
        elif c == '"':
            escaped += '\\"'
        elif ord(c) < 0x20 or ord(c) == 0x7f:
            escaped += f'\\x{ord(c):02x}'
        else:
            escaped += c

    return f'"{escaped}"'


def _yaml_scalar(value):
    """Format a scalar value per §3.3 rules.

    bool → true/false; int → str; float → repr; str → _yaml_str;
    None → asserts (should never reach here).
    """
    if value is None:
        assert False, "None should not reach emitter (H3 null-omission)"
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return _yaml_str(value)
    return _yaml_str(str(value))


def emit_routing_yaml(meta: Dict, entries: List[Dict]) -> str:
    """Emit a routing config fragment in YAML per §3.2.

    Args:
        meta: dict with 'generated_by', 'generated_at', 'warm_hook'
        entries: list of entry dicts with frozen key order

    Returns:
        YAML string with header comments + model_list
    """
    lines = []

    # Header comments — emitted verbatim from routing_meta (§3.2); server-controlled
    # strings only, no row data reaches a comment line.
    lines.append(f"# generated-by: {meta.get('generated_by', 'unknown')}")
    lines.append(f"# generated-at: {meta.get('generated_at', '?')}")
    lines.append(f"# warm-hook: {meta.get('warm_hook', '?')}")

    # model_list key
    lines.append('model_list:')

    if not entries:
        # Empty fleet
        return '\n'.join(lines) + '\n'

    # Emit entries
    for entry in entries:
        lines.append('  - model_name: ' + _yaml_scalar(entry.get('model_name')))

        # litellm_params dict
        if 'litellm_params' in entry:
            lp = entry['litellm_params']
            lines.append('    litellm_params:')
            for key in ['model', 'api_base', 'api_key']:
                if key in lp:
                    lines.append(f'      {key}: {_yaml_scalar(lp[key])}')

        # model_info dict
        if 'model_info' in entry:
            mi = entry['model_info']
            lines.append('    model_info:')
            for key in ['unit', 'logical', 'host', 'rung', 'on_demand',
                       'load_strategy', 'peak_bytes', 'peak_source', 'load_seconds']:
                if key in mi:
                    lines.append(f'      {key}: {_yaml_scalar(mi[key])}')

    return '\n'.join(lines) + '\n'


def logical_of(row: Dict) -> str:
    """Extract the logical model name from a row.

    row['alias'] can be None (recon 2): fall back to the unit stem.
    """
    alias = row.get('alias')
    if alias is not None:
        return alias
    unit_name = row['unit']
    return unit_name[:-len('.service')] if unit_name.endswith('.service') else unit_name


def include_in_routing(row: Dict) -> bool:
    """Decide if a row should appear in the routing config.

    Hot always; cold only if marked; an on-demand entry stays listed
    through its own warm-up (STARTING/LOADING); STANDBY/FAILED/RETIRED never.
    """
    if row.get('retired'):
        return False

    rung = row.get('rung')
    if rung in ('READY', 'BUSY'):
        return True

    if row.get('on_demand') and rung in ('OFF', 'STARTING', 'LOADING'):
        return True

    return False


def build_routing_entries(snapshot: Dict, advertise_host: str) -> List[Dict]:
    """Build routing config entries from the snapshot.

    Args:
        snapshot: from watcher.snapshot()
        advertise_host: the advertise-host flag value or snapshot['host']

    Returns:
        list of entry dicts, sorted by model_name
    """
    entries = []
    host = snapshot.get('host', '?')

    for row in snapshot.get('units', []):
        if not include_in_routing(row):
            continue

        unit_name = row['unit']
        logical = logical_of(row)
        model_name = f"{host}-{logical}"
        port = row.get('port', 8080)
        enabled = row.get('enabled', False)

        # Memory info
        mem = row.get('mem') or {}
        model_info = {
            'unit': unit_name,
            'logical': logical,
            'host': host,
            'rung': row.get('rung', 'OFF'),
            'on_demand': row.get('on_demand', False),
            'load_strategy': 'on-boot' if enabled else 'manual'
        }

        # Add peak and load_seconds only when known (H3 null-omission)
        if mem.get('bytes') is not None:
            model_info['peak_bytes'] = mem['bytes']
            model_info['peak_source'] = mem.get('label', 'unknown')

        if mem.get('load_seconds') is not None:
            model_info['load_seconds'] = mem['load_seconds']

        entry = {
            'model_name': model_name,
            'litellm_params': {
                'model': f'openai/{logical}',
                'api_base': f'http://{advertise_host}:{port}/v1',
                'api_key': 'none'
            },
            'model_info': model_info
        }

        entries.append(entry)

    # Sort by model_name for determinism
    entries.sort(key=lambda e: e['model_name'])

    return entries


def routing_meta(snapshot: Dict, advertise_host: str, self_port: int, now_utc) -> Dict:
    """Build routing metadata (header comments).

    Args:
        snapshot: from watcher.snapshot()
        advertise_host: the advertise-host flag value
        self_port: Roundhouse's HTTP port
        now_utc: datetime.now(timezone.utc)

    Returns:
        dict with 'generated_by', 'generated_at', 'warm_hook'
    """
    host = snapshot.get('host', '?')
    iso_dt = now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')

    return {
        # H3/§3.1: the source string is 'roundhouse@<host>' — the JSON twin serves this
        # dict verbatim, so the prefix belongs here and NOT in the YAML emitter, or the
        # two documents disagree about who generated them.
        'generated_by': f'roundhouse@{host}',
        'generated_at': iso_dt,
        'warm_hook': f'POST http://{advertise_host}:{self_port}/api/warm'
    }


def resolve_warm_target(logical: Optional[str], unit: Optional[str],
                        snapshot: Dict, units: Dict[str, 'UnitFile']
                        ) -> tuple:
    """Resolve a warm request target.

    Returns:
        ('ok', unit_name) or ('error', status_code, error_code, extra_dict)

    Exactly one of logical/unit must be non-None; route has already 400'd
    if both or neither given (function asserts).
    """
    assert (logical is None) != (unit is None), "exactly one of logical/unit must be given"

    if unit is not None:
        # Direct unit path. `is not None`, never truthiness: `{"unit": ""}` is a
        # perfectly well-formed request for a unit that does not exist (404), and
        # falling through to the logical branch would dereference logical=None.
        if unit not in units:
            return ('error', 404, 'unknown_unit', {})
        # Retired units resolve here; the retired refusal happens in the handler
        return ('ok', unit)

    # logical path: resolve against logical_of(row) for non-RETIRED rows
    host = snapshot.get('host', '?')
    namespace_prefix = f"{host}-"

    search_logical = logical
    if logical.startswith(namespace_prefix):
        search_logical = logical[len(namespace_prefix):]

    matches = []
    for row in snapshot.get('units', []):
        if row.get('retired'):
            continue
        if logical_of(row) == search_logical or logical_of(row) == logical:
            matches.append(row['unit'])

    if len(matches) == 0:
        return ('error', 404, 'unknown_alias', {})
    if len(matches) > 1:
        return ('error', 422, 'ambiguous_alias', {'units': sorted(matches)})

    return ('ok', matches[0])


def warm_plan(target: str, snapshot: Dict, units: Dict[str, 'UnitFile'],
              cgroup_cache: Dict, mem_store: 'MemStore') -> Dict:
    """Plan a warm-up, filtering candidates to on-demand-marked units (layer 1 fence).

    Args:
        target: target unit name (must be selected and on-demand; caller enforces)
        snapshot: from locked_snapshot
        units: selected units dict
        cgroup_cache: for suggest_stops
        mem_store: for memory estimation

    Returns:
        dict with 'fits', 'stops', 'estimate_bytes', estimate_source',
        'mem_available_bytes', 'headroom_bytes', 'freed_by', 'shortfall_bytes',
        'consenting', 'excluded_unmarked'
    """
    profile = {}
    if units[target].exec_start:
        profile = extract_param_profile(units[target].exec_start.engine_argv)

    # Estimate memory needed
    estimate, estimate_source = _estimate_start_bytes(target, profile, mem_store)

    # Get available memory
    mem_snapshot = snapshot.get('mem', {})
    budget = mem_snapshot.get('available_bytes') or 0
    mem_available_source = mem_snapshot.get('available_source', 'unknown')

    # Build consenting and excluded lists. `suggest_stops` and `_freed_bytes` read
    # residency off the FULL snapshot row (`mem`/`rung`) plus the watcher's cgroup
    # cache, so the greedy pool must be rows, not projections — a projection with no
    # `mem` key values every candidate at 0 bytes and destroys the F7 ordering.
    consenting_rows = []
    consenting = []
    excluded_unmarked = []

    for row in snapshot.get('units', []):
        if row['unit'] == target or row.get('retired'):
            continue
        if row.get('rung') not in ACTIVE_RUNGS:
            continue

        unit_obj = units.get(row['unit'])
        if not unit_obj:
            continue

        resident_bytes, resident_source = _freed_bytes(row['unit'], row, cgroup_cache)
        row_info = {
            'unit': row['unit'],
            'rung': row.get('rung', 'OFF'),
            'resident_bytes': resident_bytes,
            'resident_source': resident_source
        }

        if unit_obj.on_demand:
            consenting_rows.append(row)          # THE FENCE, layer 1
            consenting.append(row_info)
        else:
            excluded_unmarked.append(row_info)

    # Run the UNMODIFIED F7 greedy over the consent-filtered pool
    stops = suggest_stops(target, [], consenting_rows, estimate, budget, [],
                          cgroup_cache, mem_store)

    # Compute freed bytes
    freed_by = []
    freed_total = 0
    rows_by_unit = {row['unit']: row for row in snapshot.get('units', [])}
    for stop_unit in stops:
        stop_row = rows_by_unit.get(stop_unit) or {}
        freed_bytes, freed_source = _freed_bytes(stop_unit, stop_row, cgroup_cache)
        freed_by.append({'unit': stop_unit, 'bytes': freed_bytes, 'source': freed_source})
        freed_total += freed_bytes

    # Check fit
    fits = estimate + HEADROOM_BYTES <= budget + freed_total
    shortfall = max(0, estimate + HEADROOM_BYTES - budget - freed_total)

    return {
        'fits': fits,
        'stops': stops,
        'estimate_bytes': estimate,
        'estimate_source': estimate_source,
        'mem_available_bytes': budget,
        'mem_available_source': mem_available_source,
        'headroom_bytes': HEADROOM_BYTES,
        'freed_by': freed_by,
        'shortfall_bytes': shortfall,
        'consenting': consenting,
        'excluded_unmarked': excluded_unmarked
    }


# ===== SECTION D: MAIN / CLI =====


def main():
    parser = argparse.ArgumentParser(description='Roundhouse MVP1 — fleet driver for boltzmann')
    parser.add_argument('--serve', action='store_true', help='Run server (default)')
    parser.add_argument('--scan', metavar='DIR', help='Scan unit directory and print report')
    parser.add_argument('--unit-dir', default=os.path.expanduser('~/.config/systemd/user'),
                        help='Unit directory (default: ~/.config/systemd/user)')
    parser.add_argument('--port', type=int, default=8090, help='HTTP port (default: 8090)')
    parser.add_argument('--advertise-host', default=None,
                        help='Advertised hostname for API base URLs (default: kernel hostname)')
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
    watcher_lock = watcher.lock

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
                    unit_order = selected_unit_names + ['roundhouse.service']
                    output = run_ro([
                        'systemctl', '--user', 'show', '-p', prop_args, '--'
                    ] + unit_order)
                    props = parse_show_blocks(output, unit_order)
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

                # Tick pending warm (outside the lock; method locks internally)
                if rollout_engine:
                    rollout_engine.tick_pending_warm()

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

    # Create RolloutEngine if armed (before poll_thread starts — tick_pending_warm needs it)
    rollout_engine = None
    if ACTUATE_ARMED:
        rollout_engine = RolloutEngine(watcher, units, unit_dir, port, event_bus, watcher_lock)

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
        advertise_host = args.advertise_host or os.uname()[1]
        server = ThreadingHTTPServer(
            ('0.0.0.0', port),
            RoundhouseRequestHandler,
            watcher,
            event_bus,
            port,
            watcher_lock=watcher_lock,
            rollout_engine=rollout_engine,
            advertise_host=advertise_host
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

    # Compute boot status for self cell (G8)
    self_unit = snapshot.get('self_unit', {})
    self_unit_file_state = self_unit.get('unit_file_state', '')
    if self_unit_file_state == 'enabled':
        boot_note = 'enabled'
    elif self_unit_file_state == 'disabled':
        boot_note = 'manual'
    else:
        boot_note = 'not installed'

    return {
        'ports': port_list,
        'self': {
            'port': self_port,
            'claims_by_units': [c['unit'] for c in ports.get(self_port, [])],
            'boot': boot_note,
        },
    }


if __name__ == '__main__':
    sys.exit(main())
