// The state from /api/state and /api/stream, painted on every screen; the title bar and the look.

import { $, el } from "./core.js";
import { paintServices } from "./services.js";
import { logs, paintDock } from "./logs.js";
import { paintStacks } from "./stacks.js";
import { paintProxy, paintRequests, paintRoutes } from "./proxy.js";
import { eventsView, paintEvents, paintMap, paintQueues, paintSns, showEventsTab } from "./events.js";
import { paintDefaults, paintSettings, settingsView, syncSettings } from "./settings.js";
import { paintHome } from "./home.js";
import { paintUpdate, updateView } from "./updates.js";
import { toggleRepoMenu } from "./repos.js";
import { doctorView, loadDoctor, paintDoctor, paintDoctorBadge } from "./doctor.js";
import { currentView } from "./router.js";
import { paintStatus } from "./status.js";
import { syncTests, watchStartDb } from "./tests.js";
import { syncData } from "./data.js";

export let state = null;

function chip(label, value) {
  return el("span", { class: "chip" }, label, el("b", {}, value || "-"));
}

// Who a user is, to tell them apart: "Liana Roget · TPR.Supervisor +1".
function whoIs(name) {
  const profile = (state.profiles || {})[name];
  if (!profile) return "";
  const roles = profile.roles.split(",").map((role) => role.trim()).filter(Boolean);
  const role = roles.length > 1 ? `${roles[0]} +${roles.length - 1}` : roles[0] || "";
  return [profile.name, role].filter(Boolean).join(" · ");
}

function userChip() {
  const node = chip(t("user"), state.user);
  const who = state.user ? whoIs(state.user) : "";
  if (who) {
    node.append(el("span", { class: "who" }, who));
    node.title = `${state.user}: ${state.profiles[state.user].name} · ${state.profiles[state.user].roles}`;
  }
  return node;
}

function paintContext() {
  const repo = el("button", { class: "chip repo-chip", type: "button", "aria-haspopup": "menu", onclick: toggleRepoMenu },
    t("repo"), el("b", {}, (state.repo && state.repo.alias) || "-"));
  $("ctx").replaceChildren(repo, userChip()); // the database, proxy and events are in the status bar
}

// Settings → Defaults → Look: like the computer ("system"), or always light or dark.
let themePreview = ""; // a look picked in Settings → Defaults and not saved yet

export function setThemePreview(theme) {
  themePreview = theme;
}

export function useTheme(theme) {
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
}

export function paint(next) {
  // pdms ui restarted with a new version (an update): load its page, which may have changed too.
  if (updateView.loaded === null) updateView.loaded = next.version;
  else if (next.version !== updateView.loaded) { location.reload(); return; }
  // Defaults for what an older pdms ui server does not send yet (its page files may already be newer).
  state = {
    jobs: {}, users: [], dbs: [], stacks: [], health: { dbs: [], running: false, slow_ms: 0 },
    tests: { running: [], queued: [], failing: 0, version: 0, error: "" }, ...next,
  };
  const relabel = useLanguage(state.language);
  if (themePreview === state.theme) themePreview = ""; // saved, or picked back
  useTheme(themePreview || state.theme);
  paintContext();
  paintStatus();
  paintUpdate();
  paintDoctorBadge();
  if (currentView() === "doctor") {
    if (state.doctor.at !== doctorView.at) loadDoctor();
    else paintDoctor();
  }
  paintHome();
  paintServices();
  paintStacks();
  paintProxy();
  const wasUp = eventsView.up;
  eventsView.up = state.events.up;
  paintEvents();
  syncTests();
  watchStartDb();
  syncData();
  if (wasUp !== undefined && wasUp !== eventsView.up && currentView() === "events") showEventsTab(eventsView.tab);
  if (logs.sources.length) paintDock();
  syncSettings();
  if (relabel) repaintTexts();
}

// The language changed: paint again what does not come from the state (the state's own parts were just painted).
function repaintTexts() {
  paintRequests();
  paintRoutes();
  paintQueues();
  paintMap();
  eventsView.topics = ""; // the topic list is only rebuilt when its signature changes
  paintSns();
  if (settingsView.data) {
    paintSettings();
    paintDefaults();
  }
}

export function connect() {
  const stream = new EventSource("/api/stream");
  stream.addEventListener("state", (event) => paint(JSON.parse(event.data)));
  stream.onopen = () => { $("live").className = "live on"; $("live").title = t("Live"); };
  // EventSource reconnects by itself; the dot shows when pdms ui is not reachable.
  stream.onerror = () => { $("live").className = "live off"; $("live").title = t("Disconnected: is pdms ui still running?"); };
}
