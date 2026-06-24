#!/usr/bin/env bash
# Run claude --remote-control inside a tmux session, restarting it if it dies.
# Re-execed by tmux-bridge.py when a new session is created.

set -u

# tmux sets the cwd via `-c` (chosen by the daemon, optionally per-session
# from the API). Only fall back if for some reason we landed in "/".
if [ "${PWD:-/}" = "/" ]; then
    cd "${TMUX_BRIDGE_DEFAULT_CWD:-$HOME}" || cd /
fi

# Sanity backoff so a hard-failing claude doesn't spin the CPU.
MIN_BACKOFF=1
MAX_BACKOFF=30
backoff=$MIN_BACKOFF

# If the daemon told us the tmux session name, pass it to claude as the
# Remote Control name so the same label shows in the mobile app.
remote_name="${TMUX_BRIDGE_SESSION_NAME:-}"

# The daemon sends SIGTERM here to ask us to stop restarting claude. Bash
# defers the trap until the foreground child (claude) exits, which is
# exactly what we want: the daemon also sends SIGINT to claude so it can
# deregister, claude exits, then this trap fires and we break out instead
# of looping.
STOP=
trap 'STOP=1' TERM

while [ -z "$STOP" ]; do
    echo "[tmux-bridge] starting claude --remote-control${remote_name:+ $remote_name} at $(date -Is)"
    if claude --remote-control ${remote_name:+"$remote_name"}; then
        echo "[tmux-bridge] claude exited 0 at $(date -Is)"
        backoff=$MIN_BACKOFF
    else
        rc=$?
        echo "[tmux-bridge] claude exited $rc at $(date -Is)"
    fi
    [ -n "$STOP" ] && break
    echo "[tmux-bridge] restarting in ${backoff}s"
    sleep "$backoff" &
    wait $!
    [ -n "$STOP" ] && break
    backoff=$(( backoff * 2 ))
    if [ "$backoff" -gt "$MAX_BACKOFF" ]; then backoff=$MAX_BACKOFF; fi
done

echo "[tmux-bridge] supervisor exiting at $(date -Is)"
