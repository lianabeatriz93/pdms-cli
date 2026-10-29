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
  if (status === "asleep") return t("asleep");
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

// A short message above the status bar: "info" (done: saved, copied…) goes by itself; an "error" stays longer, and
// both close with ✕. The newest goes at the bottom; a few at most.
const MAX_TOASTS = 4;

export function toast(message, kind = "error") {
  const close = () => {
    node.classList.add("leaving");
    setTimeout(() => node.remove(), 180);
  };
  const node = el("div", { class: `toast ${kind}`, role: kind === "error" ? "alert" : "status" },
    el("span", { class: "toast-icon" }, icon(kind === "error" ? "alert" : "check")),
    el("span", { class: "toast-text" }, message),
    el("button", { class: "toast-close", type: "button", "aria-label": t("Close"), title: t("Close"), onclick: close }, "✕"));
  $("toasts").append(node);
  while ($("toasts").children.length > MAX_TOASTS) $("toasts").firstChild.remove();
  let timer = setTimeout(close, kind === "error" ? 8000 : 3500);
  node.addEventListener("mouseenter", () => clearTimeout(timer)); // reading it: it waits
  node.addEventListener("mouseleave", () => { timer = setTimeout(close, 2000); });
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

// The rest of a row's actions behind a ⋯ button. entries() gives { icon, label, run } items (href instead of run for
// a link, bad for a destructive one) and "-" for a line between groups. One menu is open at a time; a click elsewhere,
// Esc, scrolling its button away or resizing closes it.
let shownMenu = null;

export function moreButton(label, entries) {
  return iconButton("more", label, (event) => toggleMenu(event.currentTarget, entries()), { "aria-haspopup": "menu", "aria-expanded": "false" });
}

function toggleMenu(anchor, entries) {
  const again = shownMenu && shownMenu.anchor === anchor;
  closeMenu();
  if (again) return;
  const items = entries.filter((entry, i) => entry !== "-" || (i > 0 && entries[i - 1] !== "-" && i < entries.length - 1));
  const menu = el("div", { class: "menu row-menu", role: "menu" }, ...items.map((entry) => {
    if (entry === "-") return el("hr");
    const inside = [icon(entry.icon), el("span", {}, entry.label)];
    if (entry.href) {
      return el("a", { role: "menuitem", href: entry.href, target: "_blank", rel: "noopener noreferrer", onclick: () => closeMenu() }, ...inside);
    }
    return el("button", { type: "button", role: "menuitem", class: entry.bad ? "bad" : "", onclick: () => { closeMenu(); entry.run(); } }, ...inside);
  }));
  document.body.append(menu);
  const box = anchor.getBoundingClientRect();
  const below = box.bottom + 4 + menu.offsetHeight <= window.innerHeight;
  menu.style.top = `${below ? box.bottom + 4 : Math.max(4, box.top - 4 - menu.offsetHeight)}px`;
  menu.style.right = `${Math.max(4, window.innerWidth - box.right)}px`;
  anchor.setAttribute("aria-expanded", "true");
  shownMenu = { menu, anchor, top: box.top };
  menu.querySelector("[role=menuitem]").focus();
}

export function closeMenu(refocus = false) {
  if (!shownMenu) return;
  const { menu, anchor } = shownMenu;
  shownMenu = null;
  menu.remove();
  anchor.setAttribute("aria-expanded", "false");
  if (refocus && anchor.isConnected) anchor.focus();
}

document.addEventListener("click", (event) => {
  if (shownMenu && !shownMenu.menu.contains(event.target) && !shownMenu.anchor.contains(event.target)) closeMenu();
});
document.addEventListener("keydown", (event) => {
  if (!shownMenu) return;
  if (event.key === "Escape") { event.preventDefault(); closeMenu(true); return; }
  if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
  event.preventDefault();
  const items = [...shownMenu.menu.querySelectorAll("[role=menuitem]")];
  const at = items.indexOf(document.activeElement);
  items[(at + (event.key === "ArrowDown" ? 1 : items.length - 1)) % items.length].focus();
});
// Only a scroll that moved its button closes it (not a late scroll event from bringing the button into view).
document.addEventListener("scroll", () => {
  if (shownMenu && Math.abs(shownMenu.anchor.getBoundingClientRect().top - shownMenu.top) > 1) closeMenu();
}, true);
window.addEventListener("resize", () => closeMenu());

// Grey rows the shape of a table while its data is on its way (as many cells as its header has).
export function skeletonRows(tbody, n = 3) {
  const head = tbody.closest("table").tHead;
  const cols = head ? head.rows[0].cells.length : 3;
  return Array.from({ length: n }, (_, row) => el("tr", { class: "sk-row", "aria-hidden": "true" },
    ...Array.from({ length: cols }, (_, col) => el("td", {}, el("span", { class: `sk w${(row + col) % 3}` })))));
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
  // Inside the dialog of the button that copies: with a modal open the rest of the page is inert, also a dialog
  // opened before it (the SQL of the views over Start), and a selection there copies nothing (an empty clipboard).
  const open = [...document.querySelectorAll("dialog[open]")];
  (focused && focused.closest && focused.closest("dialog[open]") || open[open.length - 1] || document.body).append(area);
  area.focus();
  area.select();
  let copied = false;
  try {
    // An area that could not take the focus (an inert part of the page) copies nothing, though it says it did.
    copied = document.activeElement === area && document.execCommand("copy");
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
