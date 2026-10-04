// Data: the databases with their round trip and migrations, pdms's Postgres with the local copy (refresh, snapshots)
// and the test databases. Reloaded when the screen opens and when one of its jobs ends.

import { $, act, button, el, failedText, getJson, phaseLabel, post, toast } from "./core.js";
import { state } from "./state.js";
import { openFlyway } from "./migrations.js";
import { openLogs } from "./logs.js";
import { confirmDialog } from "./stacks.js";

export const dataView = { data: null, loading: false, jobWas: false };

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
}

// A job of the screen ended (Postgres started, the copy refreshed): its numbers changed.
export function syncData() {
  const job = state.jobs[JOB];
  const running = Boolean(job && !job.error);
  if (dataView.jobWas && !running && !document.getElementById("view-data").hidden) loadData();
  dataView.jobWas = running;
  if (dataView.data && !document.getElementById("view-data").hidden) paintJob();
}

function roundTrip(name) {
  const measured = (state.health.dbs || []).find((db) => db.name === name);
  if (!measured) return el("span", { class: "muted" }, "–");
  if (measured.error) return el("span", { class: "st stopped", title: measured.error }, t("no answer"));
  const slow = state.health.slow_ms && measured.ms >= state.health.slow_ms;
  return el("span", { class: slow ? "slow" : "" }, `${Math.round(measured.ms)} ms`);
}

function paintConnections(data) {
  const rows = data.dbs.map((db) => el("tr", {},
    el("td", { class: "name" }, db.name, db.copy ? el("span", { class: "tag current" }, t("local copy")) : "",
      db.protected ? el("span", { class: "tag protected" }, t("protected")) : ""),
    el("td", { class: "mono muted" }, `${db.host}:${db.port}/${db.database}`),
    el("td", { class: "num" }, roundTrip(db.name)),
    el("td", { class: "row-actions" }, button(t("Migrations"), () => openFlyway(db.name)))));
  $("data-dbs").replaceChildren(...rows);
}

function paintJob() {
  const job = state.jobs[JOB];
  $("data-job").hidden = !job;
  $("data-job").className = job && job.error ? "error" : "muted";
  $("data-job").textContent = !job ? "" : job.error ? failedText(job) : phaseLabel(job.phase);
  $("data-job-log").hidden = !(job && job.installed);
  for (const id of ["data-up", "data-down", "data-refresh"]) $(id).disabled = Boolean(job && !job.error);
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
  act(`/api/data/${verb}`, {});
}

export async function refreshCopy(confirmed = false) {
  try {
    const { status, data } = await post("/api/data/refresh", { confirmed });
    if (status === 409) {
      if (await confirmDialog(t("Replace the local copy?"), data.error, t("Replace it"))) refreshCopy(true);
      return;
    }
    if (status >= 400) toast(data.error || t("pdms ui answered {status}", { status }));
  } catch {
    toast(t("pdms ui is not reachable: is it still running?"));
  }
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

async function restoreSnapshot(name) {
  if (!await confirmDialog(t("Restore '{name}'?", { name }),
    t("The local copy goes back to what this snapshot keeps; what changed since is lost. Services using it reconnect."),
    t("Restore"))) return;
  act("/api/data/snapshot-restore", { name }, () => { toast(t("Local copy restored from '{name}'.", { name }), "info"); loadData(); });
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
