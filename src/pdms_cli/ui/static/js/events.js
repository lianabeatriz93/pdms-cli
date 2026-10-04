// Events: the local ElasticMQ, its queues and messages, the event types, the local SNS and sending events.

import { $, act, button, dateTime, el, getJson, phaseLabel, post, toast } from "./core.js";
import { state } from "./state.js";
import { showLogs } from "./logs.js";
import { openLaunch, servicesCount } from "./launch.js";
import { confirmDialog, slashes } from "./stacks.js";
import { info } from "./proxy.js";
import { currentView } from "./router.js";

export const EVENTS_JOB = "events:elasticmq";

const SNS_LINES = 20000;

const MAX_SNS = 2000;

const SNS_HEADER = /^(\S+) (\S+) → (\S+)(.*)$/; // events' sitecustomize.write_log: when, who → topic, extras

export const eventsView = {
  browse: false, tab: "queues", queues: null, map: null, peek: null, sns: [], snsStream: null, topics: "",
  ready: null, readyAt: 0, readyLoading: false, readyFailed: false, // GET /api/events/ready, while they are off
};

function eventsJob() {
  return state.jobs[EVENTS_JOB] || null;
}

function eventsPath(verb) {
  return `/api/events/${verb}`;
}

export function paintEvents() {
  const job = eventsJob();
  const busy = job && !job.error;
  const up = state.events.up;
  $("events-on").classList.toggle("on", Boolean(up));
  $("events-on").title = up ? t("on :{port}", { port: state.events.port }) : t("off");
  $("events-summary").textContent = busy ? phaseLabel(job.phase) : up ? t("ElasticMQ running on :{port}", { port: state.events.port }) : t("off");
  if (up) eventsView.browse = false;
  $("events-start").hidden = up || busy || (!eventsView.browse && !(job && job.error));
  $("events-stop").hidden = !up || busy;
  $("events-send").hidden = !up;
  const brokerRow = up && !busy ? brokerQueue() : null;
  $("events-broker").hidden = !brokerRow || !brokerRow.consumer || Boolean(brokerRow.running) || Boolean(consumerJob(brokerRow.consumer))
    || state.instances.some((i) => i.queue === brokerRow.name && i.status !== "stopped");

  const card = $("events-info");
  const data = eventsView.queues;
  // Off: what local events are and how to start them, instead of tables of dashes (they stay a click away).
  const intro = !up && !busy && !(job && job.error) && !eventsView.browse;
  $("events-off").hidden = !intro;
  card.hidden = intro;
  $("events-tabs").hidden = intro;
  if (intro) paintEventsOff();
  const queues = data && data.queues ? data.queues.length : eventsView.ready && eventsView.ready.queues;
  $("events-off-browse").textContent = queues ? t("See the {n} queues", { n: queues }) : t("See the queues");
  if (up) {
    const consumers = state.instances.filter((i) => i.queue && i.status !== "stopped");
    const broker = data && data.broker ? consumers.find((i) => i.queue === data.broker) : null;
    const publishers = state.instances.filter((i) => i.events === "local" && i.status !== "stopped").length;
    card.replaceChildren(
      info(t("Endpoint"), `http://localhost:${state.events.port}`),
      info(t("Broker"), !data ? "…" : broker ? broker.key : data.broker ? t("not running") : t("not in the repo")),
      info(t("Consumers running"), String(consumers.length)),
      info(t("Publishing locally"), servicesCount(publishers)),
      info(t("Last SNS publish"), state.sns && state.sns.last_publish ? dateTime(state.sns.last_publish) : t("nothing yet")),
    );
  } else {
    card.replaceChildren(el("p", { class: "muted note" }, busy ? t("Starting the local ElasticMQ…")
      : t("Off: services publish to AWS. Start the local events to run a local ElasticMQ (Docker) with every queue of the repo and the broker; services started afterwards publish there and to a local SNS.")));
  }
  if (job && job.error) {
    card.append(el("p", { class: "error" }, job.action === "up" ? t("Start failed: {error}", { error: job.error }) : t("Stop failed: {error}", { error: job.error })));
    const row = el("div", {});
    if (job.log_key) row.append(button(t("Install log"), () => showLogs(job.log_key, "install")));
    row.append(button(t("Dismiss"), () => act(eventsPath("dismiss")), { class: "btn small ghost" }));
    card.append(row);
  }
  for (const tab of $("events-tabs").children) tab.setAttribute("aria-selected", String(tab.dataset.tab === eventsView.tab));
  $("events-queues").hidden = intro || eventsView.tab !== "queues";
  $("events-types").hidden = intro || eventsView.tab !== "types";
  $("events-sns").hidden = intro || eventsView.tab !== "sns";
  if (eventsView.tab === "queues") paintQueues(); // a consumer's start shows as it goes
  syncSns();
}

export function showEventsTab(tab) {
  eventsView.tab = tab;
  paintEvents();
  if (tab === "queues") loadQueues();
  if (tab === "types") loadMap();
}

export function eventsVisible(tab) {
  return currentView() === "events" && eventsView.tab === tab && !document.hidden;
}

// ---- off: what starting them needs, and an event of the repo on its way

const READY_TTL = 30_000; // Docker can be started meanwhile: look again when the screen comes back after a while

function paintEventsOff() {
  const ready = eventsView.ready;
  if ((!ready || Date.now() - eventsView.readyAt > READY_TTL) && !eventsView.readyLoading) loadReady();
  const checks = [];
  if (!ready) {
    if (!eventsView.readyFailed) checks.push(el("span", { class: "st starting" }, t("checking Docker…")));
  } else {
    checks.push(ready.docker.ok ? el("span", { class: "st ok" }, t("Docker {version}", { version: ready.docker.version }))
      : el("span", { class: "st error" }, t("Docker is not running")));
    checks.push(el("span", { class: `st ${ready.port.free ? "ok" : "error"}` },
      ready.port.free ? t("port {port} free", { port: ready.port.port }) : t("port {port} in use", { port: ready.port.port })));
  }
  $("events-off-checks").replaceChildren(...checks);
  const example = ready && ready.example;
  const hop = (...parts) => el("li", {}, ...parts);
  $("events-off-flow").replaceChildren(...(example ? [
    hop(t("A service publishes"), el("code", {}, example.type), el("span", { class: "muted" }, "(pdms logs sns)")),
    hop(el("span", { class: "mono" }, example.broker), "→", el("span", { class: "muted" }, t("the broker"))),
    hop(el("span", { class: "mono" }, example.queue), "→", el("span", { class: "mono" }, example.consumer)),
  ] : [
    hop(t("A service publishes an event"), el("span", { class: "muted" }, "(pdms logs sns)")),
    hop(el("span", { class: "mono" }, (eventsView.queues && eventsView.queues.broker) || "broker-sqs-queue.fifo"), "→", el("span", { class: "muted" }, t("the broker"))),
    hop(t("The queue of its type"), "→", el("span", { class: "muted" }, t("its consumer"))),
  ]));
}

async function loadReady() {
  eventsView.readyLoading = true;
  const { data } = await getJson(eventsPath("ready"));
  eventsView.readyLoading = false;
  eventsView.readyAt = Date.now();
  eventsView.readyFailed = !data;
  if (data) eventsView.ready = data;
  if (currentView() === "events") paintEvents();
}

// ---- queues

export async function loadQueues() {
  const { data, error } = await getJson(eventsPath("queues"));
  if (error) {
    eventsView.queues = null;
    $("queue-rows").replaceChildren();
    $("queue-count").textContent = "";
    $("queue-empty").textContent = error;
    $("queue-empty").hidden = false;
    return;
  }
  eventsView.queues = data;
  paintQueues();
  paintEvents();
}

function count(value) {
  return value === null ? el("td", { class: "num muted" }, "-")
    : el("td", { class: `num ${value ? "count-on" : "muted"}` }, String(value));
}

// The job starting a consumer (pdms keys a consumer service by its folder: lead/lead-sqs-consumer → lead-sqs-consumer@sqs).
function consumerJob(service) {
  return service ? state.jobs[`${service.slice(service.lastIndexOf("/") + 1)}@sqs`] || null : null;
}

export function openConsumerStart(service, what = "consumer") {
  openLaunch({
    title: t("Start"), key: service, go: "start", path: "/api/run", service, user: state.user, db: state.db,
    hint: what === "broker" ? t("The broker reads its queue from the local ElasticMQ, in the background like pdms run -b.")
      : t("The consumer reads its queue from the local ElasticMQ, in the background like pdms run -b."),
    after: (_install, data) => { toast(t("Starting {job}…", { job: data.job }), "info"); loadQueues(); },
  });
}

export function brokerQueue() {
  const data = eventsView.queues;
  return data && data.queues.find((queue) => queue.broker) || null;
}

// A live instance of the queue's consumer reading another queue (a service that consumes two, like a retry queue).
function consumerElsewhere(queue) {
  if (!queue.consumer || queue.running) return null;
  return state.instances.find((i) => i.queue && i.status !== "stopped" && slashes(i.service).endsWith(`/${queue.consumer}`)) || null;
}

function queueRow(queue) {
  const elsewhere = consumerElsewhere(queue);
  const job = queue.running || elsewhere ? null : consumerJob(queue.consumer);
  const consumer = queue.running
    ? button(queue.running, () => showLogs(queue.running), { class: "btn tiny link", title: t("Logs of {key}", { key: queue.running }) })
    : elsewhere ? el("span", {}, button(elsewhere.key, () => showLogs(elsewhere.key), { class: "btn tiny link", title: t("Logs of {key}", { key: elsewhere.key }) }),
      el("span", { class: "muted" }, " ", t("reads {queue}", { queue: elsewhere.queue })))
    : queue.sns ? el("span", { class: "muted" }, t("local SNS: every publish"))
      : queue.broker ? el("span", { class: "muted" }, queue.consumer ? t("{consumer} (not running)", { consumer: queue.consumer }) : t("broker (not running)"))
        : el("span", { class: "muted" }, queue.consumer || "-");
  const actions = el("td", { class: "row-actions" });
  if (job) {
    actions.append(job.error
      ? button(t("Start failed"), () => showLogs(job.log_key || job.key, job.installed ? "install" : "current"), { class: "btn small bad", title: job.error })
      : el("span", { class: "st starting" }, phaseLabel(job.phase)));
  } else if (queue.consumer && !queue.running && !elsewhere && eventsView.queues.up) {
    actions.append(button(t("Start"), () => openConsumerStart(queue.consumer, queue.broker ? "broker" : "consumer"), { class: "btn small primary" }));
  }
  if (queue.visible !== null) {
    actions.append(button(t("Messages"), () => openPeek(queue.name)));
    if (!queue.sns) actions.append(button(t("Send"), () => openSend(queue.name)));
    if (queue.visible || queue.in_flight) actions.append(button(t("Purge"), () => purge([queue.name]), { class: "btn small bad" }));
  }
  const tags = [queue.fifo ? "fifo" : "", queue.broker ? t("broker") : "", queue.source === "elasticmq.conf" ? t("elasticmq.conf only") : ""].filter(Boolean);
  return el("tr", eventsView.peek === queue.name ? { class: "picked" } : {},
    el("td", { class: "mono wrap" }, queue.name, tags.length ? el("span", { class: "tag" }, tags.join(" · ")) : ""),
    count(queue.visible), count(queue.in_flight),
    el("td", { class: "wrap" }, consumer),
    el("td", { class: "num muted" }, queue.types ? String(queue.types) : ""),
    actions,
  );
}

export function paintQueues() {
  const data = eventsView.queues;
  if (!data) return;
  const text = $("queue-filter").value.trim().toLowerCase();
  const busyOnly = $("queue-busy").checked;
  const shown = data.queues.filter((queue) => (!busyOnly || queue.visible || queue.in_flight)
    && (!text || `${queue.name} ${queue.consumer} ${queue.running}`.toLowerCase().includes(text)));
  $("queue-rows").replaceChildren(...shown.map(queueRow));
  const waiting = data.queues.reduce((sum, queue) => sum + (queue.visible || 0), 0);
  const parts = [t("{shown} of {total} queues", { shown: shown.length, total: data.queues.length })];
  if (data.up) parts.push(waiting === 1 ? t("{n} message waiting", { n: waiting }) : t("{n} messages waiting", { n: waiting }));
  $("queue-count").textContent = parts.join(" · ");
  $("queue-purge-all").hidden = !data.up || !waiting;
  $("queue-empty").textContent = data.up ? t("No queue matches the filter.") : "";
  $("queue-empty").hidden = shown.length > 0 || !data.up;
}

export async function purge(queues) {
  const title = queues.length ? t("Purge {queues}?", { queues: queues.join(", ") }) : t("Purge every queue?");
  if (!await confirmDialog(title, t("Every message waiting there is deleted; nothing consumes them."), t("Purge"))) return;
  act(eventsPath("purge"), { queues }, (data) => {
    toast(data.purged.length ? t("Purged {queues}.", { queues: data.purged.join(", ") }) : t("Nothing to purge."), "info");
    loadQueues();
    if (eventsView.peek) openPeek(eventsView.peek);
  });
}

// ---- messages of a queue (peek)

const JSON_TOKEN = /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g;

// ``value`` with every string that holds a JSON object or list decoded, at any depth: an SNS message keeps the
// published Message as a string, whose data is often a JSON string too (like sitecustomize._unnest for sns.log).
function unnest(value) {
  if (typeof value === "string" && /^\s*[[{]/.test(value)) {
    try {
      return unnest(JSON.parse(value));
    } catch {
      return value;
    }
  }
  if (Array.isArray(value)) return value.map(unnest);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, unnest(item)]));
  return value;
}

// Pretty JSON with its keys, strings, numbers and literals coloured, JSON inside its strings decoded too; text that
// is not JSON stays as it is.
function jsonView(text) {
  let source;
  try {
    source = JSON.stringify(unnest(JSON.parse(text)), null, 2);
  } catch {
    return document.createTextNode(text);
  }
  const out = document.createDocumentFragment();
  let at = 0;
  for (const match of source.matchAll(JSON_TOKEN)) {
    if (match.index > at) out.append(source.slice(at, match.index));
    const [token, string, colon, literal] = match;
    if (string) {
      out.append(el("span", { class: colon ? "j-key" : "j-str" }, string));
      if (colon) out.append(colon);
    } else {
      out.append(el("span", { class: literal ? "j-lit" : "j-num" }, token));
    }
    at = match.index + token.length;
  }
  out.append(source.slice(at));
  return out;
}

// The message of a details row, built when it is first opened (a long SNS log holds many large ones).
function messageDetails(summary, text) {
  const node = el("details", { class: "msg" }, summary);
  const fill = () => {
    if (node.querySelector("pre")) return;
    const copy = button(t("Copy"), async () => {
      try {
        await navigator.clipboard.writeText(text);
        toast(t("Copied."), "info");
      } catch {
        toast(t("The browser did not allow copying."));
      }
    }, { class: "btn tiny copy" });
    node.append(el("div", { class: "msg-body" }, copy, el("pre", {}, jsonView(text))));
  };
  node.addEventListener("toggle", () => { if (node.open) fill(); });
  node.fill = fill;
  return node;
}

// What a message is about: the event name or type it carries, if any.
function messageKind(text) {
  try {
    const parsed = JSON.parse(text);
    if (!parsed || typeof parsed !== "object") return "";
    return String(parsed.event || parsed.type || parsed.event_type || (parsed.Message && messageKind(parsed.Message)) || "");
  } catch {
    return "";
  }
}

export async function openPeek(queue) {
  eventsView.peek = queue;
  $("peek").hidden = false;
  $("peek-queue").textContent = queue;
  $("peek-note").textContent = t("reading…");
  paintQueues();
  const { data, error } = await getJson(`${eventsPath("peek")}?${new URLSearchParams({ queue })}`);
  if (eventsView.peek !== queue) return;
  if (error) {
    $("peek-note").textContent = error;
    $("peek-messages").replaceChildren();
    return;
  }
  const waiting = data.messages.length;
  $("peek-note").textContent = waiting
    ? [waiting >= 50 ? t("{n} waiting (first 50)", { n: waiting }) : t("{n} waiting", { n: waiting }), t("read without consuming them")].join(" · ")
    : t("empty");
  $("peek-messages").replaceChildren(...data.messages.map((message) => {
    const kind = messageKind(message.body);
    return messageDetails(
      el("summary", {},
        el("span", { class: "mono muted" }, message.id.slice(0, 8)),
        kind ? el("span", { class: "topic" }, kind) : "",
        message.sent ? el("span", { class: "muted" }, new Date(message.sent).toLocaleString()) : "",
        el("span", { class: "muted" }, message.receives === 1
          ? t("received {n} time", { n: message.receives }) : t("received {n} times", { n: message.receives })),
      ),
      message.body,
    );
  }));
  $("peek").scrollIntoView({ block: "nearest" });
}

export function closePeek() {
  eventsView.peek = null;
  $("peek").hidden = true;
  paintQueues();
}

// ---- event types

async function loadMap() {
  const { data, error } = await getJson(eventsPath("map"));
  if (error) {
    $("type-rows").replaceChildren();
    $("type-empty").textContent = error;
    $("type-empty").hidden = false;
    return;
  }
  eventsView.map = data;
  paintMap();
}

export function paintMap() {
  const data = eventsView.map;
  if (!data) return;
  const text = $("type-filter").value.trim().toLowerCase();
  const shown = data.types.filter((item) => !text || `${item.type} ${item.queue} ${item.consumer}`.toLowerCase().includes(text));
  $("type-rows").replaceChildren(...shown.map((item) => el("tr", {},
    el("td", { class: "mono" }, item.type),
    el("td", { class: "mono" }, item.queue),
    item.consumer ? el("td", { class: "mono" }, item.consumer) : el("td", { class: "target-missing" }, t("none")),
    el("td", { class: "row-actions" }, state.events.up ? button(t("Send"), () => openSend(item.type)) : ""),
  )));
  $("type-count").textContent = [
    t("{shown} of {total} event types", { shown: shown.length, total: data.types.length }),
    data.broker ? t("broker {name}", { name: data.broker }) : t("broker not found"),
  ].join(" · ");
  $("type-empty").textContent = data.types.length ? t("No event type matches the filter.") : t("The broker routes no event types.");
  $("type-empty").hidden = shown.length > 0;
}

// ---- local SNS: sns.log, followed while the tab is open

function syncSns() {
  const wanted = currentView() === "events" && eventsView.tab === "sns";
  if (wanted && !eventsView.snsStream) {
    const stream = eventsView.snsStream = new EventSource(`/api/logs/stream?${new URLSearchParams({ key: "sns", lines: SNS_LINES })}`);
    stream.onopen = () => { eventsView.sns = []; paintSns(); };
    stream.addEventListener("lines", (event) => addSns(JSON.parse(event.data)));
    stream.addEventListener("reset", () => { eventsView.sns = []; paintSns(); }); // the log rotated
  } else if (!wanted && eventsView.snsStream) {
    eventsView.snsStream.close();
    eventsView.snsStream = null;
  }
}

function parseSnsHeader(line) {
  const match = SNS_HEADER.exec(line);
  if (!match) return null;
  let rest = match[4];
  let attributes = {};
  const at = rest.indexOf(" attributes={");
  if (at >= 0) {
    try { attributes = JSON.parse(rest.slice(at + " attributes=".length)); } catch { /* shown without them */ }
    rest = rest.slice(0, at);
  }
  let subject = "";
  const sj = rest.indexOf(" subject=");
  if (sj >= 0) { subject = rest.slice(sj + " subject=".length); rest = rest.slice(0, sj); }
  const g = rest.indexOf(" group=");
  const group = g >= 0 ? rest.slice(g + " group=".length).trim() : "";
  return { time: match[1], service: match[2], topic: match[3], subject, group, attributes, body: [] };
}

function addSns(lines) {
  for (const line of lines) {
    const last = eventsView.sns[eventsView.sns.length - 1];
    if (line.startsWith("  ") && last) last.body.push(line.slice(2));
    else if (!line.startsWith(" ")) {
      const entry = parseSnsHeader(line);
      if (entry) eventsView.sns.push(entry);
    }
  }
  if (eventsView.sns.length > MAX_SNS * 1.2) eventsView.sns = eventsView.sns.slice(-MAX_SNS);
  paintSns();
}

function snsShown(entry, topic, text) {
  if (topic && entry.topic !== topic) return false;
  if (!text) return true;
  const attrs = Object.entries(entry.attributes).map(([key, value]) => `${key}=${typeof value === "object" ? JSON.stringify(value) : value}`).join(" ");
  return `${entry.service} ${entry.topic} ${entry.subject} ${entry.group} ${attrs} ${entry.body.join("\n")}`.toLowerCase().includes(text);
}

function snsEntry(entry) {
  const attrs = Object.entries(entry.attributes).map(([key, value]) =>
    el("span", { class: "attr" }, `${key}=${typeof value === "object" ? JSON.stringify(value) : value}`));
  const text = entry.body.join("\n");
  const kind = messageKind(text);
  return messageDetails(
    el("summary", {},
      el("time", { class: "mono muted", datetime: entry.time }, dateTime(entry.time)),
      el("span", { class: "topic" }, entry.topic),
      kind ? el("span", { class: "kind" }, kind) : "",
      el("span", { class: "muted" }, t("from {service}", { service: entry.service })),
      entry.subject ? el("span", {}, entry.subject) : "",
      entry.group ? el("span", { class: "muted", title: "MessageGroupId" }, t("group {group}", { group: entry.group })) : "",
      ...attrs,
    ),
    text,
  );
}

function paintTopics() {
  const counts = {};
  for (const entry of eventsView.sns) counts[entry.topic] = (counts[entry.topic] || 0) + 1;
  const topics = Object.keys(counts).sort();
  const signature = topics.map((topic) => `${topic}:${counts[topic]}`).join("|");
  if (signature === eventsView.topics) return;
  eventsView.topics = signature;
  const current = $("sns-topic").value;
  $("sns-topic").replaceChildren(
    el("option", { value: "" }, t("All topics ({n})", { n: eventsView.sns.length })),
    ...topics.map((topic) => el("option", topic === current ? { value: topic, selected: "" } : { value: topic }, `${topic} (${counts[topic]})`)),
  );
}

export function paintSns() {
  paintTopics();
  const topic = $("sns-topic").value;
  const text = $("sns-filter").value.trim().toLowerCase();
  const open = new Set([...$("sns-entries").querySelectorAll("details[open]")].map((node) => node.dataset.at));
  const shown = [];
  for (let i = eventsView.sns.length - 1; i >= 0 && shown.length < 300; i--) {
    if (snsShown(eventsView.sns[i], topic, text)) shown.push([i, eventsView.sns[i]]);
  }
  $("sns-entries").replaceChildren(...shown.map(([i, entry]) => {
    const node = snsEntry(entry);
    node.dataset.at = `${entry.time}|${i}`;
    if (open.has(node.dataset.at)) {
      node.fill();
      node.open = true;
    }
    return node;
  }));
  const total = eventsView.sns.length;
  $("sns-count").textContent = !total ? ""
    : shown.length < 300 ? t("{shown} of {total} publishes", { shown: shown.length, total }) : t("latest 300 of {total} publishes", { total });
  $("sns-empty").textContent = !total
    ? state && state.events.up ? t("Nothing was published to the local SNS yet. Services started with local events publish here.") : t("Nothing published locally yet. Start the local events, then the services that publish.")
    : t("No publish matches the filter.");
  $("sns-empty").hidden = shown.length > 0;
}

// ---- start, stop and send

export function openEventsUp() {
  const broker = !eventsView.queues || eventsView.queues.broker_service;
  openLaunch({
    title: t("Start"), key: t("local events"),
    hint: t("A local ElasticMQ (Docker) with every queue of the repo, like pdms events up. The broker runs as this user and database."),
    user: state.user, db: state.db, go: "start", path: eventsPath("up"), broker,
    after: () => showEventsTab(eventsView.tab),
  });
}

export async function stopEvents() {
  const consumers = state.instances.filter((i) => i.queue && i.status !== "stopped").map((i) => i.key);
  const text = consumers.length
    ? t("Its messages are lost, and the consumers stop too: {consumers}. Services keep running, but what they publish now fails until it starts again.", { consumers: consumers.join(", ") })
    : t("Its messages are lost. Services keep running, but what they publish now fails until it starts again.");
  if (await confirmDialog(t("Stop the local events?"), text, t("Stop"))) act(eventsPath("down"));
}

const sending = { types: new Set(), queues: new Set(), broker: "" };

export async function openSend(target = "") {
  const [map, queues] = await Promise.all([
    eventsView.map ? { data: eventsView.map } : getJson(eventsPath("map")),
    eventsView.queues ? { data: eventsView.queues } : getJson(eventsPath("queues")),
  ]);
  if (map.error || queues.error) { toast(map.error || queues.error); return; }
  eventsView.map = map.data;
  eventsView.queues = queues.data;
  sending.types = new Set(map.data.types.map((item) => item.type));
  sending.queues = new Set(queues.data.queues.filter((queue) => !queue.sns).map((queue) => queue.name));
  sending.broker = map.data.broker;
  $("send-targets").replaceChildren(...[...sending.types, ...sending.queues].map((name) => el("option", { value: name })));
  $("send-target").value = target;
  $("send-direct").checked = false;
  $("send-body").value = "{}";
  $("send-error").hidden = true;
  sendTargetChanged();
  $("send").showModal();
  if (sending.types.has(target)) fillTemplate();
  else (target ? $("send-body") : $("send-target")).focus();
}

export function sendTargetChanged() {
  const target = $("send-target").value.trim();
  const isType = sending.types.has(target);
  $("send-direct-label").hidden = !isType || !sending.broker;
  $("send-template").hidden = !isType;
  $("send-body-label").textContent = isType ? t("Event fields (JSON; event_id and type are added)") : t("Message body (JSON)");
}

export async function fillTemplate() {
  const target = $("send-target").value.trim();
  const { data, error } = await getJson(`${eventsPath("template")}?${new URLSearchParams({ type: target })}`);
  if (error) {
    $("send-error").textContent = error;
    $("send-error").hidden = false;
    return;
  }
  $("send-body").value = JSON.stringify(data, null, 2);
  $("send-body").focus();
}

export async function submitSend(event) {
  event.preventDefault();
  const body = { target: $("send-target").value.trim(), body: $("send-body").value, direct: $("send-direct").checked };
  $("send-go").disabled = true;
  $("send-error").hidden = true;
  try {
    const { status, data } = await post(eventsPath("send"), body);
    if (status !== 200) {
      $("send-error").textContent = data.error || t("pdms ui answered {status}", { status });
      $("send-error").hidden = false;
      return;
    }
    $("send").close();
    const notes = [t("Sent {id} to {queue}.", { id: data.id.slice(0, 8), queue: data.queue })];
    if (data.routed_to) {
      notes.push(data.consumer ? t("The broker routes it to {queue} (consumer: {consumer}).", { queue: data.routed_to, consumer: data.consumer })
        : t("The broker routes it to {queue} (consumer: none).", { queue: data.routed_to }));
    }
    if (!data.consumed) notes.push(t("Nothing consumes {queue} right now: it waits there.", { queue: data.queue }));
    toast(notes.join(" "), "info");
    loadQueues();
  } catch {
    $("send-error").textContent = t("pdms ui is not reachable: is it still running?");
    $("send-error").hidden = false;
  } finally {
    $("send-go").disabled = false;
  }
}
