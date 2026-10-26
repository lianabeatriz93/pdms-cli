// pdms front redirect: while pdms ui says so, the known front (https://dev-pdm.alivi.com/... and the like) opens the
// PDMS web app that pdms runs on this computer (https://localhost:3000/...), with the same path and query.
//
// pdms ui tells what to do: every half a minute this asks it (POST /extension/state on 127.0.0.1, on its default port
// or the next ones) and turns the answer into redirect rules. Without an answer (pdms ui closed) there are no rules,
// and the known front opens as always.
"use strict";

const PORTS = Array.from({ length: 20 }, (_, i) => 8765 + i); // pdms ui --port's default, then the next free ones
const TIMEOUT = 5000; // pdms ui looks whether the frontend answers before it answers
const ALARM = "sync";

async function browserName() {
  if (location.protocol === "moz-extension:") return "firefox";
  try {
    if (navigator.brave && await navigator.brave.isBrave()) return "brave";
  } catch {
    // not Brave
  }
  const brands = ((navigator.userAgentData || {}).brands || []).map((item) => item.brand);
  if (brands.includes("Microsoft Edge")) return "edge";
  if (brands.includes("Brave")) return "brave";
  if (brands.includes("Google Chrome")) return "chrome";
  return "chromium";
}

async function ask(port, body) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT);
  try {
    const response = await fetch(`http://127.0.0.1:${port}/extension/state`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      signal: controller.signal, credentials: "omit", cache: "no-store",
    });
    if (!response.ok) return null;
    const data = await response.json();
    return data && data.pdms === "front-redirect" ? data : null;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

// The answer of the pdms ui running now, trying first the port that answered last time.
async function pdmsState() {
  const body = { browser: await browserName(), version: chrome.runtime.getManifest().version };
  const { port } = await chrome.storage.local.get("port");
  for (const candidate of port ? [port, ...PORTS.filter((p) => p !== port)] : PORTS) {
    const data = await ask(candidate, body);
    if (data) {
      if (candidate !== port) await chrome.storage.local.set({ port: candidate });
      return data;
    }
  }
  return null;
}

function rulesFor(data) {
  if (!data || !data.active || !data.target || !Array.isArray(data.hosts)) return [];
  let target;
  try {
    target = new URL(data.target);
  } catch {
    return [];
  }
  const transform = { scheme: target.protocol.replace(":", ""), host: target.hostname, port: target.port };
  return data.hosts.map((host, index) => ({
    id: index + 1, priority: 1,
    action: { type: "redirect", redirect: { transform } },
    condition: { requestDomains: [host], resourceTypes: ["main_frame"] },
  }));
}

// The files on disk are newer (pdms wrote a new version): load them, once per version.
async function reloadIfNewer(data) {
  if (!data || !data.version || data.version === chrome.runtime.getManifest().version) return false;
  const { reloadedFor } = await chrome.storage.local.get("reloadedFor");
  if (reloadedFor === data.version) return false;
  await chrome.storage.local.set({ reloadedFor: data.version });
  chrome.runtime.reload();
  return true;
}

async function sync() {
  const data = await pdmsState();
  if (await reloadIfNewer(data)) return;
  const old = await chrome.declarativeNetRequest.getDynamicRules();
  await chrome.declarativeNetRequest.updateDynamicRules({
    removeRuleIds: old.map((rule) => rule.id), addRules: rulesFor(data),
  });
}

function schedule() {
  chrome.alarms.create(ALARM, { periodInMinutes: 0.5 });
}

chrome.runtime.onInstalled.addListener(schedule);
chrome.runtime.onStartup.addListener(schedule);
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM) sync();
});
sync();
