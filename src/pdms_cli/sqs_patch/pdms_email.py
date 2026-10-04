"""The emails a service sends through SES, seen in pdms ui instead of reaching people (loaded by sitecustomize.py).

pdms sets ``PDMS_EMAIL_LOG`` on every service it runs. Every SES send of a boto client (ses ``SendEmail``,
``SendRawEmail`` and ``SendTemplatedEmail``, sesv2 ``SendEmail``) is then written there, readable (``pdms logs
emails``, the Emails tab of pdms ui), and timed for the request it was sent for (pdms_trace.py):

* without ``PDMS_EMAIL_TO`` it never leaves the machine: it is answered like AWS would, with a made-up MessageId;
* with ``PDMS_EMAIL_TO`` it goes through the real SES to that address only, whoever it was for (Cc and Bcc
  dropped), with ``[to: <recipients>]`` before its subject.

Bulk sends are never sent, and neither is an email pdms cannot read: both are only written.
"""

import datetime
import email
import email.policy
import email.utils
import functools
import os
import sys
import time
import uuid

LOG_LIMIT = 5 * 1024 * 1024  # then the log starts again, keeping the previous one as <log>.1
HTML_MARK = "--- HTML ---"  # between the text and the HTML of an email in the log
OPERATIONS = {
    "ses": ("send_email", "send_raw_email", "send_templated_email", "send_bulk_templated_email"),
    "sesv2": ("send_email", "send_bulk_email"),
}
BULK = ("send_bulk_templated_email", "send_bulk_email")


def _request_id():
    try:
        import pdms_trace  # this folder

        return pdms_trace.current.get()
    except Exception:  # noqa: BLE001 - tracing is optional
        return ""


def _destination(destination):
    destination = destination or {}
    return [list(destination.get(key) or []) for key in ("ToAddresses", "CcAddresses", "BccAddresses")]


def _data(part):
    return str((part or {}).get("Data") or "")


def _parse_raw(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return email.message_from_bytes(bytes(data), policy=email.policy.default)


def _header_addresses(message, name):
    return [email.utils.formataddr(pair) for pair in email.utils.getaddresses(message.get_all(name, [])) if pair[1]]


def _bodies(message):
    text = html = ""
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        try:
            content = part.get_content()
        except Exception:  # noqa: BLE001 - an encoding the email package cannot read: leave that part out
            continue
        if part.get_content_type() == "text/plain" and not text:
            text = content
        elif part.get_content_type() == "text/html" and not html:
            html = content
    return text, html


def _raw_email(data, envelope=None):
    message = _parse_raw(data)
    text, html = _bodies(message)
    to, cc = _header_addresses(message, "To"), _header_addresses(message, "Cc")
    bcc = [address for address in envelope or [] if address not in to + cc]
    return {"from": str(message.get("From", "")), "to": to, "cc": cc, "bcc": bcc,
            "subject": str(message.get("Subject", "")), "text": text, "html": html}


def _template(name, data):
    return {"text": "", "html": "", "note": f"SES template {name}, with the data {data}"}


def describe(service, operation, params):
    """What an SES send carries: from, to, cc, bcc, subject, text, html (and a note when pdms cannot show it)."""
    if operation in BULK:
        if service == "ses":
            to = [address for entry in params.get("Destinations") or []
                  for address in (entry.get("Destination") or {}).get("ToAddresses") or []]
            return {"from": params.get("Source", ""), "to": to, "cc": [], "bcc": [], "subject": "", "text": "",
                    "html": "", "note": f"a bulk send of the SES template {params.get('Template', '')}: pdms never sends it"}
        to = [address for entry in params.get("BulkEmailEntries") or []
              for address in (entry.get("Destination") or {}).get("ToAddresses") or []]
        return {"from": params.get("FromEmailAddress", ""), "to": to, "cc": [], "bcc": [], "subject": "",
                "text": "", "html": "", "note": "a bulk send: pdms never sends it"}
    if service == "ses" and operation == "send_raw_email":
        seen = _raw_email(params["RawMessage"]["Data"], params.get("Destinations"))
        seen["from"] = params.get("Source") or seen["from"]
        return seen
    if service == "ses":
        to, cc, bcc = _destination(params.get("Destination"))
        seen = {"from": params.get("Source", ""), "to": to, "cc": cc, "bcc": bcc}
        if operation == "send_templated_email":
            return {**seen, "subject": "", **_template(params.get("Template", ""), params.get("TemplateData", ""))}
        message = params.get("Message") or {}
        body = message.get("Body") or {}
        return {**seen, "subject": _data(message.get("Subject")), "text": _data(body.get("Text")),
                "html": _data(body.get("Html"))}
    content = params.get("Content") or {}
    to, cc, bcc = _destination(params.get("Destination"))
    seen = {"from": params.get("FromEmailAddress", ""), "to": to, "cc": cc, "bcc": bcc}
    if "Raw" in content:
        raw = _raw_email(content["Raw"].get("Data", b""))
        return {**raw, **{key: value for key, value in seen.items() if value}}
    if "Template" in content:
        template = content["Template"]
        return {**seen, "subject": "",
                **_template(template.get("TemplateName") or template.get("TemplateArn", ""), template.get("TemplateData", ""))}
    simple = content.get("Simple") or {}
    body = simple.get("Body") or {}
    return {**seen, "subject": _data(simple.get("Subject")), "text": _data(body.get("Text")),
            "html": _data(body.get("Html"))}


def tagged(subject, seen):
    """The subject of a redirected email: who it was for, then its own."""
    recipients = ", ".join(seen["to"] + seen["cc"] + seen["bcc"]) or "nobody"
    return f"[to: {recipients}] {subject}".rstrip()


def _retagged_raw(data, seen):
    message = _parse_raw(data)
    subject = str(message.get("Subject", ""))
    del message["Subject"]
    message["Subject"] = tagged(subject, seen)
    return message.as_bytes()


def redirect(service, operation, params, address, seen):
    """The parameters that send the email to ``address`` only, with who it was for in its subject (None: not sent)."""
    if operation in BULK or seen.get("unreadable"):
        return None
    sent = dict(params)
    if service == "ses" and operation == "send_raw_email":
        sent["Destinations"] = [address]
        sent["RawMessage"] = {**params["RawMessage"], "Data": _retagged_raw(params["RawMessage"]["Data"], seen)}
        return sent
    sent["Destination"] = {"ToAddresses": [address]}
    if service == "ses" and operation == "send_email":
        message = dict(params.get("Message") or {})
        message["Subject"] = {**(message.get("Subject") or {}), "Data": tagged(_data(message.get("Subject")), seen)}
        sent["Message"] = message
    elif service == "sesv2":
        content = dict(params.get("Content") or {})
        if "Simple" in content:
            simple = dict(content["Simple"])
            simple["Subject"] = {**(simple.get("Subject") or {}), "Data": tagged(_data(simple.get("Subject")), seen)}
            content["Simple"] = simple
        elif "Raw" in content:
            content["Raw"] = {**content["Raw"], "Data": _retagged_raw(content["Raw"].get("Data", b""), seen)}
        sent["Content"] = content
    return sent


def answer(operation, params):
    """What AWS would have answered (boto's parsed shape)."""
    metadata = {"HTTPStatusCode": 200, "RequestId": str(uuid.uuid4())}
    if operation == "send_bulk_templated_email":
        return {"Status": [{"Status": "Success", "MessageId": str(uuid.uuid4())}
                           for _ in params.get("Destinations") or []], "ResponseMetadata": metadata}
    if operation == "send_bulk_email":
        return {"BulkEmailEntryResults": [{"Status": "SUCCESS", "MessageId": str(uuid.uuid4())}
                                          for _ in params.get("BulkEmailEntries") or []],
                "ResponseMetadata": metadata}
    return {"MessageId": str(uuid.uuid4()), "ResponseMetadata": metadata}


def _one_line(value):
    return " ".join(str(value).split())


def write_log(path, seen, status, sent_to="", error=""):
    """One readable entry per email: when, which service, kept | sent | failed, an id and the request, then its
    headers, a blank line and its text (and its HTML after ``HTML_MARK``), every line indented."""
    try:
        if os.path.getsize(path) > LOG_LIMIT:
            os.replace(path, f"{path}.1")
    except OSError:
        pass
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")  # local, like the other logs
    request = _request_id()
    ident = uuid.uuid4().hex[:12]  # pdms ui finds the email's HTML by it
    header = f"{now} {os.path.basename(os.getcwd())} {status} {ident}" + (f" #{request}" if request else "")
    fields = [("From", seen.get("from", "")), ("To", ", ".join(seen.get("to", []))), ("Cc", ", ".join(seen.get("cc", []))),
              ("Bcc", ", ".join(seen.get("bcc", []))), ("Sent to", sent_to), ("Error", error),
              ("Note", seen.get("note", "")), ("Subject", seen.get("subject", ""))]
    lines = [f"{name}: {_one_line(value)}" for name, value in fields if value or name in ("To", "Subject")]
    lines.append("")
    lines.extend(str(seen.get("text") or "").splitlines())
    if seen.get("html"):
        lines += ["", HTML_MARK, *str(seen["html"]).splitlines()]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:  # one write per entry: several services may append at once
        fh.write(header + "\n" + "".join(f"  {line}\n" for line in lines))


def _trace(seen, status, started):
    try:
        import pdms_trace  # this folder

        pdms_trace.record("email", ", ".join(seen.get("to", [])) or "?", started, time.time() - started, status=status,
                          subject=seen.get("subject", ""))
    except Exception:  # noqa: BLE001 - tracing is optional
        pass


def _keep(log, seen, status, **extra):
    try:
        write_log(log, seen, status, **extra)
    except Exception as error:  # noqa: BLE001 - say why in the service's log; the send itself goes on
        print(f"[pdms] could not write the email to {log}: {error}", file=sys.stderr, flush=True)


def _wrap(send, service, operation, log, address):
    @functools.wraps(send)
    def wrapped(*args, **params):
        started = time.time()
        try:
            seen = describe(service, operation, params)
        except Exception as error:  # noqa: BLE001 - never sent: pdms cannot tell what or to whom
            seen = {"to": [], "subject": "", "note": f"pdms could not read it ({error.__class__.__name__}: {error})",
                    "unreadable": True}
        sent = None
        if address:
            try:
                sent = redirect(service, operation, params, address, seen)
            except Exception as error:  # noqa: BLE001 - same: kept rather than sent to its real recipients
                seen["note"] = f"pdms could not redirect it ({error.__class__.__name__}: {error})"
        if sent is None:
            _keep(log, seen, "kept")
            _trace(seen, "kept", started)
            return answer(operation, params)
        try:
            result = send(*args, **sent)
        except Exception as error:
            _keep(log, seen, "failed", sent_to=address, error=f"{error.__class__.__name__}: {error}")
            _trace(seen, "failed", started)
            raise
        _keep(log, seen, "sent", sent_to=address)
        _trace(seen, "sent", started)
        return result

    setattr(wrapped, "_pdms_email", True)  # noqa: B010 - a mark on the function, for watching a client once
    return wrapped


def watch_client(client, service_name):
    """Make the SES sends of a boto client go to the log (and, with ``PDMS_EMAIL_TO``, to that address only)."""
    log = os.environ.get("PDMS_EMAIL_LOG", "")
    if not log or service_name not in OPERATIONS:
        return
    address = os.environ.get("PDMS_EMAIL_TO", "").strip()
    for operation in OPERATIONS[service_name]:
        send = getattr(client, operation, None)
        if send is not None and not getattr(send, "_pdms_email", False):
            setattr(client, operation, _wrap(send, service_name, operation, log, address))
