// Updates of pdms: the title bar chip, the release notes and Update and restart.

import { $, button, copyText, el, failedText, getJson, phaseLabel, post, toast } from "./core.js";
import { state } from "./state.js";
import { confirmDialog } from "./stacks.js";

export const UPDATE_JOB = "update";

// loaded: the version this page came with; notes: the release notes fetched for notesFor.
export const updateView = { loaded: null, notesFor: "", notes: null };

// Settings → Versions: versions as GitHub lists them (null while loading, a string for an error), the one whose notes
// show.
const versionsView = { versions: null, picked: "" };

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
  if ($("versions-dialog").open) {
    paintVersions(); // opened before the state said how this pdms is installed: its buttons follow it
    paintVersionsPhase();
  }
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
  if (info.kind !== "editable" && info.skipped) note += " " + t("{version} skipped.", { version: info.skipped });
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

// The Markdown of a release (headings, lists, paragraphs, ``` blocks), as nodes.
function notesNodes(text) {
  const nodes = [];
  let list = null;
  let block = null; // the lines of an open ``` block
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (line.startsWith("```")) {
      if (block) { nodes.push(el("pre", {}, block.join("\n"))); block = null; } else { block = []; list = null; }
      continue;
    }
    if (block) { if (line) block.push(line); continue; }
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
  $("update-skip").hidden = !updating || busy;
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

// Never offer this one again (a newer one, yes).
export async function skipOffered() {
  const offer = updateOffer();
  if (!offer) return;
  const { status, data } = await post("/api/update/skip", { version: offer.version });
  if (status >= 400) { toast(data.error || t("pdms ui answered {status}", { status })); return; }
  $("update-dialog").close();
  toast(t("pdms will not offer {version} again; a newer version, yes.", { version: offer.version }), "info");
}

export async function openVersions() {
  versionsView.versions = null;
  versionsView.picked = "";
  $("versions-error").hidden = true;
  paintVersions();
  if (!$("versions-dialog").open) $("versions-dialog").showModal();
  const { data, error } = await getJson("/api/update/versions");
  versionsView.versions = error ? t("Could not reach GitHub: {error}", { error }) : data.versions;
  paintVersions();
}

function paintVersions() {
  const box = $("versions-list");
  const versions = versionsView.versions;
  const checkout = Boolean(state.update) && state.update.kind === "editable";
  $("versions-command-hint").hidden = !state.update || state.update.updates_itself || checkout;
  $("versions-checkout-hint").hidden = !checkout;
  $("versions-intro").hidden = checkout;
  if (versions === null) { box.replaceChildren(el("p", { class: "muted" }, t("Loading the versions…"))); return; }
  if (typeof versions === "string") { box.replaceChildren(el("p", { class: "muted" }, versions)); return; }
  const current = state.update ? state.update.current : "";
  const canInstall = Boolean(state.update && state.update.updates_itself);
  box.replaceChildren(...versions.map((release) => {
    const tags = [];
    if (release.current) tags.push(el("span", { class: "tag current" }, t("you have it")));
    if (release.skipped) tags.push(el("span", { class: "tag skipped" }, t("skipped")));
    if (release.prerelease) tags.push(el("span", { class: "tag dev" }, "beta"));
    const older = !release.current && newerThan(current, release.version);
    const actions = [button(t("Notes"), () => pickVersion(release.version), { class: "btn small ghost" })];
    if (canInstall && !release.current) {
      actions.push(button(older ? t("Go back to it") : t("Install it"), () => installVersion(release.version, older), { class: "btn small" }));
    }
    return el("div", { class: `version-item${release.version === versionsView.picked ? " picked" : ""}` },
      el("span", { class: "mono" }, release.version), el("span", { class: "muted" }, release.date), ...tags,
      el("span", { class: "push" }, ...actions));
  }));
  const picked = versions.find((release) => release.version === versionsView.picked);
  $("versions-notes").replaceChildren(...(picked ? [
    el("h3", {}, `pdms ${picked.version} `, ...(picked.url ? [el("a", { href: picked.url, target: "_blank", rel: "noreferrer" }, t("on GitHub"))] : [])),
    ...(picked.body ? notesNodes(picked.body) : [el("p", { class: "muted" }, t("This release has no notes."))]),
  ] : []));
  paintVersionsPhase();
}

function pickVersion(version) {
  versionsView.picked = versionsView.picked === version ? "" : version;
  paintVersions();
}

// Like the server's comparison, for a.b.c with an optional a/b/rc suffix (an earlier version to go back to).
function versionKey(text) {
  const match = /^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$/.exec(text || "");
  if (!match) return [0, 0, 0, -1, 0];
  return [+match[1], +match[2], +match[3], match[4] ? { a: 0, b: 1, rc: 2 }[match[4]] : 3, +(match[5] || 0)];
}

function newerThan(a, b) {
  const [x, y] = [versionKey(a), versionKey(b)];
  for (let i = 0; i < x.length; i++) if (x[i] !== y[i]) return x[i] > y[i];
  return false;
}

async function installVersion(version, older) {
  const title = older ? t("Go back to pdms {version}?", { version }) : t("Install pdms {version}?", { version });
  const text = older
    ? t("pdms ui installs it and restarts with it; the services, the proxy and the frontend keep running. pdms will not offer {current} again, but a newer version, yes.", { current: state.update.current })
    : t("pdms ui installs it and restarts with it; the services, the proxy and the frontend keep running.");
  if (!(await confirmDialog(title, text, older ? t("Go back to it") : t("Install it")))) return;
  $("versions-error").hidden = true;
  const { status, data } = await post("/api/update/install", { version, restart_proxy: true });
  if (status >= 400) {
    $("versions-error").textContent = data.error || t("pdms ui answered {status}", { status });
    $("versions-error").hidden = false;
  }
  paintVersionsPhase();
}

function paintVersionsPhase() {
  const job = state.jobs[UPDATE_JOB];
  const busy = Boolean(job && !job.error);
  $("versions-phase").hidden = !busy;
  $("versions-phase").textContent = busy ? phaseLabel(job.phase) : "";
  if (job && job.error) {
    $("versions-error").textContent = failedText(job);
    $("versions-error").hidden = false;
  }
  for (const action of $("versions-list").querySelectorAll(".push .btn:not(.ghost)")) action.disabled = busy;
}
