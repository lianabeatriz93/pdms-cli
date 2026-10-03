// Proxy: starting it, its requests with their detail (curl, replay) and its routes.

import { $, act, button, copyText, el, failedText, phaseLabel, post, toast, uptime } from "./core.js";
import { state } from "./state.js";
import { nearBottom, showLogs } from "./logs.js";
import { options } from "./launch.js";
import { currentView } from "./router.js";

const MAX_REQUESTS = 2000;

// One line of the background proxy's log (proxy.format_request): 15:42:07 GET    /api/v1/lead/tp 200 → lead-tp-list@8081  14ms
// A background proxy that keeps its requests ends the line with the id of the capture: … 14ms #1a2b3c4d
const REQUEST = /^(\d\d:\d\d:\d\d) (\S+) +(\S+) (\d{3}) → (\S+) +(\d+)ms(?: #([0-9a-f]{8}))?$/;

const NOT_LOCAL = new Set(["remote", "missing", "other-repo", "docs"]);

export const proxyView = { tab: "requests", stream: null, requests: [], routes: null, routesFor: "", picked: "" }; // picked: capture id

export function proxyJob() {
  return state.jobs.proxy || (state.proxy && state.jobs[state.proxy.key]) || null;
}

export function info(label, value) {
  return el("div", {}, el("span", {}, label), el("b", {}, value || "-"));
}

export function paintProxy() {
  const running = state.proxy;
  const job = proxyJob();
  const busy = job && !job.error;
  $("proxy-on").classList.toggle("on", Boolean(running));
  $("proxy-on").title = running ? t("on :{port}", { port: running.port }) : t("off");
  $("proxy-summary").textContent = busy ? phaseLabel(job.phase)
    : running ? running.status === "ok" ? t("running on :{port}", { port: running.port }) : t("starting on :{port}", { port: running.port })
      : t("off");
  $("proxy-start").hidden = Boolean(running || busy);
  $("proxy-stop").hidden = !running || busy;
  $("proxy-docs").hidden = !running;
  if (running) $("proxy-docs").href = `http://localhost:${running.port}/docs`;

  const card = $("proxy-info");
  if (running) {
    card.replaceChildren(
      info(t("URL"), `http://localhost:${running.port}`),
      info(t("Repo"), running.repo_alias || running.repo),
      info(t("Environment"), running.env),
      info(t("Remote API"), running.remote || t("none (only local services)")),
      info(t("Acting as"), running.as || t("each service's own profile")),
      info(t("Timeout"), running.timeout ? t("{seconds} s per request", { seconds: running.timeout }) : "-"),
      info(t("Frontend"), running.frontend ? t(".env.local → proxy") : t("not changed")),
      info(t("Runs"), running.background ? t("in the background") : t("in a terminal")),
      el("div", {}, el("span", {}, t("Uptime")), el("b", running.started_at ? { "data-started": running.started_at } : {},
        running.started_at ? uptime(running.started_at) : "-")),
    );
  } else {
    card.replaceChildren(el("p", { class: "muted note" }, busy ? t("Starting the proxy…")
      : t("Off. The proxy gives the frontend one port for every service: what runs here answers locally, the rest goes to the remote API.")));
  }
  if (job && job.error) {
    card.append(el("p", { class: "error" }, failedText(job)));
    card.append(el("div", {},
      button(t("Log"), () => showLogs("proxy")),
      button(t("Dismiss"), () => act(`/api/instances/${encodeURIComponent(job.key)}/dismiss`), { class: "btn small ghost" }),
    ));
  }
  for (const tab of $("proxy-tabs").children) tab.setAttribute("aria-selected", String(tab.dataset.tab === proxyView.tab));
  $("proxy-requests").hidden = proxyView.tab !== "requests";
  $("proxy-routes").hidden = proxyView.tab !== "routes";
  syncRequests();
  if (proxyView.tab === "routes" && currentView() === "proxy" && routesKey() !== proxyView.routesFor) loadRoutes();
  else if (proxyView.tab === "requests") paintRequestsNote();
}

// ---- requests: the background proxy's log, followed while the screen is open

function syncRequests() {
  const job = proxyJob();
  const wanted = currentView() === "proxy" && Boolean(state.proxy ? state.proxy.background : job && !job.error);
  if (wanted && !proxyView.stream) {
    const stream = proxyView.stream = new EventSource(`/api/logs/stream?${new URLSearchParams({ key: "proxy", lines: 1000 })}`);
    stream.onopen = () => { proxyView.requests = []; paintRequests(); };
    stream.addEventListener("lines", (event) => addRequests(JSON.parse(event.data)));
    stream.addEventListener("reset", () => { proxyView.requests = []; paintRequests(); }); // started again
  } else if (!wanted && proxyView.stream) {
    proxyView.stream.close();
    proxyView.stream = null;
  }
}

function parseRequest(line) {
  const match = REQUEST.exec(line);
  if (!match) return null;
  const [, time, method, path, status, target, ms, id] = match;
  return { time, method, path, status: Number(status), target, ms: Number(ms), id: id || "" };
}

function requestShown(req) {
  const text = $("req-filter").value.trim().toLowerCase();
  if ($("req-errors").checked && req.status < 400) return false;
  return !text || `${req.method} ${req.path} ${req.status} ${req.target}`.toLowerCase().includes(text);
}

function codeClass(status) {
  return status < 400 ? "code-ok" : status < 500 ? "code-warn" : "code-bad";
}

// A path that may wrap after each "/" (and only there) when its column is narrow.
function slashBreaks(path) {
  return path.split(/(?<=\/)/).flatMap((part, i) => (i ? [document.createElement("wbr"), part] : [part]));
}

// The log of the service that answered, at the line of this request.
function requestLog(req) {
  showLogs(req.target, "current", {
    label: `${req.method} ${req.path}`,
    match: (line) => line.includes(`"${req.method} ${req.path} `) || line.includes(`"${req.method} ${req.path}?`),
  });
}

function requestRow(req) {
  const local = !NOT_LOCAL.has(req.target);
  // Kept by the proxy: the row opens its detail. Older lines (no id) still jump to the service's log.
  const open = req.id ? () => pickRequest(req) : local ? () => requestLog(req) : null;
  const attrs = open ? {
    class: `jump${req.id && req.id === proxyView.picked ? " picked" : ""}`, tabindex: "0",
    title: req.id ? t("See the request") : t("Open the log of {target}", { target: req.target }),
    onclick: open, onkeydown: (event) => { if (event.key === "Enter") open(); },
  } : {};
  if (req.id) attrs["data-id"] = req.id;
  return el("tr", attrs,
    el("td", { class: "mono muted" }, req.time),
    el("td", { class: "method" }, req.method),
    el("td", { class: "mono req-path" }, ...slashBreaks(req.path)),
    el("td", { class: `num ${codeClass(req.status)}` }, String(req.status)),
    el("td", { class: local ? "mono" : `target-${req.target === "other-repo" ? "other" : req.target}` }, req.target),
    el("td", { class: "num muted" }, `${req.ms}ms`),
  );
}

function addRequests(lines) {
  const fresh = lines.map(parseRequest).filter(Boolean);
  if (!fresh.length) return;
  proxyView.requests.push(...fresh);
  if (proxyView.requests.length > MAX_REQUESTS * 1.2) {
    proxyView.requests = proxyView.requests.slice(-MAX_REQUESTS);
    paintRequests();
    return;
  }
  const box = $("req-rows").closest(".tbl");
  const follow = nearBottom(box);
  $("req-rows").append(...fresh.filter(requestShown).map(requestRow));
  paintRequestsNote();
  if (follow) box.scrollTop = box.scrollHeight;
}

// ---- one request: what the proxy kept of it (secret headers hidden by the server)

async function pickRequest(req) {
  proxyView.picked = req.id;
  for (const node of $("req-rows").children) node.classList.toggle("picked", node.dataset.id === req.id);
  $("req-detail").hidden = false;
  $("req-d-method").textContent = req.method;
  $("req-d-path").textContent = req.path;
  $("req-d-body").replaceChildren(el("p", { class: "muted" }, t("Loading…")));
  let data;
  try {
    const response = await fetch(`/api/proxy/request?${new URLSearchParams({ id: req.id })}`);
    data = await response.json();
    if (!response.ok) throw new Error(data.error || t("pdms ui answered {status}", { status: response.status }));
  } catch (error) {
    if (proxyView.picked === req.id) $("req-d-body").replaceChildren(el("p", { class: "error" }, error.message));
    return;
  }
  if (proxyView.picked === req.id) paintRequestDetail(req, data);
}

function headerList(pairs) {
  const list = el("dl", { class: "kv" });
  for (const [name, value] of pairs) list.append(el("dt", {}, name), el("dd", {}, value));
  return list;
}

// A body as text, indented when it is JSON.
function bodyBlock(part) {
  const sent = part.body;
  if (sent.binary) return el("p", { class: "muted" }, t("{size} bytes of binary data", { size: sent.size }));
  if (!sent.text) return el("p", { class: "muted" }, t("(empty)"));
  let text = sent.text;
  try { text = JSON.stringify(JSON.parse(text), null, 2); } catch { /* not JSON: as it came */ }
  const block = el("pre", { class: "json" }, text);
  return sent.truncated ? el("div", {}, block, el("p", { class: "muted" }, t("Only the first 64 KB of {size} bytes.", { size: sent.size }))) : block;
}

function paintRequestDetail(req, data) {
  const capture = data.request;
  const summary = el("dl", { class: "kv" },
    el("dt", {}, t("Status")), el("dd", { class: codeClass(capture.status) }, String(capture.status)),
    el("dt", {}, t("Went to")), el("dd", {}, capture.target),
    el("dt", {}, t("Took")), el("dd", {}, `${capture.ms} ms`),
    el("dt", {}, t("At")), el("dd", {}, new Date(capture.at).toLocaleTimeString()),
  );
  const replay = button(t("Replay"), () => replayRequest(req), data.replay ? { disabled: "", title: data.replay } : {});
  const actions = el("div", { class: "req-actions" },
    button(t("Copy as curl"), () => copyText(data.curl, t("Copied, without the Authorization header.")), data.curl ? {} : { disabled: "" }),
    replay,
  );
  if (!NOT_LOCAL.has(capture.target)) actions.append(button(t("Open the log here"), () => requestLog(req)));
  $("req-d-body").replaceChildren(
    el("h3", { class: "sub" }, t("Summary")), summary,
    el("h3", { class: "sub" }, t("Request headers")), headerList(capture.request.headers),
    el("h3", { class: "sub" }, t("Request body")), bodyBlock(capture.request),
    el("h3", { class: "sub" }, t("Response")), bodyBlock(capture.response),
    actions,
  );
}

function replayRequest(req) {
  act("/api/proxy/replay", { id: req.id }, (data) => {
    toast(t("Sent again: {status} in {ms} ms. It shows up at the bottom of the list.", { status: data.status, ms: data.ms }), "info");
  });
}

export function paintRequests() {
  $("req-rows").replaceChildren(...proxyView.requests.filter(requestShown).map(requestRow));
  paintRequestsNote();
  const box = $("req-rows").closest(".tbl");
  box.scrollTop = box.scrollHeight;
}

function paintRequestsNote() {
  const shown = $("req-rows").children.length;
  const total = proxyView.requests.length;
  $("req-count").textContent = total ? t("{shown} of {total}", { shown, total }) : "";
  const running = state && state.proxy;
  let note = "";
  if (running && !running.background) {
    note = t("This proxy runs in a terminal (pid {pid}): its requests show there. Stop it and start it here, or with pdms proxy -b, to follow them.", { pid: running.pid || "?" });
  } else if (!running) {
    note = total ? t("The proxy is off: these are the requests of its last run.") : t("The proxy is off.");
  } else if (!total) {
    note = t("No requests yet. Point the frontend to {url}.", { url: `http://localhost:${running.port}` });
  } else if (!shown) {
    note = t("No request matches the filter.");
  }
  $("req-empty").textContent = note;
  $("req-empty").hidden = !note;
}

// ---- routes: where each route goes now (pdms proxy routes)

function aliveKeys() {
  return state.instances.filter((i) => i.status !== "stopped").map((i) => i.key).sort().join(",");
}

function routesKey() {
  return `${state.proxy ? `${state.proxy.port}|${state.proxy.env}` : ""}|${aliveKeys()}`;
}

export async function loadRoutes() {
  proxyView.routesFor = routesKey();
  if (!proxyView.routes) $("route-count").textContent = t("Reading the routes from Terraform…");
  try {
    const response = await fetch("/api/proxy/routes");
    const data = await response.json();
    if (!response.ok) {
      proxyView.routes = null;
      $("route-rows").replaceChildren();
      $("route-count").textContent = "";
      $("route-empty").textContent = data.error || t("pdms ui answered {status}", { status: response.status });
      $("route-empty").hidden = false;
      return;
    }
    proxyView.routes = data;
  } catch {
    toast(t("pdms ui is not reachable: is it still running?"));
    return;
  }
  paintRoutes();
}

function routeTarget(route) {
  if (route.target === "local") {
    return el("td", {}, button(route.key, () => showLogs(route.key), { class: "btn tiny link", title: t("Logs of {key}", { key: route.key }) }));
  }
  if (route.key) {
    return el("td", { class: "target-other", title: route.note }, route.target === "remote"
      ? t("remote ({key} in another repo)", { key: route.key }) : t("not available ({key} in another repo)", { key: route.key }));
  }
  return route.target === "remote" ? el("td", { class: "target-remote" }, t("remote")) : el("td", { class: "target-missing" }, t("not available"));
}

export function paintRoutes() {
  const data = proxyView.routes;
  if (!data) return;
  const text = $("route-filter").value.trim().toLowerCase();
  const localOnly = $("route-local").checked;
  const shown = data.routes.filter((route) => (!localOnly || route.target === "local")
    && (!text || route.path.toLowerCase().includes(text) || route.service.toLowerCase().includes(text)));
  $("route-rows").replaceChildren(...shown.map((route) => el("tr", {},
    el("td", { class: "method" }, route.method),
    el("td", { class: "mono" }, route.path),
    el("td", { class: "mono" }, route.service),
    routeTarget(route),
  )));
  const local = data.routes.filter((route) => route.target === "local").length;
  const parts = [t("{shown} of {total} routes", { shown: shown.length, total: data.routes.length }), t("{n} local", { n: local }), data.env];
  if (!data.remote) parts.push(t("no remote API"));
  $("route-count").textContent = parts.join(" · ");
  $("route-empty").textContent = data.routes.length ? t("No route matches the filter.") : t("No routes in the Terraform of '{env}'.", { env: data.env });
  $("route-empty").hidden = shown.length > 0;
}

export function showProxyTab(tab) {
  proxyView.tab = tab;
  if (tab === "routes") proxyView.routesFor = ""; // read them again
  paintProxy();
}

// ---- start

export async function openProxyStart() {
  let found;
  try {
    const response = await fetch("/api/proxy/options");
    found = await response.json();
    if (!response.ok) { toast(found.error || t("pdms ui answered {status}", { status: response.status })); return; }
  } catch {
    toast(t("pdms ui is not reachable: is it still running?"));
    return;
  }
  $("proxy-port").value = found.port;
  options($("proxy-env"), found.envs.length ? found.envs : [found.env], found.env);
  $("proxy-remote").value = found.remote;
  $("proxy-no-remote").checked = false;
  $("proxy-remote").disabled = false;
  options($("proxy-as"), ["", ...state.users], "", (name) => name || t("each service's own profile"));
  $("proxy-frontend-label").hidden = !found.frontend;
  $("proxy-frontend").checked = true;
  $("proxy-warn").hidden = $("proxy-error").hidden = true;
  $("proxy-go").textContent = t("Start");
  $("proxy-dialog").showModal();
}

function proxyProblem(message) {
  $("proxy-error").textContent = message;
  $("proxy-error").hidden = false;
}

export async function submitProxyStart(event) {
  event.preventDefault();
  const hasFrontend = !$("proxy-frontend-label").hidden;
  const body = {
    port: Number($("proxy-port").value), env: $("proxy-env").value, remote: $("proxy-remote").value.trim(),
    no_remote: $("proxy-no-remote").checked, user: $("proxy-as").value,
    frontend: hasFrontend ? $("proxy-frontend").checked : false,
  };
  $("proxy-go").disabled = true;
  $("proxy-error").hidden = true;
  try {
    const { status, data } = await post("/api/proxy/start", body);
    if (status === 202) {
      $("proxy-dialog").close();
      showProxyTab("requests");
      return;
    }
    if (status === 409 && data.decision === "port_busy") {
      $("proxy-port").value = data.free;
      $("proxy-warn").textContent = t("Port {port} is in use. Start on {free} instead?", { port: data.port, free: data.free });
      $("proxy-warn").hidden = false;
      $("proxy-go").textContent = t("Start on {port}", { port: data.free });
      return;
    }
    if (status === 409 && data.decision === "point_frontend") {
      $("proxy-frontend-label").hidden = false;
      $("proxy-warn").textContent = t("Point the frontend to {url}?", { url: data.url });
      $("proxy-warn").hidden = false;
      return;
    }
    proxyProblem(data.error || t("pdms ui answered {status}", { status }));
  } catch {
    proxyProblem(t("pdms ui is not reachable: is it still running?"));
  } finally {
    $("proxy-go").disabled = false;
  }
}

export function resetProxyPort() {
  $("proxy-warn").hidden = true;
  $("proxy-go").textContent = t("Start");
}

export function closeRequest() {
  proxyView.picked = "";
  $("req-detail").hidden = true;
  for (const node of $("req-rows").children) node.classList.remove("picked");
}
