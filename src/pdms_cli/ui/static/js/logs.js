// The log dock, under every screen: one or more logs followed live and interleaved as their lines come (each source
// with its colour), filtered by level, text or request (the #id services add to what they log for a request). With
// one log, its previous run, install and build are a tab away. Hide keeps it following with only its bar showing.

import { $, MAX_LOG_LINES, el, phaseLabel } from "./core.js";
import { state } from "./state.js";
import { paintServices } from "./services.js";

const COLOURS = 6;
const LEVELS = {
  all: null,
  warn: /\b(WARN(ING)?|ERROR|CRITICAL|FATAL|Traceback|Exception|FAILED)\b/,
  error: /\b(ERROR|CRITICAL|FATAL|Traceback|Exception|FAILED)\b/,
};

// key / which: the log when there is just one (what other screens read); find: lines to mark (a request clicked in
// Requests), seek: scroll to the last of them once it arrives; request: only the lines of that request.
export const logs = {
  key: null, which: "current", sources: [], lines: [], find: null, seek: false, level: "all", text: "", request: "",
  hidden: false,
};

// The time a line starts with (15:42:07, 2026-10-03T15:42:07 or 2026-10-03 15:42:07,123), as seconds of the day.
const TIME = /^\[?(?:\d{4}-\d\d-\d\d[T ])?(\d\d):(\d\d):(\d\d)/;

function timeOf(text) {
  const match = TIME.exec(text);
  return match ? Number(match[1]) * 3600 + Number(match[2]) * 60 + Number(match[3]) : null;
}

export function nearBottom(node) {
  return node.scrollHeight - node.scrollTop - node.clientHeight < 40;
}

function shown(line) {
  if (logs.request && !line.text.includes(`#${logs.request}`)) return false;
  const level = LEVELS[logs.level];
  if (level && !level.test(line.text)) return false;
  return !logs.text || line.text.toLowerCase().includes(logs.text);
}

function lineNode(line) {
  const parts = [];
  if (logs.sources.length > 1) parts.push(el("span", { class: `src c${line.colour}` }, `${line.source} `));
  parts.push(logs.find && logs.find.match(line.text) ? el("mark", {}, line.text) : line.text, "\n");
  return parts;
}

function render(lines) {
  const out = document.createDocumentFragment();
  for (const line of lines) if (shown(line)) out.append(...lineNode(line));
  return out;
}

export function repaintLog() {
  const pre = $("logs-text");
  pre.replaceChildren(render(logs.lines));
  pre.scrollTop = pre.scrollHeight;
  paintDockNote();
}

function appendLog(source, lines, tail = false) {
  if (!lines.length) return;
  const pre = $("logs-text");
  const follow = nearBottom(pre);
  const fresh = lines.map((text) => {
    const at = timeOf(text);
    if (at !== null) source.at = at; // a line without a time (a traceback) goes with the one before it
    return { source: source.key, colour: source.colour, text, at: source.at ?? null };
  });
  logs.lines.push(...fresh);
  if (tail && logs.sources.length > 1) {
    // A log's tail comes in one batch: put it among the others' lines by time (stable: same second keeps order)
    logs.lines = logs.lines.map((line, i) => [line, i])
      .sort(([a, i], [b, j]) => (a.at !== null && b.at !== null && a.at !== b.at ? a.at - b.at : i - j))
      .map(([line]) => line);
    pre.replaceChildren(render(logs.lines));
    pre.scrollTop = pre.scrollHeight;
    paintDockNote();
    return;
  }
  if (logs.lines.length > MAX_LOG_LINES * 1.2) {
    logs.lines = logs.lines.slice(-MAX_LOG_LINES);
    pre.replaceChildren(render(logs.lines));
  } else {
    pre.append(render(fresh));
  }
  paintDockNote();
  if (logs.seek && seekMark(pre)) return;
  if (follow) pre.scrollTop = pre.scrollHeight;
}

function seekMark(pre) {
  const marks = pre.querySelectorAll("mark");
  if (!marks.length) return false;
  logs.seek = false;
  pre.scrollTop = marks[marks.length - 1].offsetTop - pre.clientHeight / 2;
  return true;
}

export function clearLog() {
  logs.lines = [];
  $("logs-text").textContent = "";
}

function follow(source, lines) {
  const query = new URLSearchParams({ key: source.key, which: source.which, lines });
  const stream = source.stream = new EventSource(`/api/logs/stream?${query}`);
  let first = true;
  let tail = true; // the first batch after (re)connecting is the tail of the file
  stream.onopen = () => { // a reconnect starts again with the tail: drop what this source had
    if (!first) logs.lines = logs.lines.filter((line) => line.source !== source.key);
    first = false;
    tail = true;
    repaintLog();
    logs.seek = Boolean(logs.find);
  };
  stream.addEventListener("lines", (event) => { appendLog(source, JSON.parse(event.data), tail); tail = false; });
  stream.addEventListener("reset", () => appendLog(source, ["", t("──── restarted ────"), ""]));
  stream.onerror = () => {
    if (stream.readyState === EventSource.CLOSED) appendLog(source, [t("(no log here)")]);
  };
}

function freeColour() {
  const used = new Set(logs.sources.map((source) => source.colour));
  for (let colour = 0; colour < COLOURS; colour += 1) if (!used.has(colour)) return colour;
  return logs.sources.length % COLOURS;
}

function stopAll() {
  for (const source of logs.sources) if (source.stream) source.stream.close();
  logs.sources = [];
}

function syncSingle() {
  const only = logs.sources.length === 1 ? logs.sources[0] : null;
  logs.key = only ? only.key : null;
  logs.which = only ? only.which : "current";
}

// Opens the dock with one log (what every Logs button does), or with ``options.add`` adds it to the ones it shows.
export function openLogs(key, which = "current", find = null, options = {}) {
  if (!options.add) stopAll();
  else if (logs.sources.some((source) => source.key === key)) return;
  Object.assign(logs, { find, seek: Boolean(find), hidden: false });
  if (!options.keepRequest) logs.request = options.request || "";
  if (!options.add) clearLog();
  const source = { key, which, colour: options.add ? freeColour() : 0, stream: null };
  logs.sources.push(source);
  syncSingle();
  $("logs").hidden = false;
  $("logs").classList.remove("folded");
  follow(source, find || logs.request ? 2000 : 200);
  paintDock();
  if (state) paintServices();
}

// Several logs at once, e.g. what a request went through: the proxy, its service and the consumers of its events.
export function openRequestLogs(keys, request) {
  stopAll();
  clearLog();
  Object.assign(logs, { find: null, seek: false, request, hidden: false });
  for (const key of keys) {
    const source = { key, which: "current", colour: freeColour(), stream: null };
    logs.sources.push(source);
    follow(source, 2000);
  }
  syncSingle();
  $("logs").hidden = false;
  $("logs").classList.remove("folded");
  paintDock();
  if (state) paintServices();
}

export function removeLogSource(key) {
  const source = logs.sources.find((item) => item.key === key);
  if (!source) return;
  if (source.stream) source.stream.close();
  logs.sources = logs.sources.filter((item) => item !== source);
  logs.lines = logs.lines.filter((line) => line.source !== key);
  if (!logs.sources.length) { closeLogs(); return; }
  syncSingle();
  repaintLog();
  paintDock();
  if (state) paintServices();
}

export function closeLogs() {
  stopAll();
  Object.assign(logs, { key: null, find: null, request: "", lines: [] });
  $("logs").hidden = true;
  if (state) paintServices();
}

export function toggleDock() {
  logs.hidden = !logs.hidden;
  $("logs").classList.toggle("folded", logs.hidden);
  $("logs-hide").textContent = logs.hidden ? t("Show") : t("Hide");
  if (!logs.hidden) $("logs-text").scrollTop = $("logs-text").scrollHeight;
}

// Every Logs button: the dock shows on the screen it was pressed on.
export function showLogs(key, which = "current", find = null) {
  openLogs(key, which, find);
}

// What can be added: running instances, the proxy, the local SNS and the frontend.
function addable() {
  if (!state) return [];
  const keys = state.instances.filter((inst) => inst.status !== "stopped").map((inst) => inst.key);
  if (state.proxy) keys.push("proxy");
  if (state.events && state.events.up) keys.push("sns");
  if (state.frontend && state.frontend.running) keys.push("frontend");
  return keys.filter((key) => !logs.sources.some((source) => source.key === key));
}

function paintDockNote() {
  const total = logs.lines.length;
  const visible = logs.lines.filter(shown).length;
  const job = logs.key && state && state.jobs[logs.key];
  $("logs-note").textContent = job && !job.error ? phaseLabel(job.phase)
    : logs.find ? t("marked: {request}", { request: logs.find.label })
      : visible < total ? t("{shown} of {total} lines", { shown: visible, total }) : "";
}

export function paintDock() {
  if ($("logs").hidden) return;
  const single = logs.sources.length === 1 ? logs.sources[0] : null;
  const chips = logs.sources.map((source) => el("span", { class: `dock-src c${source.colour}` },
    el("i", { "aria-hidden": "true" }), source.key,
    logs.sources.length > 1 ? el("button", { type: "button", class: "dock-x", title: t("Stop following {key}", { key: source.key }),
      "aria-label": t("Stop following {key}", { key: source.key }), onclick: () => removeLogSource(source.key) }, "✕") : ""));
  $("logs-sources").replaceChildren(...chips);
  const add = $("logs-add");
  const options = addable();
  add.replaceChildren(el("option", { value: "" }, t("+ Add a log")), ...options.map((key) => el("option", { value: key }, key)));
  add.hidden = !options.length;
  const testRun = Boolean(single && single.key.startsWith("test:")); // a test run has one log, its install included
  const noInstall = single && (single.key.startsWith("proxy") || ["sns", "ui", "update"].includes(single.key));
  $("logs-tabs").hidden = !single;
  for (const tab of $("logs-tabs").children) {
    tab.setAttribute("aria-selected", String(Boolean(single) && tab.dataset.which === single.which));
    tab.hidden = (testRun && tab.dataset.which !== "current") || (tab.dataset.which === "install" && noInstall)
      || (tab.dataset.which === "build" && (!single || single.key !== "frontend"));
  }
  $("logs-request").hidden = !logs.request;
  $("logs-request-id").textContent = `#${logs.request}`;
  $("logs-level").value = logs.level;
  $("logs-hide").textContent = logs.hidden ? t("Show") : t("Hide");
  paintDockNote();
}

export function setLogFilter() {
  logs.text = $("logs-filter").value.trim().toLowerCase();
  logs.level = $("logs-level").value;
  repaintLog();
}

export function clearRequestFilter() {
  logs.request = "";
  repaintLog();
  paintDock();
}

export function switchLogTab(which) {
  if (logs.sources.length !== 1) return;
  openLogs(logs.sources[0].key, which, null, { keepRequest: true });
}

export function addLogSource(key) {
  if (key) openLogs(key, "current", null, { add: true, keepRequest: true });
}
