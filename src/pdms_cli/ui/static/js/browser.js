// Settings → Browser: the extension that opens the frontend pdms runs when you go to the known front (browser.py).

import { $, act, button, copyText, el, post, toast } from "./core.js";
import { state } from "./state.js";

const browserView = { hosts: "", steps: null };

function hostsText(hosts) {
  return hosts.join(", ");
}

function statusCell(row) {
  if (row.connected) return el("td", {}, el("span", { class: "st ok" }, t("connected")));
  if (row.seen) {
    return el("td", {}, el("span", { class: "st off" }, t("installed")), " ",
      el("span", { class: "muted" }, t("last seen {when}", { when: new Date(row.seen).toLocaleString() })));
  }
  return el("td", {}, el("span", { class: "muted" }, t("not installed")));
}

function browserRow(row) {
  const actions = el("td", { class: "row-actions" });
  if (row.found) {
    actions.append(button(row.seen ? t("Install again") : t("Install"), () => install(row.key),
      row.seen ? {} : { class: "btn small primary" }));
  }
  return el("tr", {}, el("td", {}, row.name), statusCell(row), actions);
}

function paintSteps() {
  const steps = browserView.steps;
  $("browser-steps").hidden = !steps;
  if (!steps) return;
  $("browser-steps-title").textContent = steps.opened
    ? t("{name} opened its extensions page. There:", { name: steps.name })
    : t("Open {page} in {name}. There:", { name: steps.name, page: steps.page });
  const folder = el("li", {}, t("Click Load unpacked and choose this folder:"), " ",
    el("code", {}, steps.folder), " ",
    button(t("Copy"), () => copyText(steps.folder, t("Folder copied: paste it in the dialog (Ctrl+L on Linux).")), { class: "btn small ghost" }));
  const connected = (state.browser.browsers.find((row) => row.key === steps.key) || {}).connected;
  $("browser-steps-list").replaceChildren(
    el("li", {}, t("Turn on Developer mode (a switch on that page).")),
    folder,
    el("li", {}, connected ? t("Done: the extension is connected.") : t("This page says connected within half a minute.")),
  );
}

export function paintBrowser() {
  const info = state && state.browser;
  if (!info) return;
  $("browser-redirect").checked = info.redirect;
  const saved = hostsText(info.hosts);
  if (saved !== browserView.hosts) {
    if (document.activeElement !== $("browser-hosts") || $("browser-hosts").value === browserView.hosts) $("browser-hosts").value = saved;
    browserView.hosts = saved;
  }
  $("browser-hosts-save").disabled = $("browser-hosts").value.trim() === saved;
  $("browser-rows").replaceChildren(...info.browsers.map(browserRow));
  $("browser-empty").hidden = info.browsers.length > 0;
  paintSteps();
}

async function install(key) {
  await act("/api/browser/install", { browser: key }, (data) => {
    browserView.steps = { key, ...data };
    paintSteps();
    $("browser-steps").scrollIntoView({ block: "nearest" });
  });
}

export async function setRedirect(on) {
  await act("/api/browser/redirect", { on }, () => {
    const connected = state.browser.browsers.some((row) => row.connected);
    if (on && !connected) toast(t("Install the pdms extension in your browser: Settings → Browser."), "info");
    else toast(on ? t("The known front opens the local frontend within half a minute.") : t("The known front opens as always within half a minute."), "info");
  });
}

export async function saveHosts(event) {
  event.preventDefault();
  $("browser-hosts-error").hidden = true;
  const hosts = $("browser-hosts").value.split(/[\s,]+/).filter(Boolean);
  try {
    const { status, data } = await post("/api/browser/hosts", { hosts });
    if (status === 200) {
      $("browser-hosts").value = browserView.hosts = hostsText(data.hosts);
      toast(t("Known fronts saved."), "info");
      return;
    }
    $("browser-hosts-error").textContent = data.error || t("pdms ui answered {status}", { status });
    $("browser-hosts-error").hidden = false;
  } catch {
    toast(t("pdms ui is not reachable: is it still running?"));
  }
}

export function hostsEdited() {
  $("browser-hosts-save").disabled = $("browser-hosts").value.trim() === browserView.hosts;
}
