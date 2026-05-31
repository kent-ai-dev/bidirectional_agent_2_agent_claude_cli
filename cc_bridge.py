#!/usr/bin/env python3
"""
cc-bridge -- a tiny bidirectional HTTP bridge between Hermes (or any external
agent) and a specific running Claude Code instance.

One bridge process per Claude Code instance. Each owns a unique PORT and a
friendly ID, and registers itself in ~/.cc-bridge/registry.json so Hermes can
discover every live instance and address it by id.

  Hermes -> Claude Code : POST /send  -> delivered into the instance's tmux
                          pane via `tmux send-keys` (delivers AND wakes idle).
  Claude Code -> Hermes : a Stop hook posts the finished turn to /_reply, which
                          is buffered for GET /recv and (optionally) forwarded
                          to --hermes-url.

Stable primitives only: stdlib http.server, Claude Code hooks, tmux. No deps.

Subcommands:
  serve  --id ID --port PORT [--hermes-url URL]   run one instance's bridge
  hook   session-start | stop                     wired into ~/.claude/settings.json
  send   --to ID --text TEXT [--sender NAME]      Hermes -> instance
  recv   --to ID [--since N] [--wait]             instance -> Hermes
  list                                            show live instances
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

try:
    import fcntl  # POSIX / WSL
except ImportError:
    fcntl = None

ROOT = Path.home() / ".cc-bridge"
REGISTRY = ROOT / "registry.json"
MSGDIR = ROOT / "msgs"
INLINE_MAX = 600       # longer / multiline messages are written to a file instead
BUSY_TIMEOUT = 900     # seconds; clears a stuck "busy" flag if a turn never ends


# --------------------------------------------------------------------------- #
# shared registry (~/.cc-bridge/registry.json), file-locked for concurrency
# --------------------------------------------------------------------------- #
def registry_update(fn):
    ROOT.mkdir(parents=True, exist_ok=True)
    if not REGISTRY.exists():
        REGISTRY.write_text("{}")
    with open(REGISTRY, "r+") as f:
        if fcntl:
            fcntl.flock(f, fcntl.LOCK_EX)
        try:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                data = {}
            out = fn(data)
            f.seek(0)
            f.truncate()
            json.dump(data, f, indent=2)
            return out
        finally:
            if fcntl:
                fcntl.flock(f, fcntl.LOCK_UN)


def registry_read():
    if not REGISTRY.exists():
        return {}
    try:
        return json.loads(REGISTRY.read_text())
    except json.JSONDecodeError:
        return {}


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def registry_live():
    """Read the registry and drop entries whose bridge process is gone."""
    def prune(d):
        dead = [k for k, v in d.items() if not pid_alive(v.get("pid"))]
        for k in dead:
            d.pop(k, None)
        return dict(d)
    return registry_update(prune)


# --------------------------------------------------------------------------- #
# tiny HTTP helpers (stdlib only)
# --------------------------------------------------------------------------- #
def http_post(url, obj, quiet=False):
    try:
        req = urllib.request.Request(
            url, data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"{}")
    except Exception as e:
        if not quiet:
            sys.stderr.write(f"[cc-bridge] POST {url} failed: {e}\n")
        return None


def http_get(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except Exception as e:
        sys.stderr.write(f"[cc-bridge] GET {url} failed: {e}\n")
        return None


# --------------------------------------------------------------------------- #
# the daemon
# --------------------------------------------------------------------------- #
class Bridge(ThreadingHTTPServer):
    def __init__(self, addr, handler, *, id, port, hermes_url):
        super().__init__(addr, handler)
        self.id = id
        self.port = port
        self.hermes_url = hermes_url
        self.pane = None
        self.busy = False
        self.busy_since = 0.0
        self.inbound = []     # [{msg_id, sender, text, ts}]
        self.replies = []     # [{seq, text, ts}]
        self.seq = 0
        self.cv = threading.Condition()

    def enqueue_inbound(self, sender, text):
        msg_id = uuid.uuid4().hex[:8]
        with self.cv:
            self.inbound.append({"msg_id": msg_id, "sender": sender,
                                 "text": text, "ts": time.time()})
            self.cv.notify_all()
        return msg_id

    def add_reply(self, text):
        with self.cv:
            self.seq += 1
            rec = {"seq": self.seq, "text": text, "ts": time.time()}
            self.replies.append(rec)
            self.busy = False
            self.cv.notify_all()
        if self.hermes_url:
            http_post(self.hermes_url, {"from": self.id, "text": text}, quiet=True)

    def replies_since(self, since):
        with self.cv:
            return self.seq, [r for r in self.replies if r["seq"] > since]


def inbound_worker(srv: Bridge):
    """Deliver one queued message at a time, only when the instance is idle."""
    while True:
        with srv.cv:
            while True:
                if srv.busy and (time.time() - srv.busy_since > BUSY_TIMEOUT):
                    srv.busy = False  # safety: turn probably died
                if (not srv.busy) and srv.inbound and srv.pane:
                    break
                srv.cv.wait(timeout=1.0)
            msg = srv.inbound.pop(0)
            srv.busy = True
            srv.busy_since = time.time()
            pane = srv.pane
        try:
            deliver(srv, pane, msg)
        except Exception as e:
            with srv.cv:
                srv.busy = False
            sys.stderr.write(f"[cc-bridge] deliver failed: {e}\n")


def deliver(srv, pane, msg):
    label = f"[Hermes->{srv.id} #{msg['msg_id']}]"
    text = msg["text"]
    if len(text) <= INLINE_MAX and "\n" not in text:
        payload = f"{label} {text}"
    else:
        d = MSGDIR / srv.id
        d.mkdir(parents=True, exist_ok=True)
        fp = d / f"{msg['msg_id']}.txt"
        fp.write_text(text)
        payload = (f"{label} A message from Hermes was saved to {fp} -- "
                   f"read that file and act on it.")
    # -l sends the text literally; a separate Enter submits the turn.
    subprocess.run(["tmux", "send-keys", "-t", pane, "-l", "--", payload],
                   check=True)
    subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # quiet

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    def _reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        srv = self.server
        path = self.path.split("?", 1)[0]
        data = self._body()
        if path == "/send":                       # Hermes -> instance
            text = (data.get("text") or "").strip()
            if not text:
                return self._reply(400, {"error": "text required"})
            mid = srv.enqueue_inbound(data.get("from", "hermes"), text)
            return self._reply(200, {"queued": True, "msg_id": mid, "id": srv.id})
        if path == "/_register":                  # from SessionStart hook
            srv.pane = data.get("pane") or srv.pane
            with srv.cv:
                srv.cv.notify_all()
            registry_update(lambda d: d.__setitem__(srv.id, {
                **d.get(srv.id, {}), "pane": srv.pane, "last_seen": time.time()}))
            return self._reply(200, {"ok": True, "pane": srv.pane})
        if path == "/_reply":                      # from Stop hook
            srv.add_reply(data.get("text", ""))
            return self._reply(200, {"ok": True})
        return self._reply(404, {"error": "not found"})

    def do_GET(self):
        srv = self.server
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/recv":                      # instance -> Hermes
            since = int((q.get("since") or ["0"])[0])
            wait = (q.get("wait") or ["0"])[0] in ("1", "true", "yes")
            deadline = time.time() + 25 if wait else 0
            while True:
                cursor, new = srv.replies_since(since)
                if new or not wait or time.time() > deadline:
                    return self._reply(200, {"cursor": cursor, "replies": new})
                with srv.cv:
                    srv.cv.wait(timeout=1.0)
        if u.path == "/status":
            return self._reply(200, {
                "id": srv.id, "port": srv.port, "pane": srv.pane,
                "busy": srv.busy, "queued": len(srv.inbound), "cursor": srv.seq})
        return self._reply(404, {"error": "not found"})


def cmd_serve(args):
    import signal
    srv = Bridge(("127.0.0.1", args.port), Handler,
                 id=args.id, port=args.port, hermes_url=args.hermes_url)
    registry_update(lambda d: d.__setitem__(args.id, {
        "port": args.port, "pid": os.getpid(), "pane": None,
        "started": time.time(), "last_seen": time.time(),
        "hermes_url": args.hermes_url}))

    def _cleanup(*_):
        registry_update(lambda d: d.pop(args.id, None))
        os._exit(0)
    signal.signal(signal.SIGTERM, _cleanup)

    threading.Thread(target=inbound_worker, args=(srv,), daemon=True).start()
    print(f"[cc-bridge] '{args.id}' listening on 127.0.0.1:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        registry_update(lambda d: d.pop(args.id, None))


# --------------------------------------------------------------------------- #
# hooks (run inside the Claude Code process)
# --------------------------------------------------------------------------- #
def cmd_hook_session_start(args):
    port = os.environ.get("CC_BRIDGE_PORT")
    pane = os.environ.get("TMUX_PANE")
    try:
        sys.stdin.read()  # drain hook input
    except Exception:
        pass
    if port and pane:
        http_post(f"http://127.0.0.1:{port}/_register", {"pane": pane}, quiet=True)
    if port:
        # SessionStart stdout is injected into Claude's context.
        print(f"You are a Claude Code instance bridged to an external agent "
              f"named 'Hermes' via cc-bridge on port {port}. Messages prefixed "
              f"with '[Hermes->...]' are instructions from Hermes; act on them "
              f"normally. Your turn output is relayed back to Hermes automatically.")


def last_assistant_text(path):
    if not path:
        return None
    try:
        lines = Path(path).read_text().splitlines()
    except Exception:
        return None
    latest = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
        role = msg.get("role") or obj.get("type")
        if role != "assistant":
            continue
        content = msg.get("content")
        parts = []
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    parts.append(c.get("text", ""))
        if parts:
            latest = "".join(parts).strip()
    return latest


def cmd_hook_stop(args):
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        payload = {}
    port = os.environ.get("CC_BRIDGE_PORT")
    text = last_assistant_text(payload.get("transcript_path")) or "(turn complete)"
    if port:
        http_post(f"http://127.0.0.1:{port}/_reply", {"text": text}, quiet=True)


# --------------------------------------------------------------------------- #
# client side (Hermes)
# --------------------------------------------------------------------------- #
def resolve_port(id):
    reg = registry_live()
    e = reg.get(id)
    if not e:
        sys.exit(f"unknown instance '{id}'. Known: {', '.join(reg) or '(none)'}")
    return e["port"]


def cmd_send(args):
    port = resolve_port(args.to)
    print(json.dumps(http_post(f"http://127.0.0.1:{port}/send",
                               {"from": args.sender, "text": args.text})))


def cmd_recv(args):
    port = resolve_port(args.to)
    suffix = f"?since={args.since}" + ("&wait=1" if args.wait else "")
    print(json.dumps(http_get(f"http://127.0.0.1:{port}/recv{suffix}"), indent=2))


def cmd_list(args):
    reg = registry_live()
    if not reg:
        print("(no live instances)")
        return
    for id, e in reg.items():
        print(f"{id:20} port={e.get('port')} pane={e.get('pane')} "
              f"pid={e.get('pid')}")


# --------------------------------------------------------------------------- #
def main():
    p = argparse.ArgumentParser(prog="cc_bridge")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve")
    s.add_argument("--id", required=True)
    s.add_argument("--port", type=int, required=True)
    s.add_argument("--hermes-url", default=os.environ.get("HERMES_URL"))
    s.set_defaults(fn=cmd_serve)

    h = sub.add_parser("hook")
    hsub = h.add_subparsers(dest="hook", required=True)
    hsub.add_parser("session-start").set_defaults(fn=cmd_hook_session_start)
    hsub.add_parser("stop").set_defaults(fn=cmd_hook_stop)

    se = sub.add_parser("send")
    se.add_argument("--to", required=True)
    se.add_argument("--text", required=True)
    se.add_argument("--sender", default="hermes")
    se.set_defaults(fn=cmd_send)

    rc = sub.add_parser("recv")
    rc.add_argument("--to", required=True)
    rc.add_argument("--since", type=int, default=0)
    rc.add_argument("--wait", action="store_true")
    rc.set_defaults(fn=cmd_recv)

    sub.add_parser("list").set_defaults(fn=cmd_list)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
