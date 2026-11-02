// AWS logs: what the dev Lambdas wrote to CloudWatch and the events they sent (pdms aws logs, read-only). pdms ui
// asks only while this screen is open: when it opens or a filter changes, and in Live once a minute for what came
// since the last read (each line shows once, by its id). The chosen lambdas and the presets are kept per repo, in this
// browser.

import { $, act, button, copyText, dateTime, el, getJson, iconButton, skeletonRows, toast } from "./core.js";
import { state } from "./state.js";
import { jsonView } from "./events.js";
import { loginAws } from "./aws.js";

const EVERY = 60000; // ms between two reads in Live
const OVERLAP = 120000; // ms read again: CloudWatch may store a line a while after its time
const FIRST_LIVE = 15 * 60000; // what Live shows when it starts
const SPANS = { "15m": 15 * 60000, "1h": 3600000, "24h": 86400000 };
const MAX_LINES = 3000; // kept on the page in Live
const ROLES = { broker: N_("broker"), sns: N_("publishes to SNS"), consumer: N_("consumes a queue") };

export const awsLogsView = {
  lambdas: null, flow: [], profile: "", loading: false, chosen: null, presets: {},
  range: "1h", level: "", lines: [], ids: new Set(), errors: {}, more: [], problem: null,
  selected: null, tab: "event", reading: false, asked: 0, timer: null, paused: false, opened: "",
};

// ---- what is kept per repo, in this browser

function storeKey() {
  return `pdms-awslogs:${(state && state.repo && state.repo.root) || ""}`;
}

function loadStored() {
  try {
    const kept = JSON.parse(localStorage.getItem(storeKey()) || "{}");
    awsLogsView.presets = kept.presets && typeof kept.presets === "object" ? kept.presets : {};
    return Array.isArray(kept.chosen) ? kept.chosen.filter((name) => typeof name === "string") : null;
  } catch {
    return null;
  }
}

function store() {
  try {
    localStorage.setItem(storeKey(), JSON.stringify({ chosen: awsLogsView.chosen, presets: awsLogsView.presets }));
  } catch { /* kept until the page reloads */ }
}

// ---- opening and leaving the screen

export async function openAwsLogs() {
  // Again when no profile was chosen last time: it may have been chosen in Data since.
  if ((!awsLogsView.lambdas || !awsLogsView.profile) && !awsLogsView.loading) await loadLambdas();
  if (!awsLogsView.lines.length && !awsLogsView.reading) readNow(true);
  else schedule();
  paintAwsLogs();
}

// The page opened on AWS logs before the state came (route() waits for it): open the screen now, once.
export function syncAwsLogs() {
  if (!$("view-aws-logs").hidden && !awsLogsView.lambdas && !awsLogsView.loading) openAwsLogs();
}

export function leaveAwsLogs() {
  clearTimeout(awsLogsView.timer);
  awsLogsView.timer = null;
  paintDot();
}

function onScreen() {
  return location.hash === "#aws-logs" && !document.hidden;
}

document.addEventListener("visibilitychange", () => {
  if (!onScreen()) { leaveAwsLogs(); return; }
  if (awsLogsView.range === "live" && !awsLogsView.paused && Date.now() - awsLogsView.asked >= EVERY) readNow(false);
  else schedule();
});

async function loadLambdas(fresh = false) {
  awsLogsView.loading = true;
  const { data, error } = await getJson(`/api/aws-logs/lambdas${fresh ? "?fresh=1" : ""}`);
  awsLogsView.loading = false;
  if (error) { awsLogsView.problem = { kind: "error", text: error }; return; }
  awsLogsView.lambdas = data.lambdas;
  awsLogsView.flow = data.flow;
  awsLogsView.profile = data.profile;
  awsLogsView.problem = data.error ? { kind: data.expired ? "expired" : "error", text: data.error } : null;
  if (awsLogsView.chosen === null) awsLogsView.chosen = loadStored() || [...data.flow];
}

// ---- reading

function rangeOf(follow) {
  const now = Date.now();
  const range = awsLogsView.range;
  if (range === "live") return { start: follow ? awsLogsView.asked - OVERLAP : now - FIRST_LIVE };
  if (range === "custom") {
    const from = Date.parse($("awslogs-from").value);
    const to = $("awslogs-to").value ? Date.parse($("awslogs-to").value) : NaN;
    return { start: Number.isNaN(from) ? now - SPANS["1h"] : from, end: Number.isNaN(to) ? undefined : to };
  }
  return { start: now - SPANS[range] };
}

export async function readNow(reset = true) {
  clearTimeout(awsLogsView.timer);
  awsLogsView.timer = null;
  if (awsLogsView.lambdas && !awsLogsView.profile) { awsLogsView.problem = { kind: "profile" }; paintAwsLogs(); return; }
  const chosen = awsLogsView.chosen || [];
  if (!chosen.length) { awsLogsView.problem = { kind: "none" }; paintAwsLogs(); return; }
  const follow = !reset && awsLogsView.range === "live" && awsLogsView.asked > 0;
  const { start, end } = rangeOf(follow);
  const query = new URLSearchParams({ functions: chosen.join(","), start: String(Math.max(0, Math.round(start))) });
  if (end !== undefined) query.set("end", String(Math.round(end)));
  const type = $("awslogs-type").value.trim();
  const grep = $("awslogs-grep").value.trim();
  if (type) query.set("type", type);
  if (grep) query.set("grep", grep);
  if ($("awslogs-all").checked) query.set("all", "1");
  awsLogsView.reading = true;
  if (reset) { awsLogsView.lines = []; awsLogsView.ids = new Set(); awsLogsView.selected = null; }
  paintAwsLogs();
  const { data, error } = await getJson(`/api/aws-logs/read?${query}`);
  awsLogsView.reading = false;
  if (error) awsLogsView.problem = { kind: "error", text: error };
  else if (data.error) awsLogsView.problem = { kind: data.expired ? "expired" : "error", text: data.error };
  else if (!data.profile) awsLogsView.problem = { kind: "profile" };
  else {
    awsLogsView.problem = null;
    awsLogsView.profile = data.profile;
    const fresh = data.lines.filter((line) => !awsLogsView.ids.has(line.id));
    for (const line of fresh) awsLogsView.ids.add(line.id);
    awsLogsView.lines = [...fresh.reverse(), ...awsLogsView.lines].slice(0, MAX_LINES); // newest first
    awsLogsView.errors = data.errors;
    awsLogsView.more = data.more;
    awsLogsView.asked = data.asked;
  }
  paintAwsLogs();
  schedule();
}

function schedule() {
  clearTimeout(awsLogsView.timer);
  awsLogsView.timer = null;
  if (awsLogsView.range === "live" && !awsLogsView.paused && onScreen() && !awsLogsView.problem) {
    awsLogsView.timer = setTimeout(() => readNow(false), Math.max(1000, EVERY - (Date.now() - awsLogsView.asked)));
  }
  paintDot();
}

// ---- the controls

export function setRange(range) {
  awsLogsView.range = range;
  awsLogsView.paused = false;
  if (range === "custom" && !$("awslogs-from").value) {
    const local = (ms) => new Date(ms - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    $("awslogs-from").value = local(Date.now() - SPANS["1h"]);
    $("awslogs-to").value = local(Date.now());
  }
  paintAwsLogs();
  if (range !== "custom") readNow(true);
}

export function setLevel(level) {
  awsLogsView.level = level;
  paintAwsLogs();
}

export function togglePause() {
  awsLogsView.paused = !awsLogsView.paused;
  if (!awsLogsView.paused) readNow(false);
  else schedule();
  paintAwsLogs();
}

export function choosePreset() {
  const name = $("awslogs-preset").value;
  awsLogsView.chosen = name === "" ? [...awsLogsView.flow] : [...(awsLogsView.presets[name] || [])];
  store();
  readNow(true);
}

function unchoose(name) {
  awsLogsView.chosen = (awsLogsView.chosen || []).filter((item) => item !== name);
  store();
  readNow(true);
}

export function hideOpened() {
  awsLogsView.opened = "";
  paintAwsLogs();
}

// Events → Event types → See in AWS: the event flow, filtered by that type.
export function seeInAws(type) {
  awsLogsView.opened = type;
  $("awslogs-type").value = type;
  awsLogsView.lines = [];
  if (awsLogsView.lambdas) awsLogsView.chosen = [...awsLogsView.flow];
  else awsLogsView.chosen = null; // the flow once the lambdas are loaded
  if (location.hash === "#aws-logs") readNow(true);
  else location.hash = "#aws-logs";
}

// ---- choosing lambdas

const picking = { picked: new Set() };

export async function openPicker() {
  if (!awsLogsView.lambdas) await loadLambdas(true);
  picking.picked = new Set(awsLogsView.chosen || []);
  const ticket = /PDMP-\d+/i.exec((state.repo && state.repo.branch) || "");
  $("awslogs-pick-name").value = "";
  $("awslogs-pick-name").placeholder = ticket ? ticket[0].toUpperCase() : "";
  $("awslogs-pick-filter").value = "";
  $("awslogs-pick-note").textContent = t("{n} lambdas with logs in {profile}. Those that take part in events are found in the repo's Terraform.",
    { n: (awsLogsView.lambdas || []).length, profile: awsLogsView.profile || "-" });
  paintPicker();
  $("awslogs-dialog").showModal();
  $("awslogs-pick-filter").focus();
}

export function paintPicker() {
  const text = $("awslogs-pick-filter").value.trim().toLowerCase();
  const onlyEvents = $("awslogs-pick-events").checked;
  const all = awsLogsView.lambdas || [];
  const order = { broker: 0, sns: 1, consumer: 2, "": 3 };
  const shown = all
    .filter((item) => (!onlyEvents || item.role || picking.picked.has(item.name)) && item.name.toLowerCase().includes(text))
    .sort((a, b) => order[a.role] - order[b.role] || a.name.localeCompare(b.name));
  $("awslogs-pick-list").replaceChildren(...shown.map((item) => {
    const box = el("input", { type: "checkbox", value: item.name });
    box.checked = picking.picked.has(item.name);
    box.addEventListener("change", () => {
      if (box.checked) picking.picked.add(item.name); else picking.picked.delete(item.name);
      paintPickerCount();
    });
    return el("label", { class: "pick" }, box, el("span", { class: "mono" }, item.name),
      el("span", { class: "muted" }, item.role ? `${t(ROLES[item.role])} ${item.detail}` : ""));
  }));
  paintPickerCount();
}

function paintPickerCount() {
  const n = picking.picked.size;
  $("awslogs-pick-count").textContent = t("{n} chosen", { n });
  $("awslogs-pick-go").textContent = t("Read {n} lambdas", { n });
  $("awslogs-pick-go").disabled = n === 0;
}

export function submitPicker(event) {
  event.preventDefault();
  awsLogsView.chosen = [...picking.picked].sort();
  const name = $("awslogs-pick-name").value.trim();
  if (name) awsLogsView.presets[name] = awsLogsView.chosen;
  store();
  $("awslogs-dialog").close();
  readNow(true);
}

// ---- painting

function paintDot() {
  const live = Boolean(awsLogsView.timer) || (awsLogsView.reading && awsLogsView.range === "live");
  $("awslogs-on").classList.toggle("on", live);
  $("awslogs-on").title = live ? t("Live: reading once a minute") : "";
}

function kindTag(message) {
  return el("span", { class: `tag k-${message.kind || "other"}` }, (message.kind || "-").toUpperCase());
}

function eventText(message) {
  const arrow = message.direction === "sent" ? "→" : "←";
  const where = message.direction === "sent" ? message.destination : t("received");
  return `${arrow} ${where || "?"} · ${message.type || "-"}`;
}

function firstLine(text) {
  const line = text.split("\n")[0];
  return line.length > 300 ? `${line.slice(0, 300)}…` : line;
}

function cloudWatchUrl(line) {
  const region = (state.aws && state.aws.region) || "us-east-1";
  const enc = (text) => encodeURIComponent(text).replaceAll("%", "$25");
  return `https://${region}.console.aws.amazon.com/cloudwatch/home?region=${region}#logsV2:log-groups/log-group/`
    + `${enc(`/aws/lambda/${line.function}`)}/log-events/${enc(line.stream)}`;
}

function paintProblem() {
  const problem = awsLogsView.problem;
  $("awslogs-problem").hidden = !problem;
  if (!problem) return;
  const actions = [];
  let title;
  let text = problem.text || "";
  if (problem.kind === "profile") {
    title = t("Choose the AWS profile of dev");
    text = t("pdms reads the logs with the profile the services use, and none is chosen yet.");
    actions.push(button(t("Open Data"), () => { location.hash = "#data"; }, { class: "btn primary" }));
  } else if (problem.kind === "expired") {
    title = t("Sign in to AWS to read the logs of {profile}", { profile: awsLogsView.profile || "-" });
    text = t("The AWS SSO session of the profile ended. Signing in opens the browser; read again when it is done.");
    actions.push(button(t("Sign in with AWS SSO"), loginAws, { class: "btn primary" }), button(t("Read again"), () => readNow(true)));
  } else if (problem.kind === "none") {
    title = t("No lambda chosen");
    text = t("Choose the lambdas to read, or a preset.");
    actions.push(button(t("Choose lambdas…"), openPicker, { class: "btn primary" }));
  } else {
    title = t("CloudWatch could not be read");
    actions.push(button(t("Read again"), () => readNow(true)));
  }
  $("awslogs-problem-title").textContent = title;
  $("awslogs-problem-text").textContent = text;
  $("awslogs-problem-actions").replaceChildren(...actions);
}

function paintFilters() {
  const view = awsLogsView;
  const chosen = view.chosen || [];
  const roles = Object.fromEntries((view.lambdas || []).map((item) => [item.name, item.role]));
  $("awslogs-chosen").replaceChildren(...chosen.map((name) => el("span", { class: "pill" }, name,
    roles[name] ? el("span", { class: `tag k-${roles[name] === "sns" ? "sns" : "sqs"}` }, roles[name]) : "",
    iconButton("forget", t("Stop reading {name}", { name }), () => unchoose(name)))));
  const presets = Object.keys(view.presets).sort();
  const current = presets.find((name) => JSON.stringify([...view.presets[name]].sort()) === JSON.stringify([...chosen].sort()));
  const flow = JSON.stringify([...view.flow].sort()) === JSON.stringify([...chosen].sort());
  $("awslogs-preset").replaceChildren(
    el("option", { value: "" }, t("Event flow (broker + SNS publishers)")),
    ...presets.map((name) => el("option", { value: name }, name)),
    ...(current || flow ? [] : [el("option", { value: "-", disabled: "" }, t("Chosen by hand"))]),
  );
  $("awslogs-preset").value = current || (flow ? "" : "-");
  for (const node of $("awslogs-range").children) node.setAttribute("aria-pressed", String(node.dataset.range === view.range));
  $("awslogs-custom").hidden = view.range !== "custom";
  $("awslogs-live").hidden = view.range !== "live";
  $("awslogs-pause").textContent = view.paused ? t("Resume") : t("Pause");
  $("awslogs-live-text").textContent = view.paused
    ? t("Paused")
    : t("Every minute while this screen is open · last read {when}", { when: view.asked ? dateTime(view.asked).slice(-8) : "-" });
  const counts = { "": view.lines.length, INFO: 0, WARN: 0, ERROR: 0 };
  for (const line of view.lines) if (line.level in counts) counts[line.level] += 1;
  for (const node of $("awslogs-level").children) {
    node.setAttribute("aria-pressed", String(node.dataset.level === view.level));
    node.querySelector("span").textContent = String(counts[node.dataset.level]);
  }
  const types = [...new Set(view.lines.flatMap((line) => line.messages.map((m) => m.type)).filter(Boolean))].sort();
  $("awslogs-types").replaceChildren(...types.map((type) => el("option", { value: type })));
}

function paintRows(shown) {
  const view = awsLogsView;
  if (view.reading && !view.lines.length) {
    $("awslogs-rows").replaceChildren(...skeletonRows($("awslogs-rows"), 6));
    $("awslogs-empty").hidden = true;
    return;
  }
  $("awslogs-rows").replaceChildren(...shown.map((line) => {
    const first = line.messages[0];
    const extra = line.messages.length > 1 ? ` +${line.messages.length - 1}` : "";
    return el("tr", { class: view.selected === line.id ? "sel" : "", onclick: () => select(line.id) },
      el("td", { class: "mono" }, dateTime(line.at)),
      el("td", { class: "mono" }, line.function),
      el("td", {}, line.level ? el("span", { class: `tag lv-${line.level}` }, line.level) : ""),
      el("td", { class: "event" }, ...(first ? [kindTag(first), " ", el("span", { class: "mono" }, eventText(first) + extra)]
        : [el("span", { class: "muted" }, firstLine(line.text).slice(0, 160))])),
      el("td", { class: "line", title: firstLine(line.text) }, firstLine(line.text)));
  }));
  $("awslogs-empty").hidden = shown.length > 0 || view.reading || Boolean(view.problem);
  $("awslogs-empty").textContent = view.lines.length
    ? t("No line has that level.")
    : t("Nothing found in this time. Try a longer time, or every line.");
}

function select(id) {
  awsLogsView.selected = awsLogsView.selected === id ? null : id;
  awsLogsView.tab = "event";
  paintAwsLogs();
}

function paintDetail() {
  const line = awsLogsView.lines.find((item) => item.id === awsLogsView.selected);
  $("awslogs-detail").hidden = !line;
  if (!line) return;
  const first = line.messages[0];
  const tab = first ? awsLogsView.tab : "raw";
  $("awslogs-d-title").textContent = first ? first.type || first.destination || t("Event") : line.function;
  $("awslogs-d-time").textContent = dateTime(line.at);
  for (const node of $("awslogs-d-tabs").children) {
    node.setAttribute("aria-selected", String(node.dataset.tab === tab));
    node.hidden = node.dataset.tab === "event" && !first;
  }
  const meta = [[t("Lambda"), line.function], [t("Log stream"), line.stream]];
  if (line.request) meta.push([t("Invocation"), line.request]);
  if (line.where) meta.push([t("Logged by"), `${line.logger} ${line.where}`]);
  $("awslogs-d-meta").replaceChildren(...meta.flatMap(([name, value]) => [el("dt", {}, name), el("dd", {}, value)]));
  const body = [];
  if (tab === "raw") {
    body.push(el("pre", { class: "awslogs-body" }, line.text, ...line.more.map((more) => `\n${more}`)));
  } else {
    for (const message of line.messages) {
      const text = typeof message.body === "string" ? message.body : JSON.stringify(message.body);
      const actions = [button(t("Copy JSON"), () => copyText(text, t("Copied.")), { class: "btn small" })];
      if (message.kind === "sqs" && message.type && state.events && state.events.up) {
        actions.push(button(t("Send to local events"), () => act("/api/events/send", { target: message.type, body: text },
          () => toastSent(message.type)), { class: "btn small" }));
      }
      body.push(el("p", { class: "awslogs-sub" }, `${message.direction === "sent" ? t("Sent") : t("Received")} · ${eventText(message)}`),
        el("pre", { class: "awslogs-body" }, jsonView(text)), el("div", { class: "bar-actions" }, ...actions));
    }
  }
  body.push(el("div", { class: "bar-actions" },
    el("a", { class: "btn small ghost", href: cloudWatchUrl(line), target: "_blank", rel: "noopener noreferrer" }, t("Open in CloudWatch"))));
  $("awslogs-d-body").replaceChildren(...body);
}

function toastSent(type) {
  toast(t("{type} sent to the local events.", { type }), "info");
}

export function showDetailTab(tab) {
  awsLogsView.tab = tab;
  paintDetail();
}

export function closeDetail() {
  awsLogsView.selected = null;
  paintAwsLogs();
}

export function paintAwsLogs() {
  if (!state) return;
  const view = awsLogsView;
  $("awslogs-profile").textContent = awsLogsView.profile
    ? t("dev · profile {profile}", { profile: awsLogsView.profile }) : t("no AWS profile");
  $("awslogs-summary").textContent = view.reading ? t("Asking CloudWatch…")
    : view.asked ? t("read {when}", { when: dateTime(view.asked) }) : "";
  $("awslogs-refresh").disabled = view.reading;
  $("awslogs-opened").hidden = !view.opened;
  $("awslogs-opened-text").textContent = view.opened
    ? t("Opened from Events › Event types: the event flow, type {type}.", { type: view.opened }) : "";
  paintFilters();
  paintProblem();
  const errors = Object.values(view.errors || {});
  $("awslogs-errors").hidden = !errors.length && !view.more.length;
  $("awslogs-errors").replaceChildren(
    ...errors.map((error) => el("p", {}, error.message)),
    ...(view.more.length ? [el("p", {}, t("More lines than were read in {names}: these are the first ones of the time; choose a shorter time.",
      { names: view.more.join(", ") }))] : []),
  );
  const shown = view.lines.filter((line) => !view.level || line.level === view.level);
  paintRows(shown);
  $("awslogs-count").textContent = t("{shown} of {total} lines", { shown: shown.length, total: view.lines.length });
  paintDetail();
  paintDot();
}
