// The dialog that starts, restarts and runs services and stacks (user, database, port, events).

import { $, el, post, toast } from "./core.js";
import { state } from "./state.js";
import { logs, openLogs } from "./logs.js";
import { slashes } from "./stacks.js";

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

export function options(select, names, current, label = (name) => name) {
  select.replaceChildren(...names.map((name) => el("option", name === current ? { value: name, selected: "" } : { value: name }, label(name))));
}

export function dbLabel(name) {
  const db = state.dbs.find((item) => item.name === name);
  return db && db.protected ? t("{name} (protected)", { name }) : name;
}

// The user, database and install of a restart or a stack's up, asking again before a protected database.
export function openLaunch({ title, key, hint, user, db, path, go, after, broker = false, run = false, service = "" }) {
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

export function openRestart(item) {
  openLaunch({
    title: t("Restart"), key: item.key, hint: t("Same port. Change the user or the database if you need to."),
    user: item.user, db: item.db, go: "restart", path: `/api/instances/${encodeURIComponent(item.key)}/restart`,
    after: (install) => { if (logs.key === item.key) openLogs(item.key, install === false ? "current" : logs.which); },
  });
}

// Ports of the running instances of each service, by its path in the repo (lead/lead-tp-list).
export function runningOn(root) {
  const ports = {};
  for (const inst of state.instances) {
    if (inst.status === "stopped") continue;
    const path = slashes(inst.service);
    if (path.startsWith(`${root}/`)) (ports[path.slice(root.length + 1)] ||= []).push(inst.port);
  }
  return ports;
}

export function runningNote(ports) {
  if (!ports) return "";
  const listening = ports.filter(Boolean);
  return el("span", { class: "muted" }, listening.length ? t("running on :{ports}", { ports: listening.join(", :") }) : t("running"));
}

export async function fetchServices() {
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

export async function openRun() {
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

export function servicesCount(n) {
  return n === 1 ? t("{n} service", { n }) : t("{n} services", { n });
}

export function filterRun() {
  const text = $("run-filter").value.trim().toLowerCase();
  let shown = 0;
  for (const item of $("run-services").children) {
    item.hidden = Boolean(text) && !item.dataset.svc.includes(text);
    if (!item.hidden) shown += 1;
  }
  const total = $("run-services").children.length;
  $("run-count").textContent = text ? t("{shown} of {total}", { shown, total }) : servicesCount(total);
}

export function openUp(stack) {
  openLaunch({
    title: t("Start"), key: stack.name, hint: t("Starts the services that are not running yet, each on a free port."),
    user: stack.user || state.user, db: stack.db || state.db, go: "start", path: `/api/stacks/${encodeURIComponent(stack.name)}/up`,
    after: (_install, data) => { if (!data.job) toast(t("The whole stack '{name}' is already running.", { name: stack.name }), "info"); },
  });
}

export async function submitLaunch(event) {
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

export function resetConfirmation() {
  launch.confirmed = false;
  $("restart-warn").hidden = true;
  $("restart-go").textContent = goLabel();
}
