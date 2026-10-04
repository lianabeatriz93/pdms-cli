// Services: the background instances, grouped by stack, and the processes outside pdms.

import {
  $, act, button, dateTime, el, failedText, iconButton, iconLink, phaseLabel, repoPath, statusLabel, toast, uptime,
} from "./core.js";
import { state } from "./state.js";
import { logs, openLogs } from "./logs.js";
import { openRestart } from "./launch.js";
import { confirmDialog, runningInRepo } from "./stacks.js";
import { frontendActions, jobLog, staleText } from "./frontend.js";
import { HOME_JOB } from "./home.js";

// Services pdms started but lost track of (keys: null for every one).
export function adoptStrays(keys, after = null) {
  act("/api/strays/adopt", keys ? { keys } : {}, (data) => {
    const n = data.adopted.length;
    toast(n === 1 ? t("{key} adopted: pdms manages it again.", { key: data.adopted[0] }) : t("{n} services adopted: pdms manages them again.", { n }), "info");
    if (after) after();
  });
}

export async function stopStrays(keys) {
  const all = state.strays || [];
  const chosen = keys ? all.filter((s) => keys.includes(s.key)) : all;
  if (!chosen.length) return;
  const what = chosen.length === 1 ? chosen[0].key : t("{n} services", { n: chosen.length });
  if (!await confirmDialog(t("Stop {what}?", { what }), t("They stop with their reloader and workers, like pdms stop."), t("Stop"))) return;
  act("/api/strays/stop", keys ? { keys } : {}, (data) => toast(t("{n} stopped.", { n: data.stopped.length }), "info"));
}

function strayActions(cell, item) {
  cell.append(button(t("Adopt"), () => adoptStrays([item.key]), { title: t("Manage it again: logs, stop and restart") }));
  cell.append(iconButton("stop", t("Stop"), () => stopStrays([item.key]), { class: "ibtn bad" }));
  return cell;
}

function rowActions(item, job) {
  const cell = el("td", { class: "row-actions" });
  if (item.isStray) return strayActions(cell, item);
  const busy = job && !job.error;
  cell.append(iconButton("logs", t("Logs"), () => openLogs(item.key, busy ? jobLog(job) : "current")));
  if (item.isFrontend && !busy) return frontendActions(cell, item);
  if (busy || item.isSns) return cell;
  const alive = item.status !== "stopped";
  if (alive && !item.queue && !item.isProxy) cell.append(iconLink("open", t("Swagger (/docs)"), `http://localhost:${item.port}/docs`));
  if (alive && !item.isProxy) cell.append(iconButton("debug", t("Debug in VS Code"), () => debugInstance(item)));
  if (!item.isProxy && !item.placeholder) cell.append(iconButton("restart", t("Restart"), () => openRestart(item)));
  if (alive) {
    cell.append(iconButton("stop", t("Stop"), () => act(`/api/instances/${encodeURIComponent(item.key)}/stop`), { class: "ibtn bad" }));
  } else if (!item.placeholder) {
    cell.append(iconButton("forget", t("Forget"), () => act(`/api/instances/${encodeURIComponent(item.key)}/forget`)));
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
  if (item.detail) cell.append(el("span", { class: item.status === "busy" ? "detail quiet" : "detail" }, item.detail));
  if (item.note) cell.append(el("span", { class: "detail quiet" }, item.note));
  if (item.traceback && item.traceback.length) {
    const open = servicesView.traces.has(item.key);
    cell.append(el("button", {
      class: "link-btn", type: "button", "aria-expanded": String(open),
      onclick: () => { if (open) servicesView.traces.delete(item.key); else servicesView.traces.add(item.key); paintServices(); },
    }, open ? t("hide traceback") : t("show traceback")));
  }
  if (job && job.error) {
    cell.append(el("span", { class: "detail" }, failedText(job)));
    cell.append(button(t("Dismiss"), () => act(`/api/instances/${encodeURIComponent(item.key)}/dismiss`), { class: "btn tiny" }));
  }
  return cell;
}

// Hand a running instance over to VS Code: pdms stops it and writes its launch.json entry; F5 there starts it.
export async function debugInstance(item) {
  const yes = await confirmDialog(t("Debug {key} in VS Code?", { key: item.key }),
    t("pdms stops it and opens VS Code with a configuration for the same user, database and port, without --reload. In VS Code, press F5 to start it with breakpoints."),
    t("Stop and open VS Code"));
  if (!yes) return;
  act(`/api/instances/${encodeURIComponent(item.key)}/debug`, {}, (data) => {
    toast(t("In VS Code: Run and Debug → {name} → F5.", { name: data.name }), "info");
    if (data.backup) toast(t("launch.json had comments and they were lost; original copy at {backup}", { backup: data.backup }));
  });
}

// The latest traceback of an instance that failed to load: lines of files in a registered repo open VS Code there.
function tracebackRow(item) {
  const pre = el("pre", { class: "trace" });
  for (const line of item.traceback) {
    if (line.path) {
      const at = line.text.indexOf(line.path);
      pre.append(line.text.slice(0, at), el("a", {
        href: "#", title: t("Open in VS Code"),
        onclick: (event) => { event.preventDefault(); act("/api/code/open", { path: line.path, line: line.line }); },
      }, `${repoPath(line.path)}:${line.line}`), line.text.slice(at + line.path.length).replace(/^", line \d+/, '"'), "\n");
    } else {
      pre.append(el("span", EXCEPTION.test(line.text) ? { class: "exc" } : {}, line.text), "\n");
    }
  }
  return el("tr", { class: "trace-row error-row" }, el("td", { colspan: "9" }, pre));
}

function lastPublish(item) {
  return item.last_publish ? t("last {time}", { time: dateTime(item.last_publish) }) : t("nothing yet");
}

// Services answer requests in parallel by default: tell the ones that don't (started with --no-parallel, the setting
// off, or by an older pdms).
function serialTag(item) {
  if (item.parallel !== false || item.isSns || item.isStray || item.queue || item.status === "stopped") return "";
  return el("span", { class: "tag", title: t("A slow query holds up every other request of this service. Restart it to use the Requests in parallel setting.") },
    t("one request at a time"));
}

// A pdms instance (not the proxy, the local SNS, the frontend nor a process outside pdms) can be picked.
function selectable(item) {
  return !item.isProxy && !item.isSns && !item.isStray && !item.isFrontend && !item.placeholder;
}

function pickCell(item) {
  if (!selectable(item)) return el("td", { class: "pick" });
  const box = el("input", { type: "checkbox", "aria-label": t("Select {key}", { key: item.key }) });
  box.checked = servicesView.selected.has(item.key);
  box.addEventListener("change", () => {
    if (box.checked) servicesView.selected.add(item.key); else servicesView.selected.delete(item.key);
    paintServices();
  });
  return el("td", { class: "pick" }, box);
}

function row(item) {
  const job = state.jobs[item.key];
  const running = item.status !== "stopped" && item.started_at && !(job && !job.error);
  const uptimeCell = item.isSns
    ? el("td", { class: "num muted" }, lastPublish(item))
    : el("td", running ? { class: "num", "data-started": item.started_at } : { class: "num" }, running ? uptime(item.started_at) : "");
  const kind = [item.key === logs.key ? "picked" : "", item.isStray ? "outside-row" : "", item.status === "error" ? "error-row" : "",
    servicesView.selected.has(item.key) ? "chosen" : ""].filter(Boolean).join(" ");
  return el("tr", kind ? { class: kind } : {},
    pickCell(item),
    el("td", { class: "mono" }, item.label || item.key, serialTag(item)),
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
  if (servicesView.show === "problems" && !problem(item)) return false;
  if (servicesView.show === "outside" && !item.isStray) return false;
  return !text || [item.key, item.status, statusLabel(item.status), item.detail, item.url, item.repo, item.user, item.db]
    .join(" ").toLowerCase().includes(text);
}

// traces: keys whose traceback is open; selected: keys picked to restart or stop together
export const servicesView = { show: "all", traces: new Set(), selected: new Set() };

const EXCEPTION = /^[\w.]*(Error|Exception|Exit)\b/;

// The stack each instance belongs to (its first one), for the group rows of Services.
function stackOf(key) {
  const stack = state.stacks.find((item) => item.services.some((svc) => svc.running.includes(key)));
  return stack ? stack.name : "";
}

// Rows under a header per stack, then the rest, then what runs outside pdms.
function groupedRows(items) {
  const groups = new Map();
  for (const item of items) {
    const name = item.isStray ? "\u0002outside" : (!item.isProxy && !item.isSns && !item.isFrontend && stackOf(item.key)) || "\u0001other";
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(item);
  }
  const order = [...state.stacks.map((stack) => stack.name), "\u0001other", "\u0002outside"].filter((name) => groups.has(name));
  const rows = [];
  for (const name of order) {
    const label = name === "\u0001other" ? t("Other") : name === "\u0002outside" ? t("Outside pdms") : t("Stack {name}", { name });
    const head = el("td", { colspan: "8" }, label, el("span", { class: "muted" }, ` · ${groups.get(name).length}`));
    const pickable = groups.get(name).filter(selectable).map((item) => item.key);
    if (name !== "\u0002outside" && pickable.length > 1) {
      const all = pickable.every((key) => servicesView.selected.has(key));
      head.append(button(all ? t("Unselect them") : t("Select all {n}", { n: pickable.length }), () => {
        for (const key of pickable) if (all) servicesView.selected.delete(key); else servicesView.selected.add(key);
        paintServices();
      }, { class: "btn small ghost" }));
    }
    const extra = el("td", { class: "row-actions" });
    if (name === "\u0002outside" && groups.get(name).length > 1) extra.append(button(t("Adopt all"), () => adoptStrays(null)));
    rows.push(el("tr", { class: "group" }, head, extra));
    for (const item of groups.get(name)) {
      rows.push(row(item));
      if (item.traceback && item.traceback.length && servicesView.traces.has(item.key)) rows.push(tracebackRow(item));
    }
  }
  return rows;
}

export function paintServices() {
  const items = serviceItems();
  const pickable = new Set(items.filter(selectable).map((item) => item.key));
  for (const key of servicesView.selected) if (!pickable.has(key)) servicesView.selected.delete(key); // gone meanwhile
  const shown = items.filter(serviceShown);
  $("rows").replaceChildren(...groupedRows(shown));
  paintSelection(items.filter((item) => pickable.has(item.key)), shown.filter(selectable));
  const counts = { all: items.length, problems: items.filter(problem).length, outside: items.filter((item) => item.isStray).length };
  for (const node of document.querySelectorAll("#svc-seg button")) {
    node.querySelector("span").textContent = String(counts[node.dataset.show]);
    node.setAttribute("aria-pressed", String(node.dataset.show === servicesView.show));
    node.hidden = node.dataset.show === "outside" && !counts.outside && servicesView.show !== "outside";
  }
  $("empty").hidden = items.length > 0;
  $("svc-none").hidden = !items.length || shown.length > 0;
  $("svc-count").textContent = items.length ? t("{shown} of {total}", { shown: shown.length, total: items.length }) : "";
  const alive = state.instances.filter((i) => i.status !== "stopped");
  const failing = state.instances.filter((i) => i.status === "error" || i.status === "stopped");
  $("count").textContent = alive.length || "";
  const broken = failing.length + (state.strays || []).length;
  $("svc-badge").hidden = !broken;
  $("svc-badge").textContent = String(broken);
  $("svc-badge").title = t("{n} failing", { n: failing.length }) + ((state.strays || []).length ? ` · ${t("{n} outside pdms", { n: state.strays.length })}` : "");
  const parts = [t("{n} running", { n: alive.length }), t("{n} failing", { n: failing.length })];
  if ((state.strays || []).length) parts.push(t("{n} outside pdms", { n: state.strays.length }));
  $("summary").textContent = parts.join(" · ");
  $("clean").hidden = !state.instances.some((i) => i.status === "stopped");
  $("svc-save-stack").hidden = !runningInRepo();
  $("adopt-all").hidden = !(state.strays || []).length;
  $("front-new").hidden = !state.frontend || state.frontend.running || Boolean(state.jobs.frontend && !state.jobs.frontend.error);
}

export function tickUptimes() {
  for (const cell of document.querySelectorAll("[data-started]")) cell.textContent = uptime(cell.dataset.started);
}

// The bar of what is picked, and the header's box that picks every shown service.
function paintSelection(pickable, shownPickable) {
  const picked = pickable.filter((item) => servicesView.selected.has(item.key));
  $("svc-selbar").hidden = !picked.length;
  $("svc-sel-count").textContent = picked.length === 1 ? t("1 selected") : t("{n} selected", { n: picked.length });
  $("svc-sel-stop").textContent = t("Stop {n}", { n: picked.length });
  $("svc-sel-restart").textContent = t("Restart {n}…", { n: picked.length });
  const all = $("svc-all");
  all.checked = shownPickable.length > 0 && shownPickable.every((item) => servicesView.selected.has(item.key));
  all.indeterminate = !all.checked && shownPickable.some((item) => servicesView.selected.has(item.key));
  all.disabled = !shownPickable.length;
}

export function pickedItems() {
  return serviceItems().filter((item) => selectable(item) && servicesView.selected.has(item.key));
}

export function pickAllShown(on) {
  for (const item of serviceItems().filter(serviceShown).filter(selectable)) {
    if (on) servicesView.selected.add(item.key); else servicesView.selected.delete(item.key);
  }
  paintServices();
}

export function stopPicked() {
  const keys = pickedItems().map((item) => item.key);
  for (const key of keys) act(`/api/instances/${encodeURIComponent(key)}/stop`);
  servicesView.selected.clear();
  paintServices();
}
