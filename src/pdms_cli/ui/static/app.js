// pdms ui: paints /api/state, follows /api/stream and runs the actions. Text only goes in through textContent,
// never as HTML.
"use strict";

const $ = (id) => document.getElementById(id);
const PHASES = { stopping: "stopping…", installing: "installing…", starting: "starting…" };
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
    if (status >= 400) toast(data.error || `pdms ui answered ${status}`);
    else if (done) done(data);
  } catch {
    toast("pdms ui is not reachable: is it still running?");
  }
}

// ---------------------------------------------------------------------------- state

function chip(label, value) {
  return el("span", { class: "chip" }, label, el("b", {}, value || "-"));
}

function paintContext() {
  const chips = [chip("repo", state.repo && state.repo.alias), chip("user", state.user), chip("db", state.db)];
  chips.push(chip("proxy", state.proxy ? `:${state.proxy.port}` : "off"));
  chips.push(chip("events", state.events.up ? `:${state.events.port}` : "off"));
  $("ctx").replaceChildren(...chips);
}

function button(label, onclick, attrs = {}) {
  return el("button", { class: "btn small", type: "button", onclick, ...attrs }, label);
}

function rowActions(item, job) {
  const cell = el("td", { class: "row-actions" });
  const busy = job && !job.error;
  cell.append(button("Logs", () => openLogs(item.key, job && job.installed && busy ? "install" : "current")));
  if (busy) return cell;
  const alive = item.status !== "stopped";
  if (alive && !item.queue && !item.isProxy) {
    cell.append(el("a", {
      class: "btn small", href: `http://localhost:${item.port}/docs`, target: "_blank", rel: "noopener noreferrer",
    }, "Docs"));
  }
  if (!item.isProxy && !item.placeholder) cell.append(button("Restart", () => openRestart(item)));
  if (alive) {
    cell.append(button("Stop", () => act(`/api/instances/${encodeURIComponent(item.key)}/stop`), { class: "btn small bad" }));
  } else if (!item.placeholder) {
    cell.append(button("Forget", () => act(`/api/instances/${encodeURIComponent(item.key)}/forget`)));
  }
  return cell;
}

function statusCell(item, job) {
  const cell = el("td");
  if (job && !job.error) {
    cell.append(el("span", { class: "st starting" }, PHASES[job.phase] || job.phase));
    return cell;
  }
  cell.append(el("span", { class: `st ${item.status}` }, item.status));
  if (item.detail) cell.append(el("span", { class: "detail" }, item.detail));
  if (job && job.error) {
    cell.append(el("span", { class: "detail" }, `${job.action} failed: ${job.error}`));
    cell.append(button("Dismiss", () => act(`/api/instances/${encodeURIComponent(item.key)}/dismiss`), { class: "btn tiny" }));
  }
  return cell;
}

function row(item) {
  const job = state.jobs[item.key];
  const running = item.status !== "stopped" && item.started_at && !(job && !job.error);
  return el("tr", item.key === logs.key ? { class: "picked" } : {},
    el("td", { class: "mono" }, item.key),
    statusCell(item, job),
    el("td", { class: "mono" }, item.url || ""),
    el("td", {}, item.repo || "-"),
    el("td", {}, item.user || "-"),
    el("td", {}, item.db || "-"),
    el("td", running ? { class: "num", "data-started": item.started_at } : { class: "num" }, running ? uptime(item.started_at) : ""),
    rowActions(item, job),
  );
}

function serviceItems() {
  const items = state.instances.map((inst) => ({
    ...inst, url: inst.queue ? `sqs ← ${inst.queue}` : `http://localhost:${inst.port}`,
  }));
  if (state.proxy) {
    items.push({
      ...state.proxy, isProxy: true, user: state.proxy.as, db: "", url: `http://localhost:${state.proxy.port}`, detail: "",
    });
  }
  // A restart forgets the instance for a moment: keep its row while the job runs.
  for (const [key, job] of Object.entries(state.jobs)) {
    if (!items.some((item) => item.key === key)) {
      items.push({ key, status: "stopped", placeholder: true, detail: "", url: "", repo: "", user: "", db: "" });
    }
  }
  return items;
}

function paintServices() {
  const items = serviceItems();
  $("rows").replaceChildren(...items.map(row));
  $("empty").hidden = items.length > 0;
  const alive = state.instances.filter((i) => i.status !== "stopped");
  const failing = state.instances.filter((i) => i.status === "error" || i.status === "stopped");
  $("count").textContent = alive.length || "";
  $("summary").textContent = `${alive.length} running · ${failing.length} failing`;
  $("clean").hidden = !state.instances.some((i) => i.status === "stopped");
}

function tickUptimes() {
  for (const cell of document.querySelectorAll("td[data-started]")) cell.textContent = uptime(cell.dataset.started);
}

function paint(next) {
  state = { jobs: {}, users: [], dbs: [], ...next };
  paintContext();
  paintServices();
  if (logs.key) paintLogTabs();
}

function connect() {
  const stream = new EventSource("/api/stream");
  stream.addEventListener("state", (event) => paint(JSON.parse(event.data)));
  stream.onopen = () => { $("live").className = "live on"; $("live").title = "Live"; };
  // EventSource reconnects by itself; the dot shows when pdms ui is not reachable.
  stream.onerror = () => { $("live").className = "live off"; $("live").title = "Disconnected: is pdms ui still running?"; };
}

// ---------------------------------------------------------------------------- logs

const logs = { key: null, which: "current", stream: null, lines: [] };

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
    pre.textContent = logs.lines.join("\n") + "\n";
  } else {
    pre.append(lines.join("\n") + "\n");
  }
  if (follow) pre.scrollTop = pre.scrollHeight;
}

function clearLog() {
  logs.lines = [];
  $("logs-text").textContent = "";
}

function paintLogTabs() {
  const isProxy = logs.key.startsWith("proxy");
  for (const tab of $("logs-tabs").children) {
    tab.setAttribute("aria-selected", String(tab.dataset.which === logs.which));
    tab.hidden = tab.dataset.which === "install" && isProxy;
  }
  const job = state && state.jobs[logs.key];
  $("logs-note").textContent = job && !job.error ? PHASES[job.phase] || job.phase : "";
}

function openLogs(key, which = "current") {
  if (logs.stream) logs.stream.close();
  Object.assign(logs, { key, which });
  $("logs").hidden = false;
  $("logs-key").textContent = key;
  paintLogTabs();
  clearLog();
  const query = new URLSearchParams({ key, which });
  const stream = logs.stream = new EventSource(`/api/logs/stream?${query}`);
  stream.onopen = clearLog; // a reconnect starts again with the tail
  stream.addEventListener("lines", (event) => appendLog(JSON.parse(event.data)));
  stream.addEventListener("reset", () => appendLog(["", "──── restarted ────", ""]));
  stream.onerror = () => {
    if (stream.readyState === EventSource.CLOSED) appendLog(["(no log here)"]);
  };
  if (state) paintServices();
  $("logs").scrollIntoView({ block: "nearest" });
}

function closeLogs() {
  if (logs.stream) logs.stream.close();
  Object.assign(logs, { key: null, stream: null });
  $("logs").hidden = true;
  paintServices();
}

// ---------------------------------------------------------------------------- restart

const restart = { item: null, confirmed: false };

function options(select, names, current, label = (name) => name) {
  select.replaceChildren(...names.map((name) => el("option", name === current ? { value: name, selected: "" } : { value: name }, label(name))));
}

function openRestart(item) {
  Object.assign(restart, { item, confirmed: false });
  $("restart-key").textContent = item.key;
  options($("restart-user"), state.users, item.user);
  const protectedDbs = new Set(state.dbs.filter((db) => db.protected).map((db) => db.name));
  options($("restart-db"), state.dbs.map((db) => db.name), item.db, (name) => protectedDbs.has(name) ? `${name} (protected)` : name);
  $("restart-warn").hidden = $("restart-error").hidden = true;
  $("restart-go").textContent = "Restart";
  $("restart-form").install.value = "auto";
  $("restart").showModal();
}

async function submitRestart(event) {
  event.preventDefault();
  const install = { auto: null, force: true, skip: false }[$("restart-form").install.value];
  const body = { user: $("restart-user").value, db: $("restart-db").value, install, confirmed: restart.confirmed };
  const key = restart.item.key;
  $("restart-go").disabled = true;
  try {
    const { status, data } = await post(`/api/instances/${encodeURIComponent(key)}/restart`, body);
    if (status === 202) {
      $("restart").close();
      if (logs.key === key) openLogs(key, install === false ? "current" : logs.which);
      return;
    }
    if (status === 409 && data.decision === "protected_database") {
      restart.confirmed = true;
      $("restart-warn").textContent = `'${data.name}' is a protected database. Restart on it anyway?`;
      $("restart-warn").hidden = false;
      $("restart-go").textContent = "Restart on the protected DB";
      return;
    }
    $("restart-error").textContent = data.error || `pdms ui answered ${status}`;
    $("restart-error").hidden = false;
  } catch {
    $("restart-error").textContent = "pdms ui is not reachable: is it still running?";
    $("restart-error").hidden = false;
  } finally {
    $("restart-go").disabled = false;
  }
}

function resetConfirmation() {
  restart.confirmed = false;
  $("restart-warn").hidden = true;
  $("restart-go").textContent = "Restart";
}

// ---------------------------------------------------------------------------- wiring

$("logs-close").addEventListener("click", closeLogs);
$("logs-clear").addEventListener("click", clearLog);
for (const tab of $("logs-tabs").children) tab.addEventListener("click", () => openLogs(logs.key, tab.dataset.which));
$("clean").addEventListener("click", () => act("/api/clean", {}, (data) => toast(`Forgot ${data.forgotten.length} stopped.`, "info")));
$("restart-form").addEventListener("submit", submitRestart);
$("restart-cancel").addEventListener("click", () => $("restart").close());
$("restart-db").addEventListener("change", resetConfirmation);

fetch("/api/state").then((response) => response.json()).then(paint).finally(connect);
setInterval(tickUptimes, 1000);
