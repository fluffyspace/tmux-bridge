#!/usr/bin/env python3
"""tmux-bridge: pairing + tmux session control daemon for an Android client."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import socketserver
import subprocess
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("TMUX_BRIDGE_CONFIG", "/etc/tmux-bridge/config.json"))
STATE_DIR = Path(os.environ.get("TMUX_BRIDGE_STATE", "/var/lib/tmux-bridge"))
RUNTIME_DIR = Path(os.environ.get("TMUX_BRIDGE_RUNTIME", "/run/tmux-bridge"))
DEVICES_FILE = STATE_DIR / "devices.json"
PENDING_FILE = STATE_DIR / "pending.json"
ADMIN_SOCK = RUNTIME_DIR / "admin.sock"
SUPERVISOR = Path("/opt/tmux-bridge/claude-supervised.sh")

DEFAULT_CONFIG = {
    "bind": "0.0.0.0",
    "port": 7842,
    "ntfy_url": "",          # e.g. "http://192.168.20.3:8090/kodba" — optional
    "pending_ttl_seconds": 600,
}

_state_lock = threading.Lock()


# ---------- storage ----------

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    return cfg


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default


def _save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, path)


def load_devices() -> dict:
    return _load_json(DEVICES_FILE, {})


def save_devices(devices: dict) -> None:
    _save_json(DEVICES_FILE, devices)


def load_pending() -> dict:
    return _load_json(PENDING_FILE, {})


def save_pending(pending: dict) -> None:
    _save_json(PENDING_FILE, pending)


# ---------- tmux ----------

def _tmux(*args: str) -> tuple[int, str, str]:
    p = subprocess.run(
        ["tmux", *args], capture_output=True, text=True, timeout=10
    )
    return p.returncode, p.stdout, p.stderr


def hostname_prefix() -> str:
    return socket.gethostname().split(".")[0]


def session_full_name(name: str) -> str:
    prefix = hostname_prefix()
    if name.startswith(prefix + "-"):
        return name
    return f"{prefix}-{name}"


def tmux_list_sessions() -> list[dict]:
    rc, out, _ = _tmux(
        "list-sessions",
        "-F",
        "#{session_name}\t#{session_attached}\t#{session_created}\t#{session_windows}",
    )
    if rc != 0:
        return []
    sessions = []
    for line in out.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        sessions.append({
            "name": parts[0],
            "attached": parts[1] != "0",
            "created": int(parts[2]),
            "windows": int(parts[3]),
        })
    return sessions


def tmux_create_session(name: str) -> tuple[bool, str]:
    full = session_full_name(name)
    rc, _, err = _tmux("has-session", "-t", full)
    if rc == 0:
        return False, f"session '{full}' already exists"
    rc, _, err = _tmux("new-session", "-d", "-s", full, str(SUPERVISOR))
    if rc != 0:
        return False, err.strip() or "tmux new-session failed"
    return True, full


def tmux_kill_session(name: str) -> tuple[bool, str]:
    full = session_full_name(name)
    rc, _, err = _tmux("kill-session", "-t", full)
    if rc != 0:
        return False, err.strip() or "tmux kill-session failed"
    return True, full


# ---------- ntfy (optional) ----------

def ntfy_send(cfg: dict, title: str, body: str) -> None:
    url = cfg.get("ntfy_url", "")
    if not url:
        return
    try:
        import urllib.request
        req = urllib.request.Request(
            url, data=body.encode("utf-8"), method="POST",
            headers={"Title": title, "Priority": "high", "Tags": "key"},
        )
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass  # best-effort


# ---------- HTTP handler ----------

class APIHandler(BaseHTTPRequestHandler):
    server_version = "tmux-bridge/1"
    cfg: dict = {}

    # --- helpers ---
    def _client_ip(self) -> str:
        return self.client_address[0]

    def _json(self, status: int, body) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self) -> dict | None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        if length > 8192:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            return None

    def _authed_device(self) -> dict | None:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth[len("Bearer "):].strip()
        with _state_lock:
            devices = load_devices()
            for dev_id, dev in devices.items():
                if secrets.compare_digest(dev.get("token", ""), token):
                    return {"id": dev_id, **dev}
        return None

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # --- routing ---
    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"ok": True, "host": hostname_prefix()})
        if self.path.startswith("/pair/"):
            return self._pair_status(self.path[len("/pair/"):])
        if self.path == "/sessions":
            return self._sessions_list()
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/pair":
            return self._pair_request()
        if self.path == "/sessions":
            return self._sessions_create()
        return self._json(404, {"error": "not found"})

    def do_DELETE(self):
        if self.path.startswith("/sessions/"):
            return self._sessions_delete(self.path[len("/sessions/"):])
        if self.path == "/devices/self":
            return self._unpair_self()
        return self._json(404, {"error": "not found"})

    # --- pairing ---
    def _pair_request(self):
        body = self._read_json()
        if body is None:
            return self._json(400, {"error": "bad json"})
        name = (body.get("device_name") or "").strip()[:64] or "android"
        req_id = uuid.uuid4().hex
        ip = self._client_ip()
        with _state_lock:
            pending = load_pending()
            _prune_pending(pending, self.cfg.get("pending_ttl_seconds", 600))
            code = _unique_code(pending)
            entry = {
                "device_name": name,
                "ip": ip,
                "created": int(time.time()),
                "status": "pending",
                "code": code,
            }
            pending[req_id] = entry
            save_pending(pending)
        ntfy_send(
            self.cfg,
            "tmux-bridge pairing request",
            f"{name} @ {ip}  code: {code}",
        )
        return self._json(202, {
            "request_id": req_id,
            "status": "pending",
            "pairing_code": code,
        })

    def _pair_status(self, req_id: str):
        with _state_lock:
            pending = load_pending()
            entry = pending.get(req_id)
            if entry is None:
                return self._json(404, {"error": "unknown request"})
            if entry["status"] == "approved":
                # one-shot delivery — strip from pending and return token
                token = entry.get("token", "")
                del pending[req_id]
                save_pending(pending)
                return self._json(200, {"status": "approved", "token": token})
            if entry["status"] == "denied":
                del pending[req_id]
                save_pending(pending)
                return self._json(200, {"status": "denied"})
            return self._json(200, {"status": "pending"})

    def _unpair_self(self):
        dev = self._authed_device()
        if not dev:
            return self._json(401, {"error": "unauthorized"})
        with _state_lock:
            devices = load_devices()
            devices.pop(dev["id"], None)
            save_devices(devices)
        return self._json(200, {"ok": True})

    # --- sessions ---
    def _sessions_list(self):
        if not self._authed_device():
            return self._json(401, {"error": "unauthorized"})
        return self._json(200, {"sessions": tmux_list_sessions()})

    def _sessions_create(self):
        if not self._authed_device():
            return self._json(401, {"error": "unauthorized"})
        body = self._read_json()
        if body is None:
            return self._json(400, {"error": "bad json"})
        name = (body.get("name") or "").strip()
        if not name or not all(c.isalnum() or c in "-_" for c in name):
            return self._json(400, {"error": "name must be alphanumeric/-/_"})
        ok, info = tmux_create_session(name)
        if not ok:
            return self._json(409, {"error": info})
        return self._json(201, {"name": info})

    def _sessions_delete(self, name: str):
        if not self._authed_device():
            return self._json(401, {"error": "unauthorized"})
        if not name or not all(c.isalnum() or c in "-_" for c in name):
            return self._json(400, {"error": "bad name"})
        ok, info = tmux_kill_session(name)
        if not ok:
            return self._json(404, {"error": info})
        return self._json(200, {"name": info})


def _prune_pending(pending: dict, ttl: int) -> None:
    now = int(time.time())
    expired = [k for k, v in pending.items()
               if v.get("status") == "pending" and now - v.get("created", now) > ttl]
    for k in expired:
        del pending[k]


def _unique_code(pending: dict) -> str:
    active = {v.get("code") for v in pending.values() if v.get("status") == "pending"}
    for _ in range(20):
        code = f"{secrets.randbelow(1_000_000):06d}"
        if code not in active:
            return code
    raise RuntimeError("could not allocate unique pairing code")


# ---------- admin UNIX socket ----------

class AdminHandler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            raw = self.rfile.readline().decode("utf-8").strip()
            if not raw:
                return
            req = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._reply({"error": "bad json"})
            return
        op = req.get("op")
        if op == "pending":
            with _state_lock:
                pending = load_pending()
                _prune_pending(pending, 10**9)
                items = [{"id": k, **v} for k, v in pending.items()
                         if v.get("status") == "pending"]
            self._reply({"pending": items})
        elif op == "pair_by_code":
            self._pair_by_code(req.get("code", ""))
        elif op == "devices":
            with _state_lock:
                devices = load_devices()
            self._reply({"devices": [
                {"id": k, **{kk: vv for kk, vv in v.items() if kk != "token"}}
                for k, v in devices.items()
            ]})
        elif op == "unpair":
            self._unpair(req.get("device_id", ""))
        else:
            self._reply({"error": f"unknown op '{op}'"})

    def _reply(self, obj) -> None:
        self.wfile.write((json.dumps(obj) + "\n").encode("utf-8"))

    def _pair_by_code(self, code: str):
        code = code.strip().replace("-", "").replace(" ", "")
        if not code.isdigit() or len(code) != 6:
            self._reply({"error": "code must be 6 digits"})
            return
        with _state_lock:
            pending = load_pending()
            _prune_pending(pending, 10**9)
            match = None
            for req_id, entry in pending.items():
                if entry.get("status") == "pending" and entry.get("code") == code:
                    match = (req_id, entry)
                    break
            if not match:
                self._reply({"error": "no pending request with that code"})
                return
            req_id, entry = match
            token = secrets.token_urlsafe(32)
            dev_id = uuid.uuid4().hex
            devices = load_devices()
            devices[dev_id] = {
                "device_name": entry["device_name"],
                "ip": entry["ip"],
                "token": token,
                "paired": int(time.time()),
            }
            save_devices(devices)
            entry["status"] = "approved"
            entry["token"] = token
            entry["device_id"] = dev_id
            save_pending(pending)
        self._reply({"ok": True, "device_id": dev_id,
                     "device_name": entry["device_name"], "ip": entry["ip"]})

    def _unpair(self, dev_id: str):
        with _state_lock:
            devices = load_devices()
            if dev_id not in devices:
                self._reply({"error": "no such device"})
                return
            del devices[dev_id]
            save_devices(devices)
        self._reply({"ok": True})


class ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------- daemon entry ----------

def run_daemon() -> int:
    cfg = load_config()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    if ADMIN_SOCK.exists():
        ADMIN_SOCK.unlink()

    APIHandler.cfg = cfg
    httpd = ThreadingHTTPServer((cfg["bind"], int(cfg["port"])), APIHandler)
    admind = ThreadingUnixServer(str(ADMIN_SOCK), AdminHandler)
    os.chmod(ADMIN_SOCK, 0o660)

    print(f"tmux-bridge listening on {cfg['bind']}:{cfg['port']} "
          f"(admin: {ADMIN_SOCK})", flush=True)

    t = threading.Thread(target=admind.serve_forever, daemon=True)
    t.start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        admind.shutdown()
        admind.server_close()
        if ADMIN_SOCK.exists():
            ADMIN_SOCK.unlink()
    return 0


# ---------- admin client (subcommands) ----------

def _admin_call(op: str, **fields) -> dict:
    if not ADMIN_SOCK.exists():
        sys.exit(f"daemon not running (no {ADMIN_SOCK})")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(str(ADMIN_SOCK))
    s.sendall((json.dumps({"op": op, **fields}) + "\n").encode("utf-8"))
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    s.close()
    if not buf:
        sys.exit("empty reply from daemon")
    return json.loads(buf.decode("utf-8").splitlines()[0])


def cmd_pending(_args) -> int:
    reply = _admin_call("pending")
    items = reply.get("pending", [])
    if not items:
        print("(no pending pairing requests)")
        return 0
    for it in items:
        age = int(time.time() - it["created"])
        print(f"{it['id'][:8]}  {it['device_name']:<24}  {it['ip']:<16}  {age}s ago")
    return 0


def cmd_pair(args) -> int:
    reply = _admin_call("pair_by_code", code=args.code)
    if "error" in reply:
        print("error:", reply["error"]); return 1
    print(f"paired '{reply['device_name']}' @ {reply['ip']} "
          f"as device {reply['device_id'][:8]}")
    return 0


def cmd_list(_args) -> int:
    reply = _admin_call("devices")
    devices = reply.get("devices", [])
    if not devices:
        print("(no paired devices)")
        return 0
    for d in devices:
        print(f"{d['id'][:8]}  {d['device_name']:<24}  {d['ip']:<16}  "
              f"paired {time.strftime('%Y-%m-%d %H:%M', time.localtime(d['paired']))}")
    return 0


def cmd_unpair(args) -> int:
    reply = _admin_call("devices")
    devices = reply.get("devices", [])
    candidates = [d for d in devices if d["id"].startswith(args.device_id)]
    if len(candidates) != 1:
        print(f"matched {len(candidates)} devices; refusing to guess"); return 1
    reply = _admin_call("unpair", device_id=candidates[0]["id"])
    if "error" in reply:
        print("error:", reply["error"]); return 1
    print("unpaired"); return 0


# ---------- main ----------

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="tmux-bridge")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("daemon", help="run the daemon (foreground)")
    sub.add_parser("pending", help="show pending pairing requests")

    pp = sub.add_parser("pair", help="complete pairing using the code shown on the phone")
    pp.add_argument("code", help="6-digit pairing code from the Android app")

    sub.add_parser("list", help="list paired devices")

    up = sub.add_parser("unpair", help="remove a paired device")
    up.add_argument("device_id", help="device id prefix")

    args = p.parse_args(argv)
    if args.cmd == "daemon":
        return run_daemon()
    if args.cmd == "pending":
        return cmd_pending(args)
    if args.cmd == "pair":
        return cmd_pair(args)
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "unpair":
        return cmd_unpair(args)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
