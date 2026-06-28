# tmux-bridge

**Run Claude Code on your real machine. Drive it from your phone.**

`tmux-bridge` is a tiny, zero-dependency daemon that turns a headless Linux box
into something you can pair with and steer from your pocket. It spins up
[Claude Code](https://docs.claude.com/en/docs/claude-code) sessions inside
`tmux` — each launched as `claude --remote-control <name>`, so you drive the
actual coding from **Claude's official native apps** (iOS, Android, and the web),
not a cramped terminal emulator squeezed onto a phone screen. The session name
you pick is the same label that shows up in the official app's Code section.

The split is the whole point: your beefy dev box (or that rented GPU server) does
the work and holds the repo, your phone is just the remote. Kick off a
*refactor-auth* agent while you're on the bus, check on it at lunch, kill it when
it's done — all over your own private network (LAN, WireGuard, or Tailscale),
with no Electron desktop in the loop and nothing round-tripping through a cloud
you don't control. Sessions are ordinary `tmux` sessions, so you can always
`ssh in && tmux attach` and take the keyboard back.

Pairing is deliberately dead simple: the app shows a 6-digit code, you type
`tmux-bridge pair 384291` on the box, done.

> **📱 Companion app:**
> [tmux-bridge-android](https://github.com/fluffyspace/tmux-bridge-android) —
> a native Jetpack Compose client to pair, browse, spawn and kill sessions.
>
> **Status:** the daemon is complete and runs in production under `systemd`; the
> Android client is live and F-Droid-ready.

## Why

Orca, mosh+tmux, ttyd and friends all assume you either (a) have a desktop GUI
to pair with, or (b) want a full terminal in your pocket. Neither fits if all
you want is to tell your headless server "spawn a new agent session named
*refactor-auth*" while you're on the bus.

`tmux-bridge` is intentionally narrow:

- A tiny HTTP API on a configurable port (default `7842/tcp`).
- A pairing flow with **no interactive prompt on the server** — the phone shows
  a 6-digit code, you type `tmux-bridge pair 384291` on the box. That's it.
- Sessions are normal `tmux` sessions on the user's tmux server, so you can
  still `ssh in && tmux attach -t <name>` and take over the agent yourself.
- The tmux session name is passed straight through as the
  `claude --remote-control <name>` argument, so it's also the label that
  shows in the official Claude mobile app's Code section — one name
  everywhere.
- `claude` inside each session is supervised: if it exits (crash, `/exit`,
  whatever), it's restarted with exponential backoff.
- `DELETE /sessions/<name>` shuts claude down **gracefully** (SIGINT first
  so it can deregister with Anthropic's cloud, then SIGTERM/SIGKILL
  escalation, then `tmux kill-session` as a catch-all). Skips ghost
  "connected" entries in the mobile app's archived list.
- Zero third-party Python dependencies. Stdlib only.

## How it works

```
  Android app                Linux server
  -----------                ------------
    POST /pair  ───────────►  daemon (kodba user)
                              ├── generates 6-digit code
                              └── writes pending request to disk
    ◄────  {request_id,
            pairing_code}

  user reads code on phone, types on the server:
    $ tmux-bridge pair 384291
                              daemon mints a bearer token
                              and marks the request approved

    GET /pair/<request_id> ─►
    ◄────  {status: approved,
            token}            (one-shot delivery)

    POST /sessions
      Authorization: Bearer …
      body: {"name":"refactor-auth"}
                              tmux new-session -d -s kodba-refactor-auth \
                                  /opt/tmux-bridge/claude-supervised.sh
```

The session name is always prefixed with the server's short hostname (so when
you have several boxes paired, sessions show up as `kodba-…`, `beefy-…`, etc.).

## HTTP API

All session endpoints require `Authorization: Bearer <token>`.

| Method | Path | Auth | Body / Notes |
|---|---|---|---|
| `GET`  | `/health`              | no  | liveness probe — returns hostname |
| `POST` | `/pair`                | no  | `{"device_name": "phone"}` → `{request_id, pairing_code}` |
| `GET`  | `/pair/<request_id>`   | no  | poll for approval; returns `token` once and only once |
| `GET`  | `/sessions`            | yes | list tmux sessions |
| `POST` | `/sessions`            | yes | `{"name": "alphanum-_", "cwd": "/abs/path"}` — both optional; `name` is auto-generated when omitted; `cwd` (alias: `path`) sets the start dir; full name is `<host>-<name>`; response includes `{name, cwd}` |
| `DELETE` | `/sessions/<name>`   | yes | graceful kill: SIGINT → wait → SIGTERM → SIGKILL → `tmux kill-session` |
| `GET`  | `/session-paths`       | yes | recently used `cwd`s, most-recent-first (up to 20) |
| `GET`  | `/check-path?path=…`  | yes | `{"exists": bool, "is_dir": bool}` — validate a path on the server |
| `DELETE` | `/devices/self`      | yes | self-unpair (server forgets the token) |

## Admin CLI

The same script doubles as a tiny CLI that talks to the daemon over a local
UNIX socket (`/run/tmux-bridge/admin.sock`):

```
tmux-bridge pending              # show pending pair requests
tmux-bridge pair 384291          # complete a pairing using the phone's code
tmux-bridge list                 # list paired devices
tmux-bridge unpair <id-prefix>   # revoke a device
```

There is **no** interactive y/N prompt in the pairing flow — the daemon doesn't
need a TTY, so it runs fine under systemd. The 6-digit code is the proof that
the operator can read both the phone screen and the server console.

## Install

```bash
sudo install -d /opt/tmux-bridge /etc/tmux-bridge
sudo install -m 0755 tmux-bridge.py       /opt/tmux-bridge/
sudo install -m 0755 claude-supervised.sh /opt/tmux-bridge/
sudo install -m 0644 config.example.json  /etc/tmux-bridge/config.json
sudo install -m 0644 systemd/tmux-bridge.service /etc/systemd/system/

# Edit the service file's User=/Group= to match the user that should own the
# tmux sessions (claude reads/writes ~/.claude under that user's HOME).
sudo systemctl daemon-reload
sudo systemctl enable --now tmux-bridge
```

### Configuration (`/etc/tmux-bridge/config.json`)

```json
{
  "bind": "0.0.0.0",
  "port": 7842,
  "ntfy_url": "",
  "pending_ttl_seconds": 600
}
```

- `ntfy_url` — optional. If set (e.g. `http://ntfy.example/your-topic`), a
  notification with the pairing code is sent to that topic when a request
  arrives. Handy if the phone shows the code in a place that's awkward to
  hand-type from.
- `pending_ttl_seconds` — pending requests are pruned after this many seconds.

### Working directory & workspace trust

Each `POST /sessions` can include an optional `cwd` (absolute path). When
omitted, the daemon falls back to the `TMUX_BRIDGE_DEFAULT_CWD` env var
(set in the systemd unit) and then to `$HOME`.

⚠️ The chosen folder **must already be in claude's trusted-folders list for
the user the daemon runs as**, otherwise claude will block on its
workspace-trust prompt and the session will hang. To trust a folder, do it
once interactively:

```bash
sudo -u <daemon-user> -i
cd /the/folder
claude   # answer "Yes, I trust this folder", then /exit
```

Sessions started later in that folder will skip the prompt.

## Security model — read this

`tmux-bridge` is designed to live on a **trusted private network** (LAN or
WireGuard/Tailscale). It is **not** hardened for direct exposure to the public
internet. In particular:

- Pairing requests are accepted from any source that can reach the port. The
  6-digit code is your only protection against a stranger pairing while you
  happen to be looking at the phone — fine on a VPN, terrible on the open
  internet.
- The HTTP transport is plain HTTP. If you need TLS, terminate it at a reverse
  proxy (Traefik, Caddy, etc.).
- Tokens are random 256-bit strings stored in `/var/lib/tmux-bridge/devices.json`
  in cleartext. Treat that file as a secret.

For LAN-or-VPN-only access, restrict the port at your firewall, e.g.:

```bash
sudo ufw allow from 10.0.0.0/24 to any port 7842 proto tcp comment 'tmux-bridge'
```

## File layout

```
/opt/tmux-bridge/tmux-bridge.py         # daemon + CLI
/opt/tmux-bridge/claude-supervised.sh   # restart wrapper
/etc/tmux-bridge/config.json            # bind/port/ntfy
/etc/systemd/system/tmux-bridge.service # systemd unit
/var/lib/tmux-bridge/devices.json       # paired devices + tokens
/var/lib/tmux-bridge/pending.json       # in-flight pairing requests
/run/tmux-bridge/admin.sock             # CLI ↔ daemon
```

## Android client

The native Android client lives at
**[fluffyspace/tmux-bridge-android](https://github.com/fluffyspace/tmux-bridge-android)**
(Jetpack Compose, F-Droid-ready). The pairing UX:

1. Tap **Add server**, type the server URL (`http://10.0.0.2:7842`).
2. App POSTs `/pair`, receives a 6-digit code, displays it big.
3. You run `tmux-bridge pair 384291` on the server.
4. App polls `/pair/<request_id>`, stores the bearer token, drops you on the
   sessions list.

Anything that can speak HTTP + read a JSON response can act as a client; the
API surface is small enough that an iOS app, a CLI, or a `curl`-driven script
are all viable.

## License

MIT — see [LICENSE](LICENSE).
