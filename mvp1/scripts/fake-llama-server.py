#!/usr/bin/env python3
"""Fake llama-server for testing. Emits real log lines, sleeps, then exits."""

import sys
import time
import os
from datetime import datetime

def log_line(level, tag, msg):
    """Print a log line like llama-server does."""
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"{ts} {level} {tag:<8} {msg}", flush=True)

def arg_value(argv, *names):
    """Exact-token flag lookup, the way llama-server itself reads argv.

    argparse must NOT be used here: with `-c` registered it binds `-ctk q8_0` as
    `-c tk` (short-option clustering) and every fixture unit that carries a KV cache
    type dies with 'invalid int value'. Exact token matching is the whole point.
    """
    for i, tok in enumerate(argv):
        if tok in names and i + 1 < len(argv):
            return argv[i + 1]
    return None

def main():
    argv = sys.argv[1:]

    model = arg_value(argv, '-m', '--model') or 'unknown'
    port_text = arg_value(argv, '--port')
    ctx_text = arg_value(argv, '-c', '--ctx-size')

    try:
        port = int(port_text) if port_text is not None else 8080
    except ValueError:
        port = 8080
    try:
        ctx = int(ctx_text) if ctx_text is not None else None
    except ValueError:
        ctx = None

    # Check env for test scenarios
    exit_1 = os.environ.get('FAKE_EXIT_1') == '1'
    busy_after = os.environ.get('FAKE_BUSY_AFTER')
    fake_load_seconds = int(os.environ.get('FAKE_LOAD_SECONDS', '10'))

    # ctx sentinel: 424242 triggers bind failure (for rollback drill)
    if ctx == 424242:
        log_line('E', 'srv', f"start: couldn't bind HTTP server socket, hostname: 0.0.0.0, port: {port}")
        log_line('E', 'srv', "llama_server: exiting due to HTTP server error")
        sys.exit(1)

    if exit_1:
        # Simulate bind failure
        log_line('E', 'srv', f"start: couldn't bind HTTP server socket, hostname: 0.0.0.0, port: {port}")
        log_line('E', 'srv', "llama_server: exiting due to HTTP server error")
        sys.exit(1)

    # Normal startup
    log_line('I', 'srv', f"load_model: loading model '{model}'")
    time.sleep(fake_load_seconds)

    log_line('I', 'srv', "llama_server: model loaded")
    log_line('I', 'srv', f"llama_server: listening on http://0.0.0.0:{port}")

    # If FAKE_BUSY_AFTER is set, go busy
    if busy_after:
        time.sleep(float(busy_after))
        log_line('I', 'slot', "launch_slot_: id  0 | task 0 | processing task")
        time.sleep(5)
        log_line('I', 'slot', "release: id  0 | task 0 | stop processing")

    # Sleep forever (until SIGTERM)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        sys.exit(0)

if __name__ == '__main__':
    main()
