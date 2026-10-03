// Stacks: their cards, starting and stopping them, and the stack editor.

import { $, act, button, el, failedText, icon, phaseLabel, post, toast } from "./core.js";
import { state } from "./state.js";
import { showLogs } from "./logs.js";
import { dbLabel, fetchServices, openUp, options, runningNote, runningOn } from "./launch.js";

// What a stack without its own user or database shows.
export function ask() {
  return t("(ask when starting)");
}

export function stackPath(name) {
  return `/api/stacks/${encodeURIComponent(name)}`;
}

// The stack's services by domain (lead/lead-tp-list → lead), in the stack's order.
function stackFilterText() {
  return $("stack-filter").value.trim().toLowerCase();
}

function stackShown(stack) {
  const text = stackFilterText();
  if ($("stack-running").checked && !stack.services.some((svc) => svc.running.length)) return false;
  return !text || [stack.name, stack.user, stack.db, ...stack.services.map((svc) => svc.path)]
    .join(" ").toLowerCase().includes(text);
}

// Stopped stacks are folded to one line; these were opened by hand (running ones are always open).
const stacksView = { open: new Set() };

function stackChip(svc, text) {
  const cut = svc.path.lastIndexOf("/");
  const name = cut < 0 ? svc.path : svc.path.slice(cut + 1);
  const failing = svc.running.some((key) => (state.instances.find((i) => i.key === key) || {}).status === "error");
  const kind = ["svc", svc.running.length ? (failing ? "bad" : "on") : "", text && svc.path.toLowerCase().includes(text) ? "hit" : ""];
  return el("li", { class: kind.filter(Boolean).join(" "), title: svc.path },
    el("span", { class: "name" }, name),
    ...svc.running.map((key) => button(key.slice(key.indexOf("@") + 1), () => showLogs(key), {
      class: "btn tiny link", title: t("Logs of {key}", { key }),
    })));
}

function stackCard(stack) {
  const text = stackFilterText();
  const job = state.jobs[`stack:${stack.name}`];
  const busy = job && !job.error;
  const total = stack.services.length;
  const up = stack.services.filter((svc) => svc.running.length).length;
  const failing = stack.services.filter((svc) => svc.running.some((key) => (state.instances.find((i) => i.key === key) || {}).status === "error")).length;
  const status = busy
    ? el("span", { class: "st starting" }, phaseLabel(job.phase))
    : el("span", { class: `st ${failing ? "fail" : up === total ? "ok" : up ? "starting" : "off"}` },
      failing ? t("{up}/{total} · {n} failing", { up, total, n: failing })
        : up === total ? t("running") : up ? t("{up} of {total} running", { up, total }) : t("stopped"));
  const open = up > 0 || busy || Boolean(text) || stacksView.open.has(stack.name);
  const toggle = el("button", {
    class: "fold", type: "button", "aria-expanded": String(open), title: open ? t("Fold") : t("Show its services"),
    onclick: () => { if (stacksView.open.has(stack.name)) stacksView.open.delete(stack.name); else stacksView.open.add(stack.name); paintStacks(); },
  }, icon("chevron"));
  if (up > 0 || busy) toggle.disabled = true;

  const actions = el("div", { class: "stack-actions" });
  if (job && job.log_key) actions.append(button(t("Install log"), () => showLogs(job.log_key, "install")));
  if (job && job.error) actions.append(button(t("Dismiss"), () => act(`${stackPath(stack.name)}/dismiss`)));
  if (!busy) {
    if (up < total) actions.append(button(t("Start"), () => openUp(stack), { class: "btn small primary" }));
    if (up) actions.append(button(t("Stop"), () => act(`${stackPath(stack.name)}/down`), { class: "btn small bad" }));
    actions.append(button(t("Edit"), () => openEditor(stack)));
    actions.append(button(t("Delete"), () => removeStack(stack), { class: "btn small ghost" }));
  }
  const card = el("article", { class: `card stack${open ? "" : " folded"}` },
    el("header", {}, toggle, el("h2", { class: "mono" }, stack.name), status, actions),
    el("p", { class: "muted meta" }, t("user {user} · db {db}", { user: stack.user || ask(), db: stack.db || ask() })),
  );
  if (open) card.append(el("ul", { class: "svc-chips" }, ...stack.services.map((svc) => stackChip(svc, text))));
  else {
    const names = stack.services.map((svc) => svc.path.slice(svc.path.lastIndexOf("/") + 1));
    card.append(el("p", { class: "folded-list mono" }, names.slice(0, 3).join(", ")
      + (names.length > 3 ? ` ${t("+{n} more", { n: names.length - 3 })}` : "")));
  }
  if (job && job.error) card.append(el("p", { class: "error" }, failedText(job)));
  return card;
}

export function paintStacks() {
  const shown = state.stacks.filter(stackShown);
  $("stacks").replaceChildren(...shown.map(stackCard));
  $("stacks-empty").hidden = state.stacks.length > 0;
  $("stacks-none").hidden = !state.stacks.length || shown.length > 0;
  $("stack-shown").textContent = state.stacks.length ? t("{shown} of {total}", { shown: shown.length, total: state.stacks.length }) : "";
  const running = state.stacks.filter((stack) => stack.services.some((svc) => svc.running.length)).length;
  $("stack-count").textContent = state.stacks.length || "";
  const stacks = state.stacks.length;
  $("stack-summary").textContent = [
    stacks === 1 ? t("{n} stack", { n: stacks }) : t("{n} stacks", { n: stacks }), t("{n} running", { n: running }),
  ].join(" · ");
}

export function confirmDialog(title, text, yes) {
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
  if (await confirmDialog(t("Delete stack {name}?", { name: stack.name }), t("Only the stack goes; its services keep running if they are."), t("Delete"))) {
    act(`${stackPath(stack.name)}/remove`, {}, () => toast(t("Stack '{name}' deleted.", { name: stack.name }), "info"));
  }
}

const editor = { name: null, order: [], picked: new Set() };

export function slashes(path) {
  return path.replaceAll("\\", "/").replace(/\/+$/, "");
}

export async function openEditor(stack = null) {
  const found = await fetchServices();
  if (!found) return;
  const ports = runningOn(slashes(found.root));
  // Like pdms stack edit: the stack's services first, then the running ones, then the rest.
  const current = stack ? stack.services.map((svc) => svc.path) : [];
  const rest = found.services.filter((svc) => !current.includes(svc));
  editor.order = [...current, ...rest.filter((svc) => ports[svc]), ...rest.filter((svc) => !ports[svc])];
  editor.picked = new Set(current);
  editor.name = stack ? stack.name : null;

  $("editor-title").textContent = stack ? t("Edit {name}", { name: stack.name }) : t("New stack");
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
  options($("editor-user"), ["", ...state.users], stack ? stack.user : "", (name) => name || ask());
  options($("editor-db"), ["", ...state.dbs.map((item) => item.name)], stack ? stack.db : "", (name) => name ? dbLabel(name) : ask());
  $("editor-error").hidden = true;
  editorCount();
  $("editor").showModal();
  (stack ? $("editor-filter") : $("editor-name")).focus();
}

function editorCount() {
  $("editor-count").textContent = t("{picked} of {total} selected", { picked: editor.picked.size, total: editor.order.length });
}

export function filterEditor() {
  const text = $("editor-filter").value.trim().toLowerCase();
  for (const item of $("editor-services").children) item.hidden = Boolean(text) && !item.dataset.svc.includes(text);
}

export async function saveEditor(event) {
  event.preventDefault();
  const name = editor.name || $("editor-name").value.trim();
  const body = {
    services: editor.order.filter((svc) => editor.picked.has(svc)),
    user: $("editor-user").value, db: $("editor-db").value, new: !editor.name,
  };
  if (!body.services.length) {
    $("editor-error").textContent = t("A stack needs at least one service.");
    $("editor-error").hidden = false;
    return;
  }
  $("editor-save").disabled = true;
  try {
    const { status, data } = await post(`${stackPath(name)}/save`, body);
    if (status === 200) {
      $("editor").close();
      toast(t("Stack '{name}' saved.", { name }), "info");
      return;
    }
    $("editor-error").textContent = data.error || t("pdms ui answered {status}", { status });
    $("editor-error").hidden = false;
  } catch {
    $("editor-error").textContent = t("pdms ui is not reachable: is it still running?");
    $("editor-error").hidden = false;
  } finally {
    $("editor-save").disabled = false;
  }
}
