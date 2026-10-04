// Updates of pdms: the title bar chip, the release notes and Update and restart.

import { $, copyText, el, failedText, getJson, phaseLabel, post, toast } from "./core.js";
import { state } from "./state.js";

export const UPDATE_JOB = "update";

// loaded: the version this page came with; notes: the release notes fetched for notesFor.
export const updateView = { loaded: null, notesFor: "", notes: null };

// What there is to offer: a newer release, or a version that pdms self-update installed and this pdms ui does not run.
export function updateOffer() {
  const info = state.update;
  if (!info) return null;
  if (info.installed && info.installed !== info.current) return { kind: "restart", version: info.installed };
  if (info.latest) return { kind: "update", version: info.latest };
  return null;
}

export function offerTitle(offer) {
  return offer.kind === "restart" ? t("pdms {version} is installed", { version: offer.version })
    : t("pdms {version} is available", { version: offer.version });
}

export function offerText(offer) {
  return offer.kind === "restart" ? t("This pdms ui still runs {current}.", { current: state.update.current })
    : t("You have {current}.", { current: state.update.current });
}

export function paintUpdate() {
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

export async function checkNow() {
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

export function openUpdate() {
  const offer = updateOffer();
  if (offer && offer.kind === "update" && updateView.notesFor !== offer.version) loadNotes(offer.version);
  paintUpdateDialog();
  if (!$("update-dialog").open) $("update-dialog").showModal();
}

export async function submitUpdate(event) {
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

export function copyCommand() {
  copyText(state.update.command, t("Copied."));
}
