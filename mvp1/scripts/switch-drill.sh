#!/bin/sh
# Live LOADING->READY exercise on boltzmann. Roundhouse must be running on :8090.
# Roundhouse itself performs ZERO actuation; this script is the operator's hand.
set -eu
echo "== watch http://boltzmann:8090 while this runs =="
systemctl --user stop qwen3.6-coding.service
systemctl --user start llama-server-gemma4-q4km.service
echo "expect: gemma4-q4km STARTING -> LOADING (elapsed counter) -> READY; qwen3.6-coding -> OFF"
printf "verify in UI, then press enter to revert... "; read _
systemctl --user stop llama-server-gemma4-q4km.service
systemctl --user start qwen3.6-coding.service
echo "expect: qwen3.6-coding LOADING ~72s -> READY (and a measured-peak row in /api/mem)"
