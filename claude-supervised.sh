#!/usr/bin/env bash
# Run claude --remote-control inside a tmux session, restarting it if it dies.
# Re-execed by tmux-bridge.py when a new session is created.

set -u

# Sanity backoff so a hard-failing claude doesn't spin the CPU.
MIN_BACKOFF=1
MAX_BACKOFF=30
backoff=$MIN_BACKOFF

while true; do
    echo "[tmux-bridge] starting claude --remote-control at $(date -Is)"
    if claude --remote-control; then
        echo "[tmux-bridge] claude exited 0 at $(date -Is); restarting"
        backoff=$MIN_BACKOFF
    else
        rc=$?
        echo "[tmux-bridge] claude exited $rc at $(date -Is); restarting in ${backoff}s"
        sleep "$backoff"
        backoff=$(( backoff * 2 ))
        if [ "$backoff" -gt "$MAX_BACKOFF" ]; then backoff=$MAX_BACKOFF; fi
    fi
done
