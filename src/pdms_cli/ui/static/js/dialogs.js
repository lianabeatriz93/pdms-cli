// Every dialog (but the command palette) gets the same head: its title and a ✕ that stay at the top while the rest
// scrolls. The ✕ closes like Esc, with an empty answer, so no form is sent and whoever waits for the dialog reads
// "cancelled". Opening a dialog still focuses its first field or button, as before the ✕ came first.

import { el, icon } from "./core.js";

const FOCUSABLE = "input, select, textarea, button, a[href], [tabindex]:not([tabindex='-1'])";

export function frameDialogs() {
  for (const dialog of document.querySelectorAll("dialog:not(.palette)")) {
    const title = dialog.querySelector("h2");
    if (!title || title.parentElement.classList.contains("dialog-head")) continue;
    const close = el("button", {
      class: "dialog-close", type: "button", title: "Close", "aria-label": "Close", onclick: () => dialog.close(""),
    }, icon("forget"));
    const head = el("div", { class: "dialog-head" });
    title.before(head);
    head.append(title, close);
    collectStatics(close); // i18n.js: its label follows the language like the page's own texts
    const show = dialog.showModal.bind(dialog);
    dialog.showModal = () => {
      show();
      if (document.activeElement !== close) return;
      const first = [...dialog.querySelectorAll(FOCUSABLE)].find((node) => node !== close && !node.disabled && node.offsetParent);
      if (first) first.focus();
    };
  }
}
