#!/usr/bin/env python3
"""Mynah OpenCode bridge.

Runs on a machine inside the tailnet and does the two things a browser PWA
cannot do for itself:

  1. Search Tailscale for OpenCode instances. It shells out to
     `tailscale status --json`, walks every peer (and self), and probes the
     OpenCode port for /api/health.
  2. Proxy OpenCode calls. It forwards /api/server/<host>/... to that host's
     OpenCode server with basic auth injected, so the PWA never handles
     OpenCode credentials and never fights CORS.

It also serves the PWA itself from ../docs, so one process behind
`tailscale serve` is the whole deployment:

    python3 tools/mynah_opencode_bridge.py
    tailscale serve --bg --https=443 http://127.0.0.1:8790

All /api/* endpoints require the bridge's bearer token (printed at startup as
a mynah-opencode:// pairing link).
"""

import argparse
import base64
import hmac
import json
import os
import secrets
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlsplit

BRIDGE_PORT = 8790
OPENCODE_PORT = 4096
OPENCODE_USER = "opencode"
DISCOVERY_INTERVAL = 5.0
PROBE_TIMEOUT = 1.2
PROBE_TIMEOUT_HTTPS = 1.5

# The tailscale CLI may not be on launchd's PATH; find it manually.
TAILSCALE = next(path for path in
                 (shutil.which("tailscale"), "/opt/homebrew/bin/tailscale", "/usr/local/bin/tailscale")
                 if path and Path(path).exists())

TAILSCALE_ARGS = []  # explicit --socket when the CLI default cannot reach the daemon

# When tailscaled runs userspace-networking, traffic to tailnet IPs must go
# through its local HTTP/SOCKS proxy (--ts-proxy http://127.0.0.1:1055).
OPENER = None


def detect_tailscale_socket():
    global TAILSCALE_ARGS
    candidates = ([], ["--socket=/opt/homebrew/var/run/tailscaled.sock"], ["--socket=/var/run/tailscaled.socket"])
    for candidate in candidates:
        try:
            result = subprocess.run([TAILSCALE, *candidate, "status", "--json"],
                                    capture_output=True, text=True, timeout=10)
            if result.stdout.strip().startswith("{"):
                TAILSCALE_ARGS = candidate
                return
        except (OSError, subprocess.SubprocessError):
            continue


def urlopen(request, timeout, context=None):
    if OPENER:
        return OPENER.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout, context=context)


def tailscale(*args, **kwargs):
    return subprocess.run([TAILSCALE, *TAILSCALE_ARGS, *args], **kwargs)

PWA_DIR = Path(__file__).resolve().parent.parent / "docs"
CONFIG_DIR = Path.home() / ".config" / "mynah-opencode"

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".json": "application/json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}

SSL_INSECURE = ssl.create_default_context()
SSL_INSECURE.check_hostname = False
SSL_INSECURE.verify_mode = ssl.CERT_NONE


def read_opencode_password():
    """Fall back to this Mac's OpenCode web password."""
    for path in (Path.home() / ".config" / "opencode" / "server-password",):
        try:
            password = path.read_text().strip()
            if password:
                return password
        except OSError:
            pass
    return ""


class TodoWatch:
    """Taps each found server's /api/event SSE stream and caches todo state.

    OpenCode publishes per-session todos only as events (todo.updated), so the
    long-lived bridge subscribes once per server and exposes the cache over a
    plain REST endpoint for the PWA.
    """

    def __init__(self, credentials_for):
        self.credentials_for = credentials_for
        self.lock = threading.Lock()
        self.cache = {}  # host -> {sessionID: [todos]}
        self.watchers = set()

    def get(self, host):
        with self.lock:
            return dict(self.cache.get(host) or {})

    def record(self, host, payload):
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        session_id = data.get("sessionID")
        todos = data.get("todos")
        if not session_id or not isinstance(todos, list):
            return
        with self.lock:
            self.cache.setdefault(host, {})[session_id] = todos

    def ensure(self, host, server):
        if host in self.watchers:
            return
        self.watchers.add(host)
        threading.Thread(target=self._watch, args=(host, server), daemon=True).start()

    def _watch(self, host, server):
        url = f"{server['scheme']}://{server['addr']}:{server['port']}/api/event"
        while True:
            try:
                request = urllib.request.Request(url)
                request.add_header("Accept", "text/event-stream")
                user, password = self.credentials_for(host)
                if password:
                    credential = base64.b64encode(f"{user}:{password}".encode()).decode()
                    request.add_header("Authorization", f"Basic {credential}")
                with urlopen(request, timeout=None, context=SSL_INSECURE) as response:
                    print(f"bridge: watching events on {host}")
                    for line in response:
                        line = line.strip()
                        if not line.startswith(b"data:"):
                            continue
                        try:
                            payload = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        if payload.get("type") == "todo.updated":
                            self.record(host, payload)
            except Exception as exc:
                print(f"bridge: event stream {host} dropped: {exc}", file=sys.stderr)
            time.sleep(5)


class Servers:
    """Cached tailnet + OpenCode probe results."""

    def __init__(self, port):
        self.port = port
        self.lock = threading.Lock()
        self.snapshot = {"servers": [], "checked_at": 0}
        self.todos = None  # set by main()

    def scan_once(self):
        servers = []
        for peer in self._peers():
            probe = self._probe(peer)
            server = {**peer, **probe}
            if server["host"].endswith(".ts.net"):
                server["webui"] = f"https://{server['host']}:{server['port']}"
            elif server["ip"]:
                server["webui"] = f"{server['scheme']}://{server['ip']}:{server['port']}"
            else:
                server["webui"] = ""
            servers.append(server)
            if probe["found"] and self.todos:
                self.todos.ensure(peer["host"], server)
        servers.sort(key=lambda s: (not s["found"], not s["online"], s["host"]))
        with self.lock:
            self.snapshot = {"servers": servers, "checked_at": time.time()}
        return servers

    def _peers(self):
        try:
            raw = tailscale("status", "--json", capture_output=True, text=True, timeout=10).stdout
            status = json.loads(raw)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            print(f"bridge: tailscale status failed: {exc}", file=sys.stderr)
            detect_tailscale_socket()  # daemon may have moved sockets (reboot, upgrade)
            return []
        peers = [status.get("Self") or {}] + list((status.get("Peer") or {}).values())
        seen = set()
        out = []
        for index, peer in enumerate(peers):
            dns = (peer.get("DNSName") or "").rstrip(".")
            host = dns or peer.get("HostName") or ""
            if not host or host in seen:
                continue
            seen.add(host)
            ips = peer.get("TailscaleIPs") or []
            out.append({
                "host": host,
                "hostname": peer.get("HostName") or host,
                "os": peer.get("OS") or "unknown",
                "online": bool(peer.get("Online")),
                "self": index == 0,
                "ip": ips[0] if ips else "",
            })
        return out

    def _probe(self, peer):
        if not peer["online"] and not peer["self"]:
            return {"found": False, "scheme": "", "addr": "", "port": self.port, "status": 0}
        candidates = []
        if peer["self"]:
            candidates.append(("http", "127.0.0.1", self.port))
        dns = peer["host"]
        if peer["host"].endswith(".ts.net"):
            candidates.append(("https", dns, self.port))
        if peer["ip"]:
            candidates.append(("http", peer["ip"], self.port))
            candidates.append(("https", peer["ip"], self.port))
        for scheme, addr, port in candidates:
            url = f"{scheme}://{addr}:{port}/api/health"
            code = self._health_code(url)
            if code in (200, 401):
                return {"found": True, "scheme": scheme, "addr": addr, "port": port, "status": code}
            if code:
                return {"found": False, "scheme": scheme, "addr": addr, "port": port, "status": code}
        return {"found": False, "scheme": "", "addr": "", "port": self.port, "status": 0}

    def _health_code(self, url):
        try:
            request = urllib.request.Request(url, method="GET")
            with urlopen(request, timeout=PROBE_TIMEOUT, context=SSL_INSECURE) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code
        except (urllib.error.URLError, OSError, ssl.SSLError):
            return 0

    def get(self):
        with self.lock:
            return self.snapshot

    def find(self, host):
        for server in self.get()["servers"]:
            if server["host"] == host:
                return server
        return None

    def loop(self):
        while True:
            try:
                self.scan_once()
            except Exception as exc:  # keep the bridge alive no matter what
                print(f"bridge: discovery error: {exc}", file=sys.stderr)
            time.sleep(DISCOVERY_INTERVAL)


class Handler(BaseHTTPRequestHandler):
    server_version = "mynah-opencode-bridge/1"
    protocol_version = "HTTP/1.1"
    servers: Servers = None
    token: str = ""
    passwords: dict = {}

    def log_message(self, fmt, *args):
        print(f"bridge: {self.address_string()} {fmt % args}")

    # -- helpers ---------------------------------------------------------

    def send_common(self, code, body=b"", content_type="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def send_json(self, code, payload, extra=None):
        body = json.dumps(payload).encode()
        self.send_common(code, body, "application/json", extra)

    def authorized(self):
        header = self.headers.get("Authorization", "")
        expected = f"Bearer {self.token}"
        return hmac.compare_digest(header, expected)

    def handle_auth(self):
        if self.authorized():
            return True
        self.send_json(401, {"error": "pair with the bridge token in Mynah settings"})
        return False

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else None

    # -- routing ---------------------------------------------------------

    def do_OPTIONS(self):
        self.send_common(204, extra=cors_headers())

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method):
        path = urlsplit(self.path).path
        if path.startswith("/api/discovery"):
            if not self.handle_auth():
                return
            self.send_json(200, self.servers.get(), extra=cors_headers())
        elif path.startswith("/api/server/"):
            if not self.handle_auth():
                return
            self.proxy(method)
        else:
            self.static(path)

    # -- discovery -------------------------------------------------------

    def static(self, path):
        if path in ("/", "/index.html"):
            file = PWA_DIR / "index.html"
        else:
            file = (PWA_DIR / path.lstrip("/")).resolve()
            if not str(file).startswith(str(PWA_DIR.resolve())):
                return self.send_common(403, b"", "text/plain")
            if not file.is_file() and "." not in path:
                file = PWA_DIR / "index.html"  # SPA fallback for route paths
        if not file.is_file():
            return self.send_common(404, b"not found", "text/plain")
        body = file.read_bytes()
        self.send_common(200, body, MIME.get(file.suffix, "application/octet-stream"))

    # -- proxy -----------------------------------------------------------

    def proxy(self, method):
        rest = urlsplit(self.path).path[len("/api/server/"):]
        host, _, forward_path = rest.partition("/")
        server = self.servers.find(host)
        if not server or not server["found"]:
            return self.send_json(404, {"error": f"no OpenCode instance found for {host}"}, cors_headers())
        if forward_path in ("todos", "api/todos"):
            return self.send_json(200, {"data": self.servers.todos.get(host)}, cors_headers())
        target = f"{server['scheme']}://{server['addr']}:{server['port']}"
        query = urlsplit(self.path).query
        url = f"{target}/{forward_path}{('?' + query) if query else ''}"

        request = urllib.request.Request(url, data=self.read_body() if method == "POST" else None, method=method)
        request.add_header("Content-Type", self.headers.get("Content-Type", "application/json"))
        user, password = get_credentials(host)
        if password:
            credential = base64.b64encode(f"{user}:{password}".encode()).decode()
            request.add_header("Authorization", f"Basic {credential}")
        try:
            with urlopen(request, timeout=30, context=SSL_INSECURE) as response:
                body = response.read()
                self.send_common(response.status, body, response.headers.get_content_type(), cors_headers())
        except urllib.error.HTTPError as exc:
            body = exc.read()
            self.send_common(exc.code, body, exc.headers.get_content_type(), cors_headers())
        except (urllib.error.URLError, OSError, ssl.SSLError, TimeoutError) as exc:
            self.send_json(502, {"error": f"proxy to {host} failed: {exc}"}, cors_headers())


def get_credentials(host):
    """passwords.json values may be a password string or {user, password}."""
    entry = Handler.passwords.get(host) or Handler.passwords.get("*") or {}
    user = entry.get("user") or OPENCODE_USER
    password = entry.get("password") or read_opencode_password()
    return user, password


def cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type",
    }


def load_or_create_token():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    token_path = CONFIG_DIR / "bridge-token"
    try:
        token = token_path.read_text().strip()
        if token:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    token_path.write_text(token + "\n")
    os.chmod(token_path, 0o600)
    return token


def load_passwords(path):
    """Values may be a password string or {user, password} per host."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    credentials = {}
    for key, value in data.items():
        if isinstance(value, dict):
            credentials[key] = {"user": value.get("user") or OPENCODE_USER, "password": str(value.get("password") or "")}
        else:
            credentials[key] = {"user": OPENCODE_USER, "password": str(value)}
    return credentials


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=BRIDGE_PORT)
    parser.add_argument("--opencode-port", type=int, default=OPENCODE_PORT)
    parser.add_argument("--passwords", type=Path, default=Path(__file__).parent / "passwords.json",
                        help="JSON mapping host (or *) to OpenCode server password")
    parser.add_argument("--ts-proxy", default=None, metavar="URL",
                        help="route tailnet traffic through tailscaled's userspace proxy (e.g. http://127.0.0.1:1055)")
    args = parser.parse_args()

    global OPENER
    detect_tailscale_socket()
    if args.ts_proxy:
        OPENER = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": args.ts_proxy, "https": args.ts_proxy}),
            urllib.request.HTTPSHandler(context=SSL_INSECURE),
        )
        os.environ["no_proxy"] = "127.0.0.1,localhost"

    servers = Servers(port=args.opencode_port)
    Handler.servers = servers
    Handler.token = load_or_create_token()
    Handler.passwords = load_passwords(args.passwords)
    servers.todos = TodoWatch(credentials_for=get_credentials)

    threading.Thread(target=servers.loop, daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.daemon_threads = True

    try:
        dns = json.loads(tailscale("status", "--json", capture_output=True, text=True, timeout=10).stdout)
        dns = dns["Self"]["DNSName"].rstrip(".")
    except Exception:
        dns = "localhost"

    pair = "mynah-opencode://pair?" + urlencode({"host": dns, "port": args.port, "token": Handler.token})
    print(f"bridge: serving Mynah on http://127.0.0.1:{args.port}")
    print(f"bridge: pair link: {pair}")
    print(f"bridge: publish with: tailscale serve --bg --https=443 http://127.0.0.1:{args.port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
