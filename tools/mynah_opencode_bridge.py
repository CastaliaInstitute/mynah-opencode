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
import re
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


# Shared one-shot LLM (default: the local MLX server) for icons and auto model
# selection. Configured in main() from --icon-api/--icon-model.
LLM = {"base": "", "model": ""}


def llm_chat(prompt, max_tokens=20):
    """Returns the reply text, or None when the endpoint is unreachable."""
    if not LLM["base"]:
        return None
    request = urllib.request.Request(
        f"{LLM['base'].rstrip('/')}/chat/completions",
        data=json.dumps({
            "model": LLM["model"],
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0,
        }).encode(),
        method="POST",
    )
    request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=90, context=SSL_INSECURE) as response:
            return json.loads(response.read())["choices"][0]["message"]["content"].strip()
    except (urllib.error.URLError, OSError, ssl.SSLError, KeyError, json.JSONDecodeError):
        return None


# Maps auto-mode tiers to Model.Ref dicts. Overridable via --auto-ladder
# (JSON string or path to a JSON file).
LADDER = {}


def auto_pick_model(text):
    prompt = (
        "Classify this coding task into one tier.\n"
        "- light: quick question, small edit, docs, single-file change\n"
        "- mid: multi-file feature or bugfix, moderate refactor\n"
        "- heavy: large refactor, architecture, gnarly debugging, long multi-step effort\n\n"
        f"Task: {text[:500]}\n\n"
        'Reply with only JSON: {"tier": "light" | "mid" | "heavy", "why": "reason, max 8 words"}'
    )
    reply = llm_chat(prompt, max_tokens=60)
    tier = ""
    why = ""
    if reply:
        match = re.search(r"\{.*\}", reply, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
                tier = str(parsed.get("tier", "")).lower()
                why = str(parsed.get("why", ""))[:80]
            except json.JSONDecodeError:
                pass
        if tier not in ("light", "mid", "heavy"):
            tier = next((word for word in ("heavy", "mid", "light") if word in reply.lower()), "light")
    return {"tier": tier or "light", "why": why, "model": LADDER.get(tier, LADDER["light"])}


def load_tier(tier):
    entry = LADDER.get(tier) or LADDER["light"]
    return {"providerID": entry.get("providerID", "opencode"), "id": entry.get("id", "")}


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
                    print(f"bridge: watching events on {host}", flush=True)
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


class IconWatch:
    """Asks a local LLM for one emoji per task, based on the task's scope.

    Polls each found server's session list; for every unseen session it sends
    the title + project to an OpenAI-compatible endpoint (default: the MLX
    server on the laptop) and caches the emoji it replies with. Failures fall
    back to the PWA's hash-derived glyph.
    """

    def __init__(self, credentials_for):
        self.credentials_for = credentials_for
        self.lock = threading.Lock()
        self.cache = {}  # host -> {sessionID: icon}
        self.pollers = set()
        self.persist_path = CONFIG_DIR / "icons.json"
        try:
            self.cache = json.loads(self.persist_path.read_text())
        except (OSError, json.JSONDecodeError):
            pass

    def get(self, host):
        with self.lock:
            return dict(self.cache.get(host) or {})

    def _save(self):
        try:
            self.persist_path.write_text(json.dumps(self.cache))
        except OSError:
            pass

    def ensure(self, host, server):
        if host in self.pollers:
            return
        self.pollers.add(host)
        threading.Thread(target=self._poll, args=(host, server), daemon=True).start()

    def _poll(self, host, server):
        url = f"{server['scheme']}://{server['addr']}:{server['port']}/api/session?limit=30&order=desc"
        while True:
            try:
                request = urllib.request.Request(url)
                user, password = self.credentials_for(host)
                if password:
                    credential = base64.b64encode(f"{user}:{password}".encode()).decode()
                    request.add_header("Authorization", f"Basic {credential}")
                with urlopen(request, timeout=30, context=SSL_INSECURE) as response:
                    sessions = json.loads(response.read())["data"]
                with self.lock:
                    known = self.cache.get(host, {})
                pending = [session for session in sessions if session["id"] not in known]
                for session in pending[:3]:  # trickle; the LLM is local but not instant
                    icon = self._select_icon(session)
                    if icon is None:
                        continue  # LLM unreachable; retry next cycle
                    with self.lock:
                        self.cache.setdefault(host, {})[session["id"]] = icon
                    self._save()
            except Exception as exc:
                print(f"bridge: icon poll {host} failed: {exc}", file=sys.stderr)
            time.sleep(60)

    def _select_icon(self, session):
        title = session.get("title") or ""
        directory = (session.get("location") or {}).get("directory") or ""
        prompt = (
            "Reply with exactly one emoji that represents the scope of this coding task.\n"
            f"Task: {title}\n"
            f"Project: {directory.rsplit('/', 1)[-1]}\n"
            "The emoji and nothing else."
        )
        reply = llm_chat(prompt, max_tokens=10)
        if reply is None:
            return None  # unreachable or malformed: retry next cycle
        for threshold in (0x1F000, 0x2100):  # proper emoji first, then misc symbols
            for char in reply:
                code = ord(char)
                if code >= threshold and not 0xFE00 <= code <= 0xFE0F:
                    return char
        return ""  # LLM replied without an emoji: stop retrying


class Servers:
    """Cached tailnet + OpenCode probe results."""

    def __init__(self, port):
        self.ports = [port] if port == 8080 else [port, 8080]
        self.lock = threading.Lock()
        self.snapshot = {"servers": [], "checked_at": 0}
        self.todos = None  # set by main()
        self.icons = None  # set by main()

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
                if self.icons:
                    self.icons.ensure(peer["host"], server)
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
        offline = {"found": False, "scheme": "", "addr": "", "port": self.ports[0], "status": 0, "auth_required": False}
        if not peer["online"] and not peer["self"]:
            return offline
        fallback = None
        for port in self.ports:
            for scheme, addr in self._candidates(peer):
                code = self._health_code(f"{scheme}://{addr}:{port}/api/health")
                if code in (200, 401):
                    return {"found": True, "scheme": scheme, "addr": addr, "port": port, "status": code,
                            "auth_required": code == 401}
                if code and not fallback:
                    fallback = {"found": False, "scheme": scheme, "addr": addr, "port": port, "status": code,
                                "auth_required": False}
        return fallback or offline

    def _candidates(self, peer):
        if peer["self"]:
            yield ("http", "127.0.0.1")
        if peer["host"].endswith(".ts.net"):
            yield ("https", peer["host"])
        if peer["ip"]:
            yield ("http", peer["ip"])
            yield ("https", peer["ip"])

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
        if forward_path in ("icons", "api/icons"):
            return self.send_json(200, {"data": self.servers.icons.get(host)}, cors_headers())
        if forward_path in ("auto", "api/auto"):
            payload = json.loads(self.read_body() or b"{}")
            return self.send_json(200, {"data": auto_pick_model(str(payload.get("text", "")))}, cors_headers())
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
    parser.add_argument("--pair-url", default="https://castaliainstitute.github.io/mynah-opencode",
                        help="published PWA URL used to build the one-click pair link")
    parser.add_argument("--icon-api", default=os.environ.get("MYNAH_ICON_API", "https://daniels-laptop.tail667900.ts.net:8443/v1"),
                        help="OpenAI-compatible endpoint for the bridge LLM (icons + auto mode; empty string disables)")
    parser.add_argument("--icon-model", default="mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit")
    parser.add_argument("--auto-ladder", default=(
        '{"light": {"providerID": "opencode", "id": "glm-5.3-flash"},'
        ' "mid": {"providerID": "opencode", "id": "deepseek-v4-pro-offpeak"},'
        ' "heavy": {"providerID": "opencode", "id": "claude-opus-5"}}'),
                        help="tier -> Model.Ref JSON string or path to a JSON file")
    args = parser.parse_args()

    global LADDER
    ladder_value = args.auto_ladder
    ladder_path = Path(ladder_value)
    if ladder_path.is_file():
        ladder_value = ladder_path.read_text()
    try:
        LADDER = {tier: dict(ref) for tier, ref in json.loads(ladder_value).items()}
    except json.JSONDecodeError:
        print(f"bridge: bad --auto-ladder JSON, auto mode falls back to light", file=sys.stderr)

    LLM["base"] = args.icon_api.strip()
    LLM["model"] = args.icon_model.strip()

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
    servers.icons = IconWatch(credentials_for=get_credentials)

    threading.Thread(target=servers.loop, daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.daemon_threads = True

    try:
        dns = json.loads(tailscale("status", "--json", capture_output=True, text=True, timeout=10).stdout)
        dns = dns["Self"]["DNSName"].rstrip(".")
    except Exception:
        dns = "localhost"

    params = urlencode({"host": dns, "token": Handler.token})
    custom = "mynah-opencode://pair?" + params
    web = args.pair_url.rstrip("/") + "/#/pair?" + params
    print(f"bridge: serving Mynah on http://127.0.0.1:{args.port}", flush=True)
    print(f"bridge: pair the PWA with one click: {web}", flush=True)
    print(f"bridge: custom scheme link: {custom}", flush=True)
    print(f"bridge: publish with: tailscale serve --bg --https=443 http://127.0.0.1:{args.port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
