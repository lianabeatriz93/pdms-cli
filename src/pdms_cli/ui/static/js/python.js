// Services whose virtualenv runs a Python their project no longer accepts (actions.check_pythons): the user picks
// recreating it on an accepted one, keeping it, or not starting at all.

import { $, el } from "./core.js";

// true (recreate), false (keep) or null (cancel), from the services of a python_mismatch answer.
export function askPython(services) {
  const missing = services.filter((svc) => !svc.use);
  $("python-list").replaceChildren(...services.map((svc) => el("li", {},
    el("b", {}, svc.service), " ",
    svc.use
      ? t("Python {have}, it asks for {wants} → {use}", { have: svc.have, wants: svc.wants, use: svc.use })
      : t("Python {have}, it asks for {wants}: no accepted Python installed", { have: svc.have, wants: svc.wants }),
    ...(svc.program ? [" ", el("code", {}, svc.program)] : []),
  )));
  $("python-missing").hidden = !missing.length;
  $("python-missing").textContent = missing.length
    ? t("Install an accepted Python first (for example: uv python install 3.12), or keep theirs for now.") : "";
  $("python-recreate").disabled = missing.length > 0;
  $("python-dialog").returnValue = "";
  $("python-dialog").showModal();
  return new Promise((resolve) => {
    $("python-dialog").addEventListener("close", () => {
      const answer = $("python-dialog").returnValue;
      resolve(answer === "recreate" ? true : answer === "keep" ? false : null);
    }, { once: true });
  });
}
