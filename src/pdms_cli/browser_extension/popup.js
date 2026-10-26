// The panel of the icon: what the extension does now (background.js keeps the last answer of pdms ui), a switch for
// the redirect and a button to ask pdms ui right away.
"use strict";

const $ = (id) => document.getElementById(id);
let busy = false;

async function language(data) {
  if (data && data.language) return data.language;
  return (await chrome.storage.local.get("language")).language || "";
}

async function paint(last) {
  const data = last && last.data;
  const lang = await language(data);
  const kind = kindOf(data);
  $("state").className = `state ${kind}`;
  $("state").textContent = text(lang, kind, { target: data && data.target });
  $("toggle").checked = Boolean(data && data.redirect);
  $("toggle").disabled = busy || !data;
  $("toggle-label").textContent = text(lang, "toggle");
  $("fronts-label").textContent = text(lang, "fronts");
  $("fronts").textContent = data ? data.hosts.join("\n") : "-";
  $("local-label").textContent = text(lang, "local");
  $("local").textContent = data ? data.target || text(lang, "notRunning") : "-";
  $("check").textContent = text(lang, busy ? "checking" : "check");
  $("check").disabled = busy;
  $("checked").textContent = last && last.at
    ? text(lang, "checked", { time: new Date(last.at).toLocaleTimeString() }) : text(lang, "never");
}

async function send(message) {
  busy = true;
  await paint((await chrome.storage.local.get("last")).last);
  let last = null;
  try {
    last = await chrome.runtime.sendMessage(message);
  } catch {
    last = null;
  }
  busy = false;
  if (last) await paint(last);
  else {
    const stored = (await chrome.storage.local.get("last")).last;
    await paint(stored);
    $("checked").textContent = text(await language(stored && stored.data), "failed");
  }
}

$("check").addEventListener("click", () => send({ type: "sync" }));
$("toggle").addEventListener("change", (event) => send({ type: "redirect", on: event.target.checked }));
chrome.storage.local.get("last").then(({ last }) => paint(last));
send({ type: "sync" }); // what the panel shows is never older than its opening
