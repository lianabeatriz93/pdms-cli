// pdms ui: paints /api/state and follows /api/stream. Text only goes in through textContent, never as HTML.
"use strict";

const $ = (id) => document.getElementById(id);
let state = null;

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  for (const child of children) node.append(child);
  return node;
}

function uptime(startedAt) {
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(startedAt).getTime()) / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours ? `${hours}h${String(minutes).padStart(2, "0")}m` : `${minutes}m${String(seconds % 60).padStart(2, "0")}s`;
}

function chip(label, value) {
  return el("span", { class: "chip" }, label, el("b", {}, value || "-"));
}

function paintContext() {
  const chips = [chip("repo", state.repo && state.repo.alias), chip("user", state.user), chip("db", state.db)];
  chips.push(chip("proxy", state.proxy ? `:${state.proxy.port}` : "off"));
  chips.push(chip("events", state.events.up ? `:${state.events.port}` : "off"));
  $("ctx").replaceChildren(...chips);
}

function row(item) {
  const status = el("td", {}, el("span", { class: `st ${item.status}` }, item.status));
  if (item.detail) status.append(el("span", { class: "detail" }, item.detail));
  return el("tr", {},
    el("td", { class: "mono" }, item.key),
    status,
    el("td", { class: "mono" }, item.url),
    el("td", {}, item.repo || "-"),
    el("td", {}, item.user || "-"),
    el("td", {}, item.db || "-"),
    el("td", { class: "num" }, item.status === "stopped" || !item.started_at ? "" : uptime(item.started_at)),
  );
}

function paintServices() {
  const items = state.instances.map((inst) => ({
    ...inst, url: inst.queue ? `sqs ← ${inst.queue}` : `http://localhost:${inst.port}`,
  }));
  if (state.proxy) {
    items.push({ ...state.proxy, user: state.proxy.as, db: "", url: `http://localhost:${state.proxy.port}`, detail: "" });
  }
  $("rows").replaceChildren(...items.map(row));
  $("empty").hidden = items.length > 0;
  const alive = state.instances.filter((i) => i.status !== "stopped");
  const failing = state.instances.filter((i) => i.status === "error" || i.status === "stopped");
  $("count").textContent = alive.length || "";
  $("summary").textContent = `${alive.length} running · ${failing.length} failing`;
}

function paint(next) {
  state = next;
  paintContext();
  paintServices();
}

function connect() {
  const stream = new EventSource("/api/stream");
  stream.addEventListener("state", (event) => paint(JSON.parse(event.data)));
  stream.onopen = () => { $("live").className = "live on"; $("live").title = "Live"; };
  // EventSource reconnects by itself; the dot shows when pdms ui is not reachable.
  stream.onerror = () => { $("live").className = "live off"; $("live").title = "Disconnected: is pdms ui still running?"; };
}

fetch("/api/state").then((response) => response.json()).then(paint).finally(connect);
setInterval(() => state && paintServices(), 1000); // keep the uptimes ticking
