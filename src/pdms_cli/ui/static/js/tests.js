// Tests: the projects the branch's changes touch (or all of them), their last result, and running them on the local
// databases, one project at a time per database (services share tables). Never on a shared database.

import { $, act, el, failedText, getJson, icon, iconButton, phaseLabel, repoPath, toast } from "./core.js";
import { state } from "./state.js";
import { openLogs } from "./logs.js";
import { openDb } from "./settings.js";
import { openSetting } from "./repos.js";

export const testsView = {
  data: null, version: null, show: "affected", selected: new Set(), dbs: null, loading: false, failed: false,
};

const START_DB_JOB = "tests:db";
const SHOWN_FAILURES = 5;
const ASK_ABOVE = 20; // running more projects than this asks first

export function paintTestsBadge() {
  const failing = (state.tests && state.tests.failing) || 0;
  $("tests-badge").hidden = !failing;
  $("tests-badge").textContent = String(failing);
  $("tests-badge").title = failing === 1 ? t("1 project fails its tests") : t("{n} projects fail their tests", { n: failing });
}

export async function loadTests() {
  if (testsView.loading) return;
  testsView.loading = true;
  if (!testsView.data) {
    $("tests-empty").hidden = false;
    $("tests-empty").textContent = t("Loading…");
  }
  const { data, error } = await getJson("/api/tests");
  testsView.loading = false;
  testsView.version = state ? state.tests.version : null;
  testsView.failed = Boolean(error);
  if (error) {
    $("tests-empty").hidden = true;
    $("tests-error").hidden = false;
    $("tests-error").textContent = error;
    return;
  }
  $("tests-error").hidden = true;
  testsView.data = data;
  const names = data.dbs.map((db) => db.name);
  if (testsView.dbs === null) testsView.dbs = new Set(names); // all local ones by default
  else for (const name of [...testsView.dbs]) if (!names.includes(name)) testsView.dbs.delete(name);
  if (!testsView.dbs.size && names.length) testsView.dbs = new Set(names);
  paintTests();
}

// The state moved: a run finished (reload the results) or something started or queued (repaint).
export function syncTests() {
  paintTestsBadge();
  if (document.getElementById("view-tests").hidden) return;
  // The page opened on Tests before the state came: load now (once; after an error, opening the screen retries).
  if (!testsView.data) { if (!testsView.failed) loadTests(); return; }
  if (state.tests.version !== testsView.version) loadTests();
  else paintTests();
}

function running() {
  const map = new Map();
  for (const item of state.tests.running) map.set(item.project, item);
  return map;
}

function affectedMap() {
  const map = new Map();
  for (const item of testsView.data.affected.items) map.set(item.project, item.why);
  return map;
}

function failing(item) {
  return item.result && (item.result.outcome === "failed" || item.result.outcome === "broken");
}

function shownProjects() {
  const affected = affectedMap();
  const filter = $("tests-filter").value.trim().toLowerCase();
  let items = testsView.data.projects;
  if (testsView.show === "affected") items = items.filter((item) => affected.has(item.project));
  else if (testsView.show === "failing") items = items.filter(failing);
  return items.filter((item) => !filter || item.project.toLowerCase().includes(filter));
}

function whyText(why) {
  if (!why) return "";
  return why.map((part) => (part === "own" ? t("its own code changed") : t("{package} changed", { package: part }))).join(" · ");
}

function ranText(result) {
  const when = new Date(result.at);
  const minutes = Math.round((Date.now() - when.getTime()) / 60000);
  const text = minutes < 1 ? t("just now") : minutes < 60 ? t("{n} min ago", { n: minutes })
    : minutes < 1440 ? when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" }) : when.toLocaleDateString();
  return result.origin === "cli" ? t("{when} · CLI", { when: text }) : text;
}

function resultCell(item, live, queued) {
  if (live) return el("span", { class: "st busy" }, live.phase ? phaseLabel(live.phase) : t("running on {db}", { db: live.db }));
  if (queued) return el("span", { class: "st off" }, t("waiting"));
  const result = item.result;
  if (!result) return el("span", { class: "muted" }, t("not run"));
  if (result.outcome === "broken") return el("span", { class: "st stopped" }, t("pytest did not run"));
  if (result.outcome === "empty") return el("span", { class: "st off" }, t("no tests"));
  const parts = [el("span", { class: "p" }, t("{n} passed", { n: result.passed }))];
  if (result.failed) parts.push(el("span", { class: "f" }, t("{n} failed", { n: result.failed })));
  if (result.errors) parts.push(el("span", { class: "f" }, t("{n} errors", { n: result.errors })));
  if (result.skipped) parts.push(el("span", { class: "s" }, t("{n} skipped", { n: result.skipped })));
  return el("span", { class: "res" }, ...parts);
}

function failuresRow(item) {
  const pre = el("pre", { class: "trace" });
  const failures = item.result.failures;
  if (item.result.outcome === "broken") pre.append(t("pytest ended with code {code} and wrote no report: the log says why.", { code: item.result.code }), "\n");
  for (const failure of failures.slice(0, SHOWN_FAILURES)) {
    pre.append(failure.name, " ", el("span", { class: "exc" }, failure.kind === "error" ? "ERROR" : "FAILED"), "\n");
    if (failure.path) {
      pre.append("  ", el("a", {
        href: "#", title: t("Open in VS Code"),
        onclick: (event) => { event.preventDefault(); act("/api/code/open", { path: failure.path, line: failure.line }); },
      }, `${repoPath(failure.path)}:${failure.line}`), "\n");
    }
    if (failure.message) pre.append(el("span", { class: "exc" }, `  ${failure.message}`), "\n");
  }
  const more = (item.result.failed + item.result.errors) - Math.min(failures.length, SHOWN_FAILURES);
  if (more > 0) pre.append(t("… {n} more in the log", { n: more }), "\n");
  return el("tr", { class: "trace-row error-row" }, el("td", { colspan: "7" }, pre));
}

function row(item, why, live, queued) {
  const result = item.result;
  const picked = testsView.selected.has(item.project);
  const box = el("input", { type: "checkbox", "aria-label": t("Select {name}", { name: item.project }) });
  box.checked = picked;
  box.addEventListener("change", () => {
    if (box.checked) testsView.selected.add(item.project); else testsView.selected.delete(item.project);
    paintTests();
  });
  const name = el("td", { class: "name" }, item.project);
  if (item.kind === "package") name.append(" ", el("span", { class: "tag" }, t("package")));
  const devMode = live ? live.dev_mode : result && !queued && result.dev_mode;
  if (devMode) name.append(" ", el("span", { class: "tag dev", title: t("Ran with DEVELOPMENT_MODE on") }, t("dev mode")));
  const ran = result && !live ? ranText(result) : "–";
  const actions = el("td", { class: "row-actions" },
    iconButton("logs", t("Log of the last run"), () => openLogs(`test:${item.project}`), result || live ? {} : { disabled: "" }),
    iconButton("play", t("Run the tests of {name}", { name: item.project }), () => runProjects([item.project]), live || queued ? { disabled: "" } : {}));
  return el("tr", { class: `${failing(item) && !live ? "fail" : ""} ${picked ? "chosen" : ""}`.trim() },
    el("td", { class: "pick" }, box), name, el("td", { class: "why" }, whyText(why)), el("td", {}, resultCell(item, live, queued)),
    el("td", { class: "num" }, result && !live ? `${result.seconds} s` : "–"), el("td", {}, ran), actions);
}

function paintDbs() {
  const dbs = testsView.data.dbs;
  document.querySelector(".tests-dbs-bar").hidden = !dbs.length;
  const box = $("tests-dbs");
  box.replaceChildren();
  for (const db of dbs) {
    const input = el("input", { type: "checkbox" });
    input.checked = testsView.dbs.has(db.name);
    input.addEventListener("change", () => {
      if (input.checked) testsView.dbs.add(db.name); else testsView.dbs.delete(db.name);
      paintTests();
    });
    box.append(el("label", { class: "inline", title: db.where }, input, " ", db.name));
  }
  const n = testsView.dbs.size;
  $("tests-parallel").textContent = !dbs.length ? "" : n === 1 ? t("one project at a time") : t("{n} projects at a time, one per database", { n });
}

function paintNoDb() {
  const none = !testsView.data.dbs.length;
  $("tests-nodb").hidden = !none;
  if (!none) return;
  const job = state.jobs[START_DB_JOB];
  $("tests-start-db").hidden = !testsView.data.compose;
  $("tests-start-db").disabled = Boolean(job && !job.error);
  $("tests-start-db").textContent = t("Start the test database (docker compose, port {port})", { port: testsView.data.compose_port });
  $("tests-nodb-job").hidden = !job;
  $("tests-nodb-job").className = job && job.error ? "error" : "muted";
  $("tests-nodb-job").textContent = !job ? "" : job.error ? failedText(job) : phaseLabel(job.phase);
  $("tests-nodb-log").hidden = !job;
}

export function paintTests() {
  if (!testsView.data || !state) return;
  const data = testsView.data;
  const live = running();
  const queued = new Set(state.tests.queued);
  const affected = affectedMap();
  const counts = {
    affected: data.affected.items.length, all: data.projects.length, failing: data.projects.filter(failing).length,
  };
  for (const node of document.querySelectorAll("#tests-seg button")) {
    node.setAttribute("aria-pressed", String(node.dataset.show === testsView.show));
    node.querySelector("span").textContent = String(counts[node.dataset.show]);
  }
  const base = data.affected.branch;
  $("tests-base").textContent = !base ? t("No main branch found: only uncommitted files count.")
    : data.affected.files === 1 ? t("1 file changed since the branch left {base}", { base })
      : t("{n} files changed since the branch left {base}", { n: data.affected.files, base });
  $("tests-base").hidden = testsView.show !== "affected";
  paintDbs();
  paintNoDb();

  const items = shownProjects();
  const withEnv = items.filter((item) => item.env_dev_mode).length;
  $("tests-dev-note").textContent = !withEnv ? "" : $("tests-dev-mode").checked
    ? t("on for every project run now") : t("{n} of these turn it on in their .env; they run with it off", { n: withEnv });
  const rows = [];
  for (const item of items) {
    const now = live.get(item.project);
    rows.push(row(item, affected.get(item.project), now, queued.has(item.project)));
    if (!now && failing(item) && (item.result.failures.length || item.result.outcome === "broken")) rows.push(failuresRow(item));
  }
  $("tests-rows").replaceChildren(...rows);
  $("tests-empty").hidden = items.length > 0;
  $("tests-empty").textContent = testsView.show === "affected" && !$("tests-filter").value
    ? t("Your changes touch no project with tests. Uncommitted and new files count too.")
    : testsView.show === "failing" && !$("tests-filter").value ? t("Nothing fails.") : t("No project matches the filter.");
  const shown = new Set(items.map((item) => item.project));
  const picked = [...testsView.selected].filter((p) => shown.has(p));
  $("tests-all").checked = items.length > 0 && picked.length === items.length;
  $("tests-all").indeterminate = picked.length > 0 && picked.length < items.length;

  const targets = runTargets();
  const busy = live.size + queued.size;
  const canRun = data.dbs.length > 0 && testsView.dbs.size > 0;
  $("tests-run").textContent = t("Run {n}", { n: targets.length });
  $("tests-run").prepend(icon("play"));
  $("tests-run").disabled = !canRun || !targets.length;
  const failed = items.filter((item) => failing(item) && !live.has(item.project) && !queued.has(item.project));
  $("tests-rerun").hidden = !failed.length;
  $("tests-rerun").textContent = t("Re-run failed · {n}", { n: failed.length });
  $("tests-rerun").disabled = !canRun;
  $("tests-stop").hidden = !busy;
  $("tests-summary").textContent = busy ? t("{running} running · {queued} waiting", { running: live.size, queued: queued.size }) : "";
  $("tests-sel-count").textContent = t("{n} selected", { n: picked.length });
  $("tests-selbar").hidden = !picked.length;
  $("tests-run-error").hidden = !state.tests.error;
  $("tests-run-error").textContent = state.tests.error;
}

// What Run runs: the selection when there is one, else what the tab shows.
function runTargets() {
  const shown = shownProjects().map((item) => item.project);
  const picked = shown.filter((p) => testsView.selected.has(p));
  return picked.length ? picked : shown;
}

export function runProjects(projects) {
  if (!projects.length) return;
  const dbs = [...testsView.dbs];
  if (!dbs.length) { toast(t("Pick at least one local database.")); return; }
  if (projects.length > ASK_ABOVE && !confirm(t("Run the tests of {n} projects? With {dbs} databases it can take a while.", { n: projects.length, dbs: dbs.length }))) return;
  act("/api/tests/run", { projects, dbs, dev_mode: $("tests-dev-mode").checked }, () => { testsView.selected.clear(); });
}

export function runShown() {
  runProjects(runTargets());
}

export function rerunFailed() {
  const live = running();
  const queued = new Set(state.tests.queued);
  runProjects(shownProjects().filter((item) => failing(item) && !live.has(item.project) && !queued.has(item.project)).map((item) => item.project));
}

export function stopTests() {
  act("/api/tests/stop", {});
}

export function pickAllTests(on) {
  for (const item of shownProjects()) {
    if (on) testsView.selected.add(item.project); else testsView.selected.delete(item.project);
  }
  paintTests();
}

export function showTests(show) {
  testsView.show = show;
  paintTests();
}

export function startTestDb() {
  act("/api/tests/start-db", {}, () => toast(t("Starting the test database…"), "info"));
}

export function startDbLog() {
  openLogs(START_DB_JOB, "install");
}

export function addLocalDb() {
  openSetting("dbs", () => openDb());
}

// Starting the test database finished: the screen loads the databases again.
let startDbWas = false;
export function watchStartDb() {
  const job = state.jobs[START_DB_JOB];
  const now = Boolean(job && !job.error);
  if (startDbWas && !job && !document.getElementById("view-tests").hidden) loadTests();
  startDbWas = now;
}
