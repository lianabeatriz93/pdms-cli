// The Flyway migrations of a database (read-only).

import { $, el, post } from "./core.js";
import { matches } from "./settings.js";

export const flywayView = { db: "", data: null };

const FLYWAY_STATES = {
  applied: N_("applied"), pending: N_("pending"), failed: N_("failed"), outdated: N_("changed: runs again"),
  missing: N_("not in this checkout"),
};

const FLYWAY_TODO = new Set(["pending", "failed", "outdated"]);

export async function openFlyway(db) {
  flywayView.db = db;
  flywayView.data = null;
  $("flyway-title").textContent = t("Migrations of {name}", { name: db });
  $("flyway-repo").textContent = t("Reading the Flyway history…");
  $("flyway-summary").replaceChildren();
  $("flyway-rows").replaceChildren();
  $("flyway-never").hidden = $("flyway-error").hidden = true;
  if (!$("flyway-dialog").open) $("flyway-dialog").showModal();
  $("flyway-refresh").disabled = true;
  try {
    const { status, data } = await post("/api/migrations/status", { db });
    if (flywayView.db !== db) return;
    if (status !== 200) {
      $("flyway-repo").textContent = "";
      $("flyway-error").textContent = data.error || t("pdms ui answered {status}", { status });
      $("flyway-error").hidden = false;
      return;
    }
    flywayView.data = data;
    $("flyway-pending").checked = data.counts.pending + data.counts.failed + data.counts.outdated > 0;
    paintFlyway();
  } catch {
    $("flyway-error").textContent = t("pdms ui is not reachable: is it still running?");
    $("flyway-error").hidden = false;
  } finally {
    $("flyway-refresh").disabled = false;
  }
}

export function paintFlyway() {
  const data = flywayView.data;
  if (!data) return;
  $("flyway-repo").textContent = data.branch ? `${data.repo} · ${data.branch}` : data.repo;
  const pills = [];
  for (const [state, label] of [["pending", t("{n} pending", { n: data.counts.pending })], ["outdated", t("{n} changed", { n: data.counts.outdated })],
    ["failed", t("{n} failed", { n: data.counts.failed })], ["applied", t("{n} applied", { n: data.counts.applied })],
    ["missing", t("{n} not in this checkout", { n: data.counts.missing })]]) {
    if (data.counts[state] || state === "pending") pills.push(el("span", { class: `st ${state}` }, label));
  }
  $("flyway-summary").replaceChildren(...pills);
  $("flyway-never").hidden = data.flyway;
  $("flyway-never").textContent = data.flyway ? "" : t("Flyway has not run on this database yet (there is no {table}): every migration is pending.", { table: data.table });
  const text = $("flyway-filter").value.trim().toLowerCase();
  const onlyTodo = $("flyway-pending").checked;
  const shown = data.migrations.filter((m) => (!onlyTodo || FLYWAY_TODO.has(m.state)) && matches(text, [m.version, m.description, m.script]));
  $("flyway-rows").replaceChildren(...shown.map((m) => el("tr", {},
    el("td", {}, el("span", { class: `st ${m.state}` }, t(FLYWAY_STATES[m.state]))),
    el("td", { class: "mono" }, m.version || t("repeatable")),
    el("td", { title: m.script }, m.description),
    el("td", { class: "mono" }, m.installed_on ? new Date(m.installed_on).toLocaleString() : ""))));
  $("flyway-count").textContent = t("{shown} of {total}", { shown: shown.length, total: data.migrations.length });
  $("flyway-empty").hidden = shown.length > 0;
  $("flyway-empty").textContent = onlyTodo && !text ? t("Nothing to run: the database is up to date with this checkout.") : t("No migration matches the filter.");
}
