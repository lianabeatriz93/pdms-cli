// Repos: the repo chip menu, Settings → Repos and switching repos.

import { $, act, button, el, post, shortPath, toast } from "./core.js";
import { state } from "./state.js";
import { confirmDialog } from "./stacks.js";
import { formError, loadSettings, matches, resetForm, settingsView } from "./settings.js";
import { currentView, route } from "./router.js";

export function toggleRepoMenu(event) {
  event.stopPropagation();
  const menu = $("repo-menu");
  if (!menu.hidden) { menu.hidden = true; return; }
  const current = state.repo && state.repo.alias;
  menu.replaceChildren(...state.repos.map((repo) => el("button", {
    type: "button", role: "menuitem", class: repo.name === current ? "cur" : "",
    onclick: () => { menu.hidden = true; if (repo.name !== current) useRepo(repo.name); },
  }, el("b", {}, repo.name), el("small", {}, repo.path))), el("hr"), el("button", {
    type: "button", role: "menuitem", onclick: () => { menu.hidden = true; openSetting("repos"); },
  }, t("Manage repos…")));
  menu.hidden = false;
  menu.querySelector("button").focus();
}

export async function openSetting(tab, open = null) {
  settingsView.tab = tab;
  location.hash = "#settings";
  route();
  await loadSettings();
  if (open && settingsView.data) open(settingsView.data);
}

function repoRow(repo) {
  const path = el("td", { class: "mono", title: repo.path }, shortPath(repo.path));
  if (!repo.exists) path.append(" ", el("span", { class: "not-set" }, t("missing")));
  const migrations = el("td", { class: "mono", title: repo.migrations || "" },
    repo.migrations ? shortPath(repo.migrations) : el("span", { class: "found-note" }, t("not set")));
  const remote = el("td", { class: "mono", title: repo.remote || repo.remote_found || "" }, repo.remote ? shortPath(repo.remote, 36)
    : el("span", { class: "found-note" }, repo.remote_found ? t("from frontend/.env: {url}", { url: shortPath(repo.remote_found, 30) }) : t("not set")));
  const name = el("td", {}, el("b", {}, repo.name));
  if (repo.current) name.append(el("span", { class: "tag current" }, t("current")));
  return el("tr", { class: repo.current ? "current" : "" },
    el("td", { class: "cur-dot" }, repo.current ? "●" : ""), name, path, migrations, remote,
    el("td", { class: "num" }, repo.running ? String(repo.running) : ""),
    el("td", { class: "row-actions" },
      ...(repo.current ? [] : [button(t("Use"), () => useRepo(repo.name))]),
      button(t("Edit"), () => openRepo(repo)),
      button(t("Remove"), () => removeRepo(repo), { class: "btn small bad" })));
}

export function paintRepos() {
  const all = settingsView.data.repos;
  const text = $("repo-filter").value.trim().toLowerCase();
  const shown = all.filter((repo) => matches(text, [repo.name, repo.path]));
  $("repo-rows").replaceChildren(...shown.map(repoRow));
  $("repo-count").textContent = all.length ? t("{shown} of {total}", { shown: shown.length, total: all.length }) : "";
  $("repo-empty").hidden = shown.length > 0;
  $("repo-empty").textContent = all.length ? t("No repo matches the filter.") : t("No repos yet: add the folder of a PDMS checkout.");
}

export const repoForm = { repo: null, nameTouched: false, timer: null, looked: "" };

const REPO_FIELDS = ["path", "name", "migrations", "remote"];

export function openRepo(repo = null) {
  Object.assign(repoForm, { repo, nameTouched: Boolean(repo), looked: "" });
  $("repo-title").textContent = repo ? t("Edit {name}", { name: repo.name }) : t("Add repo");
  $("repo-hint").textContent = repo ? t("Changes apply to services started from now on.")
    : t("A PDMS checkout: the folder with backend/snakesdk. Like pdms repo add.");
  $("repo-path").value = repo ? repo.path : "";
  $("repo-path").disabled = Boolean(repo);
  $("repo-browse").hidden = Boolean(repo) || !settingsView.data.pick_folder;
  $("repo-found").hidden = true;
  $("repo-name").value = repo ? repo.name : "";
  $("repo-migrations").value = repo ? repo.migrations : "";
  $("repo-remote").value = repo ? repo.remote : "";
  $("repo-remote").placeholder = repo && repo.remote_found ? repo.remote_found : t("from frontend/.env (VITE_APP_API_URL)");
  $("repo-use-label").hidden = Boolean(repo);
  $("repo-save").textContent = repo ? t("Save") : t("Add");
  resetForm("repo", REPO_FIELDS);
  $("repo-dialog").showModal();
  (repo ? $("repo-name") : $("repo-path")).focus();
}

// The folder typed (or picked) is looked at as it changes: is it a PDMS checkout, and how many services it has.
export function repoPathChanged() {
  clearTimeout(repoForm.timer);
  repoForm.timer = setTimeout(lookAtRepo, 400);
}

async function lookAtRepo() {
  const path = $("repo-path").value.trim();
  if (!path || path === repoForm.looked) return;
  repoForm.looked = path;
  resetForm("repo", REPO_FIELDS);
  const { status, data } = await post("/api/repos/check", { path });
  if (repoForm.looked !== path) return;
  if (status !== 200) { $("repo-found").hidden = true; formError("repo", data, t("pdms ui answered {status}", { status })); return; }
  $("repo-found").textContent = data.registered
    ? t("Already registered as '{name}'.", { name: data.registered })
    : t("✓ PDMS repo found: {root} · {n} services", { root: data.root, n: data.services });
  $("repo-found").classList.toggle("warn", Boolean(data.registered));
  $("repo-found").hidden = false;
  if (!repoForm.nameTouched) $("repo-name").value = data.name;
}

export async function browseRepo() {
  const { status, data } = await post("/api/ui/pick-folder", { start: $("repo-path").value.trim() });
  if (status === 200 && data.path) { $("repo-path").value = data.path; lookAtRepo(); }
}

export async function saveRepo(event) {
  event.preventDefault();
  resetForm("repo", REPO_FIELDS);
  const current = repoForm.repo;
  const body = { name: $("repo-name").value.trim(), migrations: $("repo-migrations").value.trim(), remote: $("repo-remote").value.trim() };
  const path = current ? `/api/repos/${encodeURIComponent(current.name)}/save` : "/api/repos/add";
  if (!current) body.path = $("repo-path").value.trim();
  $("repo-save").disabled = true;
  try {
    const { status, data } = await post(path, body);
    if (status !== 200) { formError("repo", data, t("pdms ui answered {status}", { status })); return; }
    $("repo-dialog").close();
    toast(t("'{name}' saved.", { name: data.name }), "info");
    await loadSettings();
    if (!current && $("repo-use").checked && !(state.repo && state.repo.alias === data.name)) useRepo(data.name);
  } catch {
    formError("repo", {}, t("pdms ui is not reachable: is it still running?"));
  } finally {
    $("repo-save").disabled = false;
  }
}

async function removeRepo(repo) {
  const others = settingsView.data.repos.filter((r) => r.name !== repo.name);
  let text = t("pdms forgets it; the folder stays on disk. Instances running from it keep running.");
  if (repo.current) text += " " + (others.length ? t("It is the current repo: {name} becomes current.", { name: others[0].name }) : t("It is the current repo, and the only one."));
  if (!await confirmDialog(t("Remove {name}?", { name: repo.name }), text, t("Remove"))) return;
  act(`/api/repos/${encodeURIComponent(repo.name)}/remove`, {}, () => { toast(t("'{name}' deleted.", { name: repo.name }), "info"); loadSettings(); });
}

const switching = { name: "" };

export async function useRepo(name, running = null) {
  try {
    const { status, data } = await post(`/api/repos/${encodeURIComponent(name)}/use`, running ? { running } : {});
    if (status === 409 && data.decision === "repo_switch") { openSwitch(name, data); return; }
    if (status >= 400) { if ($("switch-dialog").open) { $("switch-error").textContent = data.error; $("switch-error").hidden = false; } else toast(data.error || t("pdms ui answered {status}", { status })); return; }
    if ($("switch-dialog").open) $("switch-dialog").close();
    toast(t("Current repo: {name}. Services, stacks and proxy routes now come from it.", { name }), "info");
    if (data.left && data.left.length) toast(t("Not in '{name}', so still running: {keys}", { name, keys: data.left.join(", ") }), "info");
    if (data.proxy) toast(t("The proxy still routes to '{old}': restart it to use '{name}'.", { old: data.old, name }), "info");
    if (currentView() === "settings") loadSettings();
  } catch {
    toast(t("pdms ui is not reachable: is it still running?"));
  }
}

function openSwitch(name, data) {
  switching.name = name;
  const keys = data.running.map((item) => item.key);
  $("switch-title").textContent = t("Use {name} as the current repo", { name });
  $("switch-text").textContent = t("{n} instances are running from '{old}': {keys}. What should pdms do with them?", { n: keys.length, old: data.old, keys: keys.join(", ") });
  const stay = data.running.filter((item) => !item.movable).map((item) => item.key);
  $("switch-move").textContent = t("Restart them from '{name}' (same user, database and port)", { name })
    + (stay.length ? " " + t("(not in '{name}', so they keep running: {keys})", { name, keys: stay.join(", ") }) : "");
  $("switch-proxy").hidden = !data.proxy;
  $("switch-proxy").textContent = data.proxy ? t("The proxy is running for '{old}'. Restart it afterwards to route to '{name}'.", { old: data.old, name }) : "";
  $("switch-error").hidden = true;
  $("switch-go").textContent = t("Use {name}", { name });
  $("switch-form").querySelector('input[value="keep"]').checked = true;
  $("switch-dialog").showModal();
}

export function submitSwitch(event) {
  event.preventDefault();
  useRepo(switching.name, $("switch-form").querySelector('input[name="running"]:checked').value);
}
