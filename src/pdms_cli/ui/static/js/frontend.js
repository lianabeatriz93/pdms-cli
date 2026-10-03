// The PDMS web app (pdms front): its row, its start dialog and its build.

import { $, act, button, el, getJson, iconButton, iconLink, post, toast } from "./core.js";
import { state } from "./state.js";
import { showLogs } from "./logs.js";
import { options } from "./launch.js";
import { openProxyStart } from "./proxy.js";

const frontStart = { port: null, options: null };

// Why a build is made again (frontend.build_needed), as the server says it.
const BUILD_REASONS = {
  "no build yet": N_("no build yet"), "the API URL changed": N_("the API URL changed"),
  "the dependencies changed": N_("the dependencies changed"), "the code changed": N_("the code changed"),
};

// The log a job is writing now: the install, the build (the frontend's) or the service's own.
export function jobLog(job) {
  if (job.phase.startsWith("building")) return "build";
  return job.installed && job.phase.startsWith("installing") ? "install" : "current";
}

export function staleText(front) {
  return t("Built for {built}; the API is now {now}. Rebuild to use it.", { built: front.api || "-", now: front.stale || "-" });
}

// The frontend calls a local API nobody answers on (state.frontend.api_problem).
export function apiProblemTitle(problem) {
  if (problem.proxy_port) return t("The frontend calls :{port}, the proxy runs on :{proxy}", { port: problem.port, proxy: problem.proxy_port });
  if (problem.leftover) return t("The frontend calls a proxy that is not running (:{port})", { port: problem.port });
  return t("Nothing answers where the frontend calls (:{port})", { port: problem.port });
}

export function apiProblemText(problem) {
  if (problem.proxy_port) return t("frontend/.env.local still has {url}. Pointing it to the proxy restarts yarn dev by itself.", { url: problem.url });
  if (problem.leftover) return t("A proxy that did not stop cleanly left {url} in frontend/.env.local.", { url: problem.url });
  return t("{url} comes from frontend/.env.local. Start the proxy, or change VITE_APP_API_URL there.", { url: problem.url });
}

export function fixFrontendApi() {
  act("/api/frontend/fix-api", {}, (data) => {
    toast(data.done === "pointed" ? t("The frontend now calls the proxy.") : t("frontend/.env.local is back as it was."), "info");
    act("/api/doctor/run", { databases: false });
  });
}

export function apiFixButtons(problem) {
  if (problem.proxy_port) return [button(t("Point it to :{port}", { port: problem.proxy_port }), fixFrontendApi, { class: "btn small primary" })];
  const start = button(t("Start proxy"), () => openProxyStart());
  return problem.leftover ? [button(t("Restore .env.local"), fixFrontendApi), start] : [start];
}

export function apiLabel(url) {
  if (!url) return "-";
  return /:\/\/(localhost|127\.0\.0\.1)[:/]/.test(url) ? t("the proxy ({url})", { url }) : url;
}

export function frontendActions(cell, item) {
  if (item.status !== "stopped") cell.append(iconLink("open", t("Open the app"), item.url));
  if (item.mode === "build") cell.append(iconButton("restart", t("Rebuild"), () => rebuildFrontend()));
  cell.append(iconButton("stop", t("Stop"), () => act("/api/instances/frontend/stop"), { class: "ibtn bad" }));
  return cell;
}

export function rebuildFrontend() {
  act("/api/frontend/start", { mode: "build", rebuild: true, restart: true }, () => showLogs("frontend", "build"));
}

function fact(label, value, kind = "") {
  return [el("dt", {}, label), el("dd", kind ? { class: kind } : {}, value)];
}

export function paintFrontendMode() {
  const options = frontStart.options;
  const mode = $("front-form").mode.value;
  $("front-rebuild-label").hidden = mode !== "build";
  if (!options) return;
  const facts = [
    ...fact(t("Node"), options.node || t("not found"), options.node ? "" : "bad"),
    ...fact("node_modules", options.dependencies_ok ? t("up to date") : t("out of date: yarn install runs first"),
      options.dependencies_ok ? "" : "warn-text"),
    ...fact(t("API"), apiLabel(options.api[mode])),
  ];
  if (mode === "build") {
    const last = options.last_build;
    facts.push(...fact(t("Last build"), last ? t("{when} · commit {commit}", {
      when: new Date(last.built_at).toLocaleString(), commit: last.commit || "-",
    }) : t("none yet")));
    facts.push(...fact(t("Build"), options.build_needed ? t("builds first: {reason}", {
      reason: BUILD_REASONS[options.build_needed] ? t(BUILD_REASONS[options.build_needed]) : options.build_needed,
    })
      : t("serves the last build")));
  }
  $("front-facts").replaceChildren(...facts);
}

export async function openFrontendStart(mode = null) {
  const found = await getJson("/api/frontend/options");
  if (found.error) { toast(found.error); return; }
  const options = found.data;
  if (!options.available) { toast(t("The current repo has no frontend.")); return; }
  Object.assign(frontStart, { options, port: null });
  $("front-form").mode.value = mode || (state.setup ? state.setup.frontend_mode : "dev");
  $("front-form").install.value = "auto";
  $("front-rebuild").checked = false;
  $("front-warn").hidden = $("front-error").hidden = true;
  $("front-go").textContent = t("Start");
  paintFrontendMode();
  $("front-dialog").showModal();
}

export function resetFrontendPort() {
  frontStart.port = null;
  $("front-warn").hidden = true;
  $("front-go").textContent = t("Start");
}

export async function submitFrontendStart(event) {
  event.preventDefault();
  const form = $("front-form");
  const body = {
    mode: form.mode.value, install: { auto: null, force: true, skip: false }[form.install.value],
    rebuild: form.mode.value === "build" && $("front-rebuild").checked, port: frontStart.port,
  };
  $("front-error").hidden = true;
  $("front-go").disabled = true;
  try {
    const { status, data } = await post("/api/frontend/start", body);
    if (status === 202) {
      $("front-dialog").close();
      showLogs("frontend", body.install === true || !frontStart.options.dependencies_ok ? "install"
        : body.mode === "build" && (body.rebuild || frontStart.options.build_needed) ? "build" : "current");
      return;
    }
    if (status === 409 && data.decision === "port_busy") {
      frontStart.port = data.free;
      $("front-warn").textContent = t("Port {port} is in use. Start on {free} instead? Logging in to the app may only work on {port}.",
        { port: data.port, free: data.free });
      $("front-warn").hidden = false;
      $("front-go").textContent = t("Start on {port}", { port: data.free });
      return;
    }
    $("front-error").textContent = data.error || t("pdms ui answered {status}", { status });
    $("front-error").hidden = false;
  } catch {
    $("front-error").textContent = t("pdms ui is not reachable: is it still running?");
    $("front-error").hidden = false;
  } finally {
    $("front-go").disabled = false;
  }
}
