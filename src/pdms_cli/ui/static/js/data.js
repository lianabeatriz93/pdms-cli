// Data: the databases with their round trip and migrations, pdms's Postgres with the local copy (refresh, snapshots)
// and the test databases. Reloaded when the screen opens and when one of its jobs ends.

import { $, act, button, el, failedText, getJson, phaseLabel, post, postNeedingImages, toast } from "./core.js";
import { state } from "./state.js";
import { openFlyway } from "./migrations.js";
import { openLogs } from "./logs.js";
import { confirmDialog } from "./stacks.js";
import { dbLevel, roundTrip as roundTripText, routeText, sparkline } from "./status.js";

// migrations: name → { loading, counts, flyway, error }, read once per database when the screen opens.
export const dataView = { data: null, loading: false, jobWas: false, migrations: {}, migrating: new Set() };

const JOB = "data:postgres";

function mb(bytes) {
  return bytes || bytes === 0 ? `${(bytes / 1048576).toFixed(1)} MB` : "–";
}

function when(iso) {
  return iso ? new Date(iso).toLocaleString([], { dateStyle: "short", timeStyle: "short" }) : "–";
}

export async function loadData() {
  if (dataView.loading) return;
  dataView.loading = true;
  const { data, error } = await getJson("/api/data");
  dataView.loading = false;
  if (error) { toast(error); return; }
  dataView.data = data;
  paintData();
  post("/api/health/run", { all: true }).catch(() => {}); // every database measured while the screen is open
  for (const db of data.dbs) if (!dataView.migrations[db.name]) loadMigrations(db.name);
}

// The migrations of one database, read from its Flyway history (a round trip or two: web-dev takes a moment).
async function loadMigrations(name) {
  dataView.migrations[name] = { loading: true };
  paintConnections(dataView.data);
  try {
    const { status, data } = await post("/api/migrations/status", { db: name });
    dataView.migrations[name] = status === 200 ? { counts: data.counts, flyway: data.flyway } : { error: data.error || String(status) };
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
  if (dataView.jobWas && !running && visible) loadData();
  dataView.jobWas = running;
  for (const name of [...dataView.migrating]) {
    const migrate = state.jobs[`migrate:${name}`];
    if (!migrate || migrate.error) {
      dataView.migrating.delete(name);
      if (!migrate && visible) loadMigrations(name);
    }
  }
  if (dataView.data && visible) { paintJob(); paintConnections(dataView.data); }
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
  const todo = known.counts.pending + known.counts.failed + known.counts.outdated;
  return todo ? el("span", { class: "st starting" }, t("{n} pending", { n: todo })) : el("span", { class: "st ok" }, t("up to date"));
}

// Only on a local database Flyway already manages: one without its history has tables made another way (the ORM,
// Alembic), and Flyway's baselineOnMigrate would run every migration over them.
function applyButton(db) {
  const known = dataView.migrations[db.name];
  if (!db.local || !known || !known.counts || !known.flyway || dataView.migrating.has(db.name)) return "";
  const todo = known.counts.pending + known.counts.failed + known.counts.outdated;
  if (!todo) return "";
  return button(t("Apply {n}", { n: todo }), () => applyMigrations(db.name, todo), { class: "btn small primary" });
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
      el("td", {}, migrationsCell(db)),
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

function paintTests(data) {
  const rows = data.tests.map((db) => el("tr", {},
    el("td", { class: "name" }, db.name), el("td", { class: "num" }, mb(db.size)),
    el("td", { class: "row-actions" }, button(t("Recreate empty"), () => recreateTestDb(db.name)))));
  $("data-tests").replaceChildren(...rows);
  $("data-tests-empty").hidden = data.tests.length > 0;
  $("data-tests-count").value = String(data.tests.length || data.test_count);
  $("data-tests-count").disabled = !data.postgres.running;
}

export function paintData() {
  const data = dataView.data;
  if (!data || !state) return;
  paintConnections(data);
  paintPostgres(data);
  paintSnapshots(data);
  paintTests(data);
  paintJob();
}

export function postgresUpDown(verb) {
  if (verb === "up") postNeedingImages("/api/data/up", {}, "postgres-up");
  else act("/api/data/down", {});
}

export async function refreshCopy(confirmed = false) {
  const { status, data } = await postNeedingImages("/api/data/refresh", { confirmed }, "refresh");
  if (status === 409 && data.decision !== "images_missing"
      && await confirmDialog(t("Replace the local copy?"), data.error, t("Replace it"))) refreshCopy(true);
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
