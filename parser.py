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
                _anreichern(parsed_section, source_bytes, section_start, section_end)
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
        _anreichern(parsed_section, source_bytes, section_start, len(source_bytes))
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

def _entquoten(wert: str) -> str:
    """Aeussere, zusammengehoerige Anfuehrungszeichen abstreifen.

    Der Wert kommt aus dem zusammengefuegten ExecStart, in dem
    --chat-template-kwargs '{"enable_thinking":false}' noch seine einfachen
    Anfuehrungszeichen traegt. Der Test verlangt den INHALT -- dieselbe Regel wie
    beim Token, nur eine Ebene weiter.
    """
    if len(wert) >= 2 and wert[0] == wert[-1] and wert[0] in ("'", '"'):
        return wert[1:-1]
    return wert


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
                params[key] = _entquoten(value)
            elif i + 1 < len(parts) and not parts[i + 1].startswith('-'):
                # Handle -param value format
                params[part] = _entquoten(parts[i + 1])
                i += 1
            else:
                # Handle flag format (no value)
                params[part] = True
        i += 1
    
    return params


# ---------------------------------------------------------------------------
# Byteweiser Zerleger. ALLE Positionen sind Offsets in `data` -- niemals in eine
# Scheibe. Der erste Versuch mischte beides und lief deshalb rueckwaerts.
# ---------------------------------------------------------------------------

_WS = (b" ", b"\t")


def _zeilenende(data: bytes, i: int, grenze: int) -> int:
    e = data.find(b"\n", i)
    return grenze if e == -1 or e > grenze else e


def _logical_value(data: bytes, start: int, grenze: int):
    """Wert ab `start` (absolut), ueber Fortsetzungszeilen hinweg.

    Liefert (stuecke, ende_absolut). `stuecke` sind (offset, bytes)-Paare, jedes
    ein zusammenhaengender Bereich der DATEI -- damit Tokengrenzen auf die Datei
    zeigen und nicht auf eine zusammengesetzte Kopie. Ein Backslash unmittelbar
    vor dem Umbruch setzt fort und gehoert zu keinem Token.
    """
    stuecke, i = [], start
    while i <= grenze:
        e = _zeilenende(data, i, grenze)
        zeile = data[i:e]
        if zeile.endswith(b"\\"):
            stuecke.append((i, zeile[:-1]))
            i = e + 1
            continue
        stuecke.append((i, zeile))
        return stuecke, e
    return stuecke, grenze


def _scan_tokens(data: bytes, stuecke):
    """Shell-artige Token. Ein Lauf in ' oder \" ist EIN Token; die Anfuehrungs-
    zeichen gehoeren weder zum Text noch zum Bereich -- die Grenzen zeigen auf den
    INHALT, wie test_quotes_single_token_json es verlangt."""
    tokens = []
    for basis, roh in stuecke:
        i, n = 0, len(roh)
        while i < n:
            c = roh[i:i + 1]
            if c in _WS:
                i += 1
                continue
            if c in (b"'", b'"'):
                ende = roh.find(c, i + 1)
                if ende == -1:
                    tokens.append({"text": roh[i:n].decode("utf-8", "replace"),
                                   "start_byte": basis + i, "end_byte": basis + n})
                    break
                tokens.append({"text": roh[i + 1:ende].decode("utf-8", "replace"),
                               "start_byte": basis + i + 1, "end_byte": basis + ende})
                i = ende + 1
                continue
            j = i
            while j < n and roh[j:j + 1] not in _WS:
                j += 1
            tokens.append({"text": roh[i:j].decode("utf-8", "replace"),
                           "start_byte": basis + i, "end_byte": basis + j})
            i = j
    return tokens


def _tokenize_section(data: bytes, sec_start: int, sec_end: int):
    """(tokens, werte) eines Abschnitts. `werte` bildet Direktivnamen auf den
    LOGISCHEN Wert ab, Fortsetzungen bereits angehaengt."""
    tokens, werte = [], {}
    i = sec_start
    while i < sec_end:
        e = _zeilenende(data, i, sec_end)
        zeile = data[i:e]
        kern = zeile.strip()
        if not kern or kern[:1] in (b"#", b";") or (kern[:1] == b"[" and kern[-1:] == b"]"):
            i = e + 1
            continue
        rel = zeile.find(b"=")
        if rel == -1:
            i = e + 1
            continue
        gleich = i + rel                       # ABSOLUT -- hier lag der Fehler
        name = data[i:gleich].strip().decode("utf-8", "replace")
        stuecke, ende = _logical_value(data, gleich + 1, sec_end)
        tokens.extend(_scan_tokens(data, stuecke))
        werte[name] = b" ".join(s for _, s in stuecke).decode("utf-8", "replace")
        i = max(ende + 1, i + 1)               # niemals rueckwaerts
    return tokens, werte


def _anreichern(abschnitt: dict, data: bytes, start: int, ende: int) -> None:
    """Token einhaengen und Parameter aus dem LOGISCHEN ExecStart ziehen.

    Bisher bekam die Parameterauswertung nur die erste Zeile. `-c 65536` steht
    hinter einer Fortsetzung, `taskset -c 4-7` in derselben Zeile davor -- also
    gewann die CPU-Bindung. Das ist der gemeinsame Grund, an dem fuenf Modelle
    haengengeblieben sind.
    """
    tokens, werte = _tokenize_section(data, start, ende)
    abschnitt["tokens"] = tokens
    if "ExecStart" in werte:
        abschnitt.setdefault("params", {}).update(_extract_execstart_params(werte["ExecStart"]))
