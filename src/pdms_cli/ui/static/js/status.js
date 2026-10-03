// The status bar at the bottom: branch, user, the current database with its round trip, the tunnel it goes through,
// proxy, events and the version. The database opens a pop-up with every database in use and its last hour.

import { $, act, el, icon, post, toast } from "./core.js";
import { state } from "./state.js";

const WARN_MS = 100; // as health.SLOW_MS: every query is noticeably slower from here on

// "ok", "warn" or "bad" for a measured database (null when not measured yet); classes lv-ok, lv-warn... (.warn is
// taken by the warning boxes).
export function dbLevel(item) {
  if (!item) return null;
  if (item.error || (item.route.kind === "tunnel" && item.route.up === false)) return "bad";
  return item.ms >= WARN_MS ? "warn" : "ok";
}

export function roundTrip(ms) {
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(2)} s`;
}

function measured(name) {
  return (state.health.dbs || []).find((item) => item.name === name) || null;
}

function routeText(route) {
  if (route.kind === "local") return t("this machine");
  if (route.kind === "direct") return t("straight out");
  if (route.up === false) return t("tunnel {address}: nothing listens there", { address: route.address });
  return route.process ? t("tunnel {address} · {process}", { address: route.address, process: route.process })
    : t("tunnel {address}", { address: route.address });
}

function item(content, { level = "", title = "", onclick = null, cls = "" } = {}) {
  const attrs = { class: `st-item ${cls}`.trim() };
  if (title) attrs.title = title;
  if (onclick) Object.assign(attrs, { type: "button", onclick });
  const node = el(onclick ? "button" : "span", attrs);
  if (level) node.append(el("i", { class: `dot lv-${level}` }));
  node.append(...content);
  return node;
}

export function paintStatus() {
  const items = [];
  const branch = state.repo && state.repo.branch;
  if (branch) items.push(item([icon("branch"), el("b", {}, branch)], { title: t("Branch of {repo}", { repo: state.repo.alias }), cls: "opt" }));
  if (state.user) items.push(item([el("b", {}, state.user)], { title: t("Current user"), cls: "opt" }));
  if (state.db) {
    const current = measured(state.db);
    const level = dbLevel(current) || "off";
    const text = !current ? t("measuring…") : current.error ? t("no answer") : roundTrip(current.ms);
    const title = current ? `${state.db}: ${current.error || t("{time} per round trip", { time: roundTrip(current.ms) })} · ${routeText(current.route)}` : state.db;
    items.push(item([icon("db"), el("span", {}, state.db), el("b", {}, text)], { level, title, onclick: toggleDbPop, cls: "db-item" }));
    if (current && current.route.kind === "tunnel") {
      items.push(item([t("tunnel")], { level: current.route.up ? "ok" : "bad", title: routeText(current.route), cls: "opt" }));
    }
  }
  items.push(item([t("proxy"), el("b", {}, state.proxy ? `:${state.proxy.port}` : t("off"))], { level: state.proxy ? "ok" : "off", cls: "opt" }));
  items.push(item([t("events"), el("b", {}, state.events.up ? `:${state.events.port}` : t("off"))], { level: state.events.up ? "ok" : "off", cls: "opt" }));
  items.push(item([`pdms ${state.version}`], { cls: "version" }));
  $("statusbar").replaceChildren(...items);
  if (!$("db-pop").hidden) paintDbPop();
}

// The last hour of round trips as a line (gaps where it did not answer); the newest one marked.
function sparkline(history, level) {
  const NS = "http://www.w3.org/2000/svg";
  const width = 120, height = 26, pad = 3;
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("class", `spark lv-${level || "off"}`);
  svg.setAttribute("aria-hidden", "true");
  const values = history.filter((v) => v !== null);
  if (!values.length) return svg;
  const top = Math.max(...values, WARN_MS);
  const x = (i) => pad + (i * (width - 2 * pad)) / Math.max(history.length - 1, 1);
  const y = (v) => height - pad - (v / top) * (height - 2 * pad);
  let d = "";
  history.forEach((v, i) => { if (v !== null) d += `${d && history[i - 1] !== null ? "L" : "M"}${x(i).toFixed(1)} ${y(v).toFixed(1)} `; });
  const path = document.createElementNS(NS, "path");
  path.setAttribute("d", d.trim());
  svg.append(path);
  const last = history.length - 1;
  if (history[last] !== null) {
    const dot = document.createElementNS(NS, "circle");
    dot.setAttribute("cx", x(last).toFixed(1));
    dot.setAttribute("cy", y(history[last]).toFixed(1));
    dot.setAttribute("r", "2.4");
    svg.append(dot);
  }
  return svg;
}

function paintDbPop() {
  const dbs = state.health.dbs || [];
  $("db-pop-list").replaceChildren(...(dbs.length ? dbs.map((db) => el("div", { class: "db-row" },
    el("i", { class: `dot lv-${dbLevel(db)}` }),
    el("div", { class: "db-what" }, el("b", {}, db.name), el("small", {}, routeText(db.route))),
    sparkline(db.history, dbLevel(db)),
    el("span", { class: `db-ms lv-${dbLevel(db)}`, title: db.error || "" }, db.error ? t("no answer") : roundTrip(db.ms)),
  )) : [el("p", { class: "muted" }, t("No database in use yet: it is measured once a service or a stack uses one."))]));
}

export function toggleDbPop(event) {
  if (event) event.stopPropagation();
  $("db-pop").hidden = !$("db-pop").hidden;
  if (!$("db-pop").hidden) paintDbPop();
}

export function measureNow() {
  act("/api/health/run", {}, () => toast(t("Measuring… the bar updates in a moment."), "info"));
}

// One connection to the internet: tells a slow line from a slow way to the database (tunnel, VPN).
export async function lineCheck() {
  try {
    const { status, data } = await post("/api/health/line");
    if (status >= 400) throw new Error(data.error || String(status));
    if (data.ms === null) toast(t("No answer from {host}: no internet, or a firewall.", { host: data.host }));
    else if (data.ms >= (state.health.slow_ms || 300)) toast(t("{time} to {host}: the line itself is slow, so every remote round trip pays at least that.", { time: roundTrip(data.ms), host: data.host }));
    else toast(t("{time} to {host}: the line is fine, so the time goes on the way to the database (tunnel, VPN) or in it.", { time: roundTrip(data.ms), host: data.host }), "info");
  } catch {
    toast(t("pdms ui is not reachable: is it still running?"));
  }
}
