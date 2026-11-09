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
import { syncAwsLogs } from "./awslogs.js";
import { paintBrowser } from "./browser.js";

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

// Where the keyboard is: a paint rebuilds lists, and the button or link that had the focus is replaced by a new one.
// It is found again by its place under the nearest element with an id, and only if it is the same kind of control
// with the same label (a row that moved is not focused instead: Enter would act on another service).
function focusSpot() {
  const node = document.activeElement;
  if (!node || node === document.body || node.closest("dialog")) return null;
  const path = [];
  let at = node;
  while (!at.id) {
    const parent = at.parentElement;
    if (!parent) return null;
    path.unshift([...parent.children].indexOf(at));
    at = parent;
  }
  return { node, id: at.id, path, tag: node.tagName, label: node.getAttribute("aria-label") || node.textContent };
}

function restoreFocus(spot) {
  if (!spot || spot.node.isConnected) return;
  if (document.activeElement && document.activeElement !== document.body) return; // it went somewhere on purpose
  let at = document.getElementById(spot.id);
  for (const index of spot.path) at = at && at.children[index];
  if (!at || at.tagName !== spot.tag || (at.getAttribute("aria-label") || at.textContent) !== spot.label) return;
  at.focus({ preventScroll: true });
}

export function paint(next) {
  // pdms ui restarted with a new version (an update): load its page, which may have changed too.
  if (updateView.loaded === null) updateView.loaded = next.version;
  else if (next.version !== updateView.loaded) { location.reload(); return; }
  const spot = focusSpot();
  // Defaults for what an older pdms ui server does not send yet (its page files may already be newer).
  state = {
    jobs: {}, users: [], dbs: [], stacks: [], health: { dbs: [], running: false, slow_ms: 0 },
    tests: { running: [], queued: [], failing: 0, version: 0, error: "" },
    browser: { redirect: false, hosts: [], folder: "", browsers: [] }, ...next,
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
  syncAwsLogs();
  paintNewTags();
  if (wasUp !== undefined && wasUp !== eventsView.up && currentView() === "events") showEventsTab(eventsView.tab);
  if (logs.sources.length) paintDock();
  syncSettings();
  paintBrowser();
  if (relabel) repaintTexts();
  restoreFocus(spot);
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

// "new" on a screen until it is opened once (kept by pdms ui: the window may not keep the browser's storage). Tests
// shows its failures badge instead when there are any.
function paintNewTags() {
  const fresh = new Set(state.new_views || []);
  $("new-data").hidden = !fresh.has("data");
  $("new-tests").hidden = !fresh.has("tests") || !$("tests-badge").hidden;
}

export function connect() {
  const stream = new EventSource("/api/stream");
  stream.addEventListener("state", (event) => paint(JSON.parse(event.data)));
  stream.onopen = () => { $("live").className = "live on"; $("live").title = t("Live"); };
  // EventSource reconnects by itself; the dot shows when pdms ui is not reachable.
  stream.onerror = () => { $("live").className = "live off"; $("live").title = t("Disconnected: is pdms ui still running?"); };
}
