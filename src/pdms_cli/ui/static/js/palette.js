// Ctrl K: search or run any action, and / to filter the current screen.

import { $, act, el, icon } from "./core.js";
import { state } from "./state.js";
import { adoptStrays, debugInstance } from "./services.js";
import { addAllLogs, showLogs } from "./logs.js";
import { openRestart, openRun, openUp } from "./launch.js";
import { stackPath } from "./stacks.js";
import { openProxyStart, showProxyTab } from "./proxy.js";
import { openEventsUp, stopEvents } from "./events.js";
import { stackUp, startAll, stopAll, switchSetup } from "./home.js";
import { useRepo } from "./repos.js";
import { runDoctor } from "./doctor.js";
import { currentView } from "./router.js";

export const palette = { index: 0, shown: [] };

export function typing(node) {
  return node instanceof HTMLElement && (node.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(node.tagName));
}

// "/" goes to the filter of what is on screen: the first visible search box of the view.
export function viewFilter() {
  const view = document.getElementById(`view-${currentView()}`);
  return [...(view ? view.querySelectorAll('input[type="search"]') : [])].find((input) => input.offsetParent !== null) || null;
}

// Every command for the state of now: [icon, label, group, run].
function paletteCommands() {
  const go = (view) => () => { location.hash = `#${view}`; };
  const commands = [
    ["home", t("Go to Home"), t("Go to"), go("home")],
    ["services", t("Go to Services"), t("Go to"), go("services")],
    ["stacks", t("Go to Stacks"), t("Go to"), go("stacks")],
    ["proxy", t("Go to Requests"), t("Go to"), () => { location.hash = "#proxy"; showProxyTab("requests"); }],
    ["proxy", t("Go to Requests → Routes"), t("Go to"), () => { location.hash = "#proxy"; showProxyTab("routes"); }],
    ["events", t("Go to Events"), t("Go to"), go("events")],
    ["flask", t("Go to Tests"), t("Go to"), go("tests")],
    ["doctor", t("Go to Doctor"), t("Go to"), go("doctor")],
    ["settings", t("Go to Settings"), t("Go to"), go("settings")],
    ["play", t("Start everything"), t("Home"), () => startAll()],
    ["play", t("Start service…"), t("Service"), () => openRun()],
  ];
  const anything = state.instances.some((i) => i.status !== "stopped") || state.proxy || state.events.up || (state.frontend && state.frontend.running);
  if (anything) commands.push(["stop", t("Stop everything"), t("Home"), () => stopAll()]);
  for (const inst of state.instances) {
    commands.push(["logs", t("Logs · {key}", { key: inst.key }), t("Service"), () => showLogs(inst.key)]);
    if (inst.status === "stopped") continue;
    commands.push(["restart", t("Restart {key}", { key: inst.key }), t("Service"), () => openRestart(inst)]);
    commands.push(["debug", t("Debug {key} in VS Code", { key: inst.key }), t("Service"), () => debugInstance(inst)]);
    commands.push(["stop", t("Stop {key}", { key: inst.key }), t("Service"), () => act(`/api/instances/${encodeURIComponent(inst.key)}/stop`)]);
  }
  for (const stack of state.stacks) {
    const up = stackUp(stack);
    if (up < stack.services.length) commands.push(["play", t("Start stack {name}", { name: stack.name }), t("Stack"), () => openUp(stack)]);
    if (up) commands.push(["stop", t("Stop stack {name}", { name: stack.name }), t("Stack"), () => act(`${stackPath(stack.name)}/down`)]);
  }
  for (const name of Object.keys(state.setups || {})) {
    if (name !== state.setup_name) commands.push(["play", t("Switch to setup {name}", { name }), t("Home"), () => switchSetup(name)]);
  }
  if (state.proxy) commands.push(["stop", t("Stop the proxy"), t("Proxy"), () => act(`/api/instances/${encodeURIComponent(state.proxy.key)}/stop`)]);
  else commands.push(["play", t("Start the proxy"), t("Proxy"), () => openProxyStart()]);
  if (state.events.up) commands.push(["stop", t("Stop local events"), t("Events"), () => stopEvents()]);
  else commands.push(["play", t("Start local events"), t("Events"), () => openEventsUp()]);
  if ((state.strays || []).length) commands.push(["services", t("Adopt the processes outside pdms"), t("Fix"), () => adoptStrays(null)]);
  for (const repo of state.repos) {
    if (!state.repo || repo.name !== state.repo.alias) commands.push(["stacks", t("Use repo {name}", { name: repo.name }), t("Repo"), () => useRepo(repo.name)]);
  }
  commands.push(["logs", t("All logs"), t("Logs"), () => addAllLogs()]);
  commands.push(["doctor", t("Run Doctor"), t("Doctor"), () => { location.hash = "#doctor"; runDoctor(); }]);
  return commands;
}

export function openPalette() {
  if (!state) return;
  $("palette-input").value = "";
  palette.index = 0;
  paintPalette();
  $("palette").showModal();
  $("palette-input").focus();
}

export function paintPalette() {
  const words = $("palette-input").value.toLowerCase().trim().split(/\s+/).filter(Boolean);
  palette.shown = paletteCommands().filter(([, label, group]) => words.every((word) => `${label} ${group}`.toLowerCase().includes(word))).slice(0, 60);
  palette.index = Math.min(palette.index, Math.max(0, palette.shown.length - 1));
  $("palette-list").replaceChildren(...(palette.shown.length ? palette.shown.map(([name, label, group], index) => el("li", {
    role: "option", "data-index": String(index), id: `palette-${index}`, "aria-selected": String(index === palette.index),
  }, icon(name), el("span", {}, label), el("span", { class: "grp" }, group)))
    : [el("li", { class: "none" }, t("Nothing matches. Try “restart”, “logs” or “stack”."))]));
  $("palette-input").setAttribute("aria-activedescendant", palette.shown.length ? `palette-${palette.index}` : "");
  const current = $("palette-list").querySelector('[aria-selected="true"]');
  if (current) current.scrollIntoView({ block: "nearest" });
}

export function runPalette(index) {
  const command = palette.shown[index];
  if (!command) return;
  $("palette").close();
  command[3]();
}
