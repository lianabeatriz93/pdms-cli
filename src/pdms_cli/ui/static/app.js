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
  for (const key of Object.keys(state.jobs)) {
    if (!key.startsWith("stack:") && !items.some((item) => item.key === key)) {
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
  state = { jobs: {}, users: [], dbs: [], stacks: [], ...next };
  paintContext();
  paintServices();
  paintStacks();
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

// ---------------------------------------------------------------------------- restart and up

const launch = { path: null, confirmed: false, after: null };

function options(select, names, current, label = (name) => name) {
  select.replaceChildren(...names.map((name) => el("option", name === current ? { value: name, selected: "" } : { value: name }, label(name))));
}

function dbLabel(name) {
  const db = state.dbs.find((item) => item.name === name);
  return db && db.protected ? `${name} (protected)` : name;
}

// The user, database and install of a restart or a stack's up, asking again before a protected database.
function openLaunch({ title, key, hint, user, db, path, go, after }) {
  Object.assign(launch, { path, after, confirmed: false });
  $("restart-title").textContent = title;
  $("restart-key").textContent = key;
  $("restart-hint").textContent = hint;
  options($("restart-user"), state.users, user);
  options($("restart-db"), state.dbs.map((item) => item.name), db, dbLabel);
  $("restart-warn").hidden = $("restart-error").hidden = true;
  $("restart-go").textContent = launch.go = go;
  $("restart-form").install.value = "auto";
  $("restart").showModal();
}

function openRestart(item) {
  openLaunch({
    title: "Restart", key: item.key, hint: "Same port. Change the user or the database if you need to.",
    user: item.user, db: item.db, go: "Restart", path: `/api/instances/${encodeURIComponent(item.key)}/restart`,
    after: (install) => { if (logs.key === item.key) openLogs(item.key, install === false ? "current" : logs.which); },
  });
}

function openUp(stack) {
  openLaunch({
    title: "Start", key: stack.name, hint: "Starts the services that are not running yet, each on a free port.",
    user: stack.user || state.user, db: stack.db || state.db, go: "Start", path: `/api/stacks/${encodeURIComponent(stack.name)}/up`,
    after: (_install, data) => { if (!data.job) toast(`The whole stack '${stack.name}' is already running.`, "info"); },
  });
}

async function submitLaunch(event) {
  event.preventDefault();
  const install = { auto: null, force: true, skip: false }[$("restart-form").install.value];
  const body = { user: $("restart-user").value, db: $("restart-db").value, install, confirmed: launch.confirmed };
  $("restart-go").disabled = true;
  try {
    const { status, data } = await post(launch.path, body);
    if (status === 200 || status === 202) {
      $("restart").close();
      if (launch.after) launch.after(install, data);
      return;
    }
    if (status === 409 && data.decision === "protected_database") {
      launch.confirmed = true;
      $("restart-warn").textContent = `'${data.name}' is a protected database. Use it anyway?`;
      $("restart-warn").hidden = false;
      $("restart-go").textContent = `${launch.go} on the protected DB`;
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
  launch.confirmed = false;
  $("restart-warn").hidden = true;
  $("restart-go").textContent = launch.go;
}

// ---------------------------------------------------------------------------- stacks

const ASK = "(ask when starting)";

function stackPath(name) {
  return `/api/stacks/${encodeURIComponent(name)}`;
}

function showLogs(key, which = "current") {
  location.hash = "#services";
  openLogs(key, which);
}

function stackCard(stack) {
  const job = state.jobs[`stack:${stack.name}`];
  const busy = job && !job.error;
  const total = stack.services.length;
  const up = stack.services.filter((svc) => svc.running.length).length;
  const status = busy
    ? el("span", { class: "st starting" }, `${job.phase}…`)
    : el("span", { class: `st ${up === total ? "ok" : up ? "starting" : "stopped"}` },
      up === total ? "running" : up ? `${up} of ${total} running` : "stopped");

  const list = el("ul", { class: "stack-services" }, ...stack.services.map((svc) => el("li", {},
    el("span", { class: `dot ${svc.running.length ? "on" : ""}` }),
    el("span", { class: "mono" }, svc.path),
    ...svc.running.map((key) => button(key.slice(key.indexOf("@")), () => showLogs(key), {
      class: "btn tiny link", title: `Logs of ${key}`,
    })),
  )));

  const card = el("article", { class: "card stack" },
    el("header", {}, el("h2", { class: "mono" }, stack.name), status),
    list,
    el("p", { class: "muted meta" }, `user ${stack.user || ASK} · db ${stack.db || ASK}`),
  );
  if (job && job.error) {
    card.append(el("p", { class: "error" }, `${job.action} failed: ${job.error}`));
  }
  const footer = el("footer", {});
  if (job && job.log_key) footer.append(button("Install log", () => showLogs(job.log_key, "install")));
  if (job && job.error) footer.append(button("Dismiss", () => act(`${stackPath(stack.name)}/dismiss`)));
  if (!busy) {
    if (up < total) footer.append(button("Start", () => openUp(stack), { class: "btn small primary" }));
    if (up) footer.append(button("Stop", () => act(`${stackPath(stack.name)}/down`), { class: "btn small bad" }));
    footer.append(button("Edit", () => openEditor(stack)));
    footer.append(button("Delete", () => removeStack(stack), { class: "btn small ghost" }));
  }
  card.append(footer);
  return card;
}

function paintStacks() {
  $("stacks").replaceChildren(...state.stacks.map(stackCard));
  $("stacks-empty").hidden = state.stacks.length > 0;
  const running = state.stacks.filter((stack) => stack.services.some((svc) => svc.running.length)).length;
  $("stack-count").textContent = state.stacks.length || "";
  $("stack-summary").textContent = `${state.stacks.length} stacks · ${running} running`;
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
  if (await confirmDialog(`Delete stack ${stack.name}?`, "Only the stack goes; its services keep running if they are.", "Delete")) {
    act(`${stackPath(stack.name)}/remove`, {}, () => toast(`Stack '${stack.name}' deleted.`, "info"));
  }
}

// ---------------------------------------------------------------------------- stack editor

const editor = { name: null, order: [], picked: new Set() };

function slashes(path) {
  return path.replaceAll("\\", "/").replace(/\/+$/, "");
}

async function openEditor(stack = null) {
  let found;
  try {
    const response = await fetch("/api/services");
    found = await response.json();
    if (!response.ok) { toast(found.error || `pdms ui answered ${response.status}`); return; }
  } catch {
    toast("pdms ui is not reachable: is it still running?");
    return;
  }
  const root = slashes(found.root);
  const runningOn = {};
  for (const inst of state.instances) {
    if (inst.status === "stopped") continue;
    const path = slashes(inst.service);
    if (path.startsWith(`${root}/`)) (runningOn[path.slice(root.length + 1)] ||= []).push(inst.port);
  }
  // Like pdms stack edit: the stack's services first, then the running ones, then the rest.
  const current = stack ? stack.services.map((svc) => svc.path) : [];
  const rest = found.services.filter((svc) => !current.includes(svc));
  editor.order = [...current, ...rest.filter((svc) => runningOn[svc]), ...rest.filter((svc) => !runningOn[svc])];
  editor.picked = new Set(current);
  editor.name = stack ? stack.name : null;

  $("editor-title").textContent = stack ? `Edit ${stack.name}` : "New stack";
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
    const ports = (runningOn[svc] || []).filter(Boolean);
    return el("label", { class: "pick", "data-svc": svc.toLowerCase() }, box, el("span", { class: "mono" }, svc),
      runningOn[svc] ? el("span", { class: "muted" }, ports.length ? `running on :${ports.join(", :")}` : "running") : "");
  }));
  options($("editor-user"), ["", ...state.users], stack ? stack.user : "", (name) => name || ASK);
  options($("editor-db"), ["", ...state.dbs.map((item) => item.name)], stack ? stack.db : "", (name) => name ? dbLabel(name) : ASK);
  $("editor-error").hidden = true;
  editorCount();
  $("editor").showModal();
  (stack ? $("editor-filter") : $("editor-name")).focus();
}

function editorCount() {
  $("editor-count").textContent = `${editor.picked.size} of ${editor.order.length} selected`;
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
    $("editor-error").textContent = "A stack needs at least one service.";
    $("editor-error").hidden = false;
    return;
  }
  $("editor-save").disabled = true;
  try {
    const { status, data } = await post(`${stackPath(name)}/save`, body);
    if (status === 200) {
      $("editor").close();
      toast(`Stack '${name}' saved.`, "info");
      return;
    }
    $("editor-error").textContent = data.error || `pdms ui answered ${status}`;
    $("editor-error").hidden = false;
  } catch {
    $("editor-error").textContent = "pdms ui is not reachable: is it still running?";
    $("editor-error").hidden = false;
  } finally {
    $("editor-save").disabled = false;
  }
}

// ---------------------------------------------------------------------------- views

function route() {
  const view = location.hash === "#stacks" ? "stacks" : "services";
  for (const section of document.querySelectorAll(".view")) section.hidden = section.id !== `view-${view}`;
  for (const link of document.querySelectorAll(".side a[data-view]")) link.classList.toggle("on", link.dataset.view === view);
}

// ---------------------------------------------------------------------------- wiring

$("logs-close").addEventListener("click", closeLogs);
$("logs-clear").addEventListener("click", clearLog);
for (const tab of $("logs-tabs").children) tab.addEventListener("click", () => openLogs(logs.key, tab.dataset.which));
$("clean").addEventListener("click", () => act("/api/clean", {}, (data) => toast(`Forgot ${data.forgotten.length} stopped.`, "info")));
$("restart-form").addEventListener("submit", submitLaunch);
$("stack-new").addEventListener("click", () => openEditor());
$("editor-form").addEventListener("submit", saveEditor);
$("editor-cancel").addEventListener("click", () => $("editor").close());
$("editor-filter").addEventListener("input", filterEditor);
$("editor-filter").addEventListener("keydown", (event) => { if (event.key === "Enter") event.preventDefault(); });
window.addEventListener("hashchange", route);
route();
$("restart-cancel").addEventListener("click", () => $("restart").close());
$("restart-db").addEventListener("change", resetConfirmation);

fetch("/api/state").then((response) => response.json()).then(paint).finally(connect);
setInterval(tickUptimes, 1000);
