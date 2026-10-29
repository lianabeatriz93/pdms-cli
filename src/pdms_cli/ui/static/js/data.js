// Data: the databases with their round trip and migrations, pdms's Postgres with the local copy (refresh, snapshots)
// and the test databases. Reloaded when the screen opens and when one of its jobs ends.

import {
  $, act, button, copyText, el, failedText, getJson, phaseLabel, post, postNeedingImages, skeletonRows, toast,
} from "./core.js";
import { state } from "./state.js";
import { askMigrations, openFlyway } from "./migrations.js";
import { openLogs } from "./logs.js";
import { confirmDialog } from "./stacks.js";
import { paintAws } from "./aws.js";
import { dbLevel, roundTrip as roundTripText, routeText, sparkline } from "./status.js";

// migrations: name → { loading, counts, flyway, error }, read once per database when the screen opens.
export const dataView = {
  data: null, loading: false, failed: false, jobWas: false, testJobWas: false, migrations: {}, migrating: new Set(), views: null,
  viewsError: "",
};

const JOB = "data:postgres";
const TEST_JOB = "data:test-dbs"; // preparing the test databases from the local copy, or migrating them

function mb(bytes) {
  return bytes || bytes === 0 ? `${(bytes / 1048576).toFixed(1)} MB` : "–";
}

function when(iso) {
  return iso ? new Date(iso).toLocaleString([], { dateStyle: "short", timeStyle: "short" }) : "–";
}

export async function loadData() {
  if (dataView.loading) return;
  dataView.loading = true;
  paintData(); // the first time, grey rows until it answers
  const { data, error } = await getJson("/api/data");
  dataView.loading = false;
  dataView.failed = Boolean(error);
  if (error) { toast(error); return; }
  dataView.data = data;
  paintData();
  post("/api/health/run", { all: true }).catch(() => {}); // every database measured while the screen is open
  for (const db of data.dbs) if (!dataView.migrations[db.name]) loadMigrations(db.name);
  if (!dataView.views && !dataView.viewsError) loadViews(); // once: it asks web-dev (Check again reads it again)
}

// The migrations of one database, read from its Flyway history (a round trip or two: web-dev takes a moment).
async function loadMigrations(name) {
  dataView.migrations[name] = { loading: true };
  paintConnections(dataView.data);
  try {
    const { status, data } = await post("/api/migrations/status", { db: name });
    dataView.migrations[name] = status === 200
      ? { counts: data.counts, flyway: data.flyway, uncommitted: data.migrations.filter((m) => m.uncommitted && ["pending", "failed", "outdated"].includes(m.state)).length }
      : { error: data.error || String(status) };
  } catch {
    dataView.migrations[name] = { error: t("pdms ui is not reachable: is it still running?") };
  }
  if (dataView.data) paintConnections(dataView.data);
}

// A job of the screen ended (Postgres started, the copy refreshed, migrations applied): its numbers changed.
export function syncData() {
  const job = state.jobs[JOB];
  const running = Boolean(job && !job.error);
  const visible = !document.getElementById("view-data").hidden;
  const testJob = state.jobs[TEST_JOB];
  const testRunning = Boolean(testJob && !testJob.error);
  // The page opened on Data before the state came: load now (once; after an error, opening the screen retries).
  if (visible && !dataView.data && !dataView.failed) loadData();
  else if (((dataView.jobWas && !running) || (dataView.testJobWas && !testRunning)) && visible) loadData();
  dataView.jobWas = running;
  dataView.testJobWas = testRunning;
  for (const name of [...dataView.migrating]) {
    const migrate = state.jobs[`migrate:${name}`];
    if (!migrate || migrate.error) {
      dataView.migrating.delete(name);
      if (!migrate && visible) loadMigrations(name);
    }
  }
  if (dataView.data && visible) { paintJob(); paintConnections(dataView.data); paintTests(dataView.data); }
  if (visible) paintAws();
}

export function testNow() {
  act("/api/health/run", { all: true }, () => toast(t("Measuring every database… the rows update in a moment."), "info"));
}

function measuredOf(name) {
  return (state.health.dbs || []).find((db) => db.name === name) || null;
}

function roundTripCell(measured) {
  if (!measured) return el("span", { class: "muted", title: t("Not measured yet: Test now") }, "–");
  if (measured.error) return el("span", { class: "st stopped", title: measured.error }, t("no answer"));
  return el("span", { class: `db-ms lv-${dbLevel(measured)}` }, roundTripText(measured.ms));
}

// How the connection travels: a container of this machine, a tunnel (devo-cli), or straight out.
function throughCell(db, measured) {
  if (db.local) {
    return el("span", { class: "through" }, el("span", { class: "k docker" }, "docker"),
      db.container ? `:${db.port} · ${db.container}` : t(":{port} · no container publishes it", { port: db.port }));
  }
  if (!measured) return el("span", { class: "muted" }, "–");
  const kind = measured.route.kind;
  return el("span", { class: "through" }, el("span", { class: `k ${kind === "tunnel" ? "tunnel" : "direct"}` }, kind === "tunnel" ? t("tunnel") : t("direct")),
    routeText(measured.route));
}

function migrationsCell(db) {
  const known = dataView.migrations[db.name];
  if (!known || known.loading) return el("span", { class: "muted" }, t("reading…"));
  if (known.error) return el("span", { class: "st stopped", title: known.error }, t("could not read"));
  if (dataView.migrating.has(db.name)) return el("span", { class: "st busy" }, t("applying…"));
  if (!known.flyway) return el("span", { class: "st off" }, t("no Flyway history"));
  return behindPill(known.counts.pending + known.counts.failed + known.counts.outdated, known.counts.changed || 0,
    known.uncommitted || 0);
}

// "21 pending", "7 uncommitted", "9 changed since they ran" (each its own pill, so a narrow column wraps them), or
// "up to date".
function behindPill(pending, changed, uncommitted) {
  if (!pending && !changed) return el("span", { class: "st ok" }, t("up to date"));
  const pills = el("span", { class: "pills" });
  if (pending) pills.append(el("span", { class: "st starting" }, t("{n} pending", { n: pending })));
  if (uncommitted) pills.append(el("span", { class: "tag", title: t("Files of your checkout without a commit: work in progress.") }, t("{n} uncommitted", { n: uncommitted })));
  if (changed) pills.append(el("span", { class: "st stopped", title: t("Their file changed after they ran: make the local copy again, or Flyway repairs their history first.") }, t("{n} changed", { n: changed })));
  return pills;
}

// Only on a local database Flyway already manages: one without its history has tables made another way (the ORM,
// Alembic), and Flyway's baselineOnMigrate would run every migration over them.
function applyButton(db) {
  const known = dataView.migrations[db.name];
  if (!db.local || !known || !known.counts || !known.flyway || dataView.migrating.has(db.name)) return "";
  const todo = known.counts.pending + known.counts.failed + known.counts.outdated;
  const changed = known.counts.changed || 0;
  if (!todo && !changed) return "";
  const label = changed ? t("Repair and apply {n}", { n: todo }) : t("Apply {n}", { n: todo });
  return button(label, () => applyMigrations(db.name, todo), { class: "btn small primary" });
}

async function applyMigrations(name, count) {
  if (!await confirmDialog(t("Apply {n} migrations to {name}?", { n: count, name }),
    t("Flyway migrate runs on {name} (a local database) in the background; the log shows its progress.", { name }), t("Apply"))) return;
  const { status } = await postNeedingImages("/api/data/migrate", { db: name }, `migrate:${name}`);
  if (status === 202) dataView.migrating.add(name);
  paintConnections(dataView.data);
}

function paintConnections(data) {
  const rows = data.dbs.map((db) => {
    const measured = measuredOf(db.name);
    return el("tr", {},
      el("td", { class: "name", title: `${db.host}:${db.port}/${db.database}` }, db.name,
        db.copy ? el("span", { class: "tag current" }, t("local copy")) : "",
        db.protected ? el("span", { class: "tag protected" }, t("protected")) : ""),
      el("td", {}, throughCell(db, measured)),
      el("td", { class: "num" }, roundTripCell(measured)),
      el("td", {}, measured ? sparkline(measured.history, dbLevel(measured)) : ""),
      el("td", { class: "migrations" }, migrationsCell(db)),
      el("td", { class: "row-actions" },
        state.jobs[`migrate:${db.name}`] ? button(t("Log"), () => openLogs(`migrate:${db.name}`, "install"), { class: "btn small ghost" }) : "",
        button(t("See the list"), () => openFlyway(db.name), { class: "btn small ghost" }), applyButton(db)));
  });
  $("data-dbs").replaceChildren(...rows);
}

function paintJob() {
  const job = state.jobs[JOB];
  $("data-job").hidden = !job;
  $("data-job").className = job && job.error ? "error" : "muted";
  $("data-job").textContent = !job ? "" : job.error ? failedText(job) : phaseLabel(job.phase);
  $("data-job-log").hidden = !(job && job.installed);
  for (const id of ["data-up", "data-down", "data-refresh"]) $(id).disabled = Boolean(job && !job.error);
  for (const node of document.querySelectorAll("#data-snaps button")) node.disabled = Boolean(job && !job.error);
}

function paintPostgres(data) {
  const pg = data.postgres;
  const status = !pg.exists ? ["off", t("not created yet")] : pg.running ? ["ok", t("running")] : ["stopped", t("stopped")];
  $("data-pg-status").className = `st ${status[0]}`;
  $("data-pg-status").textContent = status[1];
  $("data-pg-where").textContent = pg.running ? `localhost:${pg.port} · ${pg.user} / ${pg.user} · ${pg.image}` : pg.image;
  $("data-up").hidden = pg.running;
  $("data-down").hidden = !pg.running;
  const copy = data.copy;
  const refresh = copy.refresh;
  $("data-copy-alias").textContent = copy.alias || t("not copied yet");
  $("data-copy-size").textContent = mb(copy.size);
  $("data-copy-source").textContent = refresh ? refresh.source : copy.source;
  $("data-copy-when").textContent = refresh ? t("{when} · {seconds} s", { when: when(refresh.at), seconds: refresh.seconds }) : "–";
  $("data-copy-migrations").hidden = !copy.alias;
  $("data-refresh").textContent = refresh ? t("Refresh the copy") : t("Make the local copy");
  const [source, database] = (refresh ? refresh.source : copy.source).split(":");
  $("data-flow").replaceChildren(
    el("span", { class: "node src" }, el("b", {}, source), el("small", {}, t("{database} · read only", { database }))),
    el("span", { class: "arr", "aria-hidden": "true" }, "→"),
    el("span", { class: "node" }, el("b", {}, "pg_dump"), el("small", {}, t("in the Postgres image"))),
    el("span", { class: "arr", "aria-hidden": "true" }, "→"),
    el("span", { class: "node dst" }, el("b", {}, pg.container), el("small", {}, `:${pg.port || pg.default_port} / pdms`)),
    el("span", { class: "arr", "aria-hidden": "true" }, "→"),
    el("span", { class: "node" }, el("b", {}, "Flyway"), el("small", {}, t("pending migrations"))));
}

function paintSnapshots(data) {
  const running = data.postgres.running;
  $("data-snap-form").hidden = !running || !data.copy.alias;
  const rows = data.snapshots.map((snap) => el("tr", {},
    el("td", { class: "name" }, snap.name), el("td", { class: "num" }, mb(snap.size)), el("td", {}, when(snap.at)),
    el("td", { class: "row-actions" },
      button(t("Restore"), () => restoreSnapshot(snap.name)),
      button(t("Delete"), () => deleteSnapshot(snap.name), { class: "btn small ghost" }))));
  $("data-snaps").replaceChildren(...rows);
  $("data-snaps-empty").hidden = data.snapshots.length > 0;
  $("data-snaps-empty").textContent = !running ? t("pdms's Postgres is not running.")
    : t("No snapshots yet. Save one before a test that changes the copy, to come back in a second.");
}

// A test database against pdms-db-migrations: made by the tests (no Flyway), behind, or up to date.
function testMigrationsCell(db) {
  const m = db.migrations;
  if (!m) return el("span", { class: "muted" }, "–");
  if (!m.flyway) return el("span", { class: "st off", title: t("The tests made its tables from the models (or it is empty): Prepare from the local copy gives it the real schema.") }, t("tables of the tests · no Flyway"));
  return behindPill(m.pending, m.changed, m.uncommitted);
}

function testActions(db, busy) {
  const m = db.migrations;
  const disabled = busy ? { disabled: "" } : {};
  const cell = el("td", { class: "row-actions" });
  if (m && m.flyway && m.pending && !m.changed) {
    cell.append(button(t("Apply {n}", { n: m.pending }), () => migrateTestDbs([db.name]), { class: "btn small primary", ...disabled }));
  } else if (m && (!m.flyway || m.changed)) {
    cell.append(button(t("Prepare from the local copy"), () => prepareTestDbs([db.name]), { class: "btn small primary", ...disabled }));
  }
  cell.append(button(t("Recreate empty"), () => recreateTestDb(db.name), disabled));
  return cell;
}

function paintTests(data) {
  const job = state.jobs[TEST_JOB];
  const busy = Boolean(job && !job.error);
  const rows = data.tests.map((db) => el("tr", {},
    el("td", { class: "name" }, db.name), el("td", { class: "num" }, mb(db.size)), el("td", { class: "migrations" }, testMigrationsCell(db)),
    testActions(db, busy)));
  $("data-tests").replaceChildren(...rows);
  const behind = data.tests.filter((db) => db.migrations && (!db.migrations.flyway || db.migrations.pending || db.migrations.changed));
  $("data-tests-prepare").hidden = !data.tests.length || !data.copy.size;
  $("data-tests-prepare").disabled = busy;
  $("data-tests-prepare").classList.toggle("primary", behind.length > 0);
  $("data-tests-job").hidden = !job;
  $("data-tests-job").textContent = !job ? "" : job.error ? failedText(job) : phaseLabel(job.phase);
  $("data-tests-job").className = job && job.error ? "error" : "muted note";
  $("data-tests-log").hidden = !job;
  $("data-tests-empty").hidden = data.tests.length > 0;
  $("data-tests-count").value = String(data.tests.length || data.test_count);
  $("data-tests-count").disabled = !data.postgres.running;
}

export function paintData() {
  const data = dataView.data;
  if (!state) return;
  if (!data) {
    for (const id of ["data-dbs", "data-snaps", "data-tests", "data-views"]) {
      if (!$(id).children.length) $(id).replaceChildren(...skeletonRows($(id), 2));
    }
    for (const id of ["data-pg-where", "data-copy-alias", "data-copy-source", "data-copy-when", "data-copy-size"]) {
      if (!$(id).textContent) $(id).replaceChildren(el("span", { class: "sk w1" }));
    }
    return;
  }
  paintConnections(data);
  paintPostgres(data);
  paintSnapshots(data);
  paintTests(data);
  paintJob();
  paintAws();
}

export function postgresUpDown(verb) {
  if (verb === "up") postNeedingImages("/api/data/up", {}, "postgres-up");
  else act("/api/data/down", {});
}

export async function refreshCopy(confirmed = false, migrate = null) {
  const { status, data } = await postNeedingImages("/api/data/refresh", { confirmed, migrate }, "refresh");
  if (status === 409 && data.decision === "migrations_pending") {
    const answer = await askMigrations(data.dbs, "copy");
    if (answer !== null) refreshCopy(confirmed, answer);
  } else if (status === 409 && data.decision !== "images_missing"
      && await confirmDialog(t("Replace the local copy?"), data.error, t("Replace it"))) refreshCopy(true, migrate);
}

async function prepareTestDbs(names) {
  const many = names.length > 1;
  if (!await confirmDialog(many ? t("Prepare the {n} test databases from the local copy?", { n: names.length }) : t("Prepare {name} from the local copy?", { name: names[0] }),
    t("Each one becomes a copy of the local copy (its schema, Flyway's history and its little data), and gets the migrations of your checkout it lacks. What a test left in it goes; tests drop and make their tables again when they run."),
    t("Prepare"))) return;
  await postNeedingImages("/api/data/test-prepare", { names }, "");
}

async function migrateTestDbs(names) {
  await postNeedingImages("/api/data/test-migrate", { names }, "");
}

export function prepareAllTestDbs() {
  if (dataView.data) prepareTestDbs(dataView.data.tests.map((db) => db.name));
}

export function testDbsLog() {
  openLogs(TEST_JOB, "install");
}

export function copyLog() {
  openLogs(JOB, "install");
}

export function saveSnapshot(event) {
  event.preventDefault();
  const name = $("data-snap-name").value.trim();
  act("/api/data/snapshot-save", { name }, () => {
    $("data-snap-name").value = "";
    toast(t("Snapshot '{name}' saved.", { name }), "info");
    loadData();
  });
}

// Restoring stops the services that use the copy first, then starts them again as they were.
async function restoreSnapshot(name) {
  const copy = dataView.data && dataView.data.copy.alias;
  const using = state.instances.filter((inst) => copy && inst.db === copy && inst.status !== "stopped").map((inst) => inst.key);
  const steps = using.length
    ? t("1. Stops the {n} running services that use the copy ({keys}).\n2. Puts the copy back as '{name}' keeps it: about a second.\n3. Starts them again with the same user and database.", { n: using.length, keys: using.join(", "), name })
    : t("Puts the copy back as '{name}' keeps it: about a second. No running service uses it.", { name });
  const yes = using.length ? t("Restore and restart {n}", { n: using.length }) : t("Restore");
  if (!await confirmDialog(t("Restore '{name}'?", { name }), `${steps}\n\n${t("What changed in the copy since the snapshot is lost.")}`, yes)) return;
  act("/api/data/restore", { name }, () => toast(t("Restoring '{name}'…", { name }), "info"));
}

async function deleteSnapshot(name) {
  if (!await confirmDialog(t("Delete the snapshot {name}?", { name }), t("The local copy stays as it is."), t("Delete"))) return;
  act("/api/data/snapshot-delete", { name }, loadData);
}

async function recreateTestDb(name) {
  if (!await confirmDialog(t("Recreate {name} empty?", { name }),
    t("Whatever a test left in it goes. Tests create their tables again when they run."), t("Recreate"))) return;
  act("/api/data/tests-recreate", { name }, loadData);
}

export function setTestCount() {
  const count = Number($("data-tests-count").value);
  act("/api/data/tests-count", { count }, loadData);
}


// ---- test copies of materialized views (testviews.py)

export async function loadViews() {
  dataView.viewsError = "";
  $("data-views-where").textContent = t("reading…");
  const { data, error } = await getJson("/api/views");
  if (error) {
    dataView.views = null;
    dataView.viewsError = error;
  } else {
    dataView.views = data;
  }
  paintViews();
}

function viewState(row) {
  if (!row.changed) return el("span", { class: "st stopped", title: t("Your checkout's SQL is the real view's again (deployed, or undone): nothing to try.") }, t("left over: drop it"));
  if (!row.exists) return el("span", { class: "st off" }, t("not made yet"));
  if (!row.current) return el("span", { class: "st starting" }, t("your SQL changed since it was made"));
  if (row.in_use) return el("span", { class: "st ok" }, t("services read it"));
  return el("span", { class: "st ok" }, t("ready"));
}

function viewActions(row, info) {
  const cell = el("td", { class: "row-actions" });
  if (!row.name) return cell;
  if (row.changed && (!row.exists || !row.current)) {
    cell.append(button(info.can_create ? (row.exists ? t("Make it again") : t("Create")) : t("SQL to create it"), () => createView(row), { class: "btn small primary" }));
  } else if (row.changed && !row.in_use) {
    cell.append(button(t("Have services read it"), () => act("/api/views/use", { view: row.view, name: row.name }, () => {
      toast(t("Services on {db} read {name} from their next start: restart the ones running.", { db: info.db, name: row.name }), "info");
      loadViews();
    }), { class: "btn small primary" }));
  } else if (row.in_use) {
    cell.append(button(t("Read the real one"), () => act("/api/views/stop", { view: row.view }, () => {
      toast(t("Services read the real {view} again from their next start.", { view: row.view }), "info");
      loadViews();
    })));
  }
  if (row.exists && row.changed) cell.append(button(t("Refresh data"), () => refreshView(row)));
  if (row.exists) cell.append(button(t("Drop"), () => dropView(row), { class: "btn small bad" }));
  return cell;
}

export function paintViews() {
  const info = dataView.views;
  $("data-views-error").hidden = !dataView.viewsError;
  $("data-views-error").textContent = dataView.viewsError;
  if (!info) {
    $("data-views").replaceChildren();
    $("data-views-where").textContent = "";
    $("data-views-empty").hidden = true;
    return;
  }
  $("data-views-where").textContent = t("{db} · suffix {suffix} (branch {branch})", {
    db: info.db, suffix: info.suffix || t("none"), branch: info.branch || "-" });
  const using = info.views.filter((row) => row.in_use);
  $("data-views-using").hidden = !using.length;
  $("data-views-using").textContent = using.length
    ? t("Services on {db} read {names} instead of the real views.", { db: info.db, names: using.map((row) => row.name).join(", ") }) : "";
  $("data-views").replaceChildren(...info.views.map((row) => el("tr", {},
    el("td", { class: "name" }, row.view), el("td", { class: "mono" }, row.name || "–"), el("td", {}, viewState(row)),
    viewActions(row, info))));
  $("data-views-empty").hidden = info.views.length > 0;
  $("data-views-empty").textContent = t("None of the materialized views is changed in your checkout: when one is, starting a service that reads it offers its test copy.");
}

function showSql(title, text, sql) {
  $("sql-title").textContent = title;
  $("sql-text").textContent = text;
  $("sql-body").value = sql;
  $("sql-dialog").showModal();
  $("sql-body").scrollTop = 0; // from its header, which says how to drop it
}

export function copySql() {
  copyText($("sql-body").value, t("SQL copied: paste it in your database client."));
}

async function createView(row, confirmed = false) {
  const { status, data } = await post("/api/views/create", { view: row.view, confirmed });
  if (status === 409 && data.decision === "protected_database") {
    if (await confirmDialog(t("Create {name} in {db}?", { name: row.name, db: data.name }),
      t("{db} is a protected database: the copy is made beside the real view, which stays as it is.", { db: data.name }), t("Create"))) createView(row, true);
    return;
  }
  if (status >= 400) { toast(data.error || t("pdms ui answered {status}", { status })); return; }
  if (data.created) { toast(t("{name} created.", { name: data.name }), "info"); loadViews(); return; }
  showSql(t("Run this in your database client"), t("Your user cannot create views in {db}. This SQL (your checkout's, with the suffix) makes {name}; then Check again, and Have services read it.", { db: dataView.views.db, name: data.name }), data.sql);
}

async function refreshView(row, confirmed = false) {
  toast(t("Refreshing {name}…", { name: row.name }), "info");
  const { status, data } = await post("/api/views/refresh", { view: row.view, name: row.name, confirmed });
  if (status === 409 && data.decision === "protected_database") {
    if (await confirmDialog(t("Refresh {name} in {db}?", { name: row.name, db: data.name }),
      t("{db} is a protected database: only the test copy is recomputed.", { db: data.name }), t("Refresh"))) refreshView(row, true);
    return;
  }
  if (status >= 400) { toast(data.error || t("pdms ui answered {status}", { status })); return; }
  if (data.refreshed) toast(t("{name} has today's data.", { name: row.name }), "info");
  else showSql(t("Run this in your database client"), t("Only the owner of {name} can refresh it: this recomputes its data.", { name: row.name }), data.sql);
}

async function dropView(row, confirmed = false) {
  if (!confirmed && !await confirmDialog(t("Drop {name}?", { name: row.name }), t("Services read the real {view} again; the copy and its indexes go.", { view: row.view }), t("Drop"))) return;
  const { status, data } = await post("/api/views/drop", { view: row.view, name: row.name, confirmed });
  if (status === 409 && data.decision === "protected_database") { dropView(row, true); return; }
  if (status >= 400) { toast(data.error || t("pdms ui answered {status}", { status })); return; }
  if (!data.dropped) {
    showSql(t("Run this in your database client"), t("Only its owner can drop it. Services already read the real {view}.", { view: row.view }), data.sql);
  } else toast(t("{name} is gone.", { name: row.name }), "info");
  loadViews();
}
