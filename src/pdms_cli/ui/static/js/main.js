// pdms ui: paints /api/state, follows /api/stream and runs the actions. Text only goes in through textContent,
// never as HTML. Every text it shows is translated with the t and N_ functions of i18n.js (loaded before, global).
//
// This module wires the page's controls and starts painting. router.js decides which screen shows; each screen
// lives in its own module, and core.js has the helpers they share.

import { $, act, toast } from "./core.js";
import { connect, paint, state } from "./state.js";
import { adoptStrays, paintServices, pickAllShown, pickedItems, servicesView, stopPicked, tickUptimes } from "./services.js";
import {
  addAllLogs, addLogSource, clearLog, clearRequestFilter, closeLogs, setLogFilter, showLogs, switchLogTab, toggleDock,
} from "./logs.js";
import { filterRun, openRestartSet, openRun, resetConfirmation, setChange, setPick, submitLaunch } from "./launch.js";
import { filterEditor, openEditor, paintStacks, saveEditor } from "./stacks.js";
import {
  closeRequest, loadRoutes, openProxyStart, paintRequests, paintRoutes, proxyView, requestLogs, resetProxyPort, showDetailTab,
  showProxyTab, submitProxyStart,
} from "./proxy.js";
import {
  brokerQueue, closePeek, eventsView, eventsVisible, fillTemplate, loadQueues, openConsumerStart, openEventsUp,
  openPeek, openSend, paintEvents, paintMap, paintQueues, paintSns, purge, sendTargetChanged, showEventsTab, stopEvents,
  submitSend,
} from "./events.js";
import {
  defaultsChanged, filterDefaults, findDbUsers, openDb, openExport, openUser, openUserImport, paintDbs, paintDefaults,
  paintImport, paintUsers, readImport, saveDb, saveDefaults, saveUser, settingsView, showSettingsTab, submitExport,
  submitImport, submitUserImport, testDbForm, toggleDbPassword, usersCount,
} from "./settings.js";
import { openFrontendStart, paintFrontendMode, resetFrontendPort, submitFrontendStart } from "./frontend.js";
import { saveSetup, startAll, stopAll, submitSaveSetup } from "./home.js";
import { UPDATE_JOB, checkNow, copyCommand, openUpdate, submitUpdate } from "./updates.js";
import { browseRepo, openRepo, paintRepos, repoForm, repoPathChanged, saveRepo, submitSwitch } from "./repos.js";
import { flywayView, openFlyway, paintFlyway } from "./migrations.js";
import { doctorReport, paintDoctor, runDoctor } from "./doctor.js";
import { openPalette, paintPalette, palette, runPalette, typing, viewFilter } from "./palette.js";
import { route } from "./router.js";
import { checkAws, chooseAwsProfile, loginAws, readAws } from "./aws.js";
import { copyLog, dataView, postgresUpDown, refreshCopy, saveSnapshot, setTestCount, testNow } from "./data.js";
import {
  paintTests, pickAllTests, rerunFailed, runShown, showTests, startTestDb, stopTests, testsView,
} from "./tests.js";
import { lineCheck, measureNow } from "./status.js";

$("boot-fail").remove(); // the scripts loaded: the notice for an old pdms ui is not needed
$("db-pop-measure").addEventListener("click", measureNow);
$("db-pop-line").addEventListener("click", lineCheck);
document.addEventListener("click", (event) => { if (!$("db-pop").contains(event.target)) $("db-pop").hidden = true; });
document.addEventListener("keydown", (event) => { if (event.key === "Escape") $("db-pop").hidden = true; });

$("logs-close").addEventListener("click", closeLogs);
$("logs-clear").addEventListener("click", clearLog);
$("logs-hide").addEventListener("click", toggleDock);
$("logs-all").addEventListener("click", addAllLogs);
$("svc-all-logs").addEventListener("click", addAllLogs);
for (const tab of $("logs-tabs").children) tab.addEventListener("click", () => switchLogTab(tab.dataset.which));
$("logs-add").addEventListener("change", () => { addLogSource($("logs-add").value); $("logs-add").value = ""; });
$("logs-level").addEventListener("change", setLogFilter);
$("logs-filter").addEventListener("input", setLogFilter);
$("logs-request-clear").addEventListener("click", clearRequestFilter);
$("req-d-logs").addEventListener("click", requestLogs);
$("svc-all").addEventListener("change", () => pickAllShown($("svc-all").checked));
$("svc-sel-clear").addEventListener("click", () => { servicesView.selected.clear(); paintServices(); });
$("svc-sel-stop").addEventListener("click", stopPicked);
$("svc-sel-restart").addEventListener("click", () => { const items = pickedItems(); if (items.length) openRestartSet({ items }); });
$("set-change").addEventListener("change", setChange);
$("set-all").addEventListener("click", () => setPick(true));
$("set-none").addEventListener("click", () => setPick(false));
$("data-up").addEventListener("click", () => postgresUpDown("up"));
$("data-test-now").addEventListener("click", testNow);
$("data-down").addEventListener("click", () => postgresUpDown("down"));
$("data-refresh").addEventListener("click", () => refreshCopy());
$("data-job-log").addEventListener("click", copyLog);
$("data-copy-migrations").addEventListener("click", () => dataView.data && openFlyway(dataView.data.copy.alias));
$("data-snap-form").addEventListener("submit", saveSnapshot);
$("data-tests-count").addEventListener("change", setTestCount);
$("data-aws-profile").addEventListener("change", chooseAwsProfile);
$("data-aws-read-btn").addEventListener("click", readAws);
$("data-aws-login-btn").addEventListener("click", loginAws);
$("data-aws-check").addEventListener("click", checkAws);
$("tests-run").addEventListener("click", runShown);
$("tests-rerun").addEventListener("click", rerunFailed);
$("tests-stop").addEventListener("click", stopTests);
$("tests-all").addEventListener("change", () => pickAllTests($("tests-all").checked));
$("tests-sel-clear").addEventListener("click", () => { testsView.selected.clear(); paintTests(); });
$("tests-filter").addEventListener("input", paintTests);
$("tests-dev-mode").addEventListener("change", paintTests);
$("tests-start-db").addEventListener("click", startTestDb);
for (const node of document.querySelectorAll("#tests-seg button")) node.addEventListener("click", () => showTests(node.dataset.show));
$("clean").addEventListener("click", () => act("/api/clean", {}, (data) => toast(t("Forgot {n} stopped.", { n: data.forgotten.length }), "info")));
$("adopt-all").addEventListener("click", () => adoptStrays(null));
$("svc-filter").addEventListener("input", () => state && paintServices());
for (const node of document.querySelectorAll("#svc-seg button")) {
  node.addEventListener("click", () => { servicesView.show = node.dataset.show; if (state) paintServices(); });
}
$("stack-filter").addEventListener("input", () => state && paintStacks());
$("stack-running").addEventListener("change", () => state && paintStacks());
$("run-new").addEventListener("click", () => state && openRun());
$("run-filter").addEventListener("input", filterRun);
$("run-filter").addEventListener("keydown", (event) => { if (event.key === "Enter") event.preventDefault(); });
$("run-port").addEventListener("input", resetConfirmation);
$("restart-form").addEventListener("submit", submitLaunch);
$("stack-new").addEventListener("click", () => openEditor());
$("editor-form").addEventListener("submit", saveEditor);
$("editor-cancel").addEventListener("click", () => $("editor").close());
$("editor-filter").addEventListener("input", filterEditor);
$("editor-filter").addEventListener("keydown", (event) => { if (event.key === "Enter") event.preventDefault(); });
$("proxy-start").addEventListener("click", openProxyStart);
$("proxy-stop").addEventListener("click", () => act(`/api/instances/${encodeURIComponent(state.proxy.key)}/stop`));
$("proxy-form").addEventListener("submit", submitProxyStart);
$("proxy-cancel").addEventListener("click", () => $("proxy-dialog").close());
$("proxy-port").addEventListener("input", resetProxyPort);
$("proxy-no-remote").addEventListener("change", () => { $("proxy-remote").disabled = $("proxy-no-remote").checked; });
for (const tab of $("proxy-tabs").children) tab.addEventListener("click", () => showProxyTab(tab.dataset.tab));
$("req-filter").addEventListener("input", paintRequests);
for (const node of document.querySelectorAll("#req-seg button")) {
  node.addEventListener("click", () => { proxyView.show = node.dataset.show; paintRequests(); });
}
for (const tab of $("req-d-tabs").children) tab.addEventListener("click", () => showDetailTab(tab.dataset.tab));
$("req-clear").addEventListener("click", () => { proxyView.requests = []; closeRequest(); paintRequests(); });
$("req-d-close").addEventListener("click", () => closeRequest());
$("route-filter").addEventListener("input", paintRoutes);
$("route-local").addEventListener("change", paintRoutes);
$("route-refresh").addEventListener("click", loadRoutes);
$("events-start").addEventListener("click", openEventsUp);
$("events-off-start").addEventListener("click", openEventsUp);
$("events-off-browse").addEventListener("click", () => { eventsView.browse = true; paintEvents(); showEventsTab(eventsView.tab); });
$("events-stop").addEventListener("click", stopEvents);
$("events-send").addEventListener("click", () => openSend());
$("events-broker").addEventListener("click", () => openConsumerStart(brokerQueue().consumer, "broker"));
for (const tab of $("events-tabs").children) tab.addEventListener("click", () => showEventsTab(tab.dataset.tab));
$("queue-filter").addEventListener("input", paintQueues);
$("queue-busy").addEventListener("change", paintQueues);
$("queue-purge-all").addEventListener("click", () => purge([]));
$("peek-refresh").addEventListener("click", () => openPeek(eventsView.peek));
$("peek-purge").addEventListener("click", () => purge([eventsView.peek]));
$("peek-close").addEventListener("click", closePeek);
$("type-filter").addEventListener("input", paintMap);
$("sns-topic").addEventListener("change", paintSns);
$("sns-filter").addEventListener("input", paintSns);
$("sns-clear").addEventListener("click", () => { eventsView.sns = []; paintSns(); });
$("send-form").addEventListener("submit", submitSend);
$("send-cancel").addEventListener("click", () => $("send").close());
$("send-template").addEventListener("click", fillTemplate);
$("send-target").addEventListener("input", sendTargetChanged);
setInterval(() => { if (eventsVisible("queues") && state && state.events.up) loadQueues(); }, 3000);
window.addEventListener("hashchange", route);
route();
$("restart-cancel").addEventListener("click", () => $("restart").close());
for (const tab of $("settings-tabs").children) tab.addEventListener("click", () => showSettingsTab(tab.dataset.tab));
$("db-filter").addEventListener("input", paintDbs);
$("db-protected").addEventListener("change", paintDbs);
$("db-new").addEventListener("click", () => openDb());
$("db-form").addEventListener("submit", saveDb);
$("db-cancel").addEventListener("click", () => $("db-dialog").close());
$("db-test").addEventListener("click", testDbForm);
$("db-eye").addEventListener("click", toggleDbPassword);
$("db-password").addEventListener("input", () => { settingsView.passwordTouched = true; });
$("db-protected-box").addEventListener("change", () => { settingsView.protectedTouched = true; });

// Like pdms db add: a database that is not on this machine is protected unless said otherwise.
$("db-host").addEventListener("input", () => {
  if (!settingsView.protectedTouched) $("db-protected-box").checked = !["localhost", "127.0.0.1", ""].includes($("db-host").value.trim());
});
for (const form of ["db-form", "user-form"]) {
  $(form).addEventListener("input", (event) => event.target.removeAttribute("aria-invalid"));
}
$("user-filter").addEventListener("input", paintUsers);
$("user-new").addEventListener("click", () => openUser());
$("user-form").addEventListener("submit", saveUser);
$("user-cancel").addEventListener("click", () => $("user-dialog").close());
$("defaults-filter").addEventListener("input", filterDefaults);
$("defaults-form").addEventListener("input", defaultsChanged);
$("defaults-form").addEventListener("change", defaultsChanged);
$("defaults-form").addEventListener("submit", (event) => { event.preventDefault(); if (defaultsChanged()) saveDefaults(); });
$("defaults-save").addEventListener("click", saveDefaults);
$("defaults-discard").addEventListener("click", paintDefaults);
$("settings-export").addEventListener("click", () => settingsView.data && openExport());
$("export-secrets").addEventListener("change", () => { $("export-warn").hidden = !$("export-secrets").checked; });
$("export-form").addEventListener("submit", submitExport);
$("export-cancel").addEventListener("click", () => $("export-dialog").close());
$("settings-import").addEventListener("click", () => $("import-file").click());
$("import-file").addEventListener("change", readImport);
$("import-form").addEventListener("change", (event) => { if (event.target.name === "mode") paintImport(); });
$("import-form").addEventListener("submit", submitImport);
$("import-cancel").addEventListener("click", () => $("import-dialog").close());
$("user-import").addEventListener("click", () => settingsView.data && openUserImport());
$("users-find").addEventListener("click", findDbUsers);
$("users-search").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); findDbUsers(); } });
$("users-found").addEventListener("change", usersCount);
$("users-all").addEventListener("change", () => {
  for (const box of $("users-found").querySelectorAll("input")) box.checked = $("users-all").checked;
  usersCount();
});
$("users-form").addEventListener("submit", submitUserImport);
$("users-cancel").addEventListener("click", () => $("users-dialog").close());
$("restart-db").addEventListener("change", resetConfirmation);
$("front-new").addEventListener("click", () => openFrontendStart());
$("front-form").addEventListener("submit", submitFrontendStart);
$("front-cancel").addEventListener("click", () => $("front-dialog").close());
$("front-form").addEventListener("change", (event) => {
  if (event.target.name === "mode") paintFrontendMode();
  if (event.target.name === "mode" || event.target.id === "front-rebuild") resetFrontendPort();
});
$("home-start").addEventListener("click", () => startAll());
$("update-chip").addEventListener("click", openUpdate);
document.addEventListener("click", (event) => { if (!$("repo-menu").contains(event.target)) $("repo-menu").hidden = true; });
document.addEventListener("keydown", (event) => { if (event.key === "Escape") $("repo-menu").hidden = true; });
$("repo-filter").addEventListener("input", () => settingsView.data && paintRepos());
$("repo-new").addEventListener("click", () => openRepo());
$("repo-form").addEventListener("submit", saveRepo);
$("repo-cancel").addEventListener("click", () => $("repo-dialog").close());
$("repo-browse").addEventListener("click", browseRepo);
$("repo-path").addEventListener("input", repoPathChanged);
$("repo-name").addEventListener("input", () => { repoForm.nameTouched = true; });
$("repo-form").addEventListener("input", (event) => event.target.removeAttribute("aria-invalid"));
$("switch-form").addEventListener("submit", submitSwitch);
$("switch-cancel").addEventListener("click", () => $("switch-dialog").close());
$("doctor-run").addEventListener("click", runDoctor);
$("flyway-close").addEventListener("click", () => $("flyway-dialog").close());
$("flyway-refresh").addEventListener("click", () => openFlyway(flywayView.db));
$("flyway-filter").addEventListener("input", paintFlyway);
$("flyway-pending").addEventListener("change", paintFlyway);
$("doctor-copy").addEventListener("click", doctorReport);
$("palette-open").addEventListener("click", openPalette);
$("palette-input").addEventListener("input", () => { palette.index = 0; paintPalette(); });
$("palette-list").addEventListener("click", (event) => {
  const item = event.target.closest("li[data-index]");
  if (item) runPalette(Number(item.dataset.index));
});
$("palette-input").addEventListener("keydown", (event) => {
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    const last = palette.shown.length - 1;
    palette.index = event.key === "ArrowDown" ? Math.min(palette.index + 1, last) : Math.max(palette.index - 1, 0);
    paintPalette();
  } else if (event.key === "Enter") {
    event.preventDefault();
    runPalette(palette.index);
  }
});
document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === "k") {
    event.preventDefault();
    if ($("palette").open) $("palette").close(); else openPalette();
  } else if (event.key === "/" && !event.ctrlKey && !event.metaKey && !typing(event.target) && !document.querySelector("dialog[open]")) {
    const filter = viewFilter();
    if (filter) { event.preventDefault(); filter.focus(); filter.select(); }
  }
});
if (/Mac|iPhone|iPad/.test(navigator.platform)) $("palette-key").textContent = "⌘ K";
$("doctor-filter").addEventListener("input", paintDoctor);
$("doctor-problems").addEventListener("change", paintDoctor);
$("version-open").addEventListener("click", openUpdate);
$("version-check").addEventListener("click", checkNow);
$("update-form").addEventListener("submit", submitUpdate);
$("update-cancel").addEventListener("click", () => $("update-dialog").close());
$("update-copy").addEventListener("click", copyCommand);
$("update-log").addEventListener("click", () => { $("update-dialog").close(); showLogs(UPDATE_JOB); });
$("home-stop").addEventListener("click", stopAll);
$("setup-form").addEventListener("submit", submitSaveSetup);
$("setup-cancel").addEventListener("click", () => $("setup-dialog").close());
for (const id of ["setup-stack", "setup-events", "setup-proxy", "setup-frontend", "setup-mode"]) $(id).addEventListener("change", saveSetup);
fetch("/api/state").then((response) => response.json()).then(paint).finally(connect);
setInterval(tickUptimes, 1000);
