# Mynah OpenCode

A PWA for navigating OpenCode across the tailnet: **servers → projects → tasks**. Maintained by [Castalia Institute](https://github.com/CastaliaInstitute).

**Published:** https://castaliainstitute.github.io/mynah-opencode/ (GitHub Pages from `main`, root `/docs`). On first open, point ⚙ Settings at your bridge URL and paste its token — the Pages site talks to the bridge over CORS; the bridge allows any origin.

A browser can't enumerate a tailnet, so a small bridge does the searching: it runs on any tailnet machine, walks `tailscale status --json`, probes every peer's OpenCode port for `/api/health`, and serves the PWA plus an authenticated proxy. The PWA itself is dependency-free vanilla JS.

## Run

On a machine inside the tailnet (e.g. this Mac):

```sh
python3 tools/mynah_opencode_bridge.py
```

The bridge listens on `127.0.0.1:8790`, scans the tailnet every 5 s, and prints a `mynah-opencode://pair?...` link containing its bearer token. Publish it over HTTPS so phones can install the PWA:

```sh
tailscale serve --bg --https=443 http://127.0.0.1:8790
```

Then open `https://<this-machine>.ts.net` on any tailnet device, add to home screen, and pair by pasting the token in ⚙ Settings (skipped automatically when the PWA is opened from the same origin it is served from).

**Production deployment (m1):** the bridge runs on `m1.tail667900.ts.net` as a KeepAlive LaunchAgent (`com.castalia.mynah-opencode-bridge`), published at **https://m1.tail667900.ts.net/**. m1's tailscaled runs in userspace-networking mode (no root needed; LaunchAgent `com.castalia.tailscaled` with `--tun=userspace-networking --outbound-http-proxy-listen=127.0.0.1:1055`), and the bridge routes all tailnet traffic through that local proxy with `--ts-proxy http://127.0.0.1:1055`. The bridge also auto-detects the tailscale CLI socket (`/opt/homebrew/var/run/tailscaled.sock`) where the launchd PATH default fails.

## OpenCode auth

The proxy injects HTTP basic auth when forwarding to OpenCode servers. Passwords come from `tools/passwords.json`, mapping host → credentials. A value may be a plain password (username defaults to `opencode`) or `{ "user", "password" }` for servers that use a different username:

```json
{
  "*": "shared-password",
  "m1.tail667900.ts.net": { "user": "dan", "password": "its-own-password" }
}
```

`*` is the fallback; if neither matches, the bridge uses `~/.config/opencode/server-password` (this machine's own web password). Copy `tools/passwords.example.json` to start. The token lives in `~/.config/mynah-opencode/bridge-token`.

## Screens

- **Dashboard** — every task across found servers as a circular gauge: task glyph in the center (derived from its project), thin outer arc = goal completion (SOL), thick inner arc = state (TERRA pulsing while running, SOL when done, LUNA dim when idle). Tap to zoom.
- **Dial** — the single gauge, alone and large. Tap the circle to open the task's WebUI.
- **WebUI** — the opencode web UI for the task in an iframe, one task per slide; swipe left/right (or use ‹ ›) to move between tasks on that server.
- **Servers** — tailnet peers, probed live: found / no-opencode / offline, OS badge, health status.
- **Projects / Tasks** — sessions grouped by `location.directory`, with model, running badge, recency, cost, and read-only transcripts.

Goal completion comes from opencode session todos. OpenCode only publishes todos as `todo.updated` events over the SSE stream at `/api/event`, so the bridge keeps one event-stream subscriber per server and exposes the accumulated state at `GET /api/server/<host>/todos`.

## WebUI auth

The iframe points straight at the server's web UI (`https://<host>:<port>/<urlsafe-base64-of-directory>/session/<id>`). The browser will ask for the server's basic auth (`opencode` / password) once per server and remember it. Servers need a TLS endpoint reachable from the tailnet (e.g. `tailscale serve` on the opencode port); servers that only answer plain HTTP will not render inside an HTTPS-hosted PWA.

## API (bridge)

| Endpoint | Purpose |
|---|---|
| `GET /api/discovery` | Tailnet peers + OpenCode probe results (incl. per-server `webui` URL) |
| `ANY /api/server/<host>/…` | Reverse proxy to that host's OpenCode server (basic auth injected) |
| `GET /api/server/<host>/todos` | Session todos, accumulated from the server's SSE stream |

OpenCode endpoints used: `/api/health`, `/api/session`, `/api/session/active`, `/api/session/:id/message` (see `opencode/packages/protocol/src/groups/`).
