// Home: what needs attention, Start everything, the tiles and the stacks.

import { $, act, button, el, failedText, phaseLabel, post, statusLabel, toast } from "./core.js";
import { state } from "./state.js";
import { adoptStrays, servicesView, stopStrays } from "./services.js";
import { showLogs } from "./logs.js";
import { openRun, openUp, servicesCount } from "./launch.js";
import { ask, confirmDialog, stackPath } from "./stacks.js";
import { openProxyStart, proxyJob } from "./proxy.js";
import { EVENTS_JOB, openEventsUp, openSend } from "./events.js";
import {
  apiFixButtons, apiLabel, apiProblemText, apiProblemTitle, jobLog, openFrontendStart, rebuildFrontend, staleText,
} from "./frontend.js";
import { UPDATE_JOB, offerText, offerTitle, openUpdate, updateOffer } from "./updates.js";
import { HOME_COVERS, fixButton } from "./doctor.js";
import { openSetting } from "./repos.js";
import { openDb } from "./settings.js";
import { lineCheck, measureNow, roundTrip } from "./status.js";

export const HOME_JOB = "home";

const home = { saving: 0 };

function homeJob() {
  return state.jobs[HOME_JOB] || null;
}

export function stackUp(stack) {
  return stack.services.filter((svc) => svc.running.length).length;
}

function paintSetup() {
  const setup = state.setup || {};
  const names = state.stacks.map((stack) => stack.name);
  const current = names.includes(setup.stack) ? setup.stack : "";
  if (home.saving) return; // the answer of the save brings it back as the user left it
  $("setup-stack").replaceChildren(
    el("option", current ? { value: "" } : { value: "", selected: "" }, t("No stack")),
    ...names.map((name) => el("option", name === current ? { value: name, selected: "" } : { value: name }, name)),
  );
  $("setup-events").checked = Boolean(setup.events);
  $("setup-proxy").checked = Boolean(setup.proxy);
  $("setup-frontend").checked = Boolean(setup.frontend);
  $("setup-mode").value = setup.frontend_mode || "dev";
  $("setup-mode").disabled = !setup.frontend;
  $("setup-frontend-group").hidden = !state.frontend;
}

export async function saveSetup() {
  const body = {
    stack: $("setup-stack").value, events: $("setup-events").checked, proxy: $("setup-proxy").checked,
    frontend: $("setup-frontend").checked, frontend_mode: $("setup-mode").value,
  };
  $("setup-mode").disabled = !body.frontend;
  home.saving += 1;
  try {
    const { status, data } = await post("/api/setup/save", body);
    if (status !== 200) toast(data.error || t("pdms ui answered {status}", { status }));
    else state.setup = data.setup;
  } catch {
    toast(t("pdms ui is not reachable: is it still running?"));
  } finally {
    home.saving -= 1;
    paintHome();
  }
}

// Which step of Start everything a phase of the Home job belongs to.
function phaseStep(phase) {
  if (/ElasticMQ|broker-sqs-event/.test(phase)) return "events";
  if (/proxy$/.test(phase)) return "proxy";
  if (/frontend$/.test(phase)) return "frontend";
  return "stack";
}

function paintSteps() {
  const setup = state.setup || {};
  const job = homeJob();
  const busy = job && !job.error && job.action === "up" ? phaseStep(job.phase) : null;
  const stack = state.stacks.find((item) => item.name === setup.stack);
  const steps = [
    ["events", setup.events, t("Local events"), t("ElasticMQ and the broker, so the services publish locally"), state.events.up],
    ["stack", Boolean(stack), stack ? t("Stack {name}", { name: stack.name }) : t("No stack"),
      stack ? servicesCount(stack.services.length) : t("only what is already running"), stack && stackUp(stack) === stack.services.length],
    ["proxy", setup.proxy, t("Proxy"), t("local services answer, the rest goes to the remote API"), Boolean(state.proxy)],
    ["frontend", setup.frontend && Boolean(state.frontend), t("Frontend"),
      setup.frontend_mode === "build" ? t("a production build, pointed to the proxy") : t("yarn dev, pointed to the proxy"),
      Boolean(state.frontend && state.frontend.running)],
  ];
  const order = steps.map(([key]) => key);
  $("home-steps").replaceChildren(...steps.map(([key, on, title, text, done]) => {
    let kind = on ? "" : "off";
    if (on && done) kind = "done";
    if (on && busy) kind = key === busy ? "busy" : order.indexOf(key) < order.indexOf(busy) ? "done" : kind;
    return el("li", kind ? { class: kind } : {}, el("b", {}, title), el("span", {}, on ? (key === busy ? phaseLabel(job.phase) : text) : t("off")));
  }));
}

function tile({ title, status, kind = "", main, rows = [], hint = "", actions = [] }) {
  const list = el("dl", {});
  for (const [label, value] of rows) list.append(el("dt", {}, label), el("dd", typeof value === "string" ? { title: value } : {}, value));
  return el("article", { class: `card tile ${kind}`.trim() },
    el("header", {}, el("h2", {}, title), status),
    el("div", { class: "tile-main" }, main),
    list,
    ...(hint ? [el("p", { class: "tile-hint" }, hint)] : []),
    el("footer", {}, ...actions),
  );
}

function pill(kind, text) {
  return el("span", { class: `st ${kind}` }, text);
}

function link(href, text) {
  return el("a", { href, target: "_blank", rel: "noopener noreferrer" }, text);
}

function servicesTile() {
  const alive = state.instances.filter((i) => i.status !== "stopped");
  const failing = state.instances.filter((i) => i.status === "error" || i.status === "stopped");
  const status = failing.length ? pill("stopped", t("{n} failing", { n: failing.length }))
    : alive.length ? pill("ok", t("{n} running", { n: alive.length })) : pill("off", t("none running"));
  const users = [...new Set(alive.map((i) => i.user))];
  const dbs = [...new Set(alive.map((i) => i.db))];
  return tile({
    title: t("Services"), status, kind: failing.length ? "bad" : "",
    main: alive.length ? el("span", { class: "big" }, t("{n} running", { n: alive.length })) : el("span", { class: "muted" }, t("No background services")),
    rows: alive.length ? [[t("user"), users.join(", ")], [t("db"), dbs.join(", ")]] : [],
    hint: (state.strays || []).length ? t("{n} outside pdms", { n: state.strays.length }) : "",
    actions: [button(t("Start service"), () => openRun()), button(t("Open the list"), () => { location.hash = "#services"; })],
  });
}

function proxyTile() {
  const running = state.proxy;
  const job = proxyJob();
  const busy = job && !job.error;
  const status = busy ? pill("starting", phaseLabel(job.phase)) : running ? pill(running.status, statusLabel(running.status)) : pill("off", t("off"));
  const actions = running
    ? [el("a", { class: "btn small", href: `http://localhost:${running.port}/docs`, target: "_blank", rel: "noopener noreferrer" }, t("Open /docs")),
      button(t("Requests"), () => { location.hash = "#proxy"; }),
      button(t("Stop"), () => act(`/api/instances/${encodeURIComponent(running.key)}/stop`), { class: "btn small bad" })]
    : busy ? [] : [button(t("Start proxy"), openProxyStart, { class: "btn small primary" })];
  return tile({
    title: t("Proxy"), status, kind: job && job.error ? "bad" : "",
    main: running ? link(`http://localhost:${running.port}`, `http://localhost:${running.port}`) : el("span", { class: "muted" }, t("Not running")),
    rows: running ? [[t("Remote API"), running.remote || t("none")], [t("Timeout"), t("{seconds} s", { seconds: running.timeout })]] : [],
    hint: job && job.error ? failedText(job) : "",
    actions,
  });
}

function frontendTile() {
  const front = state.frontend;
  const job = state.jobs.frontend;
  const busy = job && !job.error;
  const status = busy ? pill("starting", phaseLabel(job.phase)) : front.running ? pill(front.status, statusLabel(front.status)) : pill("off", t("off"));
  const problem = front.api_problem;
  const rows = [[t("Mode"), front.mode ? front.mode : (state.setup || {}).frontend_mode || "dev"], [t("API"), problem ? front.api : apiLabel(front.api)]];
  if (front.mode === "build" && front.build) {
    rows.push([t("Built"), t("{when} · commit {commit}", { when: new Date(front.build.built_at).toLocaleString(), commit: front.build.commit || "-" })]);
  }
  let actions;
  if (busy) actions = [button(t("Logs"), () => showLogs("frontend", jobLog(job)))];
  else if (front.running) {
    actions = [el("a", { class: "btn small primary", href: front.url, target: "_blank", rel: "noopener noreferrer" }, t("Open the app")),
      button(t("Logs"), () => showLogs("frontend"))];
    if (front.mode === "build") actions.push(button(t("Rebuild"), rebuildFrontend));
    actions.push(button(t("Stop"), () => act("/api/instances/frontend/stop"), { class: "btn small bad" }));
  } else {
    actions = [button(t("Start frontend"), () => openFrontendStart(), { class: "btn small primary" })];
    if (job && job.error) actions.push(button(t("Logs"), () => showLogs("frontend", jobLog(job))),
      button(t("Dismiss"), () => act("/api/instances/frontend/dismiss"), { class: "btn small ghost" }));
  }
  return tile({
    title: t("Frontend"), status, kind: (job && job.error) || front.status === "error" ? "bad" : front.stale || problem ? "warn" : "",
    main: front.running ? link(front.url, front.url) : el("span", { class: "muted" }, t("PDMS web app (Vite, :{port})", { port: front.port })),
    rows, hint: job && job.error ? failedText(job) : front.stale ? staleText(front) : problem ? apiProblemTitle(problem) : front.detail,
    actions: problem && !busy ? [...apiFixButtons(problem), ...actions] : actions,
  });
}

function eventsTile() {
  const job = state.jobs[EVENTS_JOB];
  const busy = job && !job.error;
  const up = state.events.up;
  const consumers = state.instances.filter((i) => i.queue && i.status !== "stopped").length;
  const wanted = (state.setup || {}).events;
  const status = busy ? pill("starting", phaseLabel(job.phase)) : up ? pill("ok", t("on")) : pill(wanted ? "error" : "off", t("off"));
  return tile({
    title: t("Events"), status, kind: !up && wanted && !busy ? "warn" : "",
    main: up ? el("span", { class: "mono" }, `ElasticMQ :${state.events.port}`) : el("span", { class: "muted" }, t("Local SQS and SNS")),
    rows: up ? [[t("Consumers"), t("{n} running", { n: consumers })], ["SNS", state.sns ? state.sns.queue : "-"]] : [[t("Port"), String(state.events.port)]],
    hint: job && job.error ? failedText(job) : "",
    actions: up
      ? [button(t("Send event…"), () => openSend()), button(t("Queues"), () => { location.hash = "#events"; })]
      : busy ? [] : [button(t("Start"), openEventsUp, { class: "btn small primary" })],
  });
}

function homeStackRow(stack) {
  const total = stack.services.length;
  const up = stackUp(stack);
  const job = state.jobs[`stack:${stack.name}`];
  const busy = job && !job.error;
  const meter = el("span", { class: "meter" }, el("i", { style: `width:${total ? Math.round((up / total) * 100) : 0}%` }));
  const actions = el("td", { class: "row-actions" });
  if (busy) actions.append(el("span", { class: "st starting" }, phaseLabel(job.phase)));
  else {
    if (up < total) actions.append(button(t("Start"), () => openUp(stack)));
    if (up) actions.append(button(t("Stop"), () => act(`${stackPath(stack.name)}/down`), { class: "btn small bad" }));
  }
  return el("tr", {},
    el("td", { class: "mono" }, el("b", {}, stack.name)),
    el("td", { class: "muted" }, `${stack.user || ask()} · ${stack.db || ask()}`),
    el("td", {}, el("span", { class: "mini" }, meter, `${up}/${total}`)),
    actions,
  );
}

// What is wrong now, most serious first: failed jobs and services, then what is off or out of date.
function attention() {
  const items = [];
  for (const [key, job] of Object.entries(state.jobs)) {
    if (!job.error) continue;
    const logKey = key === HOME_JOB ? null : key.startsWith("stack:") || key === EVENTS_JOB ? job.log_key : key;
    items.push(["bad", key === HOME_JOB ? t("Start everything") : key, failedText(job), [
      ...(logKey ? [button(t("Logs"), () => showLogs(logKey, jobLog(job)))] : []),
      button(t("Dismiss"), () => act(`/api/instances/${encodeURIComponent(key)}/dismiss`), { class: "btn small ghost" }),
    ]]);
  }
  for (const inst of state.instances) {
    if (state.jobs[inst.key]) continue;
    if (inst.status === "error") {
      const seeIt = inst.traceback && inst.traceback.length
        ? [button(t("See the error"), () => { servicesView.traces.add(inst.key); location.hash = "#services"; })] : [];
      items.push(["bad", inst.key, inst.detail || t("error"), [...seeIt, button(t("Logs"), () => showLogs(inst.key))]]);
    }
    else if (inst.status === "stopped") {
      items.push(["warn", inst.key, t("stopped"), [button(t("Logs"), () => showLogs(inst.key)),
        button(t("Forget"), () => act(`/api/instances/${encodeURIComponent(inst.key)}/forget`), { class: "btn small ghost" })]]);
    }
  }
  const strays = state.strays || [];
  if (strays.length) {
    const names = strays.slice(0, 4).map((s) => s.queue ? s.name : `${s.name} :${s.port}`).join(" · ");
    items.push(["warn", strays.length === 1 ? t("1 service runs outside pdms") : t("{n} services run outside pdms", { n: strays.length }),
      `${names}${strays.length > 4 ? " · " + t("+{n} more", { n: strays.length - 4 }) : ""}. ${t("pdms lost track of them: they keep their ports, but pdms ps, logs and Stop do not see them.")}`,
      [button(strays.length === 1 ? t("Adopt") : t("Adopt all"), () => adoptStrays(null)),
        button(t("Stop them"), () => stopStrays(null), { class: "btn small bad" })]]);
  }
  const front = state.frontend;
  if (front && front.running && front.status === "error" && !state.jobs.frontend) {
    items.push(["bad", "frontend", front.detail, [button(t("Logs"), () => showLogs("frontend"))]]);
  }
  if (front && front.api_problem) items.push(["warn", apiProblemTitle(front.api_problem), apiProblemText(front.api_problem), apiFixButtons(front.api_problem)]);
  if (front && front.stale) items.push(["warn", t("The frontend build is out of date"), staleText(front), [button(t("Rebuild"), rebuildFrontend)]]);
  if ((state.setup || {}).events && !state.events.up && !state.jobs[EVENTS_JOB]) {
    items.push(["warn", t("The local events are off"), t("The services publish to AWS until they are on."), [button(t("Start"), openEventsUp)]]);
  }
  // The databases in use, measured each minute: fresher than Doctor, whose tunnel check is then left out here.
  const tunnelsDown = [];
  for (const db of state.health.dbs || []) {
    if (db.route.kind === "tunnel" && db.route.up === false) {
      tunnelsDown.push(db.route.address);
      items.push(["bad", t("The tunnel to {name} is down", { name: db.name }),
        t("Nothing listens on {address}: start the tunnel again (the command your team uses, e.g. devo ssm connect).", { address: db.route.address }),
        [button(t("Measure now"), measureNow)]]);
    } else if (db.error) {
      items.push(["bad", t("{name} does not answer", { name: db.name }), db.error,
        [button(t("Line check"), lineCheck), button(t("Edit {name}", { name: db.name }), () => openSetting("dbs", (data) => { const found = data.dbs.find((d) => d.name === db.name); if (found) openDb(found); }))]]);
    } else if (db.ms >= state.health.slow_ms) {
      items.push(["db", t("{name} takes {time} per round trip", { name: db.name, time: roundTrip(db.ms) }),
        t("Every query pays at least this, and a list request makes about six. Requests shows where the time goes."),
        [button(t("Line check"), lineCheck), button(t("Requests"), () => { location.hash = "#proxy"; })]]);
    }
  }
  const fromDoctor = (state.doctor.problems || []).filter((check) => !HOME_COVERS.has(check.fix)
    && !tunnelsDown.some((address) => check.detail.includes(address)));
  for (const check of fromDoctor.filter((c) => c.status === "fail")) {
    items.push(["bad", `${check.section} · ${check.name}`, check.detail, [check.fix ? fixButton(check) : button(t("Doctor"), () => { location.hash = "#doctor"; })]]);
  }
  const warnings = fromDoctor.filter((c) => c.status === "warn").length;
  if (warnings) {
    items.push(["warn", warnings === 1 ? t("Doctor found 1 warning") : t("Doctor found {n} warnings", { n: warnings }),
      fromDoctor.filter((c) => c.status === "warn").map((c) => `${c.section}: ${c.name}`).join(" · "), [button(t("Open Doctor"), () => { location.hash = "#doctor"; })]]);
  }
  const offer = updateOffer();
  if (offer && !state.jobs[UPDATE_JOB]) {
    items.push(["info", offerTitle(offer), offerText(offer), [button(offer.kind === "restart" ? t("Restart pdms ui") : t("See what is new"), openUpdate)]]);
  }
  const rank = { bad: 0, warn: 1, db: 1, info: 2 };
  return items.sort((a, b) => rank[a[0]] - rank[b[0]]);
}

// What a Recent entry says (ui/recent.py sends what happened, not a text).
function recentText(entry) {
  const what = entry.key.startsWith("stack:") ? t("Stack {name}", { name: entry.key.slice(6) })
    : entry.key === HOME_JOB ? t("Start everything") : entry.key === EVENTS_JOB ? t("Local events") : entry.key;
  if (entry.event === "failed") return t("{what} failed to load", { what });
  if (entry.event === "exited") return t("{what} stopped by itself", { what });
  if (entry.event === "recovered") return t("{what} loads again", { what });
  const starts = ["start", "up", "restart"].includes(entry.action), stops = ["stop", "down"].includes(entry.action);
  if (entry.event === "job-failed") return starts ? t("{what} could not start", { what }) : stops ? t("{what} could not stop", { what }) : t("{what} failed", { what });
  if (entry.action === "restart") return t("{what} restarted", { what });
  return starts ? t("{what} started", { what }) : stops ? t("{what} stopped", { what }) : t("{what}: done", { what });
}

export function paintHome() {
  paintSetup();
  paintSteps();
  const job = homeJob();
  const busy = job && !job.error;
  const alive = state.instances.filter((i) => i.status !== "stopped").length;
  const parts = [t("{n} running", { n: alive })];
  if (state.proxy) parts.push(t("proxy on"));
  if (state.frontend && state.frontend.running) parts.push(t("frontend on"));
  if (state.events.up) parts.push(t("events on"));
  $("home-summary").textContent = busy ? phaseLabel(job.phase) : parts.join(" · ");
  $("home-start").disabled = $("home-stop").disabled = Boolean(busy);
  $("home-stop").hidden = !(alive || state.proxy || (state.frontend && state.frontend.running) || state.events.up);
  $("home-error").hidden = !(job && job.error);
  $("home-error").textContent = job && job.error ? failedText(job) : "";

  const tiles = [servicesTile(), proxyTile()];
  if (state.frontend) tiles.push(frontendTile());
  tiles.push(eventsTile());
  $("home-tiles").replaceChildren(...tiles);

  $("home-stacks").replaceChildren(...state.stacks.map(homeStackRow));
  $("home-stacks-empty").hidden = state.stacks.length > 0;
  const recent = state.recent || [];
  $("home-recent").replaceChildren(...recent.map((entry) => el("li", { class: entry.kind },
    el("time", { datetime: entry.at }, new Date(entry.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" })),
    el("span", {}, el("b", {}, recentText(entry)), entry.detail ? el("small", {}, entry.detail) : ""),
  )));
  $("home-recent-empty").hidden = recent.length > 0;
  const items = attention();
  $("home-attn").replaceChildren(...items.map(([kind, title, detail, actions]) => el("li", {},
    el("span", { class: `sev ${kind}` }),
    el("div", {}, el("b", {}, title), el("small", {}, detail || "")),
    el("div", { class: "attn-actions" }, ...actions),
  )));
  $("home-attn-card").hidden = !items.length;
  $("home-attn-count").textContent = String(items.length);
  $("home-attn-count").className = `st ${items.some((item) => item[0] === "bad") ? "fail" : "warn"}`;
}

export async function startAll(confirmed = false) {
  $("home-error").hidden = true;
  $("home-start").disabled = true;
  try {
    const { status, data } = await post("/api/home/start", { confirmed });
    if (status === 200 && !data.job) toast(t("Everything in your setup is already running."), "info");
    else if (status === 409 && data.decision === "protected_database") {
      if (await confirmDialog(t("'{name}' is a protected database. Use it anyway?", { name: data.name }),
        t("The stack's services will run against it."), t("Use it"))) startAll(true);
    } else if (status === 409 && data.decision === "port_busy") {
      toast(t("Port {port} is in use, so the frontend cannot start there. Free it, or start the frontend on another port from its tile.", { port: data.port }));
    } else if (status >= 400) {
      $("home-error").textContent = data.error || t("pdms ui answered {status}", { status });
      $("home-error").hidden = false;
    }
  } catch {
    toast(t("pdms ui is not reachable: is it still running?"));
  } finally {
    $("home-start").disabled = false;
  }
}

export async function stopAll() {
  const what = [];
  if (state.frontend && state.frontend.running) what.push(t("the frontend"));
  if (state.proxy) what.push(t("the proxy"));
  const alive = state.instances.filter((i) => i.status !== "stopped").length;
  if (alive) what.push(servicesCount(alive));
  if (state.events.up) what.push(t("the local ElasticMQ (its messages are lost)"));
  if (await confirmDialog(t("Stop everything?"), t("It stops {what}.", { what: what.join(", ") }), t("Stop everything"))) {
    act("/api/home/stop", {});
  }
}
