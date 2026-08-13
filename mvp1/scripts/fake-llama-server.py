#!/usr/bin/env python3
"""Fake llama-server for testing. Emits real log lines, sleeps, then exits."""

import sys
import time
import os
import argparse
from datetime import datetime

def log_line(level, tag, msg):
    """Print a log line like llama-server does."""
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"{ts} {level} {tag:<8} {msg}", flush=True)

def main():
    # Parse args, ignoring unknown ones
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('-m', '--model', type=str, default='unknown')
    parser.add_argument('-c', '--ctx-size', type=int, default=None)
    parser.add_argument('--port', type=int, default=8080)
    args, _ = parser.parse_known_args()

    model = args.model
    port = args.port
    ctx = args.ctx_size

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
