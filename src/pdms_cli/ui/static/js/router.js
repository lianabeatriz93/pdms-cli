// Which screen shows (the #hash of the page), and what a screen loads when it opens.

import { $, post } from "./core.js";
import { state } from "./state.js";
import { paintProxy } from "./proxy.js";
import { eventsView, paintEvents, showEventsTab } from "./events.js";
import { loadSettings } from "./settings.js";
import { loadDoctor } from "./doctor.js";
import { loadTests } from "./tests.js";
import { loadData } from "./data.js";
import { leaveAwsLogs, openAwsLogs } from "./awslogs.js";
import { paintServices } from "./services.js";
import { paintStacks } from "./stacks.js";
import { paintHome } from "./home.js";

const VIEWS = ["home", "services", "stacks", "proxy", "events", "aws-logs", "data", "tests", "doctor", "settings"];

export function currentView() {
  const view = location.hash.slice(1);
  return VIEWS.includes(view) ? view : "home";
}

export function route() {
  const view = currentView();
  for (const section of document.querySelectorAll(".view")) section.hidden = section.id !== `view-${view}`;
  for (const link of document.querySelectorAll(".side a[data-view]")) link.classList.toggle("on", link.dataset.view === view);
  if (!state) return;
  if ((state.new_views || []).includes(view)) {
    state.new_views = state.new_views.filter((name) => name !== view);
    $(`new-${view}`).hidden = true;
    post("/api/ui/seen", { view }).catch(() => {}); // the next state says it too
  }
  // Their lists are only built while they show (see onScreen): build them now.
  if (view === "home") paintHome();
  if (view === "services") paintServices();
  if (view === "stacks") paintStacks();
  paintProxy();
  paintEvents();
  if (view === "events") showEventsTab(eventsView.tab);
  if (view === "settings") loadSettings();
  if (view === "doctor") loadDoctor();
  if (view === "tests") loadTests();
  if (view === "data") loadData();
  if (view === "aws-logs") openAwsLogs(); else leaveAwsLogs();
}
