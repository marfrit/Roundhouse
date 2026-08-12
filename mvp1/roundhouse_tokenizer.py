"""Helper tokenizer using the proven piece-based approach from legacy parser.py"""

from typing import List, Tuple


def collect_pieces(raw: bytes) -> List[Tuple[int, bytes]]:
    """Collect value pieces handling backslash continuations.

    Returns list of (offset, bytes) pairs. Backslash at end of line means
    the logical line continues on the next non-comment line.
    """
    pieces = []
    i = 0

    while i <= len(raw):
        end = raw.find(b'\n', i)
        if end == -1 or end > len(raw):
            end = len(raw)
            line = raw[i:end]
            pieces.append((i, line))
            break
        else:
            line = raw[i:end]

        # Check for backslash continuation
        if line.endswith(b'\\'):
            pieces.append((i, line[:-1]))
            i = end + 1
        else:
            pieces.append((i, line))
            break

    return pieces


def scan_tokens(data: bytes, pieces: List[Tuple[int, bytes]]) -> List[dict]:
    """Scan shell-like tokens from pieces, preserving byte offsets."""
    tokens = []
    ws = (b' ', b'\t')

    for base, raw in pieces:
        i, n = 0, len(raw)
        while i < n:
            c = raw[i:i + 1]
            if c in ws:
                i += 1
                continue

            # Single quote: content is literal
            if c in (b"'", b'"'):
                quote_char = c
                i += 1
                j = i
                while j < n and raw[j:j + 1] != quote_char:
                    j += 1

                if j < n:
                    # Found closing quote
                    text = raw[i:j].decode('utf-8', errors='replace')
                    tokens.append({
                        'text': text,
                        'start_byte': base + i,
                        'end_byte': base + j,
                        'raw': raw[i:j]
                    })
                    i = j + 1
                else:
                    # Unterminated quote - rest of line is token
                    text = raw[i:n].decode('utf-8', errors='replace')
                    tokens.append({
                        'text': text,
                        'start_byte': base + i,
                        'end_byte': base + n,
                        'raw': raw[i:n]
                    })
                    break
            else:
                # Unquoted token: goes until whitespace
                j = i
                while j < n and raw[j:j + 1] not in ws:
                    j += 1

                text = raw[i:j].decode('utf-8', errors='replace')
                tokens.append({
                    'text': text,
                    'start_byte': base + i,
                    'end_byte': base + j,
                    'raw': raw[i:j]
                })
                i = j

    return tokens
