# tmux-bridge

A small daemon that lets a phone app start, stop and inspect `tmux` sessions on a
remote Linux box — built specifically to drive
[Claude Code](https://docs.claude.com/en/docs/claude-code) sessions from an
Android client over a private network (LAN or VPN).

It's the missing back-end for "remote-control your headless dev box from your
phone": pair once with a 6-digit code shown in the app, and then the phone can
spin up `tmux` sessions running `claude --remote-control` and tear them down,
without an Electron desktop in the loop.

> Status: server side is complete and battle-tested locally. The Android client
> is **work in progress** — this repo contains only the daemon for now.

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
- `claude` inside each session is supervised: if it exits (crash, `/exit`,
  whatever), it's restarted with exponential backoff.
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
| `POST` | `/sessions`            | yes | `{"name": "alphanum-_"}`; full name is `<host>-<name>` |
| `DELETE` | `/sessions/<name>`   | yes | kill a session (name with or without host prefix) |
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

The Android app is being built separately and will land in a sibling repo.
The expected pairing UX:

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
