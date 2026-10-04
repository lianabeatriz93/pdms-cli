// The AWS card of Data and its item in the status bar: the profile services use, whether its session works, and the
// buckets read from that account's Lambdas. Everything comes in the state (pdms ui checks it every 10 minutes).

import { $, act, dateTime, el, toast } from "./core.js";
import { state } from "./state.js";

// "ok", "warn" or "bad" for the session (null when no profile is chosen or it was not checked yet).
export function awsLevel(aws) {
  if (!aws || !aws.profile) return null;
  const session = aws.session || {};
  if (session.state === "ok") return "ok";
  if (session.state === "expired") return "warn";
  if (session.state === "error") return "bad";
  return null;
}

function sessionText(aws) {
  if (!aws.profile) return [t("no profile"), "off"];
  const session = aws.session || {};
  if (aws.checking && !session.state) return [t("checking…"), "busy"];
  if (session.state === "ok") return [t("session works"), "ok"];
  if (session.state === "expired") return [t("session over"), "error"]; // .st.error is the warning colour
  if (session.state === "error") return [t("no answer"), "stopped"];
  return [t("not checked yet"), "off"];
}

function paintProfiles(aws) {
  const select = $("data-aws-profile");
  if (document.activeElement === select) return; // never under the user's hand
  const options = [el("option", { value: "" }, t("None: as the terminal has it"))];
  for (const name of aws.profiles || []) options.push(el("option", { value: name }, name));
  if (aws.profile && !(aws.profiles || []).includes(aws.profile)) options.push(el("option", { value: aws.profile }, t("{name} (not in your AWS config)", { name: aws.profile })));
  select.replaceChildren(...options);
  select.value = aws.profile || "";
}

export function paintAws() {
  const aws = state.aws || {};
  const [text, cls] = sessionText(aws);
  $("data-aws-status").className = `st ${cls}`;
  $("data-aws-status").textContent = text;
  $("data-aws-status").title = (aws.session && aws.session.detail) || "";
  paintProfiles(aws);
  const chosen = Boolean(aws.profile);
  $("data-aws-facts").hidden = !chosen;
  $("data-aws-account").textContent = aws.session && aws.session.account ? `${aws.session.account} · ${aws.region}` : aws.account || "–";
  $("data-aws-read").textContent = aws.reading ? t("reading… (half a minute)") : aws.read_at
    ? t("{when} · {n} Lambdas", { when: dateTime(aws.read_at), n: aws.functions }) : t("not yet");
  $("data-aws-services").textContent = aws.read_at
    ? t("{n} of {total} (the rest use no bucket)", { n: aws.with_buckets, total: aws.services }) : "–";
  $("data-aws-buckets").replaceChildren(...(aws.buckets || []).map((name) => el("span", {}, name)));
  if (!(aws.buckets || []).length) $("data-aws-buckets").textContent = "–";
  const named = Object.entries(aws.named || {});
  $("data-aws-named").hidden = !named.length;
  $("data-aws-named").querySelector("summary").textContent =
    t("{n} services have a Lambda with another name (dev Terraform)", { n: named.length });
  $("data-aws-named-list").replaceChildren(...named.map(([service, fn]) => el("li", {}, el("span", { class: "mono" }, service), " → ", el("span", { class: "mono" }, fn))));
  const changes = aws.changes || [];
  $("data-aws-changed").hidden = !changes.length;
  if (changes.length) {
    const lines = changes.slice(0, 6).map((c) => `${c.function} · ${c.variable}: ${c.old || "–"} → ${c.new || "–"}`);
    if (changes.length > 6) lines.push(t("…and {n} more", { n: changes.length - 6 }));
    const restart = (aws.restart || []).length ? `\n${t("Restart to use them: {keys}", { keys: aws.restart.join(", ") })}` : "";
    $("data-aws-changed").textContent = `${t("Buckets changed in AWS on {when}:", { when: dateTime(aws.changed_at) })}\n${lines.join("\n")}${restart}`;
  }
  const expired = aws.session && aws.session.state === "expired";
  const login = aws.login || {};
  $("data-aws-login-btn").hidden = !chosen || !(expired || login.running);
  $("data-aws-login-btn").disabled = Boolean(login.running);
  $("data-aws-login-btn").textContent = login.running ? t("Waiting for the browser…") : t("Log in (aws sso login)");
  $("data-aws-login").hidden = !(login.running && (login.url || login.code));
  $("data-aws-login").textContent = login.code
    ? t("If the browser did not open: {url} with the code {code}.", { url: login.url, code: login.code })
    : t("If the browser did not open: {url}", { url: login.url });
  $("data-aws-read-btn").hidden = !chosen;
  $("data-aws-read-btn").disabled = Boolean(aws.reading) || !(aws.session && aws.session.state === "ok");
  $("data-aws-check").hidden = !chosen;
  $("data-aws-check").disabled = Boolean(aws.checking);
  const error = aws.error || login.error || "";
  $("data-aws-error").hidden = !error;
  $("data-aws-error").textContent = error;
}

export function chooseAwsProfile() {
  const profile = $("data-aws-profile").value;
  act("/api/aws/profile", { profile }, () => toast(profile
    ? t("Services use {profile} from their next start. Checking its session…", { profile })
    : t("No AWS profile: services use AWS as the terminal has it."), "info"));
}

export function readAws() {
  act("/api/aws/read", {}, () => toast(t("Reading the Lambdas… half a minute."), "info"));
}

export function loginAws() {
  act("/api/aws/login", {}, () => toast(t("aws sso login opens your browser: confirm there."), "info"));
}

export function checkAws() {
  act("/api/aws/check", {});
}
