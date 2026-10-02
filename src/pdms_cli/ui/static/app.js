// pdms ui: paints /api/state, follows /api/stream and runs the actions. Text only goes in through textContent,
// never as HTML. Every text it shows is translated with the t and N_ functions of i18n.js.
"use strict";

const $ = (id) => document.getElementById(id);
const MAX_LOG_LINES = 5000;
let state = null;

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children) node.append(child);
  return node;
}

function uptime(startedAt) {
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(startedAt).getTime()) / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours ? `${hours}h${String(minutes).padStart(2, "0")}m` : `${minutes}m${String(seconds % 60).padStart(2, "0")}s`;
}

// A job's phase as the server sends it ("installing", "starting ElasticMQ", "stopping lead-tp-list"), translated.
function phaseLabel(phase) {
  const text = String(phase || "");
  const cut = text.indexOf(" ");
  const verb = cut < 0 ? text : text.slice(0, cut);
  const what = cut < 0 ? "" : text.slice(cut + 1);
  if (verb === "stopping") return what ? t("stopping {what}…", { what }) : t("stopping…");
  if (verb === "installing") return what ? t("installing {what}…", { what }) : t("installing…");
  if (verb === "starting") return what ? t("starting {what}…", { what }) : t("starting…");
  if (verb === "building") return what ? t("building {what}…", { what }) : t("building…");
  if (verb === "restarting") return t("restarting…");
  return text;
}

// An instance's status (also its CSS class), translated.
function statusLabel(status) {
  if (status === "ok") return t("ok");
  if (status === "starting") return t("starting");
  if (status === "error") return t("error");
  if (status === "stopped") return t("stopped");
  if (status === "off") return t("off");
  if (status === "outside") return t("outside pdms");
  return status;
}

// Why a job failed, by its action (start, stop, restart, up, down).
function failedText(job) {
  const error = job.error;
  if (job.action === "start" || job.action === "up") return t("Start failed: {error}", { error });
  if (job.action === "stop" || job.action === "down") return t("Stop failed: {error}", { error });
  if (job.action === "restart") return t("Restart failed: {error}", { error });
  if (job.action === "update") return t("Update failed: {error}", { error });
  if (job.action === "move") return t("Moving to the new repo failed: {error}", { error });
  return t("{action} failed: {error}", { action: job.action, error });
}

function toast(message, kind = "error") {
  const node = el("div", { class: `toast ${kind}`, role: "status" }, message);
  $("toasts").append(node);
  setTimeout(() => node.remove(), kind === "error" ? 8000 : 4000);
}

async function post(path, body = {}) {
  const response = await fetch(path, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  let data = {};
  try { data = await response.json(); } catch { /* an empty or plain-text answer */ }
  return { status: response.status, data };
}

async function act(path, body, done) {
  try {
    const { status, data } = await post(path, body);
    if (status >= 400) toast(data.error || t("pdms ui answered {status}", { status }));
    else if (done) done(data);
  } catch {
    toast(t("pdms ui is not reachable: is it still running?"));
  }
}

// ---------------------------------------------------------------------------- state

function chip(label, value) {
  return el("span", { class: "chip" }, label, el("b", {}, value || "-"));
}

// Who a user is, to tell them apart: "Liana Roget · TPR.Supervisor +1".
function whoIs(name) {
  const profile = (state.profiles || {})[name];
  if (!profile) return "";
  const roles = profile.roles.split(",").map((role) => role.trim()).filter(Boolean);
  const role = roles.length > 1 ? `${roles[0]} +${roles.length - 1}` : roles[0] || "";
  return [profile.name, role].filter(Boolean).join(" · ");
}

function userChip() {
  const node = chip(t("user"), state.user);
  const who = state.user ? whoIs(state.user) : "";
  if (who) {
    node.append(el("span", { class: "who" }, who));
    node.title = `${state.user}: ${state.profiles[state.user].name} · ${state.profiles[state.user].roles}`;
  }
  return node;
}

function paintContext() {
  const repo = el("button", { class: "chip repo-chip", type: "button", "aria-haspopup": "menu", onclick: toggleRepoMenu },
    t("repo"), el("b", {}, (state.repo && state.repo.alias) || "-"));
  const chips = [repo, userChip(), chip(t("db"), state.db)];
  chips.push(chip(t("proxy"), state.proxy ? `:${state.proxy.port}` : t("off")));
  chips.push(chip(t("events"), state.events.up ? `:${state.events.port}` : t("off")));
  $("ctx").replaceChildren(...chips);
}

function button(label, onclick, attrs = {}) {
  return el("button", { class: "btn small", type: "button", onclick, ...attrs }, label);
}

// Services pdms started but lost track of (keys: null for every one).
function adoptStrays(keys, after = null) {
  act("/api/strays/adopt", keys ? { keys } : {}, (data) => {
    const n = data.adopted.length;
    toast(n === 1 ? t("{key} adopted: pdms manages it again.", { key: data.adopted[0] }) : t("{n} services adopted: pdms manages them again.", { n }), "info");
    if (after) after();
  });
}

async function stopStrays(keys) {
  const all = state.strays || [];
  const chosen = keys ? all.filter((s) => keys.includes(s.key)) : all;
  if (!chosen.length) return;
  const what = chosen.length === 1 ? chosen[0].key : t("{n} services", { n: chosen.length });
  if (!await confirmDialog(t("Stop {what}?", { what }), t("They stop with their reloader and workers, like pdms stop."), t("Stop"))) return;
  act("/api/strays/stop", keys ? { keys } : {}, (data) => toast(t("{n} stopped.", { n: data.stopped.length }), "info"));
}

function strayActions(cell, item) {
  cell.append(button(t("Adopt"), () => adoptStrays([item.key]), { title: t("Manage it again: logs, stop and restart") }));
  cell.append(button(t("Stop"), () => stopStrays([item.key]), { class: "btn small bad" }));
  return cell;
}

function rowActions(item, job) {
  const cell = el("td", { class: "row-actions" });
  if (item.isStray) return strayActions(cell, item);
  const busy = job && !job.error;
  cell.append(button(t("Logs"), () => openLogs(item.key, busy ? jobLog(job) : "current")));
  if (item.isFrontend && !busy) return frontendActions(cell, item);
  if (busy || item.isSns) return cell;
  const alive = item.status !== "stopped";
  if (alive && !item.queue && !item.isProxy) {
    cell.append(el("a", {
      class: "btn small", href: `http://localhost:${item.port}/docs`, target: "_blank", rel: "noopener noreferrer",
    }, t("Docs")));
  }
  if (!item.isProxy && !item.placeholder) cell.append(button(t("Restart"), () => openRestart(item)));
  if (alive) {
    cell.append(button(t("Stop"), () => act(`/api/instances/${encodeURIComponent(item.key)}/stop`), { class: "btn small bad" }));
  } else if (!item.placeholder) {
    cell.append(button(t("Forget"), () => act(`/api/instances/${encodeURIComponent(item.key)}/forget`)));
  }
  return cell;
}

function statusCell(item, job) {
  const cell = el("td");
  if (job && !job.error) {
    cell.append(el("span", { class: "st starting" }, phaseLabel(job.phase)));
    return cell;
  }
  cell.append(el("span", item.isStray ? { class: "st outside", title: t("pdms lost track of it: it keeps its port, but pdms ps, logs and Stop do not see it.") }
    : { class: `st ${item.status}` }, statusLabel(item.status)));
  if (item.detail) cell.append(el("span", { class: "detail" }, item.detail));
  if (item.note) cell.append(el("span", { class: "detail quiet" }, item.note));
  if (job && job.error) {
    cell.append(el("span", { class: "detail" }, failedText(job)));
    cell.append(button(t("Dismiss"), () => act(`/api/instances/${encodeURIComponent(item.key)}/dismiss`), { class: "btn tiny" }));
  }
  return cell;
}

function lastPublish(item) {
  return item.last_publish ? t("last {time}", { time: new Date(item.last_publish).toLocaleTimeString() }) : t("nothing yet");
}

function row(item) {
  const job = state.jobs[item.key];
  const running = item.status !== "stopped" && item.started_at && !(job && !job.error);
  const uptimeCell = item.isSns
    ? el("td", { class: "num muted" }, lastPublish(item))
    : el("td", running ? { class: "num", "data-started": item.started_at } : { class: "num" }, running ? uptime(item.started_at) : "");
  return el("tr", item.key === logs.key ? { class: "picked" } : {},
    el("td", { class: "mono" }, item.label || item.key),
    statusCell(item, job),
    el("td", { class: "mono" }, item.url || ""),
    el("td", {}, item.repo || "-"),
    el("td", {}, item.user || "-"),
    el("td", {}, item.db || "-"),
    uptimeCell,
    rowActions(item, job),
  );
}

function serviceItems() {
  const items = state.instances.map((inst) => ({
    ...inst, url: inst.queue ? `sqs ← ${inst.queue}` : `http://localhost:${inst.port}`,
  }));
  if (state.proxy) {
    items.push({
      ...state.proxy, isProxy: true, repo: state.proxy.repo_alias || state.proxy.repo, user: state.proxy.as, db: "", url: `http://localhost:${state.proxy.port}`, detail: "",
    });
  }
  if (state.sns) {
    items.push({
      ...state.sns, isSns: true, url: `sns → ${state.sns.queue}`, repo: "", user: "", db: "", detail: "",
      note: state.sns.status === "off" ? t("Local events are off") : "",
    });
  }
  for (const stray of state.strays || []) {
    items.push({
      ...stray, isStray: true, status: "outside", url: stray.queue ? `sqs ← ${stray.queue}` : `http://localhost:${stray.port}`,
      detail: "", note: `pid ${stray.pid}`,
    });
  }
  const front = state.frontend;
  if (front && (front.running || state.jobs.frontend)) {
    items.push({
      ...front, isFrontend: true, label: front.mode ? `frontend (${front.mode})` : "frontend", user: "", db: "",
      repo: front.repo || "", detail: front.stale ? staleText(front) : front.detail,
    });
  }
  // A restart forgets the instance for a moment: keep its row while the job runs (stacks and events have their own).
  for (const key of Object.keys(state.jobs)) {
    if (!key.includes(":") && key !== HOME_JOB && !items.some((item) => item.key === key)) {
      items.push({ key, status: "stopped", placeholder: true, detail: "", url: "", repo: "", user: "", db: "" });
    }
  }
  return items;
}

function problem(item) {
  const job = state.jobs[item.key];
  return item.isStray || item.status === "error" || (item.status === "stopped" && !item.placeholder) || Boolean(job && job.error);
}

function serviceShown(item) {
  const text = $("svc-filter").value.trim().toLowerCase();
  if ($("svc-problems").checked && !problem(item)) return false;
  return !text || [item.key, item.status, statusLabel(item.status), item.detail, item.url, item.repo, item.user, item.db]
    .join(" ").toLowerCase().includes(text);
}

function paintServices() {
  const items = serviceItems();
  const shown = items.filter(serviceShown);
  $("rows").replaceChildren(...shown.map(row));
  $("empty").hidden = items.length > 0;
  $("svc-none").hidden = !items.length || shown.length > 0;
  $("svc-count").textContent = items.length ? t("{shown} of {total}", { shown: shown.length, total: items.length }) : "";
  const alive = state.instances.filter((i) => i.status !== "stopped");
  const failing = state.instances.filter((i) => i.status === "error" || i.status === "stopped");
  $("count").textContent = alive.length || "";
  const parts = [t("{n} running", { n: alive.length }), t("{n} failing", { n: failing.length })];
  if ((state.strays || []).length) parts.push(t("{n} outside pdms", { n: state.strays.length }));
  $("summary").textContent = parts.join(" · ");
  $("clean").hidden = !state.instances.some((i) => i.status === "stopped");
  $("adopt-all").hidden = !(state.strays || []).length;
  $("front-new").hidden = !state.frontend || state.frontend.running || Boolean(state.jobs.frontend && !state.jobs.frontend.error);
}

function tickUptimes() {
  for (const cell of document.querySelectorAll("[data-started]")) cell.textContent = uptime(cell.dataset.started);
}

function paint(next) {
  // pdms ui restarted with a new version (an update): load its page, which may have changed too.
  if (updateView.loaded === null) updateView.loaded = next.version;
  else if (next.version !== updateView.loaded) { location.reload(); return; }
  state = { jobs: {}, users: [], dbs: [], stacks: [], ...next };
  const relabel = useLanguage(state.language);
  paintContext();
  paintUpdate();
  paintDoctorBadge();
  if (currentView() === "doctor") {
    if (state.doctor.at !== doctorView.at) loadDoctor();
    else paintDoctor();
  }
  paintHome();
  paintServices();
  paintStacks();
  paintProxy();
  const wasUp = eventsView.up;
  eventsView.up = state.events.up;
  paintEvents();
  if (wasUp !== undefined && wasUp !== eventsView.up && currentView() === "events") showEventsTab(eventsView.tab);
  if (logs.key) paintLogTabs();
  syncSettings();
  if (relabel) repaintTexts();
}

// The language changed: paint again what does not come from the state (the state's own parts were just painted).
function repaintTexts() {
  paintRequests();
  paintRoutes();
  paintQueues();
  paintMap();
  eventsView.topics = ""; // the topic list is only rebuilt when its signature changes
  paintSns();
  if (settingsView.data) {
    paintSettings();
    paintDefaults();
  }
}

function connect() {
  const stream = new EventSource("/api/stream");
  stream.addEventListener("state", (event) => paint(JSON.parse(event.data)));
  stream.onopen = () => { $("live").className = "live on"; $("live").title = t("Live"); };
  // EventSource reconnects by itself; the dot shows when pdms ui is not reachable.
  stream.onerror = () => { $("live").className = "live off"; $("live").title = t("Disconnected: is pdms ui still running?"); };
}

// ---------------------------------------------------------------------------- logs

// find: the lines to mark (the request clicked in the proxy), seek: scroll to the last of them once it arrives.
const logs = { key: null, which: "current", stream: null, lines: [], find: null, seek: false };

function nearBottom(node) {
  return node.scrollHeight - node.scrollTop - node.clientHeight < 40;
}

function appendLog(lines) {
  if (!lines.length) return;
  const pre = $("logs-text");
  const follow = nearBottom(pre);
  logs.lines.push(...lines);
  if (logs.lines.length > MAX_LOG_LINES * 1.2) {
    logs.lines = logs.lines.slice(-MAX_LOG_LINES);
    pre.replaceChildren(logText(logs.lines));
  } else {
    pre.append(logText(lines));
  }
  if (logs.seek && seekMark(pre)) return;
  if (follow) pre.scrollTop = pre.scrollHeight;
}

function logText(lines) {
  if (!logs.find) return lines.join("\n") + "\n";
  const out = document.createDocumentFragment();
  let text = [];
  for (const line of lines) {
    if (!logs.find.match(line)) { text.push(line); continue; }
    if (text.length) out.append(text.join("\n") + "\n");
    text = [];
    out.append(el("mark", {}, line), "\n");
  }
  if (text.length) out.append(text.join("\n") + "\n");
  return out;
}

function seekMark(pre) {
  const marks = pre.querySelectorAll("mark");
  if (!marks.length) return false;
  logs.seek = false;
  pre.scrollTop = marks[marks.length - 1].offsetTop - pre.clientHeight / 2;
  return true;
}

function clearLog() {
  logs.lines = [];
  $("logs-text").textContent = "";
}

function paintLogTabs() {
  const noInstall = logs.key.startsWith("proxy") || ["sns", "ui", "update"].includes(logs.key);
  for (const tab of $("logs-tabs").children) {
    tab.setAttribute("aria-selected", String(tab.dataset.which === logs.which));
    tab.hidden = (tab.dataset.which === "install" && noInstall) || (tab.dataset.which === "build" && logs.key !== "frontend");
  }
  const job = state && state.jobs[logs.key];
  $("logs-note").textContent = job && !job.error ? phaseLabel(job.phase) : logs.find ? t("marked: {request}", { request: logs.find.label }) : "";
}

function openLogs(key, which = "current", find = null) {
  if (logs.stream) logs.stream.close();
  Object.assign(logs, { key, which, find, seek: Boolean(find) });
  $("logs").hidden = false;
  $("logs-key").textContent = key;
  paintLogTabs();
  clearLog();
  const query = new URLSearchParams({ key, which, lines: find ? 2000 : 200 });
  const stream = logs.stream = new EventSource(`/api/logs/stream?${query}`);
  stream.onopen = () => { clearLog(); logs.seek = Boolean(logs.find); }; // a reconnect starts again with the tail
  stream.addEventListener("lines", (event) => appendLog(JSON.parse(event.data)));
  stream.addEventListener("reset", () => appendLog(["", t("──── restarted ────"), ""]));
  stream.onerror = () => {
    if (stream.readyState === EventSource.CLOSED) appendLog([t("(no log here)")]);
  };
  if (state) paintServices();
  $("logs").scrollIntoView({ block: "nearest" });
}

function closeLogs() {
  if (logs.stream) logs.stream.close();
  Object.assign(logs, { key: null, stream: null, find: null });
  $("logs").hidden = true;
  paintServices();
}

// ---------------------------------------------------------------------------- restart and up

// go: what the dialog's button does, "restart" or "start" (its label comes from goLabel).
const launch = { path: null, confirmed: false, after: null, run: false, service: "", go: "start" };

function goLabel() {
  return launch.go === "restart" ? t("Restart") : t("Start");
}

function goOnPort(port) {
  return launch.go === "restart" ? t("Restart on {port}", { port }) : t("Start on {port}", { port });
}

function goOnProtected() {
  return launch.go === "restart" ? t("Restart on the protected DB") : t("Start on the protected DB");
}

function options(select, names, current, label = (name) => name) {
  select.replaceChildren(...names.map((name) => el("option", name === current ? { value: name, selected: "" } : { value: name }, label(name))));
}

function dbLabel(name) {
  const db = state.dbs.find((item) => item.name === name);
  return db && db.protected ? t("{name} (protected)", { name }) : name;
}

// The user, database and install of a restart or a stack's up, asking again before a protected database.
function openLaunch({ title, key, hint, user, db, path, go, after, broker = false, run = false, service = "" }) {
  Object.assign(launch, { path, after, run, service, confirmed: false });
  $("run-picker").hidden = $("run-port-label").hidden = !run;
  $("run-consumer").hidden = true;
  $("restart-broker-label").hidden = !broker;
  $("restart-broker").checked = true;
  $("restart-title").textContent = title;
  $("restart-key").textContent = key;
  $("restart-hint").textContent = hint;
  options($("restart-user"), state.users, user);
  options($("restart-db"), state.dbs.map((item) => item.name), db, dbLabel);
  $("restart-warn").hidden = $("restart-error").hidden = true;
  launch.go = go;
  $("restart-go").textContent = goLabel();
  $("restart-form").install.value = "auto";
  $("restart").showModal();
}

function openRestart(item) {
  openLaunch({
    title: t("Restart"), key: item.key, hint: t("Same port. Change the user or the database if you need to."),
    user: item.user, db: item.db, go: "restart", path: `/api/instances/${encodeURIComponent(item.key)}/restart`,
    after: (install) => { if (logs.key === item.key) openLogs(item.key, install === false ? "current" : logs.which); },
  });
}

// Ports of the running instances of each service, by its path in the repo (lead/lead-tp-list).
function runningOn(root) {
  const ports = {};
  for (const inst of state.instances) {
    if (inst.status === "stopped") continue;
    const path = slashes(inst.service);
    if (path.startsWith(`${root}/`)) (ports[path.slice(root.length + 1)] ||= []).push(inst.port);
  }
  return ports;
}

function runningNote(ports) {
  if (!ports) return "";
  const listening = ports.filter(Boolean);
  return el("span", { class: "muted" }, listening.length ? t("running on :{ports}", { ports: listening.join(", :") }) : t("running"));
}

async function fetchServices() {
  try {
    const response = await fetch("/api/services");
    const found = await response.json();
    if (response.ok) return found;
    toast(found.error || t("pdms ui answered {status}", { status: response.status }));
  } catch {
    toast(t("pdms ui is not reachable: is it still running?"));
  }
  return null;
}

async function openRun() {
  const found = await fetchServices();
  if (!found) return;
  const ports = runningOn(slashes(found.root));
  const consumers = new Set(found.consumers || []);
  $("run-filter").value = "";
  $("run-port").value = "";
  $("run-services").replaceChildren(...found.services.map((svc) => {
    const radio = el("input", { type: "radio", name: "run-service", value: svc });
    radio.addEventListener("change", () => {
      const consumer = consumers.has(svc);
      $("run-port-label").hidden = consumer;
      $("run-consumer").hidden = !consumer;
      resetConfirmation();
    });
    return el("label", { class: "pick", "data-svc": svc.toLowerCase() }, radio, el("span", { class: "mono" }, svc), runningNote(ports[svc]));
  }));
  $("run-count").textContent = servicesCount(found.services.length);
  openLaunch({
    title: t("Start a service"), key: "", hint: t("In the background, like pdms run -b. It keeps running when pdms ui stops."),
    user: state.user, db: state.db, go: "start", path: "/api/run", run: true,
    after: (_install, data) => openLogs(data.job),
  });
  $("run-filter").focus();
}

function servicesCount(n) {
  return n === 1 ? t("{n} service", { n }) : t("{n} services", { n });
}

function filterRun() {
  const text = $("run-filter").value.trim().toLowerCase();
  let shown = 0;
  for (const item of $("run-services").children) {
    item.hidden = Boolean(text) && !item.dataset.svc.includes(text);
    if (!item.hidden) shown += 1;
  }
  const total = $("run-services").children.length;
  $("run-count").textContent = text ? t("{shown} of {total}", { shown, total }) : servicesCount(total);
}

function openUp(stack) {
  openLaunch({
    title: t("Start"), key: stack.name, hint: t("Starts the services that are not running yet, each on a free port."),
    user: stack.user || state.user, db: stack.db || state.db, go: "start", path: `/api/stacks/${encodeURIComponent(stack.name)}/up`,
    after: (_install, data) => { if (!data.job) toast(t("The whole stack '{name}' is already running.", { name: stack.name }), "info"); },
  });
}

async function submitLaunch(event) {
  event.preventDefault();
  const install = { auto: null, force: true, skip: false }[$("restart-form").install.value];
  const body = { user: $("restart-user").value, db: $("restart-db").value, install, confirmed: launch.confirmed };
  if (!$("restart-broker-label").hidden) body.broker = $("restart-broker").checked;
  if (launch.service) Object.assign(body, { service: launch.service, port: null });
  if (launch.run) {
    const picked = $("run-services").querySelector("input:checked");
    if (!picked) {
      $("restart-error").textContent = t("Pick the service to start.");
      $("restart-error").hidden = false;
      return;
    }
    body.service = picked.value;
    body.port = $("run-port-label").hidden || !$("run-port").value ? null : Number($("run-port").value);
  }
  $("restart-error").hidden = true;
  $("restart-go").disabled = true;
  try {
    const { status, data } = await post(launch.path, body);
    if (status === 200 || status === 202) {
      $("restart").close();
      if (launch.after) launch.after(install, data);
      return;
    }
    if (status === 409 && data.decision === "port_busy") {
      $("run-port").value = data.free;
      $("restart-warn").textContent = t("Port {port} is in use. Start on {free} instead?", { port: data.port, free: data.free });
      $("restart-warn").hidden = false;
      $("restart-go").textContent = goOnPort(data.free);
      return;
    }
    if (status === 409 && data.decision === "protected_database") {
      launch.confirmed = true;
      $("restart-warn").textContent = t("'{name}' is a protected database. Use it anyway?", { name: data.name });
      $("restart-warn").hidden = false;
      $("restart-go").textContent = goOnProtected();
      return;
    }
    $("restart-error").textContent = data.error || t("pdms ui answered {status}", { status });
    $("restart-error").hidden = false;
  } catch {
    $("restart-error").textContent = t("pdms ui is not reachable: is it still running?");
    $("restart-error").hidden = false;
  } finally {
    $("restart-go").disabled = false;
  }
}

function resetConfirmation() {
  launch.confirmed = false;
  $("restart-warn").hidden = true;
  $("restart-go").textContent = goLabel();
}

// ---------------------------------------------------------------------------- stacks

// What a stack without its own user or database shows.
function ask() {
  return t("(ask when starting)");
}

function stackPath(name) {
  return `/api/stacks/${encodeURIComponent(name)}`;
}

function showLogs(key, which = "current", find = null) {
  location.hash = "#services";
  route(); // now, not on hashchange: the logs scroll into view only once the view shows
  openLogs(key, which, find);
}

// The stack's services by domain (lead/lead-tp-list → lead), in the stack's order.
function stackGroups(services) {
  const groups = new Map();
  for (const svc of services) {
    const cut = svc.path.indexOf("/");
    const domain = cut < 0 ? "" : svc.path.slice(0, cut);
    if (!groups.has(domain)) groups.set(domain, []);
    groups.get(domain).push({ ...svc, name: cut < 0 ? svc.path : svc.path.slice(cut + 1) });
  }
  return [...groups];
}

function stackFilterText() {
  return $("stack-filter").value.trim().toLowerCase();
}

function stackShown(stack) {
  const text = stackFilterText();
  if ($("stack-running").checked && !stack.services.some((svc) => svc.running.length)) return false;
  return !text || [stack.name, stack.user, stack.db, ...stack.services.map((svc) => svc.path)]
    .join(" ").toLowerCase().includes(text);
}

function stackCard(stack) {
  const text = stackFilterText();
  const job = state.jobs[`stack:${stack.name}`];
  const busy = job && !job.error;
  const total = stack.services.length;
  const up = stack.services.filter((svc) => svc.running.length).length;
  const status = busy
    ? el("span", { class: "st starting" }, phaseLabel(job.phase))
    : el("span", { class: `st ${up === total ? "ok" : up ? "starting" : "stopped"}` },
      up === total ? t("running") : up ? t("{up} of {total} running", { up, total }) : t("stopped"));

  const list = el("div", { class: "stack-groups" }, ...stackGroups(stack.services).map(([domain, services]) => {
    const running = services.filter((svc) => svc.running.length).length;
    return el("section", { class: "stack-group" },
      el("h3", {}, el("span", { class: "mono" }, domain || t("(repo root)")),
        el("span", { class: "muted" }, `${running}/${services.length}`)),
      el("ul", { class: "stack-services" }, ...services.map((svc) => el("li", {
        title: svc.path, ...(text && svc.path.toLowerCase().includes(text) ? { class: "hit" } : {}),
      },
        el("span", { class: `dot ${svc.running.length ? "on" : ""}` }),
        el("span", { class: "mono name" }, svc.name),
        el("span", { class: "ports" }, ...svc.running.map((key) => button(key.slice(key.indexOf("@")), () => showLogs(key), {
          class: "btn tiny link", title: t("Logs of {key}", { key }),
        }))),
      ))),
    );
  }));

  const card = el("article", { class: "card stack" },
    el("header", {}, el("h2", { class: "mono" }, stack.name), status),
    list,
    el("p", { class: "muted meta" }, t("user {user} · db {db}", { user: stack.user || ask(), db: stack.db || ask() })),
  );
  if (job && job.error) {
    card.append(el("p", { class: "error" }, failedText(job)));
  }
  const footer = el("footer", {});
  if (job && job.log_key) footer.append(button(t("Install log"), () => showLogs(job.log_key, "install")));
  if (job && job.error) footer.append(button(t("Dismiss"), () => act(`${stackPath(stack.name)}/dismiss`)));
  if (!busy) {
    if (up < total) footer.append(button(t("Start"), () => openUp(stack), { class: "btn small primary" }));
    if (up) footer.append(button(t("Stop"), () => act(`${stackPath(stack.name)}/down`), { class: "btn small bad" }));
    footer.append(button(t("Edit"), () => openEditor(stack)));
    footer.append(button(t("Delete"), () => removeStack(stack), { class: "btn small ghost" }));
  }
  card.append(footer);
  return card;
}

function paintStacks() {
  const shown = state.stacks.filter(stackShown);
  $("stacks").replaceChildren(...shown.map(stackCard));
  $("stacks-empty").hidden = state.stacks.length > 0;
  $("stacks-none").hidden = !state.stacks.length || shown.length > 0;
  $("stack-shown").textContent = state.stacks.length ? t("{shown} of {total}", { shown: shown.length, total: state.stacks.length }) : "";
  const running = state.stacks.filter((stack) => stack.services.some((svc) => svc.running.length)).length;
  $("stack-count").textContent = state.stacks.length || "";
  const stacks = state.stacks.length;
  $("stack-summary").textContent = [
    stacks === 1 ? t("{n} stack", { n: stacks }) : t("{n} stacks", { n: stacks }), t("{n} running", { n: running }),
  ].join(" · ");
}

function confirmDialog(title, text, yes) {
  $("confirm-title").textContent = title;
  $("confirm-text").textContent = text;
  $("confirm-yes").textContent = yes;
  $("confirm").returnValue = "";
  $("confirm").showModal();
  return new Promise((resolve) => {
    $("confirm").addEventListener("close", () => resolve($("confirm").returnValue === "yes"), { once: true });
  });
}

async function removeStack(stack) {
  if (await confirmDialog(t("Delete stack {name}?", { name: stack.name }), t("Only the stack goes; its services keep running if they are."), t("Delete"))) {
    act(`${stackPath(stack.name)}/remove`, {}, () => toast(t("Stack '{name}' deleted.", { name: stack.name }), "info"));
  }
}

// ---------------------------------------------------------------------------- stack editor

const editor = { name: null, order: [], picked: new Set() };

function slashes(path) {
  return path.replaceAll("\\", "/").replace(/\/+$/, "");
}

async function openEditor(stack = null) {
  const found = await fetchServices();
  if (!found) return;
  const ports = runningOn(slashes(found.root));
  // Like pdms stack edit: the stack's services first, then the running ones, then the rest.
  const current = stack ? stack.services.map((svc) => svc.path) : [];
  const rest = found.services.filter((svc) => !current.includes(svc));
  editor.order = [...current, ...rest.filter((svc) => ports[svc]), ...rest.filter((svc) => !ports[svc])];
  editor.picked = new Set(current);
  editor.name = stack ? stack.name : null;

  $("editor-title").textContent = stack ? t("Edit {name}", { name: stack.name }) : t("New stack");
  $("editor-name-label").hidden = Boolean(stack);
  $("editor-name").required = !stack;
  $("editor-name").value = "";
  $("editor-filter").value = "";
  $("editor-services").replaceChildren(...editor.order.map((svc) => {
    const box = el("input", { type: "checkbox", value: svc });
    box.checked = editor.picked.has(svc);
    box.addEventListener("change", () => {
      if (box.checked) editor.picked.add(svc); else editor.picked.delete(svc);
      editorCount();
    });
    return el("label", { class: "pick", "data-svc": svc.toLowerCase() }, box, el("span", { class: "mono" }, svc), runningNote(ports[svc]));
  }));
  options($("editor-user"), ["", ...state.users], stack ? stack.user : "", (name) => name || ask());
  options($("editor-db"), ["", ...state.dbs.map((item) => item.name)], stack ? stack.db : "", (name) => name ? dbLabel(name) : ask());
  $("editor-error").hidden = true;
  editorCount();
  $("editor").showModal();
  (stack ? $("editor-filter") : $("editor-name")).focus();
}

function editorCount() {
  $("editor-count").textContent = t("{picked} of {total} selected", { picked: editor.picked.size, total: editor.order.length });
}

function filterEditor() {
  const text = $("editor-filter").value.trim().toLowerCase();
  for (const item of $("editor-services").children) item.hidden = Boolean(text) && !item.dataset.svc.includes(text);
}

async function saveEditor(event) {
  event.preventDefault();
  const name = editor.name || $("editor-name").value.trim();
  const body = {
    services: editor.order.filter((svc) => editor.picked.has(svc)),
    user: $("editor-user").value, db: $("editor-db").value, new: !editor.name,
  };
  if (!body.services.length) {
    $("editor-error").textContent = t("A stack needs at least one service.");
    $("editor-error").hidden = false;
    return;
  }
  $("editor-save").disabled = true;
  try {
    const { status, data } = await post(`${stackPath(name)}/save`, body);
    if (status === 200) {
      $("editor").close();
      toast(t("Stack '{name}' saved.", { name }), "info");
      return;
    }
    $("editor-error").textContent = data.error || t("pdms ui answered {status}", { status });
    $("editor-error").hidden = false;
  } catch {
    $("editor-error").textContent = t("pdms ui is not reachable: is it still running?");
    $("editor-error").hidden = false;
  } finally {
    $("editor-save").disabled = false;
  }
}

// ---------------------------------------------------------------------------- proxy

const MAX_REQUESTS = 2000;
// One line of the background proxy's log (proxy.format_request): 15:42:07 GET    /api/v1/lead/tp 200 → lead-tp-list@8081  14ms
const REQUEST = /^(\d\d:\d\d:\d\d) (\S+) +(\S+) (\d{3}) → (\S+) +(\d+)ms$/;
const NOT_LOCAL = new Set(["remote", "missing", "other-repo", "docs"]);
const proxyView = { tab: "requests", stream: null, requests: [], routes: null, routesFor: "" };

function proxyJob() {
  return state.jobs.proxy || (state.proxy && state.jobs[state.proxy.key]) || null;
}

function info(label, value) {
  return el("div", {}, el("span", {}, label), el("b", {}, value || "-"));
}

function paintProxy() {
  const running = state.proxy;
  const job = proxyJob();
  const busy = job && !job.error;
  $("proxy-on").textContent = running ? `:${running.port}` : "";
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
  const [, time, method, path, status, target, ms] = match;
  return { time, method, path, status: Number(status), target, ms: Number(ms) };
}

function requestShown(req) {
  const text = $("req-filter").value.trim().toLowerCase();
  if ($("req-errors").checked && req.status < 400) return false;
  return !text || `${req.method} ${req.path} ${req.status} ${req.target}`.toLowerCase().includes(text);
}

function codeClass(status) {
  return status < 400 ? "code-ok" : status < 500 ? "code-warn" : "code-bad";
}

function requestRow(req) {
  const local = !NOT_LOCAL.has(req.target);
  const open = () => showLogs(req.target, "current", {
    label: `${req.method} ${req.path}`,
    match: (line) => line.includes(`"${req.method} ${req.path} `) || line.includes(`"${req.method} ${req.path}?`),
  });
  const attrs = local ? {
    class: "jump", tabindex: "0", title: t("Open the log of {target}", { target: req.target }),
    onclick: open, onkeydown: (event) => { if (event.key === "Enter") open(); },
  } : {};
  return el("tr", attrs,
    el("td", { class: "mono muted" }, req.time),
    el("td", { class: "method" }, req.method),
    el("td", { class: "mono" }, req.path),
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

function paintRequests() {
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

async function loadRoutes() {
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

function paintRoutes() {
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

function showProxyTab(tab) {
  proxyView.tab = tab;
  if (tab === "routes") proxyView.routesFor = ""; // read them again
  paintProxy();
}

// ---- start

const proxyStart = { confirmedPort: null };

async function openProxyStart() {
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

async function submitProxyStart(event) {
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

function resetProxyPort() {
  $("proxy-warn").hidden = true;
  $("proxy-go").textContent = t("Start");
}

// ---------------------------------------------------------------------------- events

const EVENTS_JOB = "events:elasticmq";
const SNS_LINES = 20000;
const MAX_SNS = 2000;
const SNS_HEADER = /^(\S+) (\S+) → (\S+)(.*)$/; // events' sitecustomize.write_log: when, who → topic, extras
const eventsView = { tab: "queues", queues: null, map: null, peek: null, sns: [], snsStream: null, topics: "" };

function eventsJob() {
  return state.jobs[EVENTS_JOB] || null;
}

function eventsPath(verb) {
  return `/api/events/${verb}`;
}

function paintEvents() {
  const job = eventsJob();
  const busy = job && !job.error;
  const up = state.events.up;
  $("events-on").textContent = up ? `:${state.events.port}` : "";
  $("events-summary").textContent = busy ? phaseLabel(job.phase) : up ? t("ElasticMQ running on :{port}", { port: state.events.port }) : t("off");
  $("events-start").hidden = up || busy;
  $("events-stop").hidden = !up || busy;
  $("events-send").hidden = !up;
  const brokerRow = up && !busy ? brokerQueue() : null;
  $("events-broker").hidden = !brokerRow || !brokerRow.consumer || Boolean(brokerRow.running) || Boolean(consumerJob(brokerRow.consumer))
    || state.instances.some((i) => i.queue === brokerRow.name && i.status !== "stopped");

  const card = $("events-info");
  const data = eventsView.queues;
  if (up) {
    const consumers = state.instances.filter((i) => i.queue && i.status !== "stopped");
    const broker = data && data.broker ? consumers.find((i) => i.queue === data.broker) : null;
    const publishers = state.instances.filter((i) => i.events === "local" && i.status !== "stopped").length;
    card.replaceChildren(
      info(t("Endpoint"), `http://localhost:${state.events.port}`),
      info(t("Broker"), !data ? "…" : broker ? broker.key : data.broker ? t("not running") : t("not in the repo")),
      info(t("Consumers running"), String(consumers.length)),
      info(t("Publishing locally"), servicesCount(publishers)),
      info(t("Last SNS publish"), state.sns && state.sns.last_publish ? new Date(state.sns.last_publish).toLocaleTimeString() : t("nothing yet")),
    );
  } else {
    card.replaceChildren(el("p", { class: "muted note" }, busy ? t("Starting the local ElasticMQ…")
      : t("Off: services publish to AWS. Start the local events to run a local ElasticMQ (Docker) with every queue of the repo and the broker; services started afterwards publish there and to a local SNS.")));
  }
  if (job && job.error) {
    card.append(el("p", { class: "error" }, job.action === "up" ? t("Start failed: {error}", { error: job.error }) : t("Stop failed: {error}", { error: job.error })));
    const row = el("div", {});
    if (job.log_key) row.append(button(t("Install log"), () => showLogs(job.log_key, "install")));
    row.append(button(t("Dismiss"), () => act(eventsPath("dismiss")), { class: "btn small ghost" }));
    card.append(row);
  }
  for (const tab of $("events-tabs").children) tab.setAttribute("aria-selected", String(tab.dataset.tab === eventsView.tab));
  $("events-queues").hidden = eventsView.tab !== "queues";
  $("events-types").hidden = eventsView.tab !== "types";
  $("events-sns").hidden = eventsView.tab !== "sns";
  if (eventsView.tab === "queues") paintQueues(); // a consumer's start shows as it goes
  syncSns();
}

function showEventsTab(tab) {
  eventsView.tab = tab;
  paintEvents();
  if (tab === "queues") loadQueues();
  if (tab === "types") loadMap();
}

function eventsVisible(tab) {
  return currentView() === "events" && eventsView.tab === tab && !document.hidden;
}

async function getJson(path) {
  try {
    const response = await fetch(path);
    const data = await response.json();
    return response.ok ? { data } : { error: data.error || t("pdms ui answered {status}", { status: response.status }) };
  } catch {
    return { error: t("pdms ui is not reachable: is it still running?") };
  }
}

// ---- queues

async function loadQueues() {
  const { data, error } = await getJson(eventsPath("queues"));
  if (error) {
    eventsView.queues = null;
    $("queue-rows").replaceChildren();
    $("queue-count").textContent = "";
    $("queue-empty").textContent = error;
    $("queue-empty").hidden = false;
    return;
  }
  eventsView.queues = data;
  paintQueues();
  paintEvents();
}

function count(value) {
  return value === null ? el("td", { class: "num muted" }, "-")
    : el("td", { class: `num ${value ? "count-on" : "muted"}` }, String(value));
}

// The job starting a consumer (pdms keys a consumer service by its folder: lead/lead-sqs-consumer → lead-sqs-consumer@sqs).
function consumerJob(service) {
  return service ? state.jobs[`${service.slice(service.lastIndexOf("/") + 1)}@sqs`] || null : null;
}

function openConsumerStart(service, what = "consumer") {
  openLaunch({
    title: t("Start"), key: service, go: "start", path: "/api/run", service, user: state.user, db: state.db,
    hint: what === "broker" ? t("The broker reads its queue from the local ElasticMQ, in the background like pdms run -b.")
      : t("The consumer reads its queue from the local ElasticMQ, in the background like pdms run -b."),
    after: (_install, data) => { toast(t("Starting {job}…", { job: data.job }), "info"); loadQueues(); },
  });
}

function brokerQueue() {
  const data = eventsView.queues;
  return data && data.queues.find((queue) => queue.broker) || null;
}

// A live instance of the queue's consumer reading another queue (a service that consumes two, like a retry queue).
function consumerElsewhere(queue) {
  if (!queue.consumer || queue.running) return null;
  return state.instances.find((i) => i.queue && i.status !== "stopped" && slashes(i.service).endsWith(`/${queue.consumer}`)) || null;
}

function queueRow(queue) {
  const elsewhere = consumerElsewhere(queue);
  const job = queue.running || elsewhere ? null : consumerJob(queue.consumer);
  const consumer = queue.running
    ? button(queue.running, () => showLogs(queue.running), { class: "btn tiny link", title: t("Logs of {key}", { key: queue.running }) })
    : elsewhere ? el("span", {}, button(elsewhere.key, () => showLogs(elsewhere.key), { class: "btn tiny link", title: t("Logs of {key}", { key: elsewhere.key }) }),
      el("span", { class: "muted" }, " ", t("reads {queue}", { queue: elsewhere.queue })))
    : queue.sns ? el("span", { class: "muted" }, t("local SNS: every publish"))
      : queue.broker ? el("span", { class: "muted" }, queue.consumer ? t("{consumer} (not running)", { consumer: queue.consumer }) : t("broker (not running)"))
        : el("span", { class: "muted" }, queue.consumer || "-");
  const actions = el("td", { class: "row-actions" });
  if (job) {
    actions.append(job.error
      ? button(t("Start failed"), () => showLogs(job.log_key || job.key, job.installed ? "install" : "current"), { class: "btn small bad", title: job.error })
      : el("span", { class: "st starting" }, phaseLabel(job.phase)));
  } else if (queue.consumer && !queue.running && !elsewhere && eventsView.queues.up) {
    actions.append(button(t("Start"), () => openConsumerStart(queue.consumer, queue.broker ? "broker" : "consumer"), { class: "btn small primary" }));
  }
  if (queue.visible !== null) {
    actions.append(button(t("Messages"), () => openPeek(queue.name)));
    if (!queue.sns) actions.append(button(t("Send"), () => openSend(queue.name)));
    if (queue.visible || queue.in_flight) actions.append(button(t("Purge"), () => purge([queue.name]), { class: "btn small bad" }));
  }
  const tags = [queue.fifo ? "fifo" : "", queue.broker ? t("broker") : "", queue.source === "elasticmq.conf" ? t("elasticmq.conf only") : ""].filter(Boolean);
  return el("tr", eventsView.peek === queue.name ? { class: "picked" } : {},
    el("td", { class: "mono wrap" }, queue.name, tags.length ? el("span", { class: "tag" }, tags.join(" · ")) : ""),
    count(queue.visible), count(queue.in_flight),
    el("td", { class: "wrap" }, consumer),
    el("td", { class: "num muted" }, queue.types ? String(queue.types) : ""),
    actions,
  );
}

function paintQueues() {
  const data = eventsView.queues;
  if (!data) return;
  const text = $("queue-filter").value.trim().toLowerCase();
  const busyOnly = $("queue-busy").checked;
  const shown = data.queues.filter((queue) => (!busyOnly || queue.visible || queue.in_flight)
    && (!text || `${queue.name} ${queue.consumer} ${queue.running}`.toLowerCase().includes(text)));
  $("queue-rows").replaceChildren(...shown.map(queueRow));
  const waiting = data.queues.reduce((sum, queue) => sum + (queue.visible || 0), 0);
  const parts = [t("{shown} of {total} queues", { shown: shown.length, total: data.queues.length })];
  if (data.up) parts.push(waiting === 1 ? t("{n} message waiting", { n: waiting }) : t("{n} messages waiting", { n: waiting }));
  $("queue-count").textContent = parts.join(" · ");
  $("queue-purge-all").hidden = !data.up || !waiting;
  $("queue-empty").textContent = data.up ? t("No queue matches the filter.") : "";
  $("queue-empty").hidden = shown.length > 0 || !data.up;
}

async function purge(queues) {
  const title = queues.length ? t("Purge {queues}?", { queues: queues.join(", ") }) : t("Purge every queue?");
  if (!await confirmDialog(title, t("Every message waiting there is deleted; nothing consumes them."), t("Purge"))) return;
  act(eventsPath("purge"), { queues }, (data) => {
    toast(data.purged.length ? t("Purged {queues}.", { queues: data.purged.join(", ") }) : t("Nothing to purge."), "info");
    loadQueues();
    if (eventsView.peek) openPeek(eventsView.peek);
  });
}

// ---- messages of a queue (peek)

const JSON_TOKEN = /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g;

// ``value`` with every string that holds a JSON object or list decoded, at any depth: an SNS message keeps the
// published Message as a string, whose data is often a JSON string too (like sitecustomize._unnest for sns.log).
function unnest(value) {
  if (typeof value === "string" && /^\s*[[{]/.test(value)) {
    try {
      return unnest(JSON.parse(value));
    } catch {
      return value;
    }
  }
  if (Array.isArray(value)) return value.map(unnest);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, unnest(item)]));
  return value;
}

// Pretty JSON with its keys, strings, numbers and literals coloured, JSON inside its strings decoded too; text that
// is not JSON stays as it is.
function jsonView(text) {
  let source;
  try {
    source = JSON.stringify(unnest(JSON.parse(text)), null, 2);
  } catch {
    return document.createTextNode(text);
  }
  const out = document.createDocumentFragment();
  let at = 0;
  for (const match of source.matchAll(JSON_TOKEN)) {
    if (match.index > at) out.append(source.slice(at, match.index));
    const [token, string, colon, literal] = match;
    if (string) {
      out.append(el("span", { class: colon ? "j-key" : "j-str" }, string));
      if (colon) out.append(colon);
    } else {
      out.append(el("span", { class: literal ? "j-lit" : "j-num" }, token));
    }
    at = match.index + token.length;
  }
  out.append(source.slice(at));
  return out;
}

// The message of a details row, built when it is first opened (a long SNS log holds many large ones).
function messageDetails(summary, text) {
  const node = el("details", { class: "msg" }, summary);
  const fill = () => {
    if (node.querySelector("pre")) return;
    const copy = button(t("Copy"), async () => {
      try {
        await navigator.clipboard.writeText(text);
        toast(t("Copied."), "info");
      } catch {
        toast(t("The browser did not allow copying."));
      }
    }, { class: "btn tiny copy" });
    node.append(el("div", { class: "msg-body" }, copy, el("pre", {}, jsonView(text))));
  };
  node.addEventListener("toggle", () => { if (node.open) fill(); });
  node.fill = fill;
  return node;
}

// What a message is about: the event name or type it carries, if any.
function messageKind(text) {
  try {
    const parsed = JSON.parse(text);
    if (!parsed || typeof parsed !== "object") return "";
    return String(parsed.event || parsed.type || parsed.event_type || (parsed.Message && messageKind(parsed.Message)) || "");
  } catch {
    return "";
  }
}

async function openPeek(queue) {
  eventsView.peek = queue;
  $("peek").hidden = false;
  $("peek-queue").textContent = queue;
  $("peek-note").textContent = t("reading…");
  paintQueues();
  const { data, error } = await getJson(`${eventsPath("peek")}?${new URLSearchParams({ queue })}`);
  if (eventsView.peek !== queue) return;
  if (error) {
    $("peek-note").textContent = error;
    $("peek-messages").replaceChildren();
    return;
  }
  const waiting = data.messages.length;
  $("peek-note").textContent = waiting
    ? [waiting >= 50 ? t("{n} waiting (first 50)", { n: waiting }) : t("{n} waiting", { n: waiting }), t("read without consuming them")].join(" · ")
    : t("empty");
  $("peek-messages").replaceChildren(...data.messages.map((message) => {
    const kind = messageKind(message.body);
    return messageDetails(
      el("summary", {},
        el("span", { class: "mono muted" }, message.id.slice(0, 8)),
        kind ? el("span", { class: "topic" }, kind) : "",
        message.sent ? el("span", { class: "muted" }, new Date(message.sent).toLocaleString()) : "",
        el("span", { class: "muted" }, message.receives === 1
          ? t("received {n} time", { n: message.receives }) : t("received {n} times", { n: message.receives })),
      ),
      message.body,
    );
  }));
  $("peek").scrollIntoView({ block: "nearest" });
}

function closePeek() {
  eventsView.peek = null;
  $("peek").hidden = true;
  paintQueues();
}

// ---- event types

async function loadMap() {
  const { data, error } = await getJson(eventsPath("map"));
  if (error) {
    $("type-rows").replaceChildren();
    $("type-empty").textContent = error;
    $("type-empty").hidden = false;
    return;
  }
  eventsView.map = data;
  paintMap();
}

function paintMap() {
  const data = eventsView.map;
  if (!data) return;
  const text = $("type-filter").value.trim().toLowerCase();
  const shown = data.types.filter((item) => !text || `${item.type} ${item.queue} ${item.consumer}`.toLowerCase().includes(text));
  $("type-rows").replaceChildren(...shown.map((item) => el("tr", {},
    el("td", { class: "mono" }, item.type),
    el("td", { class: "mono" }, item.queue),
    item.consumer ? el("td", { class: "mono" }, item.consumer) : el("td", { class: "target-missing" }, t("none")),
    el("td", { class: "row-actions" }, state.events.up ? button(t("Send"), () => openSend(item.type)) : ""),
  )));
  $("type-count").textContent = [
    t("{shown} of {total} event types", { shown: shown.length, total: data.types.length }),
    data.broker ? t("broker {name}", { name: data.broker }) : t("broker not found"),
  ].join(" · ");
  $("type-empty").textContent = data.types.length ? t("No event type matches the filter.") : t("The broker routes no event types.");
  $("type-empty").hidden = shown.length > 0;
}

// ---- local SNS: sns.log, followed while the tab is open

function syncSns() {
  const wanted = currentView() === "events" && eventsView.tab === "sns";
  if (wanted && !eventsView.snsStream) {
    const stream = eventsView.snsStream = new EventSource(`/api/logs/stream?${new URLSearchParams({ key: "sns", lines: SNS_LINES })}`);
    stream.onopen = () => { eventsView.sns = []; paintSns(); };
    stream.addEventListener("lines", (event) => addSns(JSON.parse(event.data)));
    stream.addEventListener("reset", () => { eventsView.sns = []; paintSns(); }); // the log rotated
  } else if (!wanted && eventsView.snsStream) {
    eventsView.snsStream.close();
    eventsView.snsStream = null;
  }
}

function parseSnsHeader(line) {
  const match = SNS_HEADER.exec(line);
  if (!match) return null;
  let rest = match[4];
  let attributes = {};
  const at = rest.indexOf(" attributes={");
  if (at >= 0) {
    try { attributes = JSON.parse(rest.slice(at + " attributes=".length)); } catch { /* shown without them */ }
    rest = rest.slice(0, at);
  }
  let subject = "";
  const sj = rest.indexOf(" subject=");
  if (sj >= 0) { subject = rest.slice(sj + " subject=".length); rest = rest.slice(0, sj); }
  const g = rest.indexOf(" group=");
  const group = g >= 0 ? rest.slice(g + " group=".length).trim() : "";
  return { time: match[1], service: match[2], topic: match[3], subject, group, attributes, body: [] };
}

function addSns(lines) {
  for (const line of lines) {
    const last = eventsView.sns[eventsView.sns.length - 1];
    if (line.startsWith("  ") && last) last.body.push(line.slice(2));
    else if (!line.startsWith(" ")) {
      const entry = parseSnsHeader(line);
      if (entry) eventsView.sns.push(entry);
    }
  }
  if (eventsView.sns.length > MAX_SNS * 1.2) eventsView.sns = eventsView.sns.slice(-MAX_SNS);
  paintSns();
}

function snsShown(entry, topic, text) {
  if (topic && entry.topic !== topic) return false;
  if (!text) return true;
  const attrs = Object.entries(entry.attributes).map(([key, value]) => `${key}=${typeof value === "object" ? JSON.stringify(value) : value}`).join(" ");
  return `${entry.service} ${entry.topic} ${entry.subject} ${entry.group} ${attrs} ${entry.body.join("\n")}`.toLowerCase().includes(text);
}

function snsEntry(entry) {
  const attrs = Object.entries(entry.attributes).map(([key, value]) =>
    el("span", { class: "attr" }, `${key}=${typeof value === "object" ? JSON.stringify(value) : value}`));
  const text = entry.body.join("\n");
  const kind = messageKind(text);
  return messageDetails(
    el("summary", {},
      el("span", { class: "mono muted" }, new Date(entry.time).toLocaleTimeString()),
      el("span", { class: "topic" }, entry.topic),
      kind ? el("span", { class: "kind" }, kind) : "",
      el("span", { class: "muted" }, t("from {service}", { service: entry.service })),
      entry.subject ? el("span", {}, entry.subject) : "",
      entry.group ? el("span", { class: "muted", title: "MessageGroupId" }, t("group {group}", { group: entry.group })) : "",
      ...attrs,
    ),
    text,
  );
}

function paintTopics() {
  const counts = {};
  for (const entry of eventsView.sns) counts[entry.topic] = (counts[entry.topic] || 0) + 1;
  const topics = Object.keys(counts).sort();
  const signature = topics.map((topic) => `${topic}:${counts[topic]}`).join("|");
  if (signature === eventsView.topics) return;
  eventsView.topics = signature;
  const current = $("sns-topic").value;
  $("sns-topic").replaceChildren(
    el("option", { value: "" }, t("All topics ({n})", { n: eventsView.sns.length })),
    ...topics.map((topic) => el("option", topic === current ? { value: topic, selected: "" } : { value: topic }, `${topic} (${counts[topic]})`)),
  );
}

function paintSns() {
  paintTopics();
  const topic = $("sns-topic").value;
  const text = $("sns-filter").value.trim().toLowerCase();
  const open = new Set([...$("sns-entries").querySelectorAll("details[open]")].map((node) => node.dataset.at));
  const shown = [];
  for (let i = eventsView.sns.length - 1; i >= 0 && shown.length < 300; i--) {
    if (snsShown(eventsView.sns[i], topic, text)) shown.push([i, eventsView.sns[i]]);
  }
  $("sns-entries").replaceChildren(...shown.map(([i, entry]) => {
    const node = snsEntry(entry);
    node.dataset.at = `${entry.time}|${i}`;
    if (open.has(node.dataset.at)) {
      node.fill();
      node.open = true;
    }
    return node;
  }));
  const total = eventsView.sns.length;
  $("sns-count").textContent = !total ? ""
    : shown.length < 300 ? t("{shown} of {total} publishes", { shown: shown.length, total }) : t("latest 300 of {total} publishes", { total });
  $("sns-empty").textContent = !total
    ? state && state.events.up ? t("Nothing was published to the local SNS yet. Services started with local events publish here.") : t("Nothing published locally yet. Start the local events, then the services that publish.")
    : t("No publish matches the filter.");
  $("sns-empty").hidden = shown.length > 0;
}

// ---- start, stop and send

function openEventsUp() {
  const broker = !eventsView.queues || eventsView.queues.broker_service;
  openLaunch({
    title: t("Start"), key: t("local events"),
    hint: t("A local ElasticMQ (Docker) with every queue of the repo, like pdms events up. The broker runs as this user and database."),
    user: state.user, db: state.db, go: "start", path: eventsPath("up"), broker,
    after: () => showEventsTab(eventsView.tab),
  });
}

async function stopEvents() {
  const consumers = state.instances.filter((i) => i.queue && i.status !== "stopped").map((i) => i.key);
  const text = consumers.length
    ? t("Its messages are lost, and the consumers stop too: {consumers}. Services keep running, but what they publish now fails until it starts again.", { consumers: consumers.join(", ") })
    : t("Its messages are lost. Services keep running, but what they publish now fails until it starts again.");
  if (await confirmDialog(t("Stop the local events?"), text, t("Stop"))) act(eventsPath("down"));
}

const sending = { types: new Set(), queues: new Set(), broker: "" };

async function openSend(target = "") {
  const [map, queues] = await Promise.all([
    eventsView.map ? { data: eventsView.map } : getJson(eventsPath("map")),
    eventsView.queues ? { data: eventsView.queues } : getJson(eventsPath("queues")),
  ]);
  if (map.error || queues.error) { toast(map.error || queues.error); return; }
  eventsView.map = map.data;
  eventsView.queues = queues.data;
  sending.types = new Set(map.data.types.map((item) => item.type));
  sending.queues = new Set(queues.data.queues.filter((queue) => !queue.sns).map((queue) => queue.name));
  sending.broker = map.data.broker;
  $("send-targets").replaceChildren(...[...sending.types, ...sending.queues].map((name) => el("option", { value: name })));
  $("send-target").value = target;
  $("send-direct").checked = false;
  $("send-body").value = "{}";
  $("send-error").hidden = true;
  sendTargetChanged();
  $("send").showModal();
  if (sending.types.has(target)) fillTemplate();
  else (target ? $("send-body") : $("send-target")).focus();
}

function sendTargetChanged() {
  const target = $("send-target").value.trim();
  const isType = sending.types.has(target);
  $("send-direct-label").hidden = !isType || !sending.broker;
  $("send-template").hidden = !isType;
  $("send-body-label").textContent = isType ? t("Event fields (JSON; event_id and type are added)") : t("Message body (JSON)");
}

async function fillTemplate() {
  const target = $("send-target").value.trim();
  const { data, error } = await getJson(`${eventsPath("template")}?${new URLSearchParams({ type: target })}`);
  if (error) {
    $("send-error").textContent = error;
    $("send-error").hidden = false;
    return;
  }
  $("send-body").value = JSON.stringify(data, null, 2);
  $("send-body").focus();
}

async function submitSend(event) {
  event.preventDefault();
  const body = { target: $("send-target").value.trim(), body: $("send-body").value, direct: $("send-direct").checked };
  $("send-go").disabled = true;
  $("send-error").hidden = true;
  try {
    const { status, data } = await post(eventsPath("send"), body);
    if (status !== 200) {
      $("send-error").textContent = data.error || t("pdms ui answered {status}", { status });
      $("send-error").hidden = false;
      return;
    }
    $("send").close();
    const notes = [t("Sent {id} to {queue}.", { id: data.id.slice(0, 8), queue: data.queue })];
    if (data.routed_to) {
      notes.push(data.consumer ? t("The broker routes it to {queue} (consumer: {consumer}).", { queue: data.routed_to, consumer: data.consumer })
        : t("The broker routes it to {queue} (consumer: none).", { queue: data.routed_to }));
    }
    if (!data.consumed) notes.push(t("Nothing consumes {queue} right now: it waits there.", { queue: data.queue }));
    toast(notes.join(" "), "info");
    loadQueues();
  } catch {
    $("send-error").textContent = t("pdms ui is not reachable: is it still running?");
    $("send-error").hidden = false;
  } finally {
    $("send-go").disabled = false;
  }
}

// ---------------------------------------------------------------------------- settings

const REVEAL_FOR = 30000; // a shown password hides again by itself
const settingsView = {
  tab: "repos", data: null, seen: "", revealed: {}, timers: {}, tests: {}, db: null, user: null, passwordTouched: false,
  protectedTouched: false,
};
// [key, label, kind, help]: the rows of the Defaults tab, in the order of pdms config defaults (label and help
// translated when painted).
const DEFAULTS = [
  ["language", N_("Language"), "select", N_("Of the CLI, of this page and of the messages pdms ui gets from the CLI.")],
  ["host", N_("uvicorn host"), "text", N_("Where services listen (0.0.0.0: every interface).")],
  ["port", N_("Default port"), "number", N_("The first port tried for a service; the next free one when it is busy.")],
  ["logging_level", N_("LOGGING_LEVEL"), "select", N_("Passed to every service.")],
  ["reload", N_("Reload on code changes"), "check", N_("uvicorn --reload.")],
  ["install", N_("Install dependencies before starting"), "check", N_("poetry lock && poetry install.")],
  ["smart_install", N_("Smart install"), "check", N_("Skip the install when nothing that affects it changed since the last one.")],
  ["events", N_("Where services publish SQS events"), "select", N_("auto: the local broker while pdms events up runs; local: always; aws: as each service is configured.")],
  ["events_port", N_("Local ElasticMQ port"), "number", N_("Host port of the ElasticMQ that pdms events up starts.")],
  ["db_timeout", N_("Connection test timeout"), "number", N_("Seconds to wait when testing a database.")],
  ["proxy_port", N_("Proxy port"), "number", N_("Where pdms proxy listens (pdms proxy, Start everything); the next free one when it is busy.")],
  ["proxy_timeout", N_("Proxy timeout"), "number", N_("Seconds the proxy waits for a service or the remote API before answering 502. Slow databases need more; applies when the proxy starts.")],
  ["banner", N_("Show the PDMS banner"), "check", N_("When the interactive menu opens.")],
  ["update_check", N_("Tell me about new pdms versions"), "check", N_("Checked at most once a day.")],
  ["ui_at_login", N_("Open pdms ui when I log in"), "check", N_("In the tray, without its window. To have it in the app menu too: pdms ui --install.")],
  ["env", N_("Extra environment variables"), "env", N_("Injected on every run, after the profile's own.")],
];

function settingsPath(kind, name, verb) {
  return `/api/${kind}/${encodeURIComponent(name)}/${verb}`;
}

function matches(text, values) {
  return !text || values.join(" ").toLowerCase().includes(text);
}

async function loadSettings() {
  const { data, error } = await getJson("/api/config");
  if (error) { toast(error); return; }
  settingsView.data = data;
  paintSettings();
}

// The CLI may change the databases or users while the tab is open: the state's names tell when to load them again.
function syncSettings() {
  const seen = JSON.stringify([state.users, state.dbs, state.repos, state.repo]);
  if (seen === settingsView.seen) return;
  settingsView.seen = seen;
  if (currentView() === "settings") loadSettings();
}

function paintSettings() {
  for (const tab of $("settings-tabs").children) tab.setAttribute("aria-selected", String(tab.dataset.tab === settingsView.tab));
  for (const name of ["repos", "dbs", "users", "defaults"]) $(`settings-${name}`).hidden = name !== settingsView.tab;
  const data = settingsView.data;
  if (!data) return;
  const dbs = data.dbs.length;
  const users = data.users.length;
  $("settings-summary").textContent = [
    dbs === 1 ? t("{n} database", { n: dbs }) : t("{n} databases", { n: dbs }),
    users === 1 ? t("{n} user", { n: users }) : t("{n} users", { n: users }),
  ].join(" · ");
  $("settings-path").textContent = data.path;
  $("settings-path").title = data.path;
  paintRepos();
  paintDbs();
  paintUsers();
  if (!defaultsChanged()) paintDefaults();
}

function showSettingsTab(tab) {
  settingsView.tab = tab;
  paintSettings();
}

function eyeButton(shown, onclick) {
  const label = shown ? t("Hide the password") : t("Show the password");
  return button("", onclick, { class: "btn small ghost eye", "aria-label": label, title: label, "aria-pressed": String(shown) });
}

// kind: "dbs" or "users".
function usedBy(stacks, kind) {
  if (!stacks.length) return t("Only its entry in the configuration goes.");
  if (stacks.length === 1) {
    return kind === "dbs"
      ? t("The stack {stack} uses it: it will ask for a database when it starts.", { stack: stacks[0] })
      : t("The stack {stack} uses it: it will ask for a user when it starts.", { stack: stacks[0] });
  }
  return kind === "dbs"
    ? t("The stacks {stacks} use it: they will ask for a database when they start.", { stacks: stacks.join(", ") })
    : t("The stacks {stacks} use it: they will ask for a user when they start.", { stacks: stacks.join(", ") });
}

// ---- databases

function dbRow(db) {
  const revealed = settingsView.revealed[db.name];
  const password = el("td");
  if (!db.has_password) {
    password.append(el("span", { class: "not-set" }, t("not set")));
  } else {
    password.append(el("span", { class: "secret-text" }, revealed === undefined ? "••••••••" : revealed));
    password.append(eyeButton(revealed !== undefined, () => toggleReveal(db.name)));
  }
  const test = settingsView.tests[db.name];
  const connection = el("td", { class: "wrap-detail" });
  if (test && test.busy) connection.append(el("span", { class: "st starting" }, t("testing…")));
  else if (test && test.ok) connection.append(el("span", { class: "st ok" }, t("ok")), " ", el("span", { class: "muted" }, test.text));
  else if (test) connection.append(el("span", { class: "st stopped" }, t("failed")), el("span", { class: "detail" }, test.text));
  const name = el("td", { class: "mono" }, db.name);
  if (db.protected) name.append(el("span", { class: "tag protected" }, t("protected")));
  const actionsCell = el("td", { class: "row-actions" },
    button(t("Test"), () => testDb(db.name), test && test.busy ? { disabled: "" } : {}),
    button(t("Migrations"), () => openFlyway(db.name)),
    button(t("Edit"), () => openDb(db)),
    button(t("Delete"), () => removeSetting("dbs", db.name, db.stacks), { class: "btn small bad" }),
  );
  return el("tr", {}, name, el("td", { class: "mono" }, db.host), el("td", { class: "num" }, String(db.port)),
    el("td", { class: "mono" }, db.database), el("td", { class: "mono" }, db.user), password, connection, actionsCell);
}

function paintDbs() {
  const dbs = settingsView.data.dbs;
  const text = $("db-filter").value.trim().toLowerCase();
  const shown = dbs.filter((db) => (!$("db-protected").checked || db.protected)
    && matches(text, [db.name, db.host, db.port, db.database, db.user]));
  $("db-rows").replaceChildren(...shown.map(dbRow));
  $("db-count").textContent = dbs.length ? t("{shown} of {total}", { shown: shown.length, total: dbs.length }) : "";
  $("db-empty").hidden = shown.length > 0;
  $("db-empty").textContent = dbs.length ? t("No database matches the filter.") : t("No databases yet. Services need at least one to run.");
}

async function toggleReveal(name) {
  clearTimeout(settingsView.timers[name]);
  if (settingsView.revealed[name] !== undefined) {
    delete settingsView.revealed[name];
    paintDbs();
    return;
  }
  await act(settingsPath("dbs", name, "password"), {}, (data) => {
    settingsView.revealed[name] = data.password;
    settingsView.timers[name] = setTimeout(() => { delete settingsView.revealed[name]; paintDbs(); }, REVEAL_FOR);
    paintDbs();
  });
}

async function testDb(name) {
  settingsView.tests[name] = { busy: true };
  paintDbs();
  const result = await testConnection({ name });
  settingsView.tests[name] = result;
  paintDbs();
}

async function testConnection(body) {
  try {
    const { status, data } = await post("/api/dbs/test", body);
    return status === 200 ? { ok: true, text: data.version } : { ok: false, text: data.error || t("pdms ui answered {status}", { status }), field: data.field };
  } catch {
    return { ok: false, text: t("pdms ui is not reachable: is it still running?") };
  }
}

async function removeSetting(kind, name, stacks) {
  const title = kind === "dbs" ? t("Delete database {name}?", { name }) : t("Delete user {name}?", { name });
  if (!await confirmDialog(title, usedBy(stacks, kind), t("Delete"))) return;
  await act(settingsPath(kind, name, "remove"), {}, () => {
    delete settingsView.revealed[name];
    delete settingsView.tests[name];
    toast(t("'{name}' deleted.", { name }), "info");
    loadSettings();
  });
}

// ---- forms of a database and a user

function formError(prefix, data, fallback) {
  const node = $(`${prefix}-error`);
  node.textContent = data.error || fallback;
  node.hidden = false;
  const input = data.field && $(`${prefix}-${data.field}`);
  if (input) {
    input.setAttribute("aria-invalid", "true");
    input.focus();
  }
}

function resetForm(prefix, fields) {
  $(`${prefix}-error`).hidden = true;
  for (const field of fields) $(`${prefix}-${field}`).removeAttribute("aria-invalid");
}

const DB_FIELDS = ["name", "host", "port", "database", "user", "password"];
const USER_FIELDS = ["name", "username", "first_name", "last_name", "roles", "user_id"];
const USER_TEXTS = USER_FIELDS.filter((field) => field !== "roles");

function showPassword(shown) {
  $("db-password").type = shown ? "text" : "password";
  $("db-eye").setAttribute("aria-pressed", String(shown));
  $("db-eye").setAttribute("aria-label", shown ? t("Hide the password") : t("Show the password"));
}

function openDb(db = null) {
  Object.assign(settingsView, { db, passwordTouched: false, protectedTouched: Boolean(db) });
  $("db-title").textContent = db ? t("Edit {name}", { name: db.name }) : t("New database");
  $("db-name-label").hidden = Boolean(db);
  $("db-name").required = !db;
  $("db-name").value = "";
  $("db-host").value = db ? db.host : "localhost";
  $("db-port").value = db ? db.port : 5432;
  $("db-database").value = db ? db.database : "pdm";
  $("db-user").value = db ? db.user : "";
  $("db-password").value = "";
  $("db-password").placeholder = db && db.has_password ? t("unchanged") : "";
  $("db-protected-box").checked = Boolean(db && db.protected);
  showPassword(false);
  $("db-tested").hidden = true;
  resetForm("db", DB_FIELDS);
  $("db-dialog").showModal();
  (db ? $("db-host") : $("db-name")).focus();
}

// The password of a database being edited is left out (kept) unless it was typed or shown.
function dbBody() {
  const db = settingsView.db;
  return {
    host: $("db-host").value, port: $("db-port").value === "" ? "" : Number($("db-port").value),
    database: $("db-database").value, user: $("db-user").value, protected: $("db-protected-box").checked,
    password: db && !settingsView.passwordTouched ? null : $("db-password").value,
  };
}

async function toggleDbPassword() {
  const shown = $("db-eye").getAttribute("aria-pressed") === "true";
  const db = settingsView.db;
  if (!shown && db && db.has_password && !settingsView.passwordTouched) {
    const { status, data } = await post(settingsPath("dbs", db.name, "password")).catch(() => ({ status: 0, data: {} }));
    if (status !== 200) { formError("db", data, t("Could not read the password.")); return; }
    $("db-password").value = data.password;
    settingsView.passwordTouched = true;
  }
  showPassword(!shown);
}

async function testDbForm() {
  resetForm("db", DB_FIELDS);
  $("db-test").disabled = true;
  $("db-tested").className = "muted";
  $("db-tested").textContent = t("Connecting…");
  $("db-tested").hidden = false;
  const result = await testConnection({ ...dbBody(), name: settingsView.db ? settingsView.db.name : "" });
  $("db-test").disabled = false;
  $("db-tested").className = result.ok ? "muted" : "error";
  $("db-tested").textContent = result.ok ? t("✓ Connected: {version}", { version: result.text }) : result.text;
  if (result.field && $(`db-${result.field}`)) $(`db-${result.field}`).setAttribute("aria-invalid", "true");
}

async function saveForm(prefix, kind, current, body, fields) {
  resetForm(prefix, fields);
  const name = current ? current.name : $(`${prefix}-name`).value.trim();
  $(`${prefix}-save`).disabled = true;
  try {
    const { status, data } = await post(settingsPath(kind, name, "save"), { ...body, new: !current });
    if (status === 200) {
      $(`${prefix}-dialog`).close();
      toast(t("'{name}' saved.", { name: data.name }), "info");
      delete settingsView.tests[data.name];
      loadSettings();
      return;
    }
    formError(prefix, data, t("pdms ui answered {status}", { status }));
  } catch {
    formError(prefix, {}, t("pdms ui is not reachable: is it still running?"));
  } finally {
    $(`${prefix}-save`).disabled = false;
  }
}

function saveDb(event) {
  event.preventDefault();
  saveForm("db", "dbs", settingsView.db, dbBody(), DB_FIELDS);
}

// ---- users

function userRow(user) {
  return el("tr", {},
    el("td", { class: "mono" }, user.name), el("td", {}, user.username),
    el("td", {}, `${user.first_name} ${user.last_name}`.trim() || "-"), el("td", { class: "mono" }, user.roles || "-"),
    el("td", { class: "mono muted" }, user.user_id),
    el("td", { class: "row-actions" },
      button(t("Edit"), () => openUser(user)),
      button(t("Delete"), () => removeSetting("users", user.name, user.stacks), { class: "btn small bad" })),
  );
}

function paintUsers() {
  const users = settingsView.data.users;
  const text = $("user-filter").value.trim().toLowerCase();
  const shown = users.filter((u) => matches(text, [u.name, u.username, u.first_name, u.last_name, u.roles, u.user_id]));
  $("user-rows").replaceChildren(...shown.map(userRow));
  $("user-count").textContent = users.length ? t("{shown} of {total}", { shown: shown.length, total: users.length }) : "";
  $("user-empty").hidden = shown.length > 0;
  $("user-empty").textContent = users.length ? t("No user matches the filter.") : t("No users yet. Services run as one of them.");
}

function openUser(user = null) {
  settingsView.user = user;
  $("user-title").textContent = user ? t("Edit {name}", { name: user.name }) : t("New user");
  $("user-name").required = true;
  $("user-rename-note").hidden = !user || !user.stacks.length;
  for (const field of USER_TEXTS) $(`user-${field}`).value = user ? user[field === "name" ? "name" : field] : "";
  paintRoles(user ? user.roles : "");
  resetForm("user", USER_FIELDS);
  $("user-dialog").showModal();
  (user ? $("user-username") : $("user-name")).focus();
}

function splitRoles(roles) {
  return [...new Set(roles.split(",").map((role) => role.trim()).filter(Boolean))];
}

// The repo's roles to tick; a role of the user the repo does not know shows too (ticked), so editing keeps it.
function paintRoles(current) {
  const mine = splitRoles(current);
  const known = settingsView.data.roles;
  const box = (role, extra) => {
    const input = el("input", { type: "checkbox", value: role });
    input.checked = mine.includes(role);
    return el("label", { class: "inline" }, input, role, ...extra);
  };
  $("user-roles").replaceChildren(
    ...known.map((role) => box(role, [])),
    ...mine.filter((role) => !known.includes(role)).map((role) => box(role, [el("span", { class: "tag" }, t("not a role of this repo"))])),
  );
}

// A user being edited may get another name first (pdms user rename), then the rest is saved under it.
async function saveUser(event) {
  event.preventDefault();
  const body = Object.fromEntries(USER_TEXTS.filter((f) => f !== "name").map((f) => [f, $(`user-${f}`).value]));
  body.roles = [...$("user-roles").querySelectorAll("input:checked")].map((input) => input.value).join(",");
  const current = settingsView.user;
  const name = $("user-name").value.trim();
  if (current && name !== current.name) {
    resetForm("user", USER_FIELDS);
    const { status, data } = await post(settingsPath("users", current.name, "rename"), { new_name: name })
      .catch(() => ({ status: 0, data: { error: t("pdms ui is not reachable: is it still running?") } }));
    if (status !== 200) {
      formError("user", data, t("pdms ui answered {status}", { status }));
      return;
    }
    settingsView.user = { ...current, name: data.name };
  }
  saveForm("user", "users", settingsView.user, body, USER_FIELDS);
}

// ---- defaults

function settingInput(key, kind, value) {
  const id = `default-${key}`;
  if (kind === "check") {
    const box = el("input", { type: "checkbox", id });
    box.checked = Boolean(value);
    return box;
  }
  if (kind === "select") {
    const select = el("select", { id });
    const choices = settingsView.data.choices[key];
    const names = Array.isArray(choices) ? Object.fromEntries(choices.map((c) => [c, c])) : choices;
    options(select, Object.keys(names), value, (code) => names[code]);
    return select;
  }
  if (kind === "env") {
    const rows = el("div", { class: "env-rows", id });
    for (const [name, text] of Object.entries(value)) rows.append(envRow(name, text));
    rows.append(button(t("Add variable"), () => { rows.lastChild.before(envRow("", "")); defaultsChanged(); rows.lastChild.previousSibling.firstChild.focus(); }));
    return rows;
  }
  return el("input", kind === "number" ? { type: "number", id, min: "1", value: String(value) } : { id, value });
}

function envRow(name, value) {
  const row = el("div", { class: "env-row" },
    el("input", { value: name, placeholder: t("NAME"), "aria-label": t("Variable name"), spellcheck: "false" }),
    el("input", { value, placeholder: t("value"), "aria-label": t("Value"), spellcheck: "false" }));
  row.append(button(t("Remove"), () => { row.remove(); defaultsChanged(); }, { class: "btn small ghost" }));
  return row;
}

function paintDefaults() {
  const values = settingsView.data.defaults;
  $("defaults-form").replaceChildren(...DEFAULTS.map(([key, label, kind, help]) => {
    const what = el("div", { class: "what" }, el("span", {}, el("b", {}, t(label)), el("code", {}, key)), el("small", {}, t(help)));
    const row = el(kind === "env" ? "div" : "label", { class: "setting", "data-key": key, "data-search": `${key} ${t(label)} ${t(help)}`.toLowerCase() },
      what, settingInput(key, kind, values[key]));
    if (kind !== "env") row.setAttribute("for", `default-${key}`);
    return row;
  }));
  $("defaults-error").hidden = true;
  filterDefaults();
  defaultsChanged();
}

function envValues() {
  const env = {};
  for (const row of $("default-env").querySelectorAll(".env-row")) {
    const [name, value] = row.querySelectorAll("input");
    if (name.value.trim() || value.value) env[name.value.trim()] = value.value;
  }
  return env;
}

function defaultsValues() {
  const values = {};
  for (const [key, , kind] of DEFAULTS) {
    const input = $(`default-${key}`);
    if (!input) return null;
    values[key] = kind === "check" ? input.checked : kind === "env" ? envValues() : input.value;
  }
  return values;
}

// Marks the changed rows; true when something differs from what is saved.
function defaultsChanged() {
  const values = settingsView.data && defaultsValues();
  if (!values) return false;
  const saved = settingsView.data.defaults;
  let changed = false;
  for (const [key] of DEFAULTS) {
    const differs = JSON.stringify(key === "env" ? values[key] : String(values[key])) !== JSON.stringify(key === "env" ? saved[key] : String(saved[key]));
    document.querySelector(`.setting[data-key="${key}"]`).classList.toggle("changed", differs);
    changed ||= differs;
  }
  $("default-smart_install").disabled = !$("default-install").checked;
  $("defaults-save").disabled = $("defaults-discard").disabled = !changed;
  return changed;
}

function filterDefaults() {
  const text = $("defaults-filter").value.trim().toLowerCase();
  let shown = 0;
  for (const row of $("defaults-form").children) {
    row.hidden = Boolean(text) && !row.dataset.search.includes(text);
    shown += row.hidden ? 0 : 1;
  }
  $("defaults-count").textContent = text ? t("{shown} of {total}", { shown, total: DEFAULTS.length }) : "";
  $("defaults-none").hidden = shown > 0;
  $("defaults-form").hidden = shown === 0;
}

async function saveDefaults() {
  $("defaults-error").hidden = true;
  for (const input of $("defaults-form").querySelectorAll("[aria-invalid]")) input.removeAttribute("aria-invalid");
  $("defaults-save").disabled = true;
  try {
    const { status, data } = await post("/api/defaults/save", defaultsValues());
    if (status === 200) {
      toast(t("Defaults saved."), "info");
      await loadSettings();
      paintDefaults();
      return;
    }
    $("defaults-error").textContent = data.field ? `${data.field}: ${data.error}` : data.error || t("pdms ui answered {status}", { status });
    $("defaults-error").hidden = false;
    const input = data.field && $(`default-${data.field}`);
    if (input) {
      input.setAttribute("aria-invalid", "true");
      input.closest(".setting").hidden = false;
      (input.querySelector("input") || input).focus();
    }
  } catch {
    toast(t("pdms ui is not reachable: is it still running?"));
  } finally {
    defaultsChanged();
  }
}


// ---- export and import of the settings (pdms config export / import)

const SECTION_LABELS = { defaults: N_("Defaults"), users: N_("Users"), dbs: N_("Databases"), stacks: N_("Stacks") };
const importing = { name: "", text: "", plan: null };

function sectionLabel(section) {
  return SECTION_LABELS[section] ? t(SECTION_LABELS[section]) : section;
}

function sectionBoxes(container, sections, onchange = null) {
  container.replaceChildren(...sections.map((section) => {
    const input = el("input", { type: "checkbox", value: section });
    input.checked = true;
    if (onchange) input.addEventListener("change", onchange);
    return el("label", { class: "inline" }, input, sectionLabel(section));
  }));
}

function checkedValues(container) {
  return [...container.querySelectorAll("input:checked")].map((input) => input.value);
}

function download(name, text) {
  const url = URL.createObjectURL(new Blob([text], { type: "application/toml" }));
  const link = el("a", { href: url, download: name });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function openExport() {
  sectionBoxes($("export-sections"), settingsView.data.sections);
  $("export-secrets").checked = false;
  $("export-warn").hidden = $("export-error").hidden = true;
  $("export-dialog").showModal();
}

async function submitExport(event) {
  event.preventDefault();
  const body = { sections: checkedValues($("export-sections")), secrets: $("export-secrets").checked };
  const { status, data } = await post("/api/config/export", body).catch(() => ({ status: 0, data: {} }));
  if (status !== 200) {
    $("export-error").textContent = data.error || t("pdms ui is not reachable: is it still running?");
    $("export-error").hidden = false;
    return;
  }
  download(data.filename, data.text);
  $("export-dialog").close();
  toast(body.secrets ? t("Exported to {file}, with the passwords: keep it private.", { file: data.filename })
    : t("Exported to {file}.", { file: data.filename }), "info");
}

async function readImport() {
  const file = $("import-file").files[0];
  $("import-file").value = ""; // the same file can be picked again
  if (!file) return;
  const text = await file.text();
  const { status, data } = await post("/api/config/import/plan", { text }).catch(() => ({ status: 0, data: {} }));
  if (status !== 200) { toast(data.error || t("pdms ui is not reachable: is it still running?")); return; }
  Object.assign(importing, { name: file.name, text, plan: data });
  $("import-name").textContent = file.name;
  const meta = data.meta;
  $("import-meta").textContent = t("Exported on {date} by pdms {version}.", { date: meta.exported_at || "?", version: meta.cli_version || "?" });
  sectionBoxes($("import-sections"), data.sections, paintImport);
  $("import-form").mode.value = "merge";
  $("import-conflicts").replaceChildren();
  $("import-error").hidden = true;
  paintImport();
  $("import-dialog").showModal();
}

function paintImport() {
  const plan = importing.plan;
  const sections = checkedValues($("import-sections"));
  const replace = $("import-form").mode.value === "replace";
  const plans = plan.plans.filter((item) => sections.includes(item.section));
  const list = (names) => names.join(", ") || "-";
  $("import-missing-head").textContent = replace ? t("Removed") : t("Only mine");
  $("import-rows").replaceChildren(...plans.map((item) => el("tr", {},
    el("td", {}, sectionLabel(item.section)), el("td", {}, list(item.added)), el("td", {}, list(item.changed)),
    el("td", { class: "muted" }, list(item.same)), el("td", replace && item.missing.length ? { class: "code-bad" } : { class: "muted" }, list(item.missing)))));
  const conflicts = plans.flatMap((item) => item.changed.map((name) => [item.section, name]));
  const before = new Set(checkedValues($("import-conflicts")));
  $("import-conflicts").replaceChildren(...conflicts.map(([section, name]) => {
    const value = `${section}\n${name}`;
    const input = el("input", { type: "checkbox", value });
    input.checked = plan.first_setup || before.has(value);
    if (plan.first_setup) input.disabled = true;
    return el("label", { class: "pick" }, input, section === "defaults" ? sectionLabel(section) : `${sectionLabel(section)}: ${name}`);
  }));
  $("import-conflicts-box").hidden = replace || !conflicts.length;
  const notes = [];
  if (plan.first_setup) notes.push(t("There is no configuration of yours yet: everything in the file is taken."));
  if (!plan.meta.secrets && sections.includes("dbs")) notes.push(t("The file has no passwords: the databases you already have keep theirs."));
  $("import-note").textContent = notes.join(" ");
  $("import-note").hidden = !notes.length;
  const removed = plans.reduce((total, item) => total + item.missing.length, 0);
  $("import-warn").textContent = removed === 1 ? t("Replacing deletes one of your entries that is not in the file.")
    : t("Replacing deletes {n} of your entries that are not in the file.", { n: removed });
  $("import-warn").hidden = !replace || !removed;
  $("import-go").disabled = !sections.length;
}

async function submitImport(event) {
  event.preventDefault();
  const body = {
    text: importing.text, sections: checkedValues($("import-sections")), replace: $("import-form").mode.value === "replace",
    overwrite: checkedValues($("import-conflicts")).map((value) => value.split("\n")),
  };
  $("import-go").disabled = true;
  const { status, data } = await post("/api/config/import/apply", body).catch(() => ({ status: 0, data: {} }));
  $("import-go").disabled = false;
  if (status !== 200) {
    $("import-error").textContent = data.error || t("pdms ui is not reachable: is it still running?");
    $("import-error").hidden = false;
    return;
  }
  $("import-dialog").close();
  if (!data.changed) { toast(t("Nothing changes."), "info"); return; }
  toast(data.backup ? t("Settings imported. The previous ones were saved to {file}.", { file: data.backup }) : t("Settings imported."), "info");
  if (data.no_password.length) toast(t("Databases without a password: {dbs}. Edit them to set it.", { dbs: data.no_password.join(", ") }));
  await loadSettings();
  paintDefaults();
}

// ---- users from a database (pdms user import)

const userImport = { users: [] };

function openUserImport() {
  const dbs = settingsView.data.dbs.map((db) => db.name);
  if (!dbs.length) { toast(t("Add a database first: the users come from its pdms_user table.")); return; }
  options($("users-db"), dbs, dbs.includes(state.db) ? state.db : dbs[0], dbLabel);
  options($("users-role"), ["", ...settingsView.data.roles], "", (role) => role || t("any role"));
  $("users-search").value = "";
  $("users-inactive").checked = false;
  userImport.users = [];
  $("users-picker").hidden = $("users-note").hidden = $("users-error").hidden = true;
  usersCount();
  $("users-dialog").showModal();
  $("users-search").focus();
}

async function findDbUsers() {
  const body = { db: $("users-db").value, search: $("users-search").value, role: $("users-role").value, inactive: $("users-inactive").checked };
  $("users-find").disabled = true;
  $("users-error").hidden = true;
  $("users-note").textContent = t("Reading the users of {db}…", { db: body.db });
  $("users-note").hidden = false;
  const { status, data } = await post("/api/import-users/search", body).catch(() => ({ status: 0, data: {} }));
  $("users-find").disabled = false;
  if (status !== 200) {
    $("users-note").hidden = true;
    $("users-error").textContent = data.error || t("pdms ui is not reachable: is it still running?");
    $("users-error").hidden = false;
    return;
  }
  userImport.users = data.users;
  const found = data.users.length;
  const notes = [!found ? t("No user matches.") : found === 1 ? t("{n} user found.", { n: found }) : t("{n} users found.", { n: found })];
  if (data.limited) notes.push(t("Only the first ones: narrow it down with the search or the role."));
  if (data.source === "built-in") notes.push(t("The roles of the current repo could not be read: pdms's own copy maps them."));
  $("users-note").textContent = notes.join(" ");
  $("users-found").replaceChildren(...data.users.map((user, index) => {
    const input = el("input", { type: "checkbox", value: String(index) });
    input.checked = !user.imported_as;
    const tags = [];
    if (user.imported_as) tags.push(t("already imported as {name}", { name: user.imported_as }));
    if (user.is_active === false) tags.push(t("inactive"));
    if (user.unknown_roles.length) tags.push(t("unknown roles kept: {roles}", { roles: user.unknown_roles.join(", ") }));
    return el("label", { class: "pick" }, input,
      el("span", { class: "who" }, `${user.first_name} ${user.last_name}`.trim() || user.username, " ",
        el("span", { class: "muted" }, `<${user.username}>`), el("br"), el("span", { class: "roles" }, user.dev_roles || t("no roles"))),
      el("span", { class: "muted" }, tags.join(" · ")));
  }));
  $("users-picker").hidden = !data.users.length;
  usersCount();
}

function usersCount() {
  const boxes = [...$("users-found").querySelectorAll("input")];
  const picked = boxes.filter((box) => box.checked).length;
  $("users-count").textContent = t("{picked} of {total} selected", { picked, total: boxes.length });
  $("users-all").checked = boxes.length > 0 && picked === boxes.length;
  $("users-go").disabled = picked === 0;
  $("users-go").textContent = picked ? t("Import {n}", { n: picked }) : t("Import");
}

async function submitUserImport(event) {
  event.preventDefault();
  const picked = [...$("users-found").querySelectorAll("input:checked")].map((box) => userImport.users[Number(box.value)]);
  $("users-go").disabled = true;
  const { status, data } = await post("/api/import-users/apply", { users: picked }).catch(() => ({ status: 0, data: {} }));
  if (status !== 200) {
    usersCount();
    $("users-error").textContent = data.error || t("pdms ui is not reachable: is it still running?");
    $("users-error").hidden = false;
    return;
  }
  $("users-dialog").close();
  const unchanged = picked.length - data.added.length - data.updated.length;
  toast(t("{added} added, {updated} updated, {unchanged} unchanged.", { added: data.added.length, updated: data.updated.length, unchanged }), "info");
  loadSettings();
}

// ---------------------------------------------------------------------------- frontend

const frontStart = { port: null, options: null };
// Why a build is made again (frontend.build_needed), as the server says it.
const BUILD_REASONS = {
  "no build yet": N_("no build yet"), "the API URL changed": N_("the API URL changed"),
  "the dependencies changed": N_("the dependencies changed"), "the code changed": N_("the code changed"),
};

// The log a job is writing now: the install, the build (the frontend's) or the service's own.
function jobLog(job) {
  if (job.phase.startsWith("building")) return "build";
  return job.installed && job.phase.startsWith("installing") ? "install" : "current";
}

function staleText(front) {
  return t("Built for {built}; the API is now {now}. Rebuild to use it.", { built: front.api || "-", now: front.stale || "-" });
}

// The frontend calls a local API nobody answers on (state.frontend.api_problem).
function apiProblemTitle(problem) {
  if (problem.proxy_port) return t("The frontend calls :{port}, the proxy runs on :{proxy}", { port: problem.port, proxy: problem.proxy_port });
  if (problem.leftover) return t("The frontend calls a proxy that is not running (:{port})", { port: problem.port });
  return t("Nothing answers where the frontend calls (:{port})", { port: problem.port });
}

function apiProblemText(problem) {
  if (problem.proxy_port) return t("frontend/.env.local still has {url}. Pointing it to the proxy restarts yarn dev by itself.", { url: problem.url });
  if (problem.leftover) return t("A proxy that did not stop cleanly left {url} in frontend/.env.local.", { url: problem.url });
  return t("{url} comes from frontend/.env.local. Start the proxy, or change VITE_APP_API_URL there.", { url: problem.url });
}

function fixFrontendApi() {
  act("/api/frontend/fix-api", {}, (data) => {
    toast(data.done === "pointed" ? t("The frontend now calls the proxy.") : t("frontend/.env.local is back as it was."), "info");
    act("/api/doctor/run", { databases: false });
  });
}

function apiFixButtons(problem) {
  if (problem.proxy_port) return [button(t("Point it to :{port}", { port: problem.proxy_port }), fixFrontendApi, { class: "btn small primary" })];
  const start = button(t("Start proxy"), () => openProxyStart());
  return problem.leftover ? [button(t("Restore .env.local"), fixFrontendApi), start] : [start];
}

function apiLabel(url) {
  if (!url) return "-";
  return /:\/\/(localhost|127\.0\.0\.1)[:/]/.test(url) ? t("the proxy ({url})", { url }) : url;
}

function frontendActions(cell, item) {
  if (item.status !== "stopped") {
    cell.append(el("a", { class: "btn small", href: item.url, target: "_blank", rel: "noopener noreferrer" }, t("Open")));
  }
  if (item.mode === "build") cell.append(button(t("Rebuild"), () => rebuildFrontend()));
  cell.append(button(t("Stop"), () => act("/api/instances/frontend/stop"), { class: "btn small bad" }));
  return cell;
}

function rebuildFrontend() {
  act("/api/frontend/start", { mode: "build", rebuild: true, restart: true }, () => showLogs("frontend", "build"));
}

function fact(label, value, kind = "") {
  return [el("dt", {}, label), el("dd", kind ? { class: kind } : {}, value)];
}

function paintFrontendMode() {
  const options = frontStart.options;
  const mode = $("front-form").mode.value;
  $("front-rebuild-label").hidden = mode !== "build";
  if (!options) return;
  const facts = [
    ...fact(t("Node"), options.node || t("not found"), options.node ? "" : "bad"),
    ...fact("node_modules", options.dependencies_ok ? t("up to date") : t("out of date: yarn install runs first"),
      options.dependencies_ok ? "" : "warn-text"),
    ...fact(t("API"), apiLabel(options.api[mode])),
  ];
  if (mode === "build") {
    const last = options.last_build;
    facts.push(...fact(t("Last build"), last ? t("{when} · commit {commit}", {
      when: new Date(last.built_at).toLocaleString(), commit: last.commit || "-",
    }) : t("none yet")));
    facts.push(...fact(t("Build"), options.build_needed ? t("builds first: {reason}", {
      reason: BUILD_REASONS[options.build_needed] ? t(BUILD_REASONS[options.build_needed]) : options.build_needed,
    })
      : t("serves the last build")));
  }
  $("front-facts").replaceChildren(...facts);
}

async function openFrontendStart(mode = null) {
  const found = await getJson("/api/frontend/options");
  if (found.error) { toast(found.error); return; }
  const options = found.data;
  if (!options.available) { toast(t("The current repo has no frontend.")); return; }
  Object.assign(frontStart, { options, port: null });
  $("front-form").mode.value = mode || (state.setup ? state.setup.frontend_mode : "dev");
  $("front-form").install.value = "auto";
  $("front-rebuild").checked = false;
  $("front-warn").hidden = $("front-error").hidden = true;
  $("front-go").textContent = t("Start");
  paintFrontendMode();
  $("front-dialog").showModal();
}

function resetFrontendPort() {
  frontStart.port = null;
  $("front-warn").hidden = true;
  $("front-go").textContent = t("Start");
}

async function submitFrontendStart(event) {
  event.preventDefault();
  const form = $("front-form");
  const body = {
    mode: form.mode.value, install: { auto: null, force: true, skip: false }[form.install.value],
    rebuild: form.mode.value === "build" && $("front-rebuild").checked, port: frontStart.port,
  };
  $("front-error").hidden = true;
  $("front-go").disabled = true;
  try {
    const { status, data } = await post("/api/frontend/start", body);
    if (status === 202) {
      $("front-dialog").close();
      showLogs("frontend", body.install === true || !frontStart.options.dependencies_ok ? "install"
        : body.mode === "build" && (body.rebuild || frontStart.options.build_needed) ? "build" : "current");
      return;
    }
    if (status === 409 && data.decision === "port_busy") {
      frontStart.port = data.free;
      $("front-warn").textContent = t("Port {port} is in use. Start on {free} instead? Logging in to the app may only work on {port}.",
        { port: data.port, free: data.free });
      $("front-warn").hidden = false;
      $("front-go").textContent = t("Start on {port}", { port: data.free });
      return;
    }
    $("front-error").textContent = data.error || t("pdms ui answered {status}", { status });
    $("front-error").hidden = false;
  } catch {
    $("front-error").textContent = t("pdms ui is not reachable: is it still running?");
    $("front-error").hidden = false;
  } finally {
    $("front-go").disabled = false;
  }
}

// ---------------------------------------------------------------------------- home

const HOME_JOB = "home";
const home = { saving: 0 };

function homeJob() {
  return state.jobs[HOME_JOB] || null;
}

function stackUp(stack) {
  return stack.services.filter((svc) => svc.running.length).length;
}

function paintSetup() {
  const setup = state.setup || {};
  const names = state.stacks.map((stack) => stack.name);
  const current = names.includes(setup.stack) ? setup.stack : "";
  if (home.saving) return; // the answer of the save brings it back as the user left it
  $("setup-stack").replaceChildren(
    el("option", current ? { value: "" } : { value: "", selected: "" }, t("No stack")),
    ...names.map((name) => el("option", name === current ? { value: name, selected: "" } : { value: name }, name)),
  );
  $("setup-events").checked = Boolean(setup.events);
  $("setup-proxy").checked = Boolean(setup.proxy);
  $("setup-frontend").checked = Boolean(setup.frontend);
  $("setup-mode").value = setup.frontend_mode || "dev";
  $("setup-mode").disabled = !setup.frontend;
  $("setup-frontend-group").hidden = !state.frontend;
}

async function saveSetup() {
  const body = {
    stack: $("setup-stack").value, events: $("setup-events").checked, proxy: $("setup-proxy").checked,
    frontend: $("setup-frontend").checked, frontend_mode: $("setup-mode").value,
  };
  $("setup-mode").disabled = !body.frontend;
  home.saving += 1;
  try {
    const { status, data } = await post("/api/setup/save", body);
    if (status !== 200) toast(data.error || t("pdms ui answered {status}", { status }));
    else state.setup = data.setup;
  } catch {
    toast(t("pdms ui is not reachable: is it still running?"));
  } finally {
    home.saving -= 1;
    paintHome();
  }
}

// Which step of Start everything a phase of the Home job belongs to.
function phaseStep(phase) {
  if (/ElasticMQ|broker-sqs-event/.test(phase)) return "events";
  if (/proxy$/.test(phase)) return "proxy";
  if (/frontend$/.test(phase)) return "frontend";
  return "stack";
}

function paintSteps() {
  const setup = state.setup || {};
  const job = homeJob();
  const busy = job && !job.error && job.action === "up" ? phaseStep(job.phase) : null;
  const stack = state.stacks.find((item) => item.name === setup.stack);
  const steps = [
    ["events", setup.events, t("Local events"), t("ElasticMQ and the broker, so the services publish locally"), state.events.up],
    ["stack", Boolean(stack), stack ? t("Stack {name}", { name: stack.name }) : t("No stack"),
      stack ? servicesCount(stack.services.length) : t("only what is already running"), stack && stackUp(stack) === stack.services.length],
    ["proxy", setup.proxy, t("Proxy"), t("local services answer, the rest goes to the remote API"), Boolean(state.proxy)],
    ["frontend", setup.frontend && Boolean(state.frontend), t("Frontend"),
      setup.frontend_mode === "build" ? t("a production build, pointed to the proxy") : t("yarn dev, pointed to the proxy"),
      Boolean(state.frontend && state.frontend.running)],
  ];
  const order = steps.map(([key]) => key);
  $("home-steps").replaceChildren(...steps.map(([key, on, title, text, done]) => {
    let kind = on ? "" : "off";
    if (on && done) kind = "done";
    if (on && busy) kind = key === busy ? "busy" : order.indexOf(key) < order.indexOf(busy) ? "done" : kind;
    return el("li", kind ? { class: kind } : {}, el("b", {}, title), el("span", {}, on ? (key === busy ? phaseLabel(job.phase) : text) : t("off")));
  }));
}

function tile({ title, status, kind = "", main, rows = [], hint = "", actions = [] }) {
  const list = el("dl", {});
  for (const [label, value] of rows) list.append(el("dt", {}, label), el("dd", {}, value));
  return el("article", { class: `card tile ${kind}`.trim() },
    el("header", {}, el("h2", {}, title), status),
    el("div", { class: "tile-main" }, main),
    list,
    ...(hint ? [el("p", { class: "tile-hint" }, hint)] : []),
    el("footer", {}, ...actions),
  );
}

function pill(kind, text) {
  return el("span", { class: `st ${kind}` }, text);
}

function link(href, text) {
  return el("a", { href, target: "_blank", rel: "noopener noreferrer" }, text);
}

function servicesTile() {
  const alive = state.instances.filter((i) => i.status !== "stopped");
  const failing = state.instances.filter((i) => i.status === "error" || i.status === "stopped");
  const status = failing.length ? pill("stopped", t("{n} failing", { n: failing.length }))
    : alive.length ? pill("ok", t("{n} running", { n: alive.length })) : pill("off", t("none running"));
  const users = [...new Set(alive.map((i) => i.user))];
  const dbs = [...new Set(alive.map((i) => i.db))];
  return tile({
    title: t("Services"), status, kind: failing.length ? "bad" : "",
    main: alive.length ? t("{n} running", { n: alive.length }) : t("No background services"),
    rows: alive.length ? [[t("user"), users.join(", ")], [t("db"), dbs.join(", ")]] : [],
    hint: (state.strays || []).length ? t("{n} outside pdms", { n: state.strays.length }) : "",
    actions: [button(t("Start service"), () => openRun()), button(t("Open the list"), () => { location.hash = "#services"; })],
  });
}

function proxyTile() {
  const running = state.proxy;
  const job = proxyJob();
  const busy = job && !job.error;
  const status = busy ? pill("starting", phaseLabel(job.phase)) : running ? pill(running.status, statusLabel(running.status)) : pill("off", t("off"));
  const actions = running
    ? [el("a", { class: "btn small", href: `http://localhost:${running.port}/docs`, target: "_blank", rel: "noopener noreferrer" }, t("Open /docs")),
      button(t("Requests"), () => { location.hash = "#proxy"; }),
      button(t("Stop"), () => act(`/api/instances/${encodeURIComponent(running.key)}/stop`), { class: "btn small bad" })]
    : busy ? [] : [button(t("Start proxy"), openProxyStart, { class: "btn small primary" })];
  return tile({
    title: t("Proxy"), status, kind: job && job.error ? "bad" : "",
    main: running ? link(`http://localhost:${running.port}`, `http://localhost:${running.port}`) : t("Not running"),
    rows: running ? [[t("Remote API"), running.remote || t("none")], [t("Timeout"), t("{seconds} s", { seconds: running.timeout })]] : [],
    hint: job && job.error ? failedText(job) : "",
    actions,
  });
}

function frontendTile() {
  const front = state.frontend;
  const job = state.jobs.frontend;
  const busy = job && !job.error;
  const status = busy ? pill("starting", phaseLabel(job.phase)) : front.running ? pill(front.status, statusLabel(front.status)) : pill("off", t("off"));
  const problem = front.api_problem;
  const rows = [[t("Mode"), front.mode ? front.mode : (state.setup || {}).frontend_mode || "dev"], [t("API"), problem ? front.api : apiLabel(front.api)]];
  if (front.mode === "build" && front.build) {
    rows.push([t("Built"), t("{when} · commit {commit}", { when: new Date(front.build.built_at).toLocaleString(), commit: front.build.commit || "-" })]);
  }
  let actions;
  if (busy) actions = [button(t("Logs"), () => showLogs("frontend", jobLog(job)))];
  else if (front.running) {
    actions = [el("a", { class: "btn small primary", href: front.url, target: "_blank", rel: "noopener noreferrer" }, t("Open the app")),
      button(t("Logs"), () => showLogs("frontend"))];
    if (front.mode === "build") actions.push(button(t("Rebuild"), rebuildFrontend));
    actions.push(button(t("Stop"), () => act("/api/instances/frontend/stop"), { class: "btn small bad" }));
  } else {
    actions = [button(t("Start frontend"), () => openFrontendStart(), { class: "btn small primary" })];
    if (job && job.error) actions.push(button(t("Logs"), () => showLogs("frontend", jobLog(job))),
      button(t("Dismiss"), () => act("/api/instances/frontend/dismiss"), { class: "btn small ghost" }));
  }
  return tile({
    title: t("Frontend"), status, kind: (job && job.error) || front.status === "error" ? "bad" : front.stale || problem ? "warn" : "",
    main: front.running ? link(front.url, front.url) : t("PDMS web app (Vite, :{port})", { port: front.port }),
    rows, hint: job && job.error ? failedText(job) : front.stale ? staleText(front) : problem ? apiProblemTitle(problem) : front.detail,
    actions: problem && !busy ? [...apiFixButtons(problem), ...actions] : actions,
  });
}

function eventsTile() {
  const job = state.jobs[EVENTS_JOB];
  const busy = job && !job.error;
  const up = state.events.up;
  const consumers = state.instances.filter((i) => i.queue && i.status !== "stopped").length;
  const wanted = (state.setup || {}).events;
  const status = busy ? pill("starting", phaseLabel(job.phase)) : up ? pill("ok", t("on")) : pill(wanted ? "error" : "off", t("off"));
  return tile({
    title: t("Events"), status, kind: !up && wanted && !busy ? "warn" : "",
    main: up ? `ElasticMQ :${state.events.port}` : t("Local SQS and SNS"),
    rows: up ? [[t("Consumers"), t("{n} running", { n: consumers })], ["SNS", state.sns ? state.sns.queue : "-"]] : [[t("Port"), String(state.events.port)]],
    hint: job && job.error ? failedText(job) : "",
    actions: up
      ? [button(t("Send event…"), () => openSend()), button(t("Queues"), () => { location.hash = "#events"; })]
      : busy ? [] : [button(t("Start"), openEventsUp, { class: "btn small primary" })],
  });
}

function homeStackRow(stack) {
  const total = stack.services.length;
  const up = stackUp(stack);
  const job = state.jobs[`stack:${stack.name}`];
  const busy = job && !job.error;
  const meter = el("span", { class: "meter" }, el("i", { style: `width:${total ? Math.round((up / total) * 100) : 0}%` }));
  const actions = el("td", { class: "row-actions" });
  if (busy) actions.append(el("span", { class: "st starting" }, phaseLabel(job.phase)));
  else {
    if (up < total) actions.append(button(t("Start"), () => openUp(stack)));
    if (up) actions.append(button(t("Stop"), () => act(`${stackPath(stack.name)}/down`), { class: "btn small bad" }));
  }
  return el("tr", {},
    el("td", { class: "mono" }, el("b", {}, stack.name)),
    el("td", { class: "muted" }, `${stack.user || ask()} · ${stack.db || ask()}`),
    el("td", {}, el("span", { class: "mini" }, meter, `${up}/${total}`)),
    actions,
  );
}

// What is wrong now, most serious first: failed jobs and services, then what is off or out of date.
function attention() {
  const items = [];
  for (const [key, job] of Object.entries(state.jobs)) {
    if (!job.error) continue;
    const logKey = key === HOME_JOB ? null : key.startsWith("stack:") || key === EVENTS_JOB ? job.log_key : key;
    items.push(["bad", key === HOME_JOB ? t("Start everything") : key, failedText(job), [
      ...(logKey ? [button(t("Logs"), () => showLogs(logKey, jobLog(job)))] : []),
      button(t("Dismiss"), () => act(`/api/instances/${encodeURIComponent(key)}/dismiss`), { class: "btn small ghost" }),
    ]]);
  }
  for (const inst of state.instances) {
    if (state.jobs[inst.key]) continue;
    if (inst.status === "error") items.push(["bad", inst.key, inst.detail || t("error"), [button(t("Logs"), () => showLogs(inst.key))]]);
    else if (inst.status === "stopped") {
      items.push(["warn", inst.key, t("stopped"), [button(t("Logs"), () => showLogs(inst.key)),
        button(t("Forget"), () => act(`/api/instances/${encodeURIComponent(inst.key)}/forget`), { class: "btn small ghost" })]]);
    }
  }
  const strays = state.strays || [];
  if (strays.length) {
    const names = strays.slice(0, 4).map((s) => s.queue ? s.name : `${s.name} :${s.port}`).join(" · ");
    items.push(["warn", strays.length === 1 ? t("1 service runs outside pdms") : t("{n} services run outside pdms", { n: strays.length }),
      `${names}${strays.length > 4 ? " · " + t("+{n} more", { n: strays.length - 4 }) : ""}. ${t("pdms lost track of them: they keep their ports, but pdms ps, logs and Stop do not see them.")}`,
      [button(strays.length === 1 ? t("Adopt") : t("Adopt all"), () => adoptStrays(null)),
        button(t("Stop them"), () => stopStrays(null), { class: "btn small bad" })]]);
  }
  const front = state.frontend;
  if (front && front.running && front.status === "error" && !state.jobs.frontend) {
    items.push(["bad", "frontend", front.detail, [button(t("Logs"), () => showLogs("frontend"))]]);
  }
  if (front && front.api_problem) items.push(["warn", apiProblemTitle(front.api_problem), apiProblemText(front.api_problem), apiFixButtons(front.api_problem)]);
  if (front && front.stale) items.push(["warn", t("The frontend build is out of date"), staleText(front), [button(t("Rebuild"), rebuildFrontend)]]);
  if ((state.setup || {}).events && !state.events.up && !state.jobs[EVENTS_JOB]) {
    items.push(["warn", t("The local events are off"), t("The services publish to AWS until they are on."), [button(t("Start"), openEventsUp)]]);
  }
  const fromDoctor = (state.doctor.problems || []).filter((check) => !HOME_COVERS.has(check.fix));
  for (const check of fromDoctor.filter((c) => c.status === "fail")) {
    items.push(["bad", `${check.section} · ${check.name}`, check.detail, [check.fix ? fixButton(check) : button(t("Doctor"), () => { location.hash = "#doctor"; })]]);
  }
  const warnings = fromDoctor.filter((c) => c.status === "warn").length;
  if (warnings) {
    items.push(["warn", warnings === 1 ? t("Doctor found 1 warning") : t("Doctor found {n} warnings", { n: warnings }),
      fromDoctor.filter((c) => c.status === "warn").map((c) => `${c.section}: ${c.name}`).join(" · "), [button(t("Open Doctor"), () => { location.hash = "#doctor"; })]]);
  }
  const offer = updateOffer();
  if (offer && !state.jobs[UPDATE_JOB]) {
    items.push(["info", offerTitle(offer), offerText(offer), [button(offer.kind === "restart" ? t("Restart pdms ui") : t("See what is new"), openUpdate)]]);
  }
  const rank = { bad: 0, warn: 1, info: 2 };
  return items.sort((a, b) => rank[a[0]] - rank[b[0]]);
}

function paintHome() {
  paintSetup();
  paintSteps();
  const job = homeJob();
  const busy = job && !job.error;
  const alive = state.instances.filter((i) => i.status !== "stopped").length;
  const parts = [t("{n} running", { n: alive })];
  if (state.proxy) parts.push(t("proxy on"));
  if (state.frontend && state.frontend.running) parts.push(t("frontend on"));
  if (state.events.up) parts.push(t("events on"));
  $("home-summary").textContent = busy ? phaseLabel(job.phase) : parts.join(" · ");
  $("home-start").disabled = $("home-stop").disabled = Boolean(busy);
  $("home-stop").hidden = !(alive || state.proxy || (state.frontend && state.frontend.running) || state.events.up);
  $("home-error").hidden = !(job && job.error);
  $("home-error").textContent = job && job.error ? failedText(job) : "";

  const tiles = [servicesTile(), proxyTile()];
  if (state.frontend) tiles.push(frontendTile());
  tiles.push(eventsTile());
  $("home-tiles").replaceChildren(...tiles);

  $("home-stacks").replaceChildren(...state.stacks.map(homeStackRow));
  $("home-stacks-empty").hidden = state.stacks.length > 0;
  const items = attention();
  $("home-attn").replaceChildren(...items.map(([kind, title, detail, actions]) => el("li", {},
    el("span", { class: `sev ${kind}` }),
    el("div", {}, el("b", {}, title), el("small", {}, detail || "")),
    el("div", { class: "attn-actions" }, ...actions),
  )));
  $("home-ok").hidden = items.length > 0;
}

async function startAll(confirmed = false) {
  $("home-error").hidden = true;
  $("home-start").disabled = true;
  try {
    const { status, data } = await post("/api/home/start", { confirmed });
    if (status === 200 && !data.job) toast(t("Everything in your setup is already running."), "info");
    else if (status === 409 && data.decision === "protected_database") {
      if (await confirmDialog(t("'{name}' is a protected database. Use it anyway?", { name: data.name }),
        t("The stack's services will run against it."), t("Use it"))) startAll(true);
    } else if (status === 409 && data.decision === "port_busy") {
      toast(t("Port {port} is in use, so the frontend cannot start there. Free it, or start the frontend on another port from its tile.", { port: data.port }));
    } else if (status >= 400) {
      $("home-error").textContent = data.error || t("pdms ui answered {status}", { status });
      $("home-error").hidden = false;
    }
  } catch {
    toast(t("pdms ui is not reachable: is it still running?"));
  } finally {
    $("home-start").disabled = false;
  }
}

async function stopAll() {
  const what = [];
  if (state.frontend && state.frontend.running) what.push(t("the frontend"));
  if (state.proxy) what.push(t("the proxy"));
  const alive = state.instances.filter((i) => i.status !== "stopped").length;
  if (alive) what.push(servicesCount(alive));
  if (state.events.up) what.push(t("the local ElasticMQ (its messages are lost)"));
  if (await confirmDialog(t("Stop everything?"), t("It stops {what}.", { what: what.join(", ") }), t("Stop everything"))) {
    act("/api/home/stop", {});
  }
}

// ---------------------------------------------------------------------------- updates

const UPDATE_JOB = "update";
// loaded: the version this page came with; notes: the release notes fetched for notesFor.
const updateView = { loaded: null, notesFor: "", notes: null };

// What there is to offer: a newer release, or a version that pdms self-update installed and this pdms ui does not run.
function updateOffer() {
  const info = state.update;
  if (!info) return null;
  if (info.installed && info.installed !== info.current) return { kind: "restart", version: info.installed };
  if (info.latest) return { kind: "update", version: info.latest };
  return null;
}

function offerTitle(offer) {
  return offer.kind === "restart" ? t("pdms {version} is installed", { version: offer.version })
    : t("pdms {version} is available", { version: offer.version });
}

function offerText(offer) {
  return offer.kind === "restart" ? t("This pdms ui still runs {current}.", { current: state.update.current })
    : t("You have {current}.", { current: state.update.current });
}

function paintUpdate() {
  const offer = updateOffer();
  const job = state.jobs[UPDATE_JOB];
  const busy = job && !job.error;
  const chip = $("update-chip");
  chip.hidden = !offer && !busy;
  chip.textContent = busy ? `⬆ ${phaseLabel(job.phase)}` : offer ? `⬆ ${offer.version}` : "";
  chip.title = offer ? offerTitle(offer) : "";
  paintVersion();
  if ($("update-dialog").open) paintUpdateDialog();
}

function paintVersion() {
  const info = state.update;
  if (!info) return;
  const offer = updateOffer();
  $("version-current").textContent = info.current;
  let note;
  if (info.kind === "editable") note = t("runs from a local checkout: update it with git pull.");
  else if (offer) note = offerTitle(offer);
  else if (info.checked_at) note = t("the latest version (checked {when}).", { when: new Date(info.checked_at).toLocaleString() });
  else note = t("not checked yet.");
  if (info.kind !== "editable" && !info.checks) note += " " + t("Automatic checks are off.");
  $("version-note").textContent = note;
  $("version-check").disabled = info.kind === "editable";
  $("version-open").hidden = !offer;
}

async function checkNow() {
  $("version-check").disabled = true;
  try {
    const { status, data } = await post("/api/update/check");
    if (status >= 400) toast(data.error || t("pdms ui answered {status}", { status }));
    else if (data.latest) toast(t("pdms {version} is available", { version: data.latest }), "info");
    else toast(t("pdms {version} is the latest version.", { version: data.current }), "info");
  } catch {
    toast(t("pdms ui is not reachable: is it still running?"));
  } finally {
    $("version-check").disabled = false;
  }
}

// **bold** and `code` of a line of the release notes, as nodes (never as HTML).
function inlineNodes(text) {
  const parts = [];
  const pattern = /\*\*(.+?)\*\*|`([^`]+)`/g;
  let last = 0;
  let match;
  while ((match = pattern.exec(text))) {
    if (match.index > last) parts.push(text.slice(last, match.index));
    parts.push(match[1] !== undefined ? el("b", {}, match[1]) : el("code", {}, match[2]));
    last = pattern.lastIndex;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

// The Markdown of a release (headings, lists, paragraphs), as nodes.
function notesNodes(text) {
  const nodes = [];
  let list = null;
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    const heading = line.match(/^#{1,6}\s+(.*)$/);
    const item = line.match(/^[-*]\s+(.*)$/);
    if (!line) list = null;
    else if (heading) { nodes.push(el("h4", {}, ...inlineNodes(heading[1]))); list = null; }
    else if (item) {
      if (!list) { list = el("ul"); nodes.push(list); }
      list.append(el("li", {}, ...inlineNodes(item[1])));
    } else { nodes.push(el("p", {}, ...inlineNodes(line))); list = null; }
  }
  return nodes;
}

function paintNotes() {
  const box = $("update-notes");
  if (updateView.notes === null) { box.replaceChildren(el("p", { class: "muted" }, t("Loading the release notes…"))); return; }
  if (typeof updateView.notes === "string") { box.replaceChildren(el("p", { class: "muted" }, updateView.notes)); return; }
  box.replaceChildren(...updateView.notes.flatMap((note) => [
    el("h3", {}, `pdms ${note.version} `, ...(note.url ? [el("a", { href: note.url, target: "_blank", rel: "noreferrer" }, t("on GitHub"))] : [])),
    ...notesNodes(note.body),
  ]));
}

async function loadNotes(version) {
  updateView.notesFor = version;
  updateView.notes = null;
  paintNotes();
  const { data, error } = await getJson(`/api/update/notes?version=${encodeURIComponent(version)}`);
  if (updateView.notesFor !== version) return;
  updateView.notes = error ? t("Could not load the release notes: {error}", { error }) : data.notes.length ? data.notes : t("This release has no notes.");
  paintNotes();
}

function paintUpdateDialog() {
  const info = state.update;
  const offer = updateOffer();
  const job = state.jobs[UPDATE_JOB];
  const busy = Boolean(job && !job.error);
  const updating = offer && offer.kind === "update";
  $("update-title").textContent = offer ? offerTitle(offer) : busy ? t("Updating pdms") : t("pdms {version} is the latest version.", { version: info.current });
  let hint = "";
  if (offer && offer.kind === "restart") hint = t("This pdms ui still runs {current}. Restarting it takes a moment; the services, the proxy and the frontend keep running.", { current: info.current });
  else if (updating && info.updates_itself) hint = t("You have {current}. pdms ui installs it, as pdms self-update does, and restarts with it: this page comes back by itself.", { current: info.current });
  else if (updating) hint = t("You have {current}. This pdms was not installed with uv tool, so it cannot update itself; run:", { current: info.current });
  $("update-hint").textContent = hint;
  $("update-notes").hidden = !updating;
  const proxy = info.proxy;
  $("update-proxy-label").hidden = !(updating && info.updates_itself && proxy);
  if (proxy) {
    $("update-proxy").hidden = !proxy.background;
    $("update-proxy-text").textContent = proxy.background
      ? t("Restart the proxy (:{port}) with the new version; otherwise it keeps running the old one.", { port: proxy.port })
      : t("The proxy on :{port} runs in a terminal: restart it there afterwards.", { port: proxy.port });
  }
  $("update-command-row").hidden = !(updating && !info.updates_itself);
  $("update-command").textContent = info.command;
  $("update-phase").hidden = !busy;
  $("update-phase").textContent = busy ? phaseLabel(job.phase) : "";
  $("update-error").hidden = !(job && job.error);
  $("update-error").textContent = job && job.error ? failedText(job) : "";
  $("update-log").hidden = !job;
  $("update-go").hidden = !offer || (updating && !info.updates_itself);
  $("update-go").disabled = busy;
  $("update-go").textContent = offer && offer.kind === "restart" ? t("Restart pdms ui") : t("Update and restart");
}

function openUpdate() {
  const offer = updateOffer();
  if (offer && offer.kind === "update" && updateView.notesFor !== offer.version) loadNotes(offer.version);
  paintUpdateDialog();
  if (!$("update-dialog").open) $("update-dialog").showModal();
}

async function submitUpdate(event) {
  event.preventDefault();
  const offer = updateOffer();
  if (!offer) return;
  $("update-error").hidden = true;
  $("update-go").disabled = true;
  const path = offer.kind === "restart" ? "/api/update/restart" : "/api/update/install";
  const body = offer.kind === "restart" ? {} : { restart_proxy: $("update-proxy").checked };
  try {
    const { status, data } = await post(path, body);
    if (status >= 400) {
      $("update-error").textContent = data.error || t("pdms ui answered {status}", { status });
      $("update-error").hidden = false;
      $("update-go").disabled = false;
    } else if (offer.kind === "restart") {
      $("update-phase").textContent = t("restarting…");
      $("update-phase").hidden = false;
    }
  } catch {
    toast(t("pdms ui is not reachable: is it still running?"));
    $("update-go").disabled = false;
  }
}

async function copyCommand() {
  try {
    await navigator.clipboard.writeText(state.update.command);
    toast(t("Copied."), "info");
  } catch {
    toast(t("The browser did not allow copying."));
  }
}

// ---------------------------------------------------------------------------- repos

function toggleRepoMenu(event) {
  event.stopPropagation();
  const menu = $("repo-menu");
  if (!menu.hidden) { menu.hidden = true; return; }
  const current = state.repo && state.repo.alias;
  menu.replaceChildren(...state.repos.map((repo) => el("button", {
    type: "button", role: "menuitem", class: repo.name === current ? "cur" : "",
    onclick: () => { menu.hidden = true; if (repo.name !== current) useRepo(repo.name); },
  }, el("b", {}, repo.name), el("small", {}, repo.path))), el("hr"), el("button", {
    type: "button", role: "menuitem", onclick: () => { menu.hidden = true; openSetting("repos"); },
  }, t("Manage repos…")));
  menu.hidden = false;
  menu.querySelector("button").focus();
}

async function openSetting(tab, open = null) {
  settingsView.tab = tab;
  location.hash = "#settings";
  route();
  await loadSettings();
  if (open && settingsView.data) open(settingsView.data);
}

function repoRow(repo) {
  const path = el("td", { class: "mono" }, repo.path);
  if (!repo.exists) path.append(" ", el("span", { class: "not-set" }, t("missing")));
  const migrations = el("td", { class: "mono" }, repo.migrations || el("span", { class: "found-note" }, t("not set")));
  const remote = el("td", { class: "mono" }, repo.remote
    || el("span", { class: "found-note" }, repo.remote_found ? t("from frontend/.env: {url}", { url: repo.remote_found }) : t("not set")));
  const name = el("td", {}, el("b", {}, repo.name));
  if (repo.current) name.append(el("span", { class: "tag current" }, t("current")));
  return el("tr", { class: repo.current ? "current" : "" },
    el("td", { class: "cur-dot" }, repo.current ? "●" : ""), name, path, migrations, remote,
    el("td", { class: "num" }, repo.running ? String(repo.running) : ""),
    el("td", { class: "row-actions" },
      ...(repo.current ? [] : [button(t("Use"), () => useRepo(repo.name))]),
      button(t("Edit"), () => openRepo(repo)),
      button(t("Remove"), () => removeRepo(repo), { class: "btn small bad" })));
}

function paintRepos() {
  const all = settingsView.data.repos;
  const text = $("repo-filter").value.trim().toLowerCase();
  const shown = all.filter((repo) => matches(text, [repo.name, repo.path]));
  $("repo-rows").replaceChildren(...shown.map(repoRow));
  $("repo-count").textContent = all.length ? t("{shown} of {total}", { shown: shown.length, total: all.length }) : "";
  $("repo-empty").hidden = shown.length > 0;
  $("repo-empty").textContent = all.length ? t("No repo matches the filter.") : t("No repos yet: add the folder of a PDMS checkout.");
}

const repoForm = { repo: null, nameTouched: false, timer: null, looked: "" };
const REPO_FIELDS = ["path", "name", "migrations", "remote"];

function openRepo(repo = null) {
  Object.assign(repoForm, { repo, nameTouched: Boolean(repo), looked: "" });
  $("repo-title").textContent = repo ? t("Edit {name}", { name: repo.name }) : t("Add repo");
  $("repo-hint").textContent = repo ? t("Changes apply to services started from now on.")
    : t("A PDMS checkout: the folder with backend/snakesdk. Like pdms repo add.");
  $("repo-path").value = repo ? repo.path : "";
  $("repo-path").disabled = Boolean(repo);
  $("repo-browse").hidden = Boolean(repo) || !settingsView.data.pick_folder;
  $("repo-found").hidden = true;
  $("repo-name").value = repo ? repo.name : "";
  $("repo-migrations").value = repo ? repo.migrations : "";
  $("repo-remote").value = repo ? repo.remote : "";
  $("repo-remote").placeholder = repo && repo.remote_found ? repo.remote_found : t("from frontend/.env (VITE_APP_API_URL)");
  $("repo-use-label").hidden = Boolean(repo);
  $("repo-save").textContent = repo ? t("Save") : t("Add");
  resetForm("repo", REPO_FIELDS);
  $("repo-dialog").showModal();
  (repo ? $("repo-name") : $("repo-path")).focus();
}

// The folder typed (or picked) is looked at as it changes: is it a PDMS checkout, and how many services it has.
function repoPathChanged() {
  clearTimeout(repoForm.timer);
  repoForm.timer = setTimeout(lookAtRepo, 400);
}

async function lookAtRepo() {
  const path = $("repo-path").value.trim();
  if (!path || path === repoForm.looked) return;
  repoForm.looked = path;
  resetForm("repo", REPO_FIELDS);
  const { status, data } = await post("/api/repos/check", { path });
  if (repoForm.looked !== path) return;
  if (status !== 200) { $("repo-found").hidden = true; formError("repo", data, t("pdms ui answered {status}", { status })); return; }
  $("repo-found").textContent = data.registered
    ? t("Already registered as '{name}'.", { name: data.registered })
    : t("✓ PDMS repo found: {root} · {n} services", { root: data.root, n: data.services });
  $("repo-found").classList.toggle("warn", Boolean(data.registered));
  $("repo-found").hidden = false;
  if (!repoForm.nameTouched) $("repo-name").value = data.name;
}

async function browseRepo() {
  const { status, data } = await post("/api/ui/pick-folder", { start: $("repo-path").value.trim() });
  if (status === 200 && data.path) { $("repo-path").value = data.path; lookAtRepo(); }
}

async function saveRepo(event) {
  event.preventDefault();
  resetForm("repo", REPO_FIELDS);
  const current = repoForm.repo;
  const body = { name: $("repo-name").value.trim(), migrations: $("repo-migrations").value.trim(), remote: $("repo-remote").value.trim() };
  const path = current ? `/api/repos/${encodeURIComponent(current.name)}/save` : "/api/repos/add";
  if (!current) body.path = $("repo-path").value.trim();
  $("repo-save").disabled = true;
  try {
    const { status, data } = await post(path, body);
    if (status !== 200) { formError("repo", data, t("pdms ui answered {status}", { status })); return; }
    $("repo-dialog").close();
    toast(t("'{name}' saved.", { name: data.name }), "info");
    await loadSettings();
    if (!current && $("repo-use").checked && !(state.repo && state.repo.alias === data.name)) useRepo(data.name);
  } catch {
    formError("repo", {}, t("pdms ui is not reachable: is it still running?"));
  } finally {
    $("repo-save").disabled = false;
  }
}

async function removeRepo(repo) {
  const others = settingsView.data.repos.filter((r) => r.name !== repo.name);
  let text = t("pdms forgets it; the folder stays on disk. Instances running from it keep running.");
  if (repo.current) text += " " + (others.length ? t("It is the current repo: {name} becomes current.", { name: others[0].name }) : t("It is the current repo, and the only one."));
  if (!await confirmDialog(t("Remove {name}?", { name: repo.name }), text, t("Remove"))) return;
  act(`/api/repos/${encodeURIComponent(repo.name)}/remove`, {}, () => { toast(t("'{name}' deleted.", { name: repo.name }), "info"); loadSettings(); });
}

const switching = { name: "" };

async function useRepo(name, running = null) {
  try {
    const { status, data } = await post(`/api/repos/${encodeURIComponent(name)}/use`, running ? { running } : {});
    if (status === 409 && data.decision === "repo_switch") { openSwitch(name, data); return; }
    if (status >= 400) { if ($("switch-dialog").open) { $("switch-error").textContent = data.error; $("switch-error").hidden = false; } else toast(data.error || t("pdms ui answered {status}", { status })); return; }
    if ($("switch-dialog").open) $("switch-dialog").close();
    toast(t("Current repo: {name}. Services, stacks and proxy routes now come from it.", { name }), "info");
    if (data.left && data.left.length) toast(t("Not in '{name}', so still running: {keys}", { name, keys: data.left.join(", ") }), "info");
    if (data.proxy) toast(t("The proxy still routes to '{old}': restart it to use '{name}'.", { old: data.old, name }), "info");
    if (currentView() === "settings") loadSettings();
  } catch {
    toast(t("pdms ui is not reachable: is it still running?"));
  }
}

function openSwitch(name, data) {
  switching.name = name;
  const keys = data.running.map((item) => item.key);
  $("switch-title").textContent = t("Use {name} as the current repo", { name });
  $("switch-text").textContent = t("{n} instances are running from '{old}': {keys}. What should pdms do with them?", { n: keys.length, old: data.old, keys: keys.join(", ") });
  const stay = data.running.filter((item) => !item.movable).map((item) => item.key);
  $("switch-move").textContent = t("Restart them from '{name}' (same user, database and port)", { name })
    + (stay.length ? " " + t("(not in '{name}', so they keep running: {keys})", { name, keys: stay.join(", ") }) : "");
  $("switch-proxy").hidden = !data.proxy;
  $("switch-proxy").textContent = data.proxy ? t("The proxy is running for '{old}'. Restart it afterwards to route to '{name}'.", { old: data.old, name }) : "";
  $("switch-error").hidden = true;
  $("switch-go").textContent = t("Use {name}", { name });
  $("switch-form").querySelector('input[value="keep"]').checked = true;
  $("switch-dialog").showModal();
}

function submitSwitch(event) {
  event.preventDefault();
  useRepo(switching.name, $("switch-form").querySelector('input[name="running"]:checked').value);
}

// ---------------------------------------------------------------------------- migrations (Flyway), read-only

const flywayView = { db: "", data: null };
const FLYWAY_STATES = {
  applied: N_("applied"), pending: N_("pending"), failed: N_("failed"), outdated: N_("changed: runs again"),
  missing: N_("not in this checkout"),
};
const FLYWAY_TODO = new Set(["pending", "failed", "outdated"]);

async function openFlyway(db) {
  flywayView.db = db;
  flywayView.data = null;
  $("flyway-title").textContent = t("Migrations of {name}", { name: db });
  $("flyway-repo").textContent = t("Reading the Flyway history…");
  $("flyway-summary").replaceChildren();
  $("flyway-rows").replaceChildren();
  $("flyway-never").hidden = $("flyway-error").hidden = true;
  if (!$("flyway-dialog").open) $("flyway-dialog").showModal();
  $("flyway-refresh").disabled = true;
  try {
    const { status, data } = await post("/api/migrations/status", { db });
    if (flywayView.db !== db) return;
    if (status !== 200) {
      $("flyway-repo").textContent = "";
      $("flyway-error").textContent = data.error || t("pdms ui answered {status}", { status });
      $("flyway-error").hidden = false;
      return;
    }
    flywayView.data = data;
    $("flyway-pending").checked = data.counts.pending + data.counts.failed + data.counts.outdated > 0;
    paintFlyway();
  } catch {
    $("flyway-error").textContent = t("pdms ui is not reachable: is it still running?");
    $("flyway-error").hidden = false;
  } finally {
    $("flyway-refresh").disabled = false;
  }
}

function paintFlyway() {
  const data = flywayView.data;
  if (!data) return;
  $("flyway-repo").textContent = data.branch ? `${data.repo} · ${data.branch}` : data.repo;
  const pills = [];
  for (const [state, label] of [["pending", t("{n} pending", { n: data.counts.pending })], ["outdated", t("{n} changed", { n: data.counts.outdated })],
    ["failed", t("{n} failed", { n: data.counts.failed })], ["applied", t("{n} applied", { n: data.counts.applied })],
    ["missing", t("{n} not in this checkout", { n: data.counts.missing })]]) {
    if (data.counts[state] || state === "pending") pills.push(el("span", { class: `st ${state}` }, label));
  }
  $("flyway-summary").replaceChildren(...pills);
  $("flyway-never").hidden = data.flyway;
  $("flyway-never").textContent = data.flyway ? "" : t("Flyway has not run on this database yet (there is no {table}): every migration is pending.", { table: data.table });
  const text = $("flyway-filter").value.trim().toLowerCase();
  const onlyTodo = $("flyway-pending").checked;
  const shown = data.migrations.filter((m) => (!onlyTodo || FLYWAY_TODO.has(m.state)) && matches(text, [m.version, m.description, m.script]));
  $("flyway-rows").replaceChildren(...shown.map((m) => el("tr", {},
    el("td", {}, el("span", { class: `st ${m.state}` }, t(FLYWAY_STATES[m.state]))),
    el("td", { class: "mono" }, m.version || t("repeatable")),
    el("td", { title: m.script }, m.description),
    el("td", { class: "mono" }, m.installed_on ? new Date(m.installed_on).toLocaleString() : ""))));
  $("flyway-count").textContent = t("{shown} of {total}", { shown: shown.length, total: data.migrations.length });
  $("flyway-empty").hidden = shown.length > 0;
  $("flyway-empty").textContent = onlyTodo && !text ? t("Nothing to run: the database is up to date with this checkout.") : t("No migration matches the filter.");
}

// ---------------------------------------------------------------------------- doctor

const doctorView = { latest: null, at: null };
const CHECK_ICONS = { ok: "✓", warn: "!", fail: "✗" };
// What Home already shows by itself (services, updates), so its Doctor lines leave them out.
const HOME_COVERS = new Set(["services", "forget_stopped", "update", "adopt", "frontend_api"]);
// A hint that is a command to run, shown with a Copy button.
const COMMAND = /^(pdms|chmod|nvm|npm|uv|sudo|yarn) \S.*[^.]$/;

function paintDoctorBadge() {
  const counts = state.doctor.counts || {};
  const problems = (counts.warn || 0) + (counts.fail || 0);
  $("doctor-badge").hidden = !problems;
  $("doctor-badge").textContent = String(problems);
  $("doctor-badge").classList.toggle("bad", Boolean(counts.fail));
  $("doctor-badge").title = t("{fail} problems · {warn} warnings", { fail: counts.fail || 0, warn: counts.warn || 0 });
}

async function loadDoctor() {
  doctorView.at = state ? state.doctor.at : null;
  const { data, error } = await getJson("/api/doctor");
  if (error) { toast(error); return; }
  doctorView.latest = data.latest;
  paintDoctor();
}

function fixLabel(fix) {
  const [kind, name] = fix.split(/:(.*)/s);
  if (kind === "update") return t("Update…");
  if (kind === "add_user") return t("Add a user");
  if (kind === "add_db") return t("Add a database");
  if (kind === "edit_db") return t("Edit {name}", { name });
  if (kind === "add_repo") return t("Add repo");
  if (kind === "repos") return t("Open Repos");
  if (kind === "edit_repo") return t("Edit {name}", { name });
  if (kind === "forget_stopped") return t("Forget stopped");
  if (kind === "adopt") return t("Adopt all");
  if (kind === "frontend_api") return t("Fix it");
  return t("Open Services");
}

function runFix(fix) {
  const [kind, name] = fix.split(/:(.*)/s);
  if (kind === "update") openUpdate();
  else if (kind === "add_user") openSetting("users", () => openUser());
  else if (kind === "add_db") openSetting("dbs", () => openDb());
  else if (kind === "edit_db") openSetting("dbs", (data) => { const db = data.dbs.find((d) => d.name === name); if (db) openDb(db); });
  else if (kind === "add_repo") openSetting("repos", () => openRepo());
  else if (kind === "repos") openSetting("repos");
  else if (kind === "edit_repo") openSetting("repos", (data) => { const repo = data.repos.find((r) => r.name === name); if (repo) openRepo(repo); });
  else if (kind === "frontend_api") fixFrontendApi();
  else if (kind === "adopt") adoptStrays(null, () => act("/api/doctor/run", { databases: false }));
  else if (kind === "forget_stopped") act("/api/clean", {}, (data) => { toast(t("Forgot {n} stopped.", { n: data.forgotten.length }), "info"); act("/api/doctor/run", { databases: false }); });
  else location.hash = "#services";
}

function fixButton(check) {
  return button(`${fixLabel(check.fix)} →`, () => runFix(check.fix));
}

function copyText(text, done) {
  navigator.clipboard.writeText(text).then(() => toast(done, "info"), () => toast(t("The browser did not allow copying.")));
}

function checkRow(check) {
  const row = el("div", { class: `check ${check.status === "ok" ? "" : check.status}` },
    el("span", { class: `icon ${check.status}`, "aria-label": statusWord(check.status) }, CHECK_ICONS[check.status] || "?"),
    el("div", { class: "what" }, el("b", {}, check.name), el("span", { class: "detail" }, check.detail)));
  if (check.status !== "ok" && (check.hint || check.fix)) {
    const hint = el("div", { class: "hint" });
    if (check.fix) hint.append(el("span", { class: "muted" }, check.hint), fixButton(check));
    else if (COMMAND.test(check.hint)) hint.append(el("code", {}, check.hint), button(t("Copy"), () => copyText(check.hint, t("Copied."))));
    else hint.append(el("span", { class: "muted" }, check.hint));
    row.append(hint);
  }
  return row;
}

function statusWord(status) {
  return status === "ok" ? t("ok") : status === "warn" ? t("warning") : t("problem");
}

function paintDoctor() {
  const latest = doctorView.latest;
  const running = state && state.doctor.running;
  $("doctor-running").hidden = !running;
  $("doctor-run").disabled = Boolean(running);
  $("doctor-error").hidden = !(latest && latest.error);
  $("doctor-error").textContent = latest ? latest.error : "";
  if (!latest) { $("doctor-checks").replaceChildren(); $("doctor-summary").replaceChildren(); $("doctor-when").textContent = ""; return; }
  const checks = latest.checks;
  const count = (status) => checks.filter((c) => c.status === status).length;
  const summary = [el("span", { class: "st ok" }, t("{n} ok", { n: count("ok") }))];
  if (count("warn")) summary.push(el("span", { class: "st warn" }, count("warn") === 1 ? t("1 warning") : t("{n} warnings", { n: count("warn") })));
  if (count("fail")) summary.push(el("span", { class: "st fail" }, count("fail") === 1 ? t("1 problem") : t("{n} problems", { n: count("fail") })));
  $("doctor-summary").replaceChildren(...summary);
  $("doctor-when").textContent = t("Last run {when} · took {seconds} s{dbs}", {
    when: new Date(latest.at).toLocaleTimeString(), seconds: latest.took,
    dbs: latest.databases ? "" : " · " + t("databases not tested"),
  });
  const text = $("doctor-filter").value.trim().toLowerCase();
  const problemsOnly = $("doctor-problems").checked;
  const sections = [...new Set(checks.map((c) => c.section))];
  const cards = [];
  for (const section of sections) {
    const all = checks.filter((c) => c.section === section);
    const shown = all.filter((c) => (!problemsOnly || c.status !== "ok") && matches(text, [section, c.name, c.detail, c.hint]));
    if (!shown.length) continue;
    const worst = all.some((c) => c.status === "fail") ? "fail" : all.some((c) => c.status === "warn") ? "warn" : "ok";
    const label = worst === "ok" ? t("all ok") : worst === "warn" ? t("warning") : t("problem");
    cards.push(el("section", { class: "card check-section" },
      el("h2", {}, section, el("span", { class: `st ${worst}` }, label)), ...shown.map(checkRow)));
  }
  $("doctor-checks").replaceChildren(...cards);
  $("doctor-none").hidden = cards.length > 0 || !checks.length;
}

function runDoctor() {
  act("/api/doctor/run", { databases: $("doctor-dbs").checked });
}

function doctorReport() {
  const latest = doctorView.latest;
  if (!latest) return;
  const lines = [`pdms ${state.version} · doctor · ${latest.at}`];
  for (const c of latest.checks) lines.push(`[${c.status}] ${c.section} · ${c.name}: ${c.detail}${c.hint && c.status !== "ok" ? `  → ${c.hint}` : ""}`);
  copyText(lines.join("\n"), t("Report copied: paste it in a chat or an issue."));
}

// ---------------------------------------------------------------------------- views

const VIEWS = ["home", "services", "stacks", "proxy", "events", "doctor", "settings"];

function currentView() {
  const view = location.hash.slice(1);
  return VIEWS.includes(view) ? view : "home";
}

function route() {
  const view = currentView();
  for (const section of document.querySelectorAll(".view")) section.hidden = section.id !== `view-${view}`;
  for (const link of document.querySelectorAll(".side a[data-view]")) link.classList.toggle("on", link.dataset.view === view);
  if (!state) return;
  paintProxy();
  paintEvents();
  if (view === "events") showEventsTab(eventsView.tab);
  if (view === "settings") loadSettings();
  if (view === "doctor") loadDoctor();
}

// ---------------------------------------------------------------------------- wiring

$("logs-close").addEventListener("click", closeLogs);
$("logs-clear").addEventListener("click", clearLog);
for (const tab of $("logs-tabs").children) tab.addEventListener("click", () => openLogs(logs.key, tab.dataset.which));
$("clean").addEventListener("click", () => act("/api/clean", {}, (data) => toast(t("Forgot {n} stopped.", { n: data.forgotten.length }), "info")));
$("adopt-all").addEventListener("click", () => adoptStrays(null));
$("svc-filter").addEventListener("input", () => state && paintServices());
$("svc-problems").addEventListener("change", () => state && paintServices());
$("stack-filter").addEventListener("input", () => state && paintStacks());
$("stack-running").addEventListener("change", () => state && paintStacks());
$("run-new").addEventListener("click", () => state && openRun());
$("run-filter").addEventListener("input", filterRun);
$("run-filter").addEventListener("keydown", (event) => { if (event.key === "Enter") event.preventDefault(); });
$("run-port").addEventListener("input", resetConfirmation);
$("restart-form").addEventListener("submit", submitLaunch);
$("stack-new").addEventListener("click", () => openEditor());
$("editor-form").addEventListener("submit", saveEditor);
$("editor-cancel").addEventListener("click", () => $("editor").close());
$("editor-filter").addEventListener("input", filterEditor);
$("editor-filter").addEventListener("keydown", (event) => { if (event.key === "Enter") event.preventDefault(); });
$("proxy-start").addEventListener("click", openProxyStart);
$("proxy-stop").addEventListener("click", () => act(`/api/instances/${encodeURIComponent(state.proxy.key)}/stop`));
$("proxy-form").addEventListener("submit", submitProxyStart);
$("proxy-cancel").addEventListener("click", () => $("proxy-dialog").close());
$("proxy-port").addEventListener("input", resetProxyPort);
$("proxy-no-remote").addEventListener("change", () => { $("proxy-remote").disabled = $("proxy-no-remote").checked; });
for (const tab of $("proxy-tabs").children) tab.addEventListener("click", () => showProxyTab(tab.dataset.tab));
$("req-filter").addEventListener("input", paintRequests);
$("req-errors").addEventListener("change", paintRequests);
$("req-clear").addEventListener("click", () => { proxyView.requests = []; paintRequests(); });
$("route-filter").addEventListener("input", paintRoutes);
$("route-local").addEventListener("change", paintRoutes);
$("route-refresh").addEventListener("click", loadRoutes);
$("events-start").addEventListener("click", openEventsUp);
$("events-stop").addEventListener("click", stopEvents);
$("events-send").addEventListener("click", () => openSend());
$("events-broker").addEventListener("click", () => openConsumerStart(brokerQueue().consumer, "broker"));
for (const tab of $("events-tabs").children) tab.addEventListener("click", () => showEventsTab(tab.dataset.tab));
$("queue-filter").addEventListener("input", paintQueues);
$("queue-busy").addEventListener("change", paintQueues);
$("queue-purge-all").addEventListener("click", () => purge([]));
$("peek-refresh").addEventListener("click", () => openPeek(eventsView.peek));
$("peek-purge").addEventListener("click", () => purge([eventsView.peek]));
$("peek-close").addEventListener("click", closePeek);
$("type-filter").addEventListener("input", paintMap);
$("sns-topic").addEventListener("change", paintSns);
$("sns-filter").addEventListener("input", paintSns);
$("sns-clear").addEventListener("click", () => { eventsView.sns = []; paintSns(); });
$("send-form").addEventListener("submit", submitSend);
$("send-cancel").addEventListener("click", () => $("send").close());
$("send-template").addEventListener("click", fillTemplate);
$("send-target").addEventListener("input", sendTargetChanged);
setInterval(() => { if (eventsVisible("queues") && state && state.events.up) loadQueues(); }, 3000);
window.addEventListener("hashchange", route);
route();
$("restart-cancel").addEventListener("click", () => $("restart").close());
for (const tab of $("settings-tabs").children) tab.addEventListener("click", () => showSettingsTab(tab.dataset.tab));
$("db-filter").addEventListener("input", paintDbs);
$("db-protected").addEventListener("change", paintDbs);
$("db-new").addEventListener("click", () => openDb());
$("db-form").addEventListener("submit", saveDb);
$("db-cancel").addEventListener("click", () => $("db-dialog").close());
$("db-test").addEventListener("click", testDbForm);
$("db-eye").addEventListener("click", toggleDbPassword);
$("db-password").addEventListener("input", () => { settingsView.passwordTouched = true; });
$("db-protected-box").addEventListener("change", () => { settingsView.protectedTouched = true; });
// Like pdms db add: a database that is not on this machine is protected unless said otherwise.
$("db-host").addEventListener("input", () => {
  if (!settingsView.protectedTouched) $("db-protected-box").checked = !["localhost", "127.0.0.1", ""].includes($("db-host").value.trim());
});
for (const form of ["db-form", "user-form"]) {
  $(form).addEventListener("input", (event) => event.target.removeAttribute("aria-invalid"));
}
$("user-filter").addEventListener("input", paintUsers);
$("user-new").addEventListener("click", () => openUser());
$("user-form").addEventListener("submit", saveUser);
$("user-cancel").addEventListener("click", () => $("user-dialog").close());
$("defaults-filter").addEventListener("input", filterDefaults);
$("defaults-form").addEventListener("input", defaultsChanged);
$("defaults-form").addEventListener("change", defaultsChanged);
$("defaults-form").addEventListener("submit", (event) => { event.preventDefault(); if (defaultsChanged()) saveDefaults(); });
$("defaults-save").addEventListener("click", saveDefaults);
$("defaults-discard").addEventListener("click", paintDefaults);
$("settings-export").addEventListener("click", () => settingsView.data && openExport());
$("export-secrets").addEventListener("change", () => { $("export-warn").hidden = !$("export-secrets").checked; });
$("export-form").addEventListener("submit", submitExport);
$("export-cancel").addEventListener("click", () => $("export-dialog").close());
$("settings-import").addEventListener("click", () => $("import-file").click());
$("import-file").addEventListener("change", readImport);
$("import-form").addEventListener("change", (event) => { if (event.target.name === "mode") paintImport(); });
$("import-form").addEventListener("submit", submitImport);
$("import-cancel").addEventListener("click", () => $("import-dialog").close());
$("user-import").addEventListener("click", () => settingsView.data && openUserImport());
$("users-find").addEventListener("click", findDbUsers);
$("users-search").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); findDbUsers(); } });
$("users-found").addEventListener("change", usersCount);
$("users-all").addEventListener("change", () => {
  for (const box of $("users-found").querySelectorAll("input")) box.checked = $("users-all").checked;
  usersCount();
});
$("users-form").addEventListener("submit", submitUserImport);
$("users-cancel").addEventListener("click", () => $("users-dialog").close());
$("restart-db").addEventListener("change", resetConfirmation);

$("front-new").addEventListener("click", () => openFrontendStart());
$("front-form").addEventListener("submit", submitFrontendStart);
$("front-cancel").addEventListener("click", () => $("front-dialog").close());
$("front-form").addEventListener("change", (event) => {
  if (event.target.name === "mode") paintFrontendMode();
  if (event.target.name === "mode" || event.target.id === "front-rebuild") resetFrontendPort();
});
$("home-start").addEventListener("click", () => startAll());
$("update-chip").addEventListener("click", openUpdate);
document.addEventListener("click", (event) => { if (!$("repo-menu").contains(event.target)) $("repo-menu").hidden = true; });
document.addEventListener("keydown", (event) => { if (event.key === "Escape") $("repo-menu").hidden = true; });
$("repo-filter").addEventListener("input", () => settingsView.data && paintRepos());
$("repo-new").addEventListener("click", () => openRepo());
$("repo-form").addEventListener("submit", saveRepo);
$("repo-cancel").addEventListener("click", () => $("repo-dialog").close());
$("repo-browse").addEventListener("click", browseRepo);
$("repo-path").addEventListener("input", repoPathChanged);
$("repo-name").addEventListener("input", () => { repoForm.nameTouched = true; });
$("repo-form").addEventListener("input", (event) => event.target.removeAttribute("aria-invalid"));
$("switch-form").addEventListener("submit", submitSwitch);
$("switch-cancel").addEventListener("click", () => $("switch-dialog").close());
$("doctor-run").addEventListener("click", runDoctor);
$("flyway-close").addEventListener("click", () => $("flyway-dialog").close());
$("flyway-refresh").addEventListener("click", () => openFlyway(flywayView.db));
$("flyway-filter").addEventListener("input", paintFlyway);
$("flyway-pending").addEventListener("change", paintFlyway);
$("doctor-copy").addEventListener("click", doctorReport);
$("doctor-filter").addEventListener("input", paintDoctor);
$("doctor-problems").addEventListener("change", paintDoctor);
$("version-open").addEventListener("click", openUpdate);
$("version-check").addEventListener("click", checkNow);
$("update-form").addEventListener("submit", submitUpdate);
$("update-cancel").addEventListener("click", () => $("update-dialog").close());
$("update-copy").addEventListener("click", copyCommand);
$("update-log").addEventListener("click", () => { $("update-dialog").close(); showLogs(UPDATE_JOB); });
$("home-stop").addEventListener("click", stopAll);
for (const id of ["setup-stack", "setup-events", "setup-proxy", "setup-frontend", "setup-mode"]) $(id).addEventListener("change", saveSetup);

fetch("/api/state").then((response) => response.json()).then(paint).finally(connect);
setInterval(tickUptimes, 1000);
