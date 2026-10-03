// Which screen shows (the #hash of the page), and what a screen loads when it opens.

import { state } from "./state.js";
import { paintProxy } from "./proxy.js";
import { eventsView, paintEvents, showEventsTab } from "./events.js";
import { loadSettings } from "./settings.js";
import { loadDoctor } from "./doctor.js";
import { loadTests } from "./tests.js";

const VIEWS = ["home", "services", "stacks", "proxy", "events", "tests", "doctor", "settings"];

export function currentView() {
  const view = location.hash.slice(1);
  return VIEWS.includes(view) ? view : "home";
}

export function route() {
  const view = currentView();
  for (const section of document.querySelectorAll(".view")) section.hidden = section.id !== `view-${view}`;
  for (const link of document.querySelectorAll(".side a[data-view]")) link.classList.toggle("on", link.dataset.view === view);
  if (!state) return;
  paintProxy();
  paintEvents();
  if (view === "events") showEventsTab(eventsView.tab);
  if (view === "settings") loadSettings();
  if (view === "doctor") loadDoctor();
  if (view === "tests") loadTests();
}
