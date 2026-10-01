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
  if (busy || item.isSns) return cell;
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

function lastPublish(item) {
  return item.last_publish ? `last ${new Date(item.last_publish).toLocaleTimeString()}` : "nothing yet";
}

function row(item) {
  const job = state.jobs[item.key];
  const running = item.status !== "stopped" && item.started_at && !(job && !job.error);
  const uptimeCell = item.isSns
    ? el("td", { class: "num muted" }, lastPublish(item))
    : el("td", running ? { class: "num", "data-started": item.started_at } : { class: "num" }, running ? uptime(item.started_at) : "");
  return el("tr", item.key === logs.key ? { class: "picked" } : {},
    el("td", { class: "mono" }, item.key),
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
      ...state.sns, isSns: true, url: `sns → ${state.sns.queue}`, repo: "", user: "", db: "",
      detail: state.sns.status === "stopped" ? "the local ElasticMQ is not running: pdms events up" : "",
    });
  }
  // A restart forgets the instance for a moment: keep its row while the job runs (stacks and events have their own).
  for (const key of Object.keys(state.jobs)) {
    if (!key.includes(":") && !items.some((item) => item.key === key)) {
      items.push({ key, status: "stopped", placeholder: true, detail: "", url: "", repo: "", user: "", db: "" });
    }
  }
  return items;
}

function problem(item) {
  const job = state.jobs[item.key];
  return item.status === "error" || (item.status === "stopped" && !item.placeholder) || Boolean(job && job.error);
}

function serviceShown(item) {
  const text = $("svc-filter").value.trim().toLowerCase();
  if ($("svc-problems").checked && !problem(item)) return false;
  return !text || [item.key, item.status, item.detail, item.url, item.repo, item.user, item.db]
    .join(" ").toLowerCase().includes(text);
}

function paintServices() {
  const items = serviceItems();
  const shown = items.filter(serviceShown);
  $("rows").replaceChildren(...shown.map(row));
  $("empty").hidden = items.length > 0;
  $("svc-none").hidden = !items.length || shown.length > 0;
  $("svc-count").textContent = items.length ? `${shown.length} of ${items.length}` : "";
  const alive = state.instances.filter((i) => i.status !== "stopped");
  const failing = state.instances.filter((i) => i.status === "error" || i.status === "stopped");
  $("count").textContent = alive.length || "";
  $("summary").textContent = `${alive.length} running · ${failing.length} failing`;
  $("clean").hidden = !state.instances.some((i) => i.status === "stopped");
}

function tickUptimes() {
  for (const cell of document.querySelectorAll("[data-started]")) cell.textContent = uptime(cell.dataset.started);
}

function paint(next) {
  state = { jobs: {}, users: [], dbs: [], stacks: [], ...next };
  paintContext();
  paintServices();
  paintStacks();
  paintProxy();
  const wasUp = eventsView.up;
  eventsView.up = state.events.up;
  paintEvents();
  if (wasUp !== undefined && wasUp !== eventsView.up && currentView() === "events") showEventsTab(eventsView.tab);
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
  const noInstall = logs.key.startsWith("proxy") || logs.key === "sns";
  for (const tab of $("logs-tabs").children) {
    tab.setAttribute("aria-selected", String(tab.dataset.which === logs.which));
    tab.hidden = tab.dataset.which === "install" && noInstall;
  }
  const job = state && state.jobs[logs.key];
  $("logs-note").textContent = job && !job.error ? PHASES[job.phase] || job.phase : logs.find ? `marked: ${logs.find.label}` : "";
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
  stream.addEventListener("reset", () => appendLog(["", "──── restarted ────", ""]));
  stream.onerror = () => {
    if (stream.readyState === EventSource.CLOSED) appendLog(["(no log here)"]);
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

const launch = { path: null, confirmed: false, after: null, run: false };

function options(select, names, current, label = (name) => name) {
  select.replaceChildren(...names.map((name) => el("option", name === current ? { value: name, selected: "" } : { value: name }, label(name))));
}

function dbLabel(name) {
  const db = state.dbs.find((item) => item.name === name);
  return db && db.protected ? `${name} (protected)` : name;
}

// The user, database and install of a restart or a stack's up, asking again before a protected database.
function openLaunch({ title, key, hint, user, db, path, go, after, broker = false, run = false }) {
  Object.assign(launch, { path, after, run, confirmed: false });
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
  return el("span", { class: "muted" }, listening.length ? `running on :${listening.join(", :")}` : "running");
}

async function fetchServices() {
  try {
    const response = await fetch("/api/services");
    const found = await response.json();
    if (response.ok) return found;
    toast(found.error || `pdms ui answered ${response.status}`);
  } catch {
    toast("pdms ui is not reachable: is it still running?");
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
  $("run-count").textContent = `${found.services.length} services`;
  openLaunch({
    title: "Start a service", key: "", hint: "In the background, like pdms run -b. It keeps running when pdms ui stops.",
    user: state.user, db: state.db, go: "Start", path: "/api/run", run: true,
    after: (_install, data) => openLogs(data.job),
  });
  $("run-filter").focus();
}

function filterRun() {
  const text = $("run-filter").value.trim().toLowerCase();
  let shown = 0;
  for (const item of $("run-services").children) {
    item.hidden = Boolean(text) && !item.dataset.svc.includes(text);
    if (!item.hidden) shown += 1;
  }
  const total = $("run-services").children.length;
  $("run-count").textContent = text ? `${shown} of ${total}` : `${total} services`;
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
  if (!$("restart-broker-label").hidden) body.broker = $("restart-broker").checked;
  if (launch.run) {
    const picked = $("run-services").querySelector("input:checked");
    if (!picked) {
      $("restart-error").textContent = "Pick the service to start.";
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
      $("restart-warn").textContent = `Port ${data.port} is in use. Start on ${data.free} instead?`;
      $("restart-warn").hidden = false;
      $("restart-go").textContent = `${launch.go} on ${data.free}`;
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
    ? el("span", { class: "st starting" }, `${job.phase}…`)
    : el("span", { class: `st ${up === total ? "ok" : up ? "starting" : "stopped"}` },
      up === total ? "running" : up ? `${up} of ${total} running` : "stopped");

  const list = el("div", { class: "stack-groups" }, ...stackGroups(stack.services).map(([domain, services]) => {
    const running = services.filter((svc) => svc.running.length).length;
    return el("section", { class: "stack-group" },
      el("h3", {}, el("span", { class: "mono" }, domain || "(repo root)"),
        el("span", { class: "muted" }, `${running}/${services.length}`)),
      el("ul", { class: "stack-services" }, ...services.map((svc) => el("li", {
        title: svc.path, ...(text && svc.path.toLowerCase().includes(text) ? { class: "hit" } : {}),
      },
        el("span", { class: `dot ${svc.running.length ? "on" : ""}` }),
        el("span", { class: "mono name" }, svc.name),
        el("span", { class: "ports" }, ...svc.running.map((key) => button(key.slice(key.indexOf("@")), () => showLogs(key), {
          class: "btn tiny link", title: `Logs of ${key}`,
        }))),
      ))),
    );
  }));

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
  const shown = state.stacks.filter(stackShown);
  $("stacks").replaceChildren(...shown.map(stackCard));
  $("stacks-empty").hidden = state.stacks.length > 0;
  $("stacks-none").hidden = !state.stacks.length || shown.length > 0;
  $("stack-shown").textContent = state.stacks.length ? `${shown.length} of ${state.stacks.length}` : "";
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
  const found = await fetchServices();
  if (!found) return;
  const ports = runningOn(slashes(found.root));
  // Like pdms stack edit: the stack's services first, then the running ones, then the rest.
  const current = stack ? stack.services.map((svc) => svc.path) : [];
  const rest = found.services.filter((svc) => !current.includes(svc));
  editor.order = [...current, ...rest.filter((svc) => ports[svc]), ...rest.filter((svc) => !ports[svc])];
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
    return el("label", { class: "pick", "data-svc": svc.toLowerCase() }, box, el("span", { class: "mono" }, svc), runningNote(ports[svc]));
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
  $("proxy-summary").textContent = busy ? PHASES[job.phase] || job.phase
    : running ? `${running.status === "ok" ? "running" : "starting"} on :${running.port}` : "off";
  $("proxy-start").hidden = Boolean(running || busy);
  $("proxy-stop").hidden = !running || busy;
  $("proxy-docs").hidden = !running;
  if (running) $("proxy-docs").href = `http://localhost:${running.port}/docs`;

  const card = $("proxy-info");
  if (running) {
    card.replaceChildren(
      info("URL", `http://localhost:${running.port}`),
      info("Repo", running.repo_alias || running.repo),
      info("Environment", running.env),
      info("Remote API", running.remote || "none (only local services)"),
      info("Acting as", running.as || "each service's own profile"),
      info("Frontend", running.frontend ? ".env.local → proxy" : "not changed"),
      info("Runs", running.background ? "in the background" : "in a terminal"),
      el("div", {}, el("span", {}, "Uptime"), el("b", running.started_at ? { "data-started": running.started_at } : {},
        running.started_at ? uptime(running.started_at) : "-")),
    );
  } else {
    card.replaceChildren(el("p", { class: "muted note" }, busy ? "Starting the proxy…"
      : "Off. The proxy gives the frontend one port for every service: what runs here answers locally, the rest goes to the remote API."));
  }
  if (job && job.error) {
    card.append(el("p", { class: "error" }, `${job.action} failed: ${job.error}`));
    card.append(el("div", {},
      button("Log", () => showLogs("proxy")),
      button("Dismiss", () => act(`/api/instances/${encodeURIComponent(job.key)}/dismiss`), { class: "btn small ghost" }),
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
    class: "jump", tabindex: "0", title: `Open the log of ${req.target}`,
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
  $("req-count").textContent = total ? `${shown} of ${total}` : "";
  const running = state && state.proxy;
  let note = "";
  if (running && !running.background) {
    note = `This proxy runs in a terminal (pid ${running.pid || "?"}): its requests show there. Stop it and start it here, or with pdms proxy -b, to follow them.`;
  } else if (!running) {
    note = total ? "The proxy is off: these are the requests of its last run." : "The proxy is off.";
  } else if (!total) {
    note = `No requests yet. Point the frontend to http://localhost:${running.port}.`;
  } else if (!shown) {
    note = "No request matches the filter.";
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
  if (!proxyView.routes) $("route-count").textContent = "Reading the routes from Terraform…";
  try {
    const response = await fetch("/api/proxy/routes");
    const data = await response.json();
    if (!response.ok) {
      proxyView.routes = null;
      $("route-rows").replaceChildren();
      $("route-count").textContent = "";
      $("route-empty").textContent = data.error || `pdms ui answered ${response.status}`;
      $("route-empty").hidden = false;
      return;
    }
    proxyView.routes = data;
  } catch {
    toast("pdms ui is not reachable: is it still running?");
    return;
  }
  paintRoutes();
}

function routeTarget(route) {
  if (route.target === "local") {
    return el("td", {}, button(route.key, () => showLogs(route.key), { class: "btn tiny link", title: `Logs of ${route.key}` }));
  }
  if (route.key) {
    return el("td", { class: "target-other", title: route.note }, `${route.target === "remote" ? "remote" : "not available"} (${route.key} in another repo)`);
  }
  return route.target === "remote" ? el("td", { class: "target-remote" }, "remote") : el("td", { class: "target-missing" }, "not available");
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
  $("route-count").textContent = `${shown.length} of ${data.routes.length} routes · ${local} local · ${data.env}`
    + (data.remote ? "" : " · no remote API");
  $("route-empty").textContent = data.routes.length ? "No route matches the filter." : `No routes in the Terraform of '${data.env}'.`;
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
    if (!response.ok) { toast(found.error || `pdms ui answered ${response.status}`); return; }
  } catch {
    toast("pdms ui is not reachable: is it still running?");
    return;
  }
  $("proxy-port").value = found.port;
  options($("proxy-env"), found.envs.length ? found.envs : [found.env], found.env);
  $("proxy-remote").value = found.remote;
  $("proxy-no-remote").checked = false;
  $("proxy-remote").disabled = false;
  options($("proxy-as"), ["", ...state.users], "", (name) => name || "each service's own profile");
  $("proxy-frontend-label").hidden = !found.frontend;
  $("proxy-frontend").checked = true;
  $("proxy-warn").hidden = $("proxy-error").hidden = true;
  $("proxy-go").textContent = "Start";
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
      $("proxy-warn").textContent = `Port ${data.port} is in use. Start on ${data.free} instead?`;
      $("proxy-warn").hidden = false;
      $("proxy-go").textContent = `Start on ${data.free}`;
      return;
    }
    if (status === 409 && data.decision === "point_frontend") {
      $("proxy-frontend-label").hidden = false;
      $("proxy-warn").textContent = `Point the frontend to ${data.url}?`;
      $("proxy-warn").hidden = false;
      return;
    }
    proxyProblem(data.error || `pdms ui answered ${status}`);
  } catch {
    proxyProblem("pdms ui is not reachable: is it still running?");
  } finally {
    $("proxy-go").disabled = false;
  }
}

function resetProxyPort() {
  $("proxy-warn").hidden = true;
  $("proxy-go").textContent = "Start";
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
  $("events-summary").textContent = busy ? `${job.phase}…` : up ? `ElasticMQ running on :${state.events.port}` : "off";
  $("events-start").hidden = up || busy;
  $("events-stop").hidden = !up || busy;
  $("events-send").hidden = !up;

  const card = $("events-info");
  const data = eventsView.queues;
  if (up) {
    const consumers = state.instances.filter((i) => i.queue && i.status !== "stopped");
    const broker = data && data.broker ? consumers.find((i) => i.queue === data.broker) : null;
    const publishers = state.instances.filter((i) => i.events === "local" && i.status !== "stopped").length;
    card.replaceChildren(
      info("Endpoint", `http://localhost:${state.events.port}`),
      info("Broker", !data ? "…" : broker ? broker.key : data.broker ? "not running" : "not in the repo"),
      info("Consumers running", String(consumers.length)),
      info("Publishing locally", `${publishers} service${publishers === 1 ? "" : "s"}`),
      info("Last SNS publish", state.sns && state.sns.last_publish ? new Date(state.sns.last_publish).toLocaleTimeString() : "nothing yet"),
    );
  } else {
    card.replaceChildren(el("p", { class: "muted note" }, busy ? "Starting the local ElasticMQ…"
      : "Off: services publish to AWS. Start the local events to run a local ElasticMQ (Docker) with every queue of the repo and the broker; services started afterwards publish there and to a local SNS."));
  }
  if (job && job.error) {
    card.append(el("p", { class: "error" }, `${job.action === "up" ? "start" : "stop"} failed: ${job.error}`));
    const row = el("div", {});
    if (job.log_key) row.append(button("Install log", () => showLogs(job.log_key, "install")));
    row.append(button("Dismiss", () => act(eventsPath("dismiss")), { class: "btn small ghost" }));
    card.append(row);
  }
  for (const tab of $("events-tabs").children) tab.setAttribute("aria-selected", String(tab.dataset.tab === eventsView.tab));
  $("events-queues").hidden = eventsView.tab !== "queues";
  $("events-types").hidden = eventsView.tab !== "types";
  $("events-sns").hidden = eventsView.tab !== "sns";
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
    return response.ok ? { data } : { error: data.error || `pdms ui answered ${response.status}` };
  } catch {
    return { error: "pdms ui is not reachable: is it still running?" };
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

function queueRow(queue) {
  const consumer = queue.running
    ? button(queue.running, () => showLogs(queue.running), { class: "btn tiny link", title: `Logs of ${queue.running}` })
    : queue.sns ? el("span", { class: "muted" }, "local SNS: every publish")
      : queue.broker ? el("span", { class: "muted" }, `${queue.consumer || "broker"} (not running)`)
        : el("span", { class: "muted" }, queue.consumer || "-");
  const actions = el("td", { class: "row-actions" });
  if (queue.visible !== null) {
    actions.append(button("Messages", () => openPeek(queue.name)));
    if (!queue.sns) actions.append(button("Send", () => openSend(queue.name)));
    if (queue.visible || queue.in_flight) actions.append(button("Purge", () => purge([queue.name]), { class: "btn small bad" }));
  }
  const tags = [queue.fifo ? "fifo" : "", queue.broker ? "broker" : "", queue.source === "elasticmq.conf" ? "elasticmq.conf only" : ""].filter(Boolean);
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
  $("queue-count").textContent = `${shown.length} of ${data.queues.length} queues` + (data.up ? ` · ${waiting} message${waiting === 1 ? "" : "s"} waiting` : "");
  $("queue-purge-all").hidden = !data.up || !waiting;
  $("queue-empty").textContent = data.up ? "No queue matches the filter." : "";
  $("queue-empty").hidden = shown.length > 0 || !data.up;
}

async function purge(queues) {
  const what = queues.length ? queues.join(", ") : "every queue";
  if (!await confirmDialog(`Purge ${what}?`, "Every message waiting there is deleted; nothing consumes them.", "Purge")) return;
  act(eventsPath("purge"), { queues }, (data) => {
    toast(data.purged.length ? `Purged ${data.purged.join(", ")}.` : "Nothing to purge.", "info");
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
    const copy = button("Copy", async () => {
      try {
        await navigator.clipboard.writeText(text);
        toast("Copied.", "info");
      } catch {
        toast("The browser did not allow copying.");
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
  $("peek-note").textContent = "reading…";
  paintQueues();
  const { data, error } = await getJson(`${eventsPath("peek")}?${new URLSearchParams({ queue })}`);
  if (eventsView.peek !== queue) return;
  if (error) {
    $("peek-note").textContent = error;
    $("peek-messages").replaceChildren();
    return;
  }
  $("peek-note").textContent = data.messages.length
    ? `${data.messages.length} waiting${data.messages.length >= 50 ? " (first 50)" : ""} · read without consuming them`
    : "empty";
  $("peek-messages").replaceChildren(...data.messages.map((message) => {
    const kind = messageKind(message.body);
    return messageDetails(
      el("summary", {},
        el("span", { class: "mono muted" }, message.id.slice(0, 8)),
        kind ? el("span", { class: "topic" }, kind) : "",
        message.sent ? el("span", { class: "muted" }, new Date(message.sent).toLocaleString()) : "",
        el("span", { class: "muted" }, `received ${message.receives} time${message.receives === 1 ? "" : "s"}`),
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
    item.consumer ? el("td", { class: "mono" }, item.consumer) : el("td", { class: "target-missing" }, "none"),
    el("td", { class: "row-actions" }, state.events.up ? button("Send", () => openSend(item.type)) : ""),
  )));
  $("type-count").textContent = `${shown.length} of ${data.types.length} event types · broker ${data.broker || "not found"}`;
  $("type-empty").textContent = data.types.length ? "No event type matches the filter." : "The broker routes no event types.";
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
      el("span", { class: "muted" }, `from ${entry.service}`),
      entry.subject ? el("span", {}, entry.subject) : "",
      entry.group ? el("span", { class: "muted", title: "MessageGroupId" }, `group ${entry.group}`) : "",
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
    el("option", { value: "" }, `All topics (${eventsView.sns.length})`),
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
  $("sns-count").textContent = total ? `${shown.length < 300 ? shown.length : "latest 300"} of ${total} publishes` : "";
  $("sns-empty").textContent = !total
    ? state && state.events.up ? "Nothing was published to the local SNS yet. Services started with local events publish here." : "Nothing published locally yet. Start the local events, then the services that publish."
    : "No publish matches the filter.";
  $("sns-empty").hidden = shown.length > 0;
}

// ---- start, stop and send

function openEventsUp() {
  const broker = !eventsView.queues || eventsView.queues.broker_service;
  openLaunch({
    title: "Start", key: "local events",
    hint: "A local ElasticMQ (Docker) with every queue of the repo, like pdms events up. The broker runs as this user and database.",
    user: state.user, db: state.db, go: "Start", path: eventsPath("up"), broker,
    after: () => showEventsTab(eventsView.tab),
  });
}

async function stopEvents() {
  const consumers = state.instances.filter((i) => i.queue && i.status !== "stopped").map((i) => i.key);
  const text = `Its messages are lost${consumers.length ? `, and the consumers stop too: ${consumers.join(", ")}` : ""}. Services keep running, but what they publish now fails until it starts again.`;
  if (await confirmDialog("Stop the local events?", text, "Stop")) act(eventsPath("down"));
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
  $("send-body-label").textContent = isType ? "Event fields (JSON; event_id and type are added)" : "Message body (JSON)";
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
      $("send-error").textContent = data.error || `pdms ui answered ${status}`;
      $("send-error").hidden = false;
      return;
    }
    $("send").close();
    let note = `Sent ${data.id.slice(0, 8)} to ${data.queue}.`;
    if (data.routed_to) note += ` The broker routes it to ${data.routed_to} (consumer: ${data.consumer || "none"}).`;
    if (!data.consumed) note += ` Nothing consumes ${data.queue} right now: it waits there.`;
    toast(note, "info");
    loadQueues();
  } catch {
    $("send-error").textContent = "pdms ui is not reachable: is it still running?";
    $("send-error").hidden = false;
  } finally {
    $("send-go").disabled = false;
  }
}

// ---------------------------------------------------------------------------- views

const VIEWS = ["services", "stacks", "proxy", "events"];

function currentView() {
  const view = location.hash.slice(1);
  return VIEWS.includes(view) ? view : "services";
}

function route() {
  const view = currentView();
  for (const section of document.querySelectorAll(".view")) section.hidden = section.id !== `view-${view}`;
  for (const link of document.querySelectorAll(".side a[data-view]")) link.classList.toggle("on", link.dataset.view === view);
  if (!state) return;
  paintProxy();
  paintEvents();
  if (view === "events") showEventsTab(eventsView.tab);
}

// ---------------------------------------------------------------------------- wiring

$("logs-close").addEventListener("click", closeLogs);
$("logs-clear").addEventListener("click", clearLog);
for (const tab of $("logs-tabs").children) tab.addEventListener("click", () => openLogs(logs.key, tab.dataset.which));
$("clean").addEventListener("click", () => act("/api/clean", {}, (data) => toast(`Forgot ${data.forgotten.length} stopped.`, "info")));
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
$("restart-db").addEventListener("change", resetConfirmation);

fetch("/api/state").then((response) => response.json()).then(paint).finally(connect);
setInterval(tickUptimes, 1000);
