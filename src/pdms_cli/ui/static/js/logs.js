// The logs pane: a service's log (also the previous run and the install) followed live.

import { $, MAX_LOG_LINES, el, phaseLabel } from "./core.js";
import { state } from "./state.js";
import { paintServices } from "./services.js";
import { route } from "./router.js";

// find: the lines to mark (the request clicked in the proxy), seek: scroll to the last of them once it arrives.
export const logs = { key: null, which: "current", stream: null, lines: [], find: null, seek: false };

export function nearBottom(node) {
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

export function clearLog() {
  logs.lines = [];
  $("logs-text").textContent = "";
}

export function paintLogTabs() {
  const noInstall = logs.key.startsWith("proxy") || ["sns", "ui", "update"].includes(logs.key);
  for (const tab of $("logs-tabs").children) {
    tab.setAttribute("aria-selected", String(tab.dataset.which === logs.which));
    tab.hidden = (tab.dataset.which === "install" && noInstall) || (tab.dataset.which === "build" && logs.key !== "frontend");
  }
  const job = state && state.jobs[logs.key];
  $("logs-note").textContent = job && !job.error ? phaseLabel(job.phase) : logs.find ? t("marked: {request}", { request: logs.find.label }) : "";
}

export function openLogs(key, which = "current", find = null) {
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

export function closeLogs() {
  if (logs.stream) logs.stream.close();
  Object.assign(logs, { key: null, stream: null, find: null });
  $("logs").hidden = true;
  paintServices();
}

export function showLogs(key, which = "current", find = null) {
  location.hash = "#services";
  route(); // now, not on hashchange: the logs scroll into view only once the view shows
  openLogs(key, which, find);
}
