// Settings: databases, users, defaults, export and import, and users from a database.

import { $, act, button, el, getJson, post, shortPath, toast } from "./core.js";
import { setThemePreview, state, useTheme } from "./state.js";
import { dbLabel, options } from "./launch.js";
import { confirmDialog } from "./stacks.js";
import { paintRepos } from "./repos.js";
import { currentView } from "./router.js";

const REVEAL_FOR = 30000; // a shown password hides again by itself

export const settingsView = {
  tab: "repos", data: null, seen: "", revealed: {}, timers: {}, tests: {}, db: null, user: null, passwordTouched: false,
  protectedTouched: false,
};

// [key, label, kind, help]: the rows of the Defaults tab, in the order of pdms config defaults (label and help
// translated when painted).
const THEME_NAMES = { system: N_("Like the computer"), light: N_("Light"), dark: N_("Dark") };

const DEFAULTS = [
  ["language", N_("Language"), "select", N_("Of the CLI, of this page and of the messages pdms ui gets from the CLI.")],
  ["theme", N_("Look"), "theme", N_("Light or dark like the computer, or always one of them. Try one: this page changes as you pick it.")],
  ["host", N_("uvicorn host"), "text", N_("Where services listen (0.0.0.0: every interface).")],
  ["port", N_("Default port"), "number", N_("The first port tried for a service; the next free one when it is busy.")],
  ["logging_level", N_("LOGGING_LEVEL"), "select", N_("Passed to every service.")],
  ["reload", N_("Reload on code changes"), "check", N_("uvicorn --reload.")],
  ["install", N_("Install dependencies before starting"), "check", N_("poetry lock && poetry install.")],
  ["smart_install", N_("Smart install"), "check", N_("Skip the install when nothing that affects it changed since the last one.")],
  ["events", N_("Where services publish SQS events"), "select", N_("auto: the local broker while pdms events up runs; local: always; aws: as each service is configured.")],
  ["events_port", N_("Local ElasticMQ port"), "number", N_("Host port of the ElasticMQ that pdms events up starts.")],
  ["db_timeout", N_("Connection test timeout"), "number", N_("Seconds to wait when testing a database.")],
  ["proxy_port", N_("Proxy port"), "number", N_("Where pdms proxy listens (pdms proxy, Start everything); the next free one when it is busy.")],
  ["proxy_timeout", N_("Proxy timeout"), "number", N_("Seconds the proxy waits for a service or the remote API before answering 502. Slow databases need more; applies when the proxy starts.")],
  ["banner", N_("Show the PDMS banner"), "check", N_("When the interactive menu opens.")],
  ["update_check", N_("Tell me about new pdms versions"), "check", N_("Checked at most once a day.")],
  ["ui_at_login", N_("Open pdms ui when I log in"), "check", N_("In the tray, without its window. To have it in the app menu too: pdms ui --install.")],
  ["parallel_requests", N_("Requests in parallel"), "check", N_("Each request of a service runs in its own thread, so a slow database query only holds up its own request. Applies when a service starts.")],
  ["query_stats", N_("Database time per request"), "check", N_("Each request of a service says how long it spent in the database and which queries it ran; Proxy shows them with the request. Applies when a service starts.")],
  ["warm_connections", N_("Connections opened at start"), "number", N_("Database connections each service opens as soon as it starts, so its first requests don't wait for them (0: when needed). Applies when a service starts.")],
  ["notify", N_("Desktop notifications"), "check", N_("When a service fails to load or stops by itself, while pdms ui runs (also from the tray).")],
  ["env", N_("Extra environment variables"), "env", N_("Injected on every run, after the profile's own.")],
];

function settingsPath(kind, name, verb) {
  return `/api/${kind}/${encodeURIComponent(name)}/${verb}`;
}

export function matches(text, values) {
  return !text || values.join(" ").toLowerCase().includes(text);
}

export async function loadSettings() {
  const { data, error } = await getJson("/api/config");
  if (error) { toast(error); return; }
  settingsView.data = data;
  paintSettings();
}

// The CLI may change the databases or users while the tab is open: the state's names tell when to load them again.
export function syncSettings() {
  const seen = JSON.stringify([state.users, state.dbs, state.repos, state.repo]);
  if (seen === settingsView.seen) return;
  settingsView.seen = seen;
  if (currentView() === "settings") loadSettings();
}

export function paintSettings() {
  for (const tab of $("settings-tabs").children) tab.setAttribute("aria-selected", String(tab.dataset.tab === settingsView.tab));
  for (const name of ["repos", "dbs", "users", "defaults"]) $(`settings-${name}`).hidden = name !== settingsView.tab;
  const data = settingsView.data;
  if (!data) return;
  const dbs = data.dbs.length;
  const users = data.users.length;
  $("settings-summary").textContent = [
    dbs === 1 ? t("{n} database", { n: dbs }) : t("{n} databases", { n: dbs }),
    users === 1 ? t("{n} user", { n: users }) : t("{n} users", { n: users }),
  ].join(" · ");
  $("settings-path").textContent = shortPath(data.path);
  $("settings-path").title = data.path;
  paintRepos();
  paintDbs();
  paintUsers();
  if (!defaultsChanged()) paintDefaults();
}

export function showSettingsTab(tab) {
  settingsView.tab = tab;
  paintSettings();
}

function eyeButton(shown, onclick) {
  const label = shown ? t("Hide the password") : t("Show the password");
  return button("", onclick, { class: "btn small ghost eye", "aria-label": label, title: label, "aria-pressed": String(shown) });
}

// kind: "dbs" or "users".
function usedBy(stacks, kind) {
  if (!stacks.length) return t("Only its entry in the configuration goes.");
  if (stacks.length === 1) {
    return kind === "dbs"
      ? t("The stack {stack} uses it: it will ask for a database when it starts.", { stack: stacks[0] })
      : t("The stack {stack} uses it: it will ask for a user when it starts.", { stack: stacks[0] });
  }
  return kind === "dbs"
    ? t("The stacks {stacks} use it: they will ask for a database when they start.", { stacks: stacks.join(", ") })
    : t("The stacks {stacks} use it: they will ask for a user when they start.", { stacks: stacks.join(", ") });
}

// ---- databases

function dbRow(db) {
  const revealed = settingsView.revealed[db.name];
  const password = el("td");
  if (!db.has_password) {
    password.append(el("span", { class: "not-set" }, t("not set")));
  } else {
    password.append(el("span", { class: "secret-text" }, revealed === undefined ? "••••••••" : revealed));
    password.append(eyeButton(revealed !== undefined, () => toggleReveal(db.name)));
  }
  const test = settingsView.tests[db.name];
  const connection = el("td", { class: "wrap-detail" });
  if (test && test.busy) connection.append(el("span", { class: "st starting" }, t("testing…")));
  else if (test && test.ok) connection.append(el("span", { class: "st ok" }, t("ok")), " ", el("span", { class: "muted" }, test.text));
  else if (test) connection.append(el("span", { class: "st stopped" }, t("failed")), el("span", { class: "detail" }, test.text));
  const name = el("td", { class: "mono" }, db.name);
  if (db.protected) name.append(el("span", { class: "tag protected" }, t("protected")));
  const actionsCell = el("td", { class: "row-actions" },
    button(t("Test"), () => testDb(db.name), test && test.busy ? { disabled: "" } : {}),

    button(t("Edit"), () => openDb(db)),
    button(t("Delete"), () => removeSetting("dbs", db.name, db.stacks), { class: "btn small bad" }),
  );
  return el("tr", {}, name, el("td", { class: "mono" }, db.host), el("td", { class: "num" }, String(db.port)),
    el("td", { class: "mono" }, db.database), el("td", { class: "mono" }, db.user), password, connection, actionsCell);
}

export function paintDbs() {
  const dbs = settingsView.data.dbs;
  const text = $("db-filter").value.trim().toLowerCase();
  const shown = dbs.filter((db) => (!$("db-protected").checked || db.protected)
    && matches(text, [db.name, db.host, db.port, db.database, db.user]));
  $("db-rows").replaceChildren(...shown.map(dbRow));
  $("db-count").textContent = dbs.length ? t("{shown} of {total}", { shown: shown.length, total: dbs.length }) : "";
  $("db-empty").hidden = shown.length > 0;
  $("db-empty").textContent = dbs.length ? t("No database matches the filter.") : t("No databases yet. Services need at least one to run.");
}

async function toggleReveal(name) {
  clearTimeout(settingsView.timers[name]);
  if (settingsView.revealed[name] !== undefined) {
    delete settingsView.revealed[name];
    paintDbs();
    return;
  }
  await act(settingsPath("dbs", name, "password"), {}, (data) => {
    settingsView.revealed[name] = data.password;
    settingsView.timers[name] = setTimeout(() => { delete settingsView.revealed[name]; paintDbs(); }, REVEAL_FOR);
    paintDbs();
  });
}

async function testDb(name) {
  settingsView.tests[name] = { busy: true };
  paintDbs();
  const result = await testConnection({ name });
  settingsView.tests[name] = result;
  paintDbs();
}

async function testConnection(body) {
  try {
    const { status, data } = await post("/api/dbs/test", body);
    return status === 200 ? { ok: true, text: data.version } : { ok: false, text: data.error || t("pdms ui answered {status}", { status }), field: data.field };
  } catch {
    return { ok: false, text: t("pdms ui is not reachable: is it still running?") };
  }
}

async function removeSetting(kind, name, stacks) {
  const title = kind === "dbs" ? t("Delete database {name}?", { name }) : t("Delete user {name}?", { name });
  if (!await confirmDialog(title, usedBy(stacks, kind), t("Delete"))) return;
  await act(settingsPath(kind, name, "remove"), {}, () => {
    delete settingsView.revealed[name];
    delete settingsView.tests[name];
    toast(t("'{name}' deleted.", { name }), "info");
    loadSettings();
  });
}

// ---- forms of a database and a user

export function formError(prefix, data, fallback) {
  const node = $(`${prefix}-error`);
  node.textContent = data.error || fallback;
  node.hidden = false;
  const input = data.field && $(`${prefix}-${data.field}`);
  if (input) {
    input.setAttribute("aria-invalid", "true");
    input.focus();
  }
}

export function resetForm(prefix, fields) {
  $(`${prefix}-error`).hidden = true;
  for (const field of fields) $(`${prefix}-${field}`).removeAttribute("aria-invalid");
}

const DB_FIELDS = ["name", "host", "port", "database", "user", "password"];

const USER_FIELDS = ["name", "username", "first_name", "last_name", "roles", "user_id"];

const USER_TEXTS = USER_FIELDS.filter((field) => field !== "roles");

function showPassword(shown) {
  $("db-password").type = shown ? "text" : "password";
  $("db-eye").setAttribute("aria-pressed", String(shown));
  $("db-eye").setAttribute("aria-label", shown ? t("Hide the password") : t("Show the password"));
}

export function openDb(db = null) {
  Object.assign(settingsView, { db, passwordTouched: false, protectedTouched: Boolean(db) });
  $("db-title").textContent = db ? t("Edit {name}", { name: db.name }) : t("New database");
  $("db-name-label").hidden = Boolean(db);
  $("db-name").required = !db;
  $("db-name").value = "";
  $("db-host").value = db ? db.host : "localhost";
  $("db-port").value = db ? db.port : 5432;
  $("db-database").value = db ? db.database : "pdm";
  $("db-user").value = db ? db.user : "";
  $("db-password").value = "";
  $("db-password").placeholder = db && db.has_password ? t("unchanged") : "";
  $("db-protected-box").checked = Boolean(db && db.protected);
  showPassword(false);
  $("db-tested").hidden = true;
  resetForm("db", DB_FIELDS);
  $("db-dialog").showModal();
  (db ? $("db-host") : $("db-name")).focus();
}

// The password of a database being edited is left out (kept) unless it was typed or shown.
function dbBody() {
  const db = settingsView.db;
  return {
    host: $("db-host").value, port: $("db-port").value === "" ? "" : Number($("db-port").value),
    database: $("db-database").value, user: $("db-user").value, protected: $("db-protected-box").checked,
    password: db && !settingsView.passwordTouched ? null : $("db-password").value,
  };
}

export async function toggleDbPassword() {
  const shown = $("db-eye").getAttribute("aria-pressed") === "true";
  const db = settingsView.db;
  if (!shown && db && db.has_password && !settingsView.passwordTouched) {
    const { status, data } = await post(settingsPath("dbs", db.name, "password")).catch(() => ({ status: 0, data: {} }));
    if (status !== 200) { formError("db", data, t("Could not read the password.")); return; }
    $("db-password").value = data.password;
    settingsView.passwordTouched = true;
  }
  showPassword(!shown);
}

export async function testDbForm() {
  resetForm("db", DB_FIELDS);
  $("db-test").disabled = true;
  $("db-tested").className = "muted";
  $("db-tested").textContent = t("Connecting…");
  $("db-tested").hidden = false;
  const result = await testConnection({ ...dbBody(), name: settingsView.db ? settingsView.db.name : "" });
  $("db-test").disabled = false;
  $("db-tested").className = result.ok ? "muted" : "error";
  $("db-tested").textContent = result.ok ? t("✓ Connected: {version}", { version: result.text }) : result.text;
  if (result.field && $(`db-${result.field}`)) $(`db-${result.field}`).setAttribute("aria-invalid", "true");
}

async function saveForm(prefix, kind, current, body, fields) {
  resetForm(prefix, fields);
  const name = current ? current.name : $(`${prefix}-name`).value.trim();
  $(`${prefix}-save`).disabled = true;
  try {
    const { status, data } = await post(settingsPath(kind, name, "save"), { ...body, new: !current });
    if (status === 200) {
      $(`${prefix}-dialog`).close();
      toast(t("'{name}' saved.", { name: data.name }), "info");
      delete settingsView.tests[data.name];
      loadSettings();
      return;
    }
    formError(prefix, data, t("pdms ui answered {status}", { status }));
  } catch {
    formError(prefix, {}, t("pdms ui is not reachable: is it still running?"));
  } finally {
    $(`${prefix}-save`).disabled = false;
  }
}

export function saveDb(event) {
  event.preventDefault();
  saveForm("db", "dbs", settingsView.db, dbBody(), DB_FIELDS);
}

// ---- users

function userRow(user) {
  return el("tr", {},
    el("td", { class: "mono" }, user.name), el("td", {}, user.username),
    el("td", {}, `${user.first_name} ${user.last_name}`.trim() || "-"), el("td", { class: "mono" }, user.roles || "-"),
    el("td", { class: "mono muted" }, user.user_id),
    el("td", { class: "row-actions" },
      button(t("Edit"), () => openUser(user)),
      button(t("Delete"), () => removeSetting("users", user.name, user.stacks), { class: "btn small bad" })),
  );
}

export function paintUsers() {
  const users = settingsView.data.users;
  const text = $("user-filter").value.trim().toLowerCase();
  const shown = users.filter((u) => matches(text, [u.name, u.username, u.first_name, u.last_name, u.roles, u.user_id]));
  $("user-rows").replaceChildren(...shown.map(userRow));
  $("user-count").textContent = users.length ? t("{shown} of {total}", { shown: shown.length, total: users.length }) : "";
  $("user-empty").hidden = shown.length > 0;
  $("user-empty").textContent = users.length ? t("No user matches the filter.") : t("No users yet. Services run as one of them.");
}

export function openUser(user = null) {
  settingsView.user = user;
  $("user-title").textContent = user ? t("Edit {name}", { name: user.name }) : t("New user");
  $("user-name").required = true;
  $("user-rename-note").hidden = !user || !user.stacks.length;
  for (const field of USER_TEXTS) $(`user-${field}`).value = user ? user[field === "name" ? "name" : field] : "";
  paintRoles(user ? user.roles : "");
  resetForm("user", USER_FIELDS);
  $("user-dialog").showModal();
  (user ? $("user-username") : $("user-name")).focus();
}

function splitRoles(roles) {
  return [...new Set(roles.split(",").map((role) => role.trim()).filter(Boolean))];
}

// The repo's roles to tick; a role of the user the repo does not know shows too (ticked), so editing keeps it.
function paintRoles(current) {
  const mine = splitRoles(current);
  const known = settingsView.data.roles;
  const box = (role, extra) => {
    const input = el("input", { type: "checkbox", value: role });
    input.checked = mine.includes(role);
    return el("label", { class: "inline" }, input, role, ...extra);
  };
  $("user-roles").replaceChildren(
    ...known.map((role) => box(role, [])),
    ...mine.filter((role) => !known.includes(role)).map((role) => box(role, [el("span", { class: "tag" }, t("not a role of this repo"))])),
  );
}

// A user being edited may get another name first (pdms user rename), then the rest is saved under it.
export async function saveUser(event) {
  event.preventDefault();
  const body = Object.fromEntries(USER_TEXTS.filter((f) => f !== "name").map((f) => [f, $(`user-${f}`).value]));
  body.roles = [...$("user-roles").querySelectorAll("input:checked")].map((input) => input.value).join(",");
  const current = settingsView.user;
  const name = $("user-name").value.trim();
  if (current && name !== current.name) {
    resetForm("user", USER_FIELDS);
    const { status, data } = await post(settingsPath("users", current.name, "rename"), { new_name: name })
      .catch(() => ({ status: 0, data: { error: t("pdms ui is not reachable: is it still running?") } }));
    if (status !== 200) {
      formError("user", data, t("pdms ui answered {status}", { status }));
      return;
    }
    settingsView.user = { ...current, name: data.name };
  }
  saveForm("user", "users", settingsView.user, body, USER_FIELDS);
}

// ---- defaults

function settingInput(key, kind, value) {
  const id = `default-${key}`;
  if (kind === "check") {
    const box = el("input", { type: "checkbox", id });
    box.checked = Boolean(value);
    return box;
  }
  if (kind === "select") {
    const select = el("select", { id });
    const choices = settingsView.data.choices[key];
    const names = key === "theme" ? Object.fromEntries(choices.map((c) => [c, t(THEME_NAMES[c] || c)]))
      : Array.isArray(choices) ? Object.fromEntries(choices.map((c) => [c, c])) : choices;
    options(select, Object.keys(names), value, (code) => names[code]);
    return select;
  }
  if (kind === "theme") {
    // Cards with a small picture of each look; picking one shows it on this page until saved or discarded.
    const group = el("div", { class: "theme-cards", id, role: "radiogroup", "aria-label": t("Look") });
    for (const choice of settingsView.data.choices.theme) {
      const radio = el("input", { type: "radio", name: "default-theme", value: choice, onchange: () => { setThemePreview(choice); useTheme(choice); } });
      radio.checked = choice === value;
      group.append(el("label", {}, radio, el("span", { class: `swatch ${choice}` }, el("i")), t(THEME_NAMES[choice] || choice)));
    }
    return group;
  }
  if (kind === "env") {
    const rows = el("div", { class: "env-rows", id });
    for (const [name, text] of Object.entries(value)) rows.append(envRow(name, text));
    rows.append(button(t("Add variable"), () => { rows.lastChild.before(envRow("", "")); defaultsChanged(); rows.lastChild.previousSibling.firstChild.focus(); }));
    return rows;
  }
  return el("input", kind === "number" ? { type: "number", id, min: key === "warm_connections" ? "0" : "1", value: String(value) } : { id, value });
}

function envRow(name, value) {
  const row = el("div", { class: "env-row" },
    el("input", { value: name, placeholder: t("NAME"), "aria-label": t("Variable name"), spellcheck: "false" }),
    el("input", { value, placeholder: t("value"), "aria-label": t("Value"), spellcheck: "false" }));
  row.append(button(t("Remove"), () => { row.remove(); defaultsChanged(); }, { class: "btn small ghost" }));
  return row;
}

export function paintDefaults() {
  const values = settingsView.data.defaults;
  // What is saved; right after a save the state may still bring the old one for a moment.
  setThemePreview(values.theme !== state.theme ? values.theme : "");
  useTheme(values.theme);
  $("defaults-form").replaceChildren(...DEFAULTS.map(([key, label, kind, help]) => {
    const what = el("div", { class: "what" }, el("span", {}, el("b", {}, t(label)), el("code", {}, key)), el("small", {}, t(help)));
    const row = el(kind === "env" || kind === "theme" ? "div" : "label", { class: "setting", "data-key": key, "data-search": `${key} ${t(label)} ${t(help)}`.toLowerCase() },
      what, settingInput(key, kind, values[key]));
    if (kind !== "env" && kind !== "theme") row.setAttribute("for", `default-${key}`);
    return row;
  }));
  $("defaults-error").hidden = true;
  filterDefaults();
  defaultsChanged();
}

function envValues() {
  const env = {};
  for (const row of $("default-env").querySelectorAll(".env-row")) {
    const [name, value] = row.querySelectorAll("input");
    if (name.value.trim() || value.value) env[name.value.trim()] = value.value;
  }
  return env;
}

function defaultsValues() {
  const values = {};
  for (const [key, , kind] of DEFAULTS) {
    const input = $(`default-${key}`);
    if (!input) return null;
    values[key] = kind === "check" ? input.checked : kind === "env" ? envValues()
      : kind === "theme" ? (input.querySelector("input:checked") || {}).value || "system" : input.value;
  }
  return values;
}

// Marks the changed rows; true when something differs from what is saved.
export function defaultsChanged() {
  const values = settingsView.data && defaultsValues();
  if (!values) return false;
  const saved = settingsView.data.defaults;
  let changed = false;
  for (const [key] of DEFAULTS) {
    const differs = JSON.stringify(key === "env" ? values[key] : String(values[key])) !== JSON.stringify(key === "env" ? saved[key] : String(saved[key]));
    document.querySelector(`.setting[data-key="${key}"]`).classList.toggle("changed", differs);
    changed ||= differs;
  }
  $("default-smart_install").disabled = !$("default-install").checked;
  $("defaults-save").disabled = $("defaults-discard").disabled = !changed;
  return changed;
}

export function filterDefaults() {
  const text = $("defaults-filter").value.trim().toLowerCase();
  let shown = 0;
  for (const row of $("defaults-form").children) {
    row.hidden = Boolean(text) && !row.dataset.search.includes(text);
    shown += row.hidden ? 0 : 1;
  }
  $("defaults-count").textContent = text ? t("{shown} of {total}", { shown, total: DEFAULTS.length }) : "";
  $("defaults-none").hidden = shown > 0;
  $("defaults-form").hidden = shown === 0;
}

export async function saveDefaults() {
  $("defaults-error").hidden = true;
  for (const input of $("defaults-form").querySelectorAll("[aria-invalid]")) input.removeAttribute("aria-invalid");
  $("defaults-save").disabled = true;
  try {
    const { status, data } = await post("/api/defaults/save", defaultsValues());
    if (status === 200) {
      toast(t("Defaults saved."), "info");
      await loadSettings();
      paintDefaults();
      return;
    }
    $("defaults-error").textContent = data.field ? `${data.field}: ${data.error}` : data.error || t("pdms ui answered {status}", { status });
    $("defaults-error").hidden = false;
    const input = data.field && $(`default-${data.field}`);
    if (input) {
      input.setAttribute("aria-invalid", "true");
      input.closest(".setting").hidden = false;
      (input.querySelector("input") || input).focus();
    }
  } catch {
    toast(t("pdms ui is not reachable: is it still running?"));
  } finally {
    defaultsChanged();
  }
}

// ---- export and import of the settings (pdms config export / import)

const SECTION_LABELS = { defaults: N_("Defaults"), users: N_("Users"), dbs: N_("Databases"), stacks: N_("Stacks") };

const importing = { name: "", text: "", plan: null };

function sectionLabel(section) {
  return SECTION_LABELS[section] ? t(SECTION_LABELS[section]) : section;
}

function sectionBoxes(container, sections, onchange = null) {
  container.replaceChildren(...sections.map((section) => {
    const input = el("input", { type: "checkbox", value: section });
    input.checked = true;
    if (onchange) input.addEventListener("change", onchange);
    return el("label", { class: "inline" }, input, sectionLabel(section));
  }));
}

function checkedValues(container) {
  return [...container.querySelectorAll("input:checked")].map((input) => input.value);
}

function download(name, text) {
  const url = URL.createObjectURL(new Blob([text], { type: "application/toml" }));
  const link = el("a", { href: url, download: name });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function openExport() {
  sectionBoxes($("export-sections"), settingsView.data.sections);
  $("export-secrets").checked = false;
  $("export-warn").hidden = $("export-error").hidden = true;
  $("export-dialog").showModal();
}

export async function submitExport(event) {
  event.preventDefault();
  const body = { sections: checkedValues($("export-sections")), secrets: $("export-secrets").checked };
  const { status, data } = await post("/api/config/export", body).catch(() => ({ status: 0, data: {} }));
  if (status !== 200) {
    $("export-error").textContent = data.error || t("pdms ui is not reachable: is it still running?");
    $("export-error").hidden = false;
    return;
  }
  download(data.filename, data.text);
  $("export-dialog").close();
  toast(body.secrets ? t("Exported to {file}, with the passwords: keep it private.", { file: data.filename })
    : t("Exported to {file}.", { file: data.filename }), "info");
}

export async function readImport() {
  const file = $("import-file").files[0];
  $("import-file").value = ""; // the same file can be picked again
  if (!file) return;
  const text = await file.text();
  const { status, data } = await post("/api/config/import/plan", { text }).catch(() => ({ status: 0, data: {} }));
  if (status !== 200) { toast(data.error || t("pdms ui is not reachable: is it still running?")); return; }
  Object.assign(importing, { name: file.name, text, plan: data });
  $("import-name").textContent = file.name;
  const meta = data.meta;
  $("import-meta").textContent = t("Exported on {date} by pdms {version}.", { date: meta.exported_at || "?", version: meta.cli_version || "?" });
  sectionBoxes($("import-sections"), data.sections, paintImport);
  $("import-form").mode.value = "merge";
  $("import-conflicts").replaceChildren();
  $("import-error").hidden = true;
  paintImport();
  $("import-dialog").showModal();
}

export function paintImport() {
  const plan = importing.plan;
  const sections = checkedValues($("import-sections"));
  const replace = $("import-form").mode.value === "replace";
  const plans = plan.plans.filter((item) => sections.includes(item.section));
  const list = (names) => names.join(", ") || "-";
  $("import-missing-head").textContent = replace ? t("Removed") : t("Only mine");
  $("import-rows").replaceChildren(...plans.map((item) => el("tr", {},
    el("td", {}, sectionLabel(item.section)), el("td", {}, list(item.added)), el("td", {}, list(item.changed)),
    el("td", { class: "muted" }, list(item.same)), el("td", replace && item.missing.length ? { class: "code-bad" } : { class: "muted" }, list(item.missing)))));
  const conflicts = plans.flatMap((item) => item.changed.map((name) => [item.section, name]));
  const before = new Set(checkedValues($("import-conflicts")));
  $("import-conflicts").replaceChildren(...conflicts.map(([section, name]) => {
    const value = `${section}\n${name}`;
    const input = el("input", { type: "checkbox", value });
    input.checked = plan.first_setup || before.has(value);
    if (plan.first_setup) input.disabled = true;
    return el("label", { class: "pick" }, input, section === "defaults" ? sectionLabel(section) : `${sectionLabel(section)}: ${name}`);
  }));
  $("import-conflicts-box").hidden = replace || !conflicts.length;
  const notes = [];
  if (plan.first_setup) notes.push(t("There is no configuration of yours yet: everything in the file is taken."));
  if (!plan.meta.secrets && sections.includes("dbs")) notes.push(t("The file has no passwords: the databases you already have keep theirs."));
  $("import-note").textContent = notes.join(" ");
  $("import-note").hidden = !notes.length;
  const removed = plans.reduce((total, item) => total + item.missing.length, 0);
  $("import-warn").textContent = removed === 1 ? t("Replacing deletes one of your entries that is not in the file.")
    : t("Replacing deletes {n} of your entries that are not in the file.", { n: removed });
  $("import-warn").hidden = !replace || !removed;
  $("import-go").disabled = !sections.length;
}

export async function submitImport(event) {
  event.preventDefault();
  const body = {
    text: importing.text, sections: checkedValues($("import-sections")), replace: $("import-form").mode.value === "replace",
    overwrite: checkedValues($("import-conflicts")).map((value) => value.split("\n")),
  };
  $("import-go").disabled = true;
  const { status, data } = await post("/api/config/import/apply", body).catch(() => ({ status: 0, data: {} }));
  $("import-go").disabled = false;
  if (status !== 200) {
    $("import-error").textContent = data.error || t("pdms ui is not reachable: is it still running?");
    $("import-error").hidden = false;
    return;
  }
  $("import-dialog").close();
  if (!data.changed) { toast(t("Nothing changes."), "info"); return; }
  toast(data.backup ? t("Settings imported. The previous ones were saved to {file}.", { file: data.backup }) : t("Settings imported."), "info");
  if (data.no_password.length) toast(t("Databases without a password: {dbs}. Edit them to set it.", { dbs: data.no_password.join(", ") }));
  await loadSettings();
  paintDefaults();
}

// ---- users from a database (pdms user import)

const userImport = { users: [] };

export function openUserImport() {
  const dbs = settingsView.data.dbs.map((db) => db.name);
  if (!dbs.length) { toast(t("Add a database first: the users come from its pdms_user table.")); return; }
  options($("users-db"), dbs, dbs.includes(state.db) ? state.db : dbs[0], dbLabel);
  options($("users-role"), ["", ...settingsView.data.roles], "", (role) => role || t("any role"));
  $("users-search").value = "";
  $("users-inactive").checked = false;
  userImport.users = [];
  $("users-picker").hidden = $("users-note").hidden = $("users-error").hidden = true;
  usersCount();
  $("users-dialog").showModal();
  $("users-search").focus();
}

export async function findDbUsers() {
  const body = { db: $("users-db").value, search: $("users-search").value, role: $("users-role").value, inactive: $("users-inactive").checked };
  $("users-find").disabled = true;
  $("users-error").hidden = true;
  $("users-note").textContent = t("Reading the users of {db}…", { db: body.db });
  $("users-note").hidden = false;
  const { status, data } = await post("/api/import-users/search", body).catch(() => ({ status: 0, data: {} }));
  $("users-find").disabled = false;
  if (status !== 200) {
    $("users-note").hidden = true;
    $("users-error").textContent = data.error || t("pdms ui is not reachable: is it still running?");
    $("users-error").hidden = false;
    return;
  }
  userImport.users = data.users;
  const found = data.users.length;
  const notes = [!found ? t("No user matches.") : found === 1 ? t("{n} user found.", { n: found }) : t("{n} users found.", { n: found })];
  if (data.limited) notes.push(t("Only the first ones: narrow it down with the search or the role."));
  if (data.source === "built-in") notes.push(t("The roles of the current repo could not be read: pdms's own copy maps them."));
  $("users-note").textContent = notes.join(" ");
  $("users-found").replaceChildren(...data.users.map((user, index) => {
    const input = el("input", { type: "checkbox", value: String(index) });
    input.checked = !user.imported_as;
    const tags = [];
    if (user.imported_as) tags.push(t("already imported as {name}", { name: user.imported_as }));
    if (user.is_active === false) tags.push(t("inactive"));
    if (user.unknown_roles.length) tags.push(t("unknown roles kept: {roles}", { roles: user.unknown_roles.join(", ") }));
    return el("label", { class: "pick" }, input,
      el("span", { class: "who" }, `${user.first_name} ${user.last_name}`.trim() || user.username, " ",
        el("span", { class: "muted" }, `<${user.username}>`), el("br"), el("span", { class: "roles" }, user.dev_roles || t("no roles"))),
      el("span", { class: "muted" }, tags.join(" · ")));
  }));
  $("users-picker").hidden = !data.users.length;
  usersCount();
}

export function usersCount() {
  const boxes = [...$("users-found").querySelectorAll("input")];
  const picked = boxes.filter((box) => box.checked).length;
  $("users-count").textContent = t("{picked} of {total} selected", { picked, total: boxes.length });
  $("users-all").checked = boxes.length > 0 && picked === boxes.length;
  $("users-go").disabled = picked === 0;
  $("users-go").textContent = picked ? t("Import {n}", { n: picked }) : t("Import");
}

export async function submitUserImport(event) {
  event.preventDefault();
  const picked = [...$("users-found").querySelectorAll("input:checked")].map((box) => userImport.users[Number(box.value)]);
  $("users-go").disabled = true;
  const { status, data } = await post("/api/import-users/apply", { users: picked }).catch(() => ({ status: 0, data: {} }));
  if (status !== 200) {
    usersCount();
    $("users-error").textContent = data.error || t("pdms ui is not reachable: is it still running?");
    $("users-error").hidden = false;
    return;
  }
  $("users-dialog").close();
  const unchanged = picked.length - data.added.length - data.updated.length;
  toast(t("{added} added, {updated} updated, {unchanged} unchanged.", { added: data.added.length, updated: data.updated.length, unchanged }), "info");
  loadSettings();
}
