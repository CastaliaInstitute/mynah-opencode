/* Mynah OpenCode — servers → projects → tasks (sessions) over the bridge. */

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const view = $("#view");

const state = {
  bridge: localStorage.getItem("mynah.bridge") || location.origin,
  token: localStorage.getItem("mynah.token") || "",
  host: "",
  directory: "",
  sessionID: "",
  poll: null,
  tasks: new Map(), // "host|sid" -> gauge task
};

const serverOf = (host) => state.servers?.find((server) => server.host === host);

/* ---------- task gauge ---------- */

const GLYPHS = ["✦", "✚", "◈", "⬢", "▲", "●", "✳", "⌘", "⚙", "☘", "⚡", "◐", "✶", "❖"];

function taskState(task) {
  if (task.active) return "running";
  if (task.todos.total > 0 && task.todos.done >= task.todos.total) return "done";
  return "idle";
}

function gaugeSvg(task, size, glyphY) {
  const r = size / 2;
  const rGoal = r - size * 0.035;
  const rState = rGoal - size * 0.085;
  const goal = task.todos.total ? task.todos.done / task.todos.total : 0;
  const status = taskState(task);
  const color = status === "running" ? "var(--terra)" : status === "done" ? "var(--sol)" : "var(--luna)";
  const goalFrac = Math.max(0.02, goal); // keep a visible sliver when started
  const goalDash = `${(2 * Math.PI * rGoal * goalFrac).toFixed(1)} ${(2 * Math.PI * rGoal).toFixed(1)}`;
  const stateDash = status === "idle" ? `0 ${2 * Math.PI * rState}` : `${2 * Math.PI * rState} 0.01`;
  return `
    <svg class="gauge ${status}" viewBox="0 0 ${size} ${size}" role="img" aria-label="${escapeHtml(task.title)}">
      <circle cx="${r}" cy="${r}" r="${rGoal}" class="gauge-track"/>
      <circle cx="${r}" cy="${r}" r="${rGoal}" class="gauge-goal" stroke-dasharray="${goalDash}" transform="rotate(-90 ${r} ${r})"/>
      <circle cx="${r}" cy="${r}" r="${rState}" class="gauge-track"/>
      <circle cx="${r}" cy="${r}" r="${rState}" class="gauge-state" stroke-dasharray="${stateDash}" transform="rotate(-90 ${r} ${r})"/>
      <text x="${r}" y="${glyphY ?? r}" class="gauge-glyph" text-anchor="middle" dominant-baseline="central">${escapeHtml(task.icon || GLYPHS[hash(task.directory) % GLYPHS.length])}</text>
    </svg>`;
}

function hash(text) {
  let value = 0;
  for (const char of text || "") value = (value * 31 + char.codePointAt(0)) >>> 0;
  return value;
}

function modelLabel(model) {
  const id = typeof model === "string" ? model : model?.id || "";
  return id.replace(/^[^/]*\//, "");
}

function hydrate(host, server) {
  return async ({ data: sessions }, activeMap) => {
    const [todos, icons] = await Promise.all([
      api(`/api/server/${host}/todos`).catch(() => ({ data: {} })),
      api(`/api/server/${host}/icons`).catch(() => ({ data: {} })),
    ]);
    for (const session of sessions) {
      const todoList = todos.data[session.id] || [];
      state.tasks.set(`${host}|${session.id}`, {
        host,
        webui: server?.webui || "",
        id: session.id,
        title: session.title || session.id,
        directory: session.location?.directory || "(unknown)",
        updated: session.time?.updated || session.time?.created || 0,
        cost: session.cost || 0,
        active: !!activeMap?.[session.id],
        icon: icons.data[session.id] || "",
        current: (todoList.find((todo) => todo.status === "in_progress") || {}).content || "",
        todos: {
          done: todoList.filter((todo) => todo.status === "completed" || todo.status === "cancelled").length,
          total: todoList.length,
        },
      });
    }
  };
}

/* ---------- screens: dashboard / dial / webui ---------- */

async function dashboardScreen() {
  setTitle("Mynah");
  stopPoll();
  view.innerHTML = `<div class="empty">Gathering tasks…</div>`;
  try {
    const discovery = await api("/api/discovery");
    state.servers = discovery.servers.filter((server) => server.found);
    await Promise.all(state.servers.map(async (server) => {
      try {
        const [sessions, active] = await Promise.all([
          serverApi(server.host, "/api/session?limit=30&order=desc"),
          serverApi(server.host, "/api/session/active"),
        ]);
        await hydrate(server.host, server)(sessions, active.data || {});
      } catch (error) {
        if (error.message !== "unpaired") console.warn(`dashboard ${server.host}:`, error.message);
      }
    }));
    renderDashboard();
  } catch (error) {
    if (error.message !== "unpaired") view.innerHTML = `<div class="empty error">${error.message}</div>`;
  }
  $("#refresh").onclick = dashboardScreen;
}

function renderDashboard() {
  const mode = localStorage.getItem("mynah.group") || "all";
  const tasks = [...state.tasks.values()].sort((a, b) => (b.active - a.active) || (b.updated - a.updated));
  const seg = `<div class="seg">${[["all", "All"], ["server", "By server"], ["project", "By project"]].map(([value, label]) =>
    `<button data-mode="${value}"${mode === value ? ' class="on"' : ""}>${label}</button>`).join("")}</div>`;
  const gaugeCell = (task) => `
    <a class="gauge-cell" href="#/dial/${enc(task.host)}/${enc(task.id)}">
      ${gaugeSvg(task, 96)}
      <div class="gauge-label">${escapeHtml(task.title)}</div>
      <div class="gauge-sub">${escapeHtml(mode === "project"
        ? serverOf(task.host)?.hostname || task.host
        : project(task.directory))} · ${ago(new Date(task.updated).toISOString())}</div>
    </a>`;
  let body;
  if (mode === "all") {
    body = `<div class="gauge-grid">${tasks.map(gaugeCell).join("")}</div>`;
  } else {
    const key = mode === "server" ? ((task) => task.host) : ((task) => task.directory);
    const groups = new Map();
    for (const task of tasks) {
      if (!groups.has(key(task))) groups.set(key(task), []);
      groups.get(key(task)).push(task);
    }
    body = [...groups.entries()].map(([name, list]) => `
      <div class="section-label">${escapeHtml(mode === "server"
        ? serverOf(name)?.hostname || name
        : `${project(name)} — ${name}`)}</div>
      <div class="gauge-grid">${list.map(gaugeCell).join("")}</div>`).join("");
  }
  view.innerHTML = `
    <div class="chip-row">
      <a class="chip" href="#/servers">${state.servers.length} server${state.servers.length === 1 ? "" : "s"}</a>
    </div>
    ${seg}
    ${tasks.length ? body : `<div class="empty">No tasks found yet.<br><span class="error">${state.token ? "" : "Pair in ⚙ Settings first."}</span></div>`}`;
  $$(".seg button").forEach((button) => (button.onclick = () => {
    localStorage.setItem("mynah.group", button.dataset.mode);
    renderDashboard();
  }));
}

function dialScreen() {
  setTitle("Task");
  stopPoll();
  const tasks = [...state.tasks.values()].filter((task) => task.host === state.host)
    .sort((a, b) => (b.active - a.active) || (b.updated - a.updated));
  if (!tasks.length || !tasks.some((task) => task.id === state.sessionID)) {
    return void (view.innerHTML = `<div class="empty">Task not in dashboard cache — <a href="#/dashboard">refresh dashboard</a>.</div>`);
  }
  const index = tasks.findIndex((task) => task.id === state.sessionID);
  view.innerHTML = `
    <div class="dial-strip" id="dial-strip">${tasks.map((task) => {
      const status = taskState(task);
      return `
        <div class="dial-slide">
          <button class="dial-circle" data-sid="${escapeHtml(task.id)}" aria-label="Open task web UI">
            ${gaugeSvg(task, 300, 300 * 0.38)}
            <div class="dial-status">
              <div class="dial-state ${status}">${status === "running" ? "running" : status === "done" ? "done" : "idle"}</div>
              ${task.current ? `<div class="dial-current">${escapeHtml(task.current)}</div>` : ""}
              <div>${task.todos.total ? `${task.todos.done}/${task.todos.total} goals` : "no goals yet"}</div>
              <div>${ago(new Date(task.updated).toISOString())}${task.cost ? ` · $${task.cost.toFixed(3)}` : ""}</div>
            </div>
          </button>
          <div class="dial-title">${escapeHtml(task.title)}</div>
          <div class="dial-sub">${escapeHtml(project(task.directory))}${task.active ? " · running" : ""}</div>
        </div>`;
    }).join("")}</div>`;
  const strip = $("#dial-strip");
  requestAnimationFrame(() => strip.children[index]?.scrollIntoView({ inline: "center" }));
  let settle;
  strip.addEventListener("scroll", () => {
    clearTimeout(settle);
    settle = setTimeout(() => {
      const slide = [...strip.children].findIndex((el) => Math.abs(el.offsetLeft - strip.scrollLeft) < strip.clientWidth / 2);
      if (slide >= 0 && tasks[slide] && tasks[slide].id !== state.sessionID) {
        state.sessionID = tasks[slide].id;
        setTitle(tasks[slide].title);
      }
    }, 120);
  });
  $$(".dial-circle", strip).forEach((button) => (button.onclick = () => {
    state.sessionID = button.dataset.sid;
    location.hash = `#/webui/${enc(state.host)}/${enc(state.sessionID)}`;
  }));
  $("#refresh").onclick = dashboardScreen;
}

function webuiScreen() {
  setTitle("WebUI");
  stopPoll();
  const host = state.host;
  const tasks = [...state.tasks.values()].filter((task) => task.host === host)
    .sort((a, b) => (b.active - a.active) || (b.updated - a.updated));
  if (!tasks.length || !tasks.some((task) => task.id === state.sessionID)) {
    return void (view.innerHTML = `<div class="empty">Task not in cache — <a href="#/dashboard">refresh dashboard</a>.</div>`);
  }
  const webui = serverOf(host)?.webui;
  const index = tasks.findIndex((task) => task.id === state.sessionID);
  const slides = tasks.map((task, i) => {
    const target = task.directory && task.directory !== "(unknown)"
      ? `${webui}/${b64url(task.directory)}/session/${task.id}`
      : webui;
    return `
      <div class="slide" data-i="${i}">
        <iframe src="${target}" ${Math.abs(i - index) <= 1 ? "" : 'loading="lazy"'} title="${task.title}"></iframe>
      </div>`;
  }).join("");
  view.innerHTML = `
    ${serverOf(host)?.auth_required ? `
      <div class="notice">
        This server asks for a login, which iframes can't show.
        <button id="preauth" class="linkbtn">Open it once in a tab</button> to log in, then come back.
      </div>` : ""}
    <div class="framebar">
      <button class="iconbtn" id="prev" ${index === 0 ? "disabled" : ""} aria-label="Previous task">&#x2039;</button>
      <div class="grow framebar-title">${escapeHtml(tasks[index].title)}</div>
      <button class="iconbtn" id="next" ${index === tasks.length - 1 ? "disabled" : ""} aria-label="Next task">&#x203a;</button>
    </div>
    <div class="frame-strip" id="strip">${slides}</div>`;
  const strip = $("#strip");
  requestAnimationFrame(() => strip.children[index]?.scrollIntoView({ inline: "center" }));
  let settle;
  strip.addEventListener("scroll", () => {
    clearTimeout(settle);
    settle = setTimeout(() => {
      const slide = [...strip.children].findIndex((el) => Math.abs(el.offsetLeft - strip.scrollLeft) < strip.clientWidth / 2);
      if (slide >= 0 && slide !== index) {
        state.sessionID = tasks[slide].id;
        const task = tasks[slide];
        $(".framebar-title").textContent = task.title;
        $("#prev").disabled = slide === 0;
        $("#next").disabled = slide === tasks.length - 1;
      }
    }, 120);
  });
  const step = (direction) => {
    const target = [...strip.children].find((el) => Math.abs(el.offsetLeft - strip.scrollLeft) < strip.clientWidth / 2);
    const next = [...strip.children][Math.max(0, Math.min(tasks.length - 1, (target?.dataset.i | 0) + direction))];
    next?.scrollIntoView({ behavior: "smooth", inline: "center" });
  };
  $("#prev").onclick = () => step(-1);
  $("#next").onclick = () => step(1);
  $("#preauth")?.addEventListener("click", () => window.open(webui + "/", "_blank"));
  $("#refresh").onclick = () => webuiScreen();
}

function b64url(text) {
  return btoa(String.fromCharCode(...new TextEncoder().encode(text)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/* ---------- screens: servers / projects / tasks ---------- */

/* ---------- bridge api ---------- */

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(state.bridge.replace(/\/$/, "") + path, { ...options, headers });
  if (response.status === 401) {
    toast("Pair this device in Settings (bridge token)");
    location.hash = "#/settings";
    throw new Error("unpaired");
  }
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

const serverApi = (host, path, options = {}) => api(`/api/server/${host}${path}`, options);

/* ---------- helpers ---------- */

function toast(message, ms = 2600) {
  const el = $("#toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(el.timer);
  el.timer = setTimeout(() => (el.hidden = true), ms);
}

function ago(iso) {
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 90) return "just now";
  const minutes = seconds / 60;
  if (minutes < 60) return `${Math.floor(minutes)}m ago`;
  const hours = minutes / 60;
  if (hours < 24) return `${Math.floor(hours)}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

const project = (directory) => directory.split("/").filter(Boolean).pop() || directory;
const enc = encodeURIComponent;

function setTitle(text) {
  $("#title").textContent = text;
  $("#back").hidden = ["#/servers", "", "#/settings", "#/dashboard"].includes(location.hash);
}

function stopPoll() {
  clearInterval(state.poll);
  state.poll = null;
}

/* ---------- screens ---------- */

function settingsScreen() {
  setTitle("Settings");
  stopPoll();
  view.innerHTML = `
    <form class="settings">
      <label>Bridge URL
        <input name="bridge" type="url" placeholder="https://daniels-laptop.tail667900.ts.net" value="${escapeHtml(state.bridge)}">
      </label>
      <label>Bridge token
        <input name="token" type="password" placeholder="from the bridge pairing link" value="${escapeHtml(state.token)}">
      </label>
      <button type="submit">Save</button>
      <p class="sub" style="color:var(--dim);font-size:13px">
        Run the bridge on a tailnet machine:
        <code>python3 tools/mynah_opencode_bridge.py</code> — it prints a one-click pair link, or paste the token here.
      </p>
    </form>`;
  $("form.settings").onsubmit = (event) => {
    event.preventDefault();
    state.bridge = $("input[name=bridge]").value.trim().replace(/\/$/, "");
    state.token = $("input[name=token]").value.trim();
    localStorage.setItem("mynah.bridge", state.bridge);
    localStorage.setItem("mynah.token", state.token);
    toast("Saved");
    location.hash = "#/servers";
  };
}

async function serversScreen() {
  setTitle("Servers");
  stopPoll();
  const render = ({ servers, checked_at }) => {
    view.innerHTML = servers.length ? servers.map((server) => {
      const dot = server.found ? "found" : server.online ? "on" : "off";
      return `
        <a class="card" href="#/s/${enc(server.host)}">
          <div class="row">
            <span class="dot ${dot}"></span>
            <div class="grow">
              <h2>${server.hostname}${server.self ? " (this machine)" : ""}</h2>
              <div class="sub">${server.host}:${server.port}</div>
            </div>
            <span class="badge os">${server.os}</span>
          </div>
          <div class="meta">
            <span>${server.found ? "opencode ✓" : server.online ? "no opencode" : "offline"}</span>
            ${server.found ? `<span>health ${server.status}</span>` : ""}
          </div>
        </a>`;
    }).join("") : `<div class="empty">No tailnet peers yet — the bridge is scanning.<br>
      <span class="error">${state.token ? "" : "Pair in ⚙ Settings first."}</span></div>
      <p class="sub" style="color:var(--dim);font-size:12px;text-align:center">checked ${new Date(checked_at * 1000).toLocaleTimeString()}</p>`;
  };
  const load = async () => {
    try {
      render(await api("/api/discovery"));
    } catch (error) {
      if (error.message !== "unpaired") view.innerHTML = `<div class="empty error">${error.message}</div>`;
    }
  };
  await load();
  state.poll = setInterval(load, 10000);
  $("#refresh").onclick = load;
}

async function projectsScreen() {
  setTitle(state.host.split(".").shift());
  stopPoll();
  view.innerHTML = `<div class="empty">Loading sessions…</div>`;
  try {
    const { data: sessions } = await serverApi(state.host, "/api/session?limit=100&order=desc");
    const byDirectory = new Map();
    for (const session of sessions) {
      const directory = session.location?.directory || "(unknown)";
      if (!byDirectory.has(directory)) byDirectory.set(directory, []);
      byDirectory.get(directory).push(session);
    }
    view.innerHTML = byDirectory.size ? [...byDirectory.entries()].map(([directory, list]) => {
      const latest = list.reduce((acc, s) => (s.time?.updated > acc ? s.time.updated : acc), "");
      return `
        <a class="card" href="#/s/${enc(state.host)}/p/${enc(directory)}">
          <h2>${project(directory)}</h2>
          <div class="sub">${directory}</div>
          <div class="meta"><span>${list.length} task${list.length === 1 ? "" : "s"}</span>
            ${latest ? `<span>active ${ago(latest)}</span>` : ""}</div>
        </a>`;
    }).join("") : `<div class="empty">No sessions on ${state.host} yet.</div>`;
  } catch (error) {
    if (error.message !== "unpaired") view.innerHTML = `<div class="empty error">${error.message}</div>`;
  }
  $("#refresh").onclick = projectsScreen;
}

async function sessionsScreen() {
  setTitle(project(state.directory));
  stopPoll();
  const render = async () => {
    try {
      const [{ data: sessions }, { data: active }] = await Promise.all([
        serverApi(state.host, `/api/session?limit=100&order=desc&directory=${enc(state.directory)}`),
        serverApi(state.host, "/api/session/active"),
      ]);
      view.innerHTML = (sessions.length ? sessions.map((session) => `
        <a class="card" href="#/s/${enc(state.host)}/p/${enc(state.directory)}/t/${enc(session.id)}">
          <div class="row">
            <span class="dot ${active[session.id] ? "found" : session.time?.updated && Date.now() - new Date(session.time.updated) < 36e5 ? "on" : "off"}"></span>
            <div class="grow">
              <h2>${session.title || session.id}</h2>
              <div class="sub">${escapeHtml(modelLabel(session.model))}</div>
            </div>
          </div>
          <div class="meta">
            ${active[session.id] ? '<span class="badge active">running</span>' : ""}
            <span>${ago(session.time?.updated || session.time?.created)}</span>
            ${session.cost ? `<span>$${session.cost.toFixed(3)}</span>` : ""}
          </div>
        </a>`).join("") : `<div class="empty">No tasks in this project.</div>`)
        + `<button class="fab" id="new">＋ New task</button>`;
      $("#new").onclick = async () => {
        try {
          const { data: session } = await serverApi(state.host, "/api/session", {
            method: "POST",
            body: JSON.stringify({ location: { directory: state.directory } }),
          });
          toast(`Created ${session.title || session.id}`);
          location.hash = `#/s/${enc(state.host)}/p/${enc(state.directory)}/t/${enc(session.id)}`;
        } catch (error) {
          toast(error.message);
        }
      };
    } catch (error) {
      if (error.message !== "unpaired") view.innerHTML = `<div class="empty error">${error.message}</div>`;
    }
  };
  await render();
  state.poll = setInterval(render, 8000);
  $("#refresh").onclick = render;
}

async function sessionScreen() {
  setTitle("Task");
  stopPoll();
  view.innerHTML = `<div class="empty">Loading task…</div>`;
  const render = async () => {
    try {
      const { data: messages } = await serverApi(state.host, `/api/session/${state.sessionID}/message`);
      view.innerHTML = messages.length ? messages.map((message) => {
        const parts = (message.parts || message.messages || [])
          .map((part) => part.text || part.content || "")
          .filter(Boolean);
        return `<pre class="msg"><div class="role">${message.role || message.type || "message"}</div>${escapeHtml(parts.join("\n\n")) || "…"}</pre>`;
      }).join("") : `<div class="empty">No messages yet.</div>
        <p class="sub" style="color:var(--dim);font-size:13px;text-align:center">Prompt this task from the opencode TUI or web UI; Mynah follows along.</p>`;
    } catch (error) {
      if (error.message !== "unpaired") view.innerHTML = `<div class="empty error">${error.message}</div>`;
    }
  };
  await render();
  state.poll = setInterval(render, 8000);
  $("#refresh").onclick = render;
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

/* ---------- pairing ---------- */

const TOKEN_PATTERN = /^[A-Za-z0-9_-]{16,64}$/;
const HOST_PATTERN = /^[A-Za-z0-9._-]+$/;

async function applyPair(params) {
  const host = (params.get("host") || "").replace(/\.+$/, "");
  const token = params.get("token") || "";
  const port = params.get("port");
  if (!HOST_PATTERN.test(host) || !TOKEN_PATTERN.test(token)) {
    toast("That pairing link is malformed");
    location.hash = "#/settings";
    return false;
  }
  state.bridge = `https://${host}${port && port !== "443" ? `:${port}` : ""}`;
  state.token = token;
  try {
    await api("/api/discovery");
    localStorage.setItem("mynah.bridge", state.bridge);
    localStorage.setItem("mynah.token", state.token);
    toast(`Paired with ${host}`);
    location.hash = "#/dashboard";
    return true;
  } catch (error) {
    if (error.message !== "unpaired") toast(`Bridge unreachable at ${state.bridge}`);
    location.hash = "#/settings";
    return false;
  }
}

function parseCustomPair(text) {
  if (!text.startsWith("mynah-opencode://")) return null;
  return new URL("https://x/?" + text.split("?", 2)[1]).searchParams;
}

function checkInboundPair() {
  const query = new URLSearchParams(location.search);
  const embedded = query.get("pair"); // registerProtocolHandler("?pair=%s")
  if (embedded) {
    const params = parseCustomPair(decodeURIComponent(embedded));
    if (params) return params;
    history.replaceState(null, "", location.pathname);
  }
  return null;
}

try {
  navigator.registerProtocolHandler?.("mynah-opencode", "?pair=%s");
} catch {
  // unsupported on this browser; the web pair link still works
}

/* ---------- router ---------- */

const routes = [
  [/^#\/pair\?(.+)$/, (_, query) => applyPair(new URLSearchParams(query.replace(/#/g, "")))],
  [/^#\/dashboard$/, () => dashboardScreen()],
  [/^#\/dial\/([^/]+)\/([^/]+)$/, (host, sessionID) => {
    state.host = decodeURIComponent(host);
    state.sessionID = decodeURIComponent(sessionID);
    dialScreen();
  }],
  [/^#\/webui\/([^/]+)\/([^/]+)$/, (host, sessionID) => {
    state.host = decodeURIComponent(host);
    state.sessionID = decodeURIComponent(sessionID);
    webuiScreen();
  }],
  [/^#\/servers$/, () => serversScreen()],
  [/^#\/settings$/, () => settingsScreen()],
  [/^#\/s\/([^/]+)$/, (host) => { state.host = decodeURIComponent(host); projectsScreen(); }],
  [/^#\/s\/([^/]+)\/p\/([^/]+)$/, (host, directory) => {
    state.host = decodeURIComponent(host);
    state.directory = decodeURIComponent(directory);
    sessionsScreen();
  }],
  [/^#\/s\/([^/]+)\/p\/([^/]+)\/t\/([^/]+)$/, (host, directory, sessionID) => {
    state.host = decodeURIComponent(host);
    state.directory = decodeURIComponent(directory);
    state.sessionID = decodeURIComponent(sessionID);
    sessionScreen();
  }],
];

async function route() {
  stopPoll();
  $("#refresh").onclick = null;
  for (const [pattern, handler] of routes) {
    const match = location.hash.match(pattern);
    if (match) return handler(...match.slice(1));
  }
  location.hash = state.token ? "#/dashboard" : "#/settings";
}

window.addEventListener("hashchange", route);
$("#back").onclick = () => {
  if (location.hash.startsWith("#/dial/")) location.hash = "#/dashboard";
  else if (location.hash.startsWith("#/webui/")) location.hash = `#/dial/${enc(state.host)}/${enc(state.sessionID)}`;
  else location.hash = "#/dashboard";
};
$("#settings").onclick = () => (location.hash = "#/settings");
navigator.serviceWorker?.register("sw.js");
const inboundPair = checkInboundPair();
if (inboundPair) applyPair(inboundPair);
else route();
