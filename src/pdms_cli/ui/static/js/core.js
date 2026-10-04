// Small helpers every screen uses: building elements, icons, calling the server, toasts, short paths.

import { state } from "./state.js";

export const $ = (id) => document.getElementById(id);

export const MAX_LOG_LINES = 5000;

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    // The page's CSP refuses style attributes; set through the CSSOM, which it allows (a meter's width).
    else if (key === "style") node.style.cssText = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) node.append(child);
  return node;
}

// A moment with its date and time, in the browser's locale (03/10/2026, 20:15:32).
export function dateTime(value) {
  return new Date(value).toLocaleString([], {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  });
}

export function uptime(startedAt) {
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(startedAt).getTime()) / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours ? `${hours}h${String(minutes).padStart(2, "0")}m` : `${minutes}m${String(seconds % 60).padStart(2, "0")}s`;
}

// A job's phase as the server sends it ("installing", "starting ElasticMQ", "stopping lead-tp-list"), translated.
export function phaseLabel(phase) {
  const text = String(phase || "");
  const cut = text.indexOf(" ");
  const verb = cut < 0 ? text : text.slice(0, cut);
  const what = cut < 0 ? "" : text.slice(cut + 1);
  if (verb === "stopping") return what ? t("stopping {what}…", { what }) : t("stopping…");
  if (verb === "installing") return what ? t("installing {what}…", { what }) : t("installing…");
  if (verb === "starting") return what ? t("starting {what}…", { what }) : t("starting…");
  if (verb === "building") return what ? t("building {what}…", { what }) : t("building…");
  if (verb === "restarting") return t("restarting…");
  if (verb === "copying") return t("copying… (pg_dump, migrations)");
  if (verb === "downloading") return what ? t("downloading {what}…", { what }) : t("downloading…");
  if (verb === "waiting") return t("waiting for its turn…");
  return text;
}

// An instance's status (also its CSS class), translated.
export function statusLabel(status) {
  if (status === "ok") return t("ok");
  if (status === "busy") return t("busy");
  if (status === "starting") return t("starting");
  if (status === "error") return t("error");
  if (status === "stopped") return t("stopped");
  if (status === "off") return t("off");
  if (status === "outside") return t("outside pdms");
  return status;
}

// Why a job failed, by its action (start, stop, restart, up, down).
export function failedText(job) {
  const error = job.error;
  if (job.action === "start" || job.action === "up") return t("Start failed: {error}", { error });
  if (job.action === "stop" || job.action === "down") return t("Stop failed: {error}", { error });
  if (job.action === "restart") return t("Restart failed: {error}", { error });
  if (job.action === "update") return t("Update failed: {error}", { error });
  if (job.action === "move") return t("Moving to the new repo failed: {error}", { error });
  return t("{action} failed: {error}", { action: job.action, error });
}

export function toast(message, kind = "error") {
  const node = el("div", { class: `toast ${kind}`, role: "status" }, message);
  $("toasts").append(node);
  setTimeout(() => node.remove(), kind === "error" ? 8000 : 4000);
}

export async function post(path, body = {}) {
  const response = await fetch(path, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  let data = {};
  try { data = await response.json(); } catch { /* an empty or plain-text answer */ }
  return { status: response.status, data };
}

export async function act(path, body, done) {
  try {
    const { status, data } = await post(path, body);
    if (status >= 400) toast(data.error || t("pdms ui answered {status}", { status }));
    else if (done) done(data);
  } catch {
    toast(t("pdms ui is not reachable: is it still running?"));
  }
}

export function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "i");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

// A row action as an icon: its name shows on hover and is what screen readers say.
export function iconButton(name, label, onclick, attrs = {}) {
  return el("button", { class: "ibtn", type: "button", title: label, "aria-label": label, onclick, ...attrs }, icon(name));
}

export function iconLink(name, label, href) {
  return el("a", { class: "ibtn", href, target: "_blank", rel: "noopener noreferrer", title: label, "aria-label": label }, icon(name));
}

export function button(label, onclick, attrs = {}) {
  return el("button", { class: "btn small", type: "button", onclick, ...attrs }, label);
}

// A path as people read it: ~ for their home folder and, when still long, … in the middle (the full one goes in a title).
export function shortPath(path, max = 44) {
  let text = String(path || "");
  const home = state && state.home;
  if (home && (text === home || text.startsWith(home + "/") || text.startsWith(home + "\\"))) text = "~" + text.slice(home.length);
  if (text.length <= max) return text;
  const keep = max - 1;
  return text.slice(0, Math.ceil(keep * 0.4)) + "…" + text.slice(text.length - Math.floor(keep * 0.6));
}

// Every path inside a text (a check's detail, a hint) shortened the same way.
// A path inside the current repo from its root (backend/lead/x/main.py); others shortened.
export function repoPath(path) {
  const root = state.repo && state.repo.root;
  for (const sep of ["/", "\\"]) if (root && path.startsWith(root + sep)) return path.slice(root.length + 1);
  return shortPath(path, 80);
}

export function shortPaths(text) {
  return String(text || "").replace(/(?:[A-Za-z]:\\|\/)[^\s·,()]+/g, (path) => shortPath(path));
}

export async function getJson(path) {
  try {
    const response = await fetch(path);
    const data = await response.json();
    return response.ok ? { data } : { error: data.error || t("pdms ui answered {status}", { status: response.status }) };
  } catch {
    return { error: t("pdms ui is not reachable: is it still running?") };
  }
}

// The old copy command first, allowed during the click: in pdms ui --window on Linux (Qt WebEngine) the clipboard
// API asks for a permission that pywebview's handler fails to answer, and its promise never settles.
export async function copyText(text, done) {
  if (!copyByCommand(text)) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      toast(t("The browser did not allow copying."));
      return;
    }
  }
  toast(done, "info");
}

function copyByCommand(text) {
  const area = el("textarea", { readonly: "", style: "position:fixed;top:0;left:0;opacity:0" });
  area.value = text;
  const focused = document.activeElement;
  document.body.append(area);
  area.select();
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch {
    copied = false;
  }
  area.remove();
  if (focused && focused.focus) focused.focus();
  return copied;
}

// An action that needs Docker images pdms does not have yet: say which and how big, and download them only if the
// user agrees; the server goes on with the action (``then``) once they are there.
export async function postNeedingImages(path, body, then, done) {
  try {
    const { status, data } = await post(path, body);
    if (status === 409 && data.decision === "images_missing") {
      const { confirmDialog } = await import("./stacks.js");
      const list = data.images.map((image) => `${image.name} (~${image.download_mb} MB): ${image.use}`).join("\n");
      if (!await confirmDialog(t("Download Docker images?"), `${data.error}\n\n${list}`, t("Download"))) return { status, data };
      act("/api/images/pull", { names: data.images.map((image) => image.name), then, ...body },
        () => toast(t("Downloading in the background; it goes on by itself when done."), "info"));
      return { status, data };
    }
    if (status >= 400) toast(data.error || t("pdms ui answered {status}", { status }));
    else if (done) done(data);
    return { status, data };
  } catch {
    toast(t("pdms ui is not reachable: is it still running?"));
    return { status: 0, data: {} };
  }
}
