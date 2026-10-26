// The texts of the icon and the panel, in pdms' language (pdms ui sends it; the browser's until it answers).
"use strict";

const TEXTS = {
  en: {
    on: "Redirecting to {target}",
    off: "The redirect is off: the known front opens as always.",
    waiting: "The redirect is on, but the frontend is not running: start it from pdms ui.",
    nopdms: "pdms ui is not open: the known front opens as always.",
    toggle: "Open the known front in my local frontend",
    fronts: "Known fronts",
    local: "Local frontend",
    notRunning: "not running",
    check: "Check now",
    checking: "Checking…",
    checked: "Checked at {time}",
    never: "Not checked yet",
    failed: "pdms ui did not answer.",
  },
  es: {
    on: "Redirigiendo a {target}",
    off: "La redirección está desactivada: el front conocido abre como siempre.",
    waiting: "La redirección está activada, pero el frontend no está corriendo: arráncalo desde pdms ui.",
    nopdms: "pdms ui no está abierto: el front conocido abre como siempre.",
    toggle: "Abrir el front conocido en mi frontend local",
    fronts: "Fronts conocidos",
    local: "Frontend local",
    notRunning: "no está corriendo",
    check: "Comprobar ahora",
    checking: "Comprobando…",
    checked: "Comprobado a las {time}",
    never: "Aún sin comprobar",
    failed: "pdms ui no respondió.",
  },
};

// "on" (redirecting), "off" (pdms ui says no), "waiting" (on, but no frontend) or "nopdms" (no pdms ui answered).
function kindOf(data) {
  if (!data) return "nopdms";
  if (data.active) return "on";
  return data.redirect && !data.target ? "waiting" : "off";
}

function text(language, key, vars = {}) {
  const table = TEXTS[language] || TEXTS[(navigator.language || "en").slice(0, 2)] || TEXTS.en;
  return (table[key] || TEXTS.en[key]).replace(/\{(\w+)\}/g, (_, name) => vars[name] ?? "");
}
