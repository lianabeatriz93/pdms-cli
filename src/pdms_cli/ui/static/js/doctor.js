// Doctor: the checks of pdms doctor, their fixes and the sidebar badge.

import { $, act, button, copyText, el, getJson, shortPaths, toast } from "./core.js";
import { state } from "./state.js";
import { adoptStrays } from "./services.js";
import { matches, openDb, openUser } from "./settings.js";
import { fixFrontendApi } from "./frontend.js";
import { openUpdate } from "./updates.js";
import { openRepo, openSetting } from "./repos.js";

export const doctorView = { latest: null, at: null };

const CHECK_ICONS = { ok: "✓", warn: "!", fail: "✗" };

// What Home already shows by itself (services, updates), so its Doctor lines leave them out.
export const HOME_COVERS = new Set(["services", "forget_stopped", "update", "adopt", "frontend_api"]);

// A hint that is a command to run, shown with a Copy button.
const COMMAND = /^(pdms|chmod|nvm|npm|uv|sudo|yarn) \S.*[^.]$/;

export function paintDoctorBadge() {
  const counts = state.doctor.counts || {};
  const problems = (counts.warn || 0) + (counts.fail || 0);
  $("doctor-badge").hidden = !problems;
  $("doctor-badge").textContent = String(problems);
  $("doctor-badge").classList.toggle("bad", Boolean(counts.fail));
  $("doctor-badge").title = t("{fail} problems · {warn} warnings", { fail: counts.fail || 0, warn: counts.warn || 0 });
}

export async function loadDoctor() {
  doctorView.at = state ? state.doctor.at : null;
  const { data, error } = await getJson("/api/doctor");
  if (error) { toast(error); return; }
  doctorView.latest = data.latest;
  paintDoctor();
}

function fixLabel(fix) {
  const [kind, name] = fix.split(/:(.*)/s);
  if (kind === "update") return t("Update…");
  if (kind === "add_user") return t("Add a user");
  if (kind === "add_db") return t("Add a database");
  if (kind === "edit_db") return t("Edit {name}", { name });
  if (kind === "add_repo") return t("Add repo");
  if (kind === "repos") return t("Open Repos");
  if (kind === "edit_repo") return t("Edit {name}", { name });
  if (kind === "forget_stopped") return t("Forget stopped");
  if (kind === "adopt") return t("Adopt all");
  if (kind === "frontend_api") return t("Fix it");
  if (kind === "pull_image") return t("Download");
  return t("Open Services");
}

function runFix(fix) {
  const [kind, name] = fix.split(/:(.*)/s);
  if (kind === "update") openUpdate();
  else if (kind === "add_user") openSetting("users", () => openUser());
  else if (kind === "add_db") openSetting("dbs", () => openDb());
  else if (kind === "edit_db") openSetting("dbs", (data) => { const db = data.dbs.find((d) => d.name === name); if (db) openDb(db); });
  else if (kind === "add_repo") openSetting("repos", () => openRepo());
  else if (kind === "repos") openSetting("repos");
  else if (kind === "edit_repo") openSetting("repos", (data) => { const repo = data.repos.find((r) => r.name === name); if (repo) openRepo(repo); });
  else if (kind === "frontend_api") fixFrontendApi();
  else if (kind === "pull_image") pullImage(name);
  else if (kind === "adopt") adoptStrays(null, () => act("/api/doctor/run", { databases: false }));
  else if (kind === "forget_stopped") act("/api/clean", {}, (data) => { toast(t("Forgot {n} stopped.", { n: data.forgotten.length }), "info"); act("/api/doctor/run", { databases: false }); });
  else location.hash = "#services";
}

// Asked first: images are hundreds of MB. Doctor runs again once it is downloaded (the job's end repaints).
async function pullImage(name) {
  const { confirmDialog } = await import("./stacks.js");
  if (!await confirmDialog(t("Download {image}?", { image: name }), t("It runs in the background; the log shows its progress."), t("Download"))) return;
  act("/api/images/pull", { names: [name] }, () => toast(t("Downloading {image} in the background.", { image: name }), "info"));
}

export function fixButton(check) {
  return button(`${fixLabel(check.fix)} →`, () => runFix(check.fix));
}

function checkRow(check) {
  const row = el("div", { class: `check ${check.status === "ok" ? "" : check.status}` },
    el("span", { class: `icon ${check.status}`, "aria-label": statusWord(check.status) }, CHECK_ICONS[check.status] || "?"),
    el("div", { class: "what" }, el("b", {}, check.name), el("span", { class: "detail", title: check.detail }, shortPaths(check.detail))));
  if (check.status !== "ok" && (check.hint || check.fix)) {
    const hint = el("div", { class: "hint" });
    if (check.fix) hint.append(el("span", { class: "muted" }, check.hint), fixButton(check));
    else if (COMMAND.test(check.hint)) hint.append(el("code", {}, check.hint), button(t("Copy"), () => copyText(check.hint, t("Copied."))));
    else hint.append(el("span", { class: "muted" }, check.hint));
    row.append(hint);
  }
  return row;
}

function statusWord(status) {
  return status === "ok" ? t("ok") : status === "warn" ? t("warning") : t("problem");
}

export function paintDoctor() {
  const latest = doctorView.latest;
  const running = state && state.doctor.running;
  $("doctor-running").hidden = !running;
  $("doctor-run").disabled = Boolean(running);
  $("doctor-error").hidden = !(latest && latest.error);
  $("doctor-error").textContent = latest ? latest.error : "";
  if (!latest) { $("doctor-checks").replaceChildren(); $("doctor-summary").replaceChildren(); $("doctor-when").textContent = ""; return; }
  const checks = latest.checks;
  const count = (status) => checks.filter((c) => c.status === status).length;
  const summary = [el("span", { class: "st ok" }, t("{n} ok", { n: count("ok") }))];
  if (count("warn")) summary.push(el("span", { class: "st warn" }, count("warn") === 1 ? t("1 warning") : t("{n} warnings", { n: count("warn") })));
  if (count("fail")) summary.push(el("span", { class: "st fail" }, count("fail") === 1 ? t("1 problem") : t("{n} problems", { n: count("fail") })));
  $("doctor-summary").replaceChildren(...summary);
  $("doctor-when").textContent = t("Last run {when} · took {seconds} s{dbs}", {
    when: new Date(latest.at).toLocaleTimeString(), seconds: latest.took,
    dbs: latest.databases ? "" : " · " + t("databases not tested"),
  });
  const text = $("doctor-filter").value.trim().toLowerCase();
  const problemsOnly = $("doctor-problems").checked;
  const worst = (section) => checks.some((c) => c.section === section && c.status === "fail") ? 0
    : checks.some((c) => c.section === section && c.status === "warn") ? 1 : 2;
  const sections = [...new Set(checks.map((c) => c.section))].sort((a, b) => worst(a) - worst(b));
  const cards = [];
  for (const section of sections) {
    const all = checks.filter((c) => c.section === section);
    const shown = all.filter((c) => (!problemsOnly || c.status !== "ok") && matches(text, [section, c.name, c.detail, c.hint]));
    if (!shown.length) continue;
    const worst = all.some((c) => c.status === "fail") ? "fail" : all.some((c) => c.status === "warn") ? "warn" : "ok";
    const label = worst === "ok" ? t("all ok") : worst === "warn" ? t("warning") : t("problem");
    cards.push(el("section", { class: "card check-section" },
      el("h2", {}, section, el("span", { class: `st ${worst}` }, label)), ...shown.map(checkRow)));
  }
  $("doctor-checks").replaceChildren(...cards);
  $("doctor-none").hidden = cards.length > 0 || !checks.length;
}

export function runDoctor() {
  act("/api/doctor/run", { databases: $("doctor-dbs").checked });
}

export function doctorReport() {
  const latest = doctorView.latest;
  if (!latest) return;
  const lines = [`pdms ${state.version} · doctor · ${latest.at}`];
  for (const c of latest.checks) lines.push(`[${c.status}] ${c.section} · ${c.name}: ${c.detail}${c.hint && c.status !== "ok" ? `  → ${c.hint}` : ""}`);
  copyText(lines.join("\n"), t("Report copied: paste it in a chat or an issue."));
}
