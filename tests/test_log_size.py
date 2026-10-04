"""Logs stay small: a running process's log is cut past a size, old instances' logs go, and pdms's own health checks
never reach a service's log."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime

from pdms_cli import events, instances


def test_tail_reads_only_the_end(tmp_path) -> None:
    log = tmp_path / "big.log"
    log.write_text("".join(f"line {n}\n" for n in range(200_000)))  # ~2.5 MB
    assert instances.tail(str(log), 3) == "line 199997\nline 199998\nline 199999\n"
    assert instances.tail(str(log), 0) == ""  # not the whole file
    small = tmp_path / "small.log"
    small.write_text("a\nb\n")
    assert instances.tail(str(small), 10) == "a\nb\n"
    assert instances.tail(str(tmp_path / "missing.log"), 5) == ""


def test_a_process_appends_to_its_log_and_the_log_says_so(tmp_path) -> None:
    log = tmp_path / "svc@1.log"
    proc = instances.spawn([sys.executable, "-c", "print('hello')"], tmp_path, dict(os.environ), log)
    proc.wait(10)
    first, rest = log.read_text().split("\n", 1)
    assert instances.APPENDS in first and "hello" in rest


def test_a_big_log_moves_to_its_previous_and_starts_again_keeping_its_start(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(instances, "KEEP_HEAD", 200)
    log = tmp_path / "broker@sqs.log"
    head = f"# pdms 2026-10-03 10:00:00{instances.APPENDS} · poetry run python poller\n[pdms-poller] Polling q\n"
    log.write_text(head + "x" * 2000 + "\n")
    assert instances.keep_log_small(log, limit=1000) is True
    kept = log.read_text()
    assert kept.startswith(head) and "older lines are in broker@sqs.log.1" in kept and "xxx" not in kept
    assert instances.contains(str(log), instances.POLLER_READY)  # still ready after the cut
    assert (tmp_path / "broker@sqs.log.1").read_text().endswith("x" * 2000 + "\n")
    assert instances.keep_log_small(log, limit=1000) is False  # small now

    old = tmp_path / "old@1.log"  # opened by an older pdms ("w"): cutting it would leave a hole of zeros
    old.write_text("# pdms 2026-10-01 10:00:00 · uvicorn\n" + "y" * 2000)
    assert instances.keep_log_small(old, limit=1000) is False and old.stat().st_size > 2000


def test_a_running_process_keeps_writing_after_its_log_is_cut(tmp_path) -> None:
    log = tmp_path / "svc@2.log"
    script = "import time\nprint('x' * 3000, flush=True)\ntime.sleep(1.5)\nprint('after the cut', flush=True)\n"
    proc = instances.spawn([sys.executable, "-c", script], tmp_path, dict(os.environ), log)
    deadline = time.monotonic() + 10
    while log.stat().st_size < 3000:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert instances.keep_log_small(log, limit=1000)
    proc.wait(10)
    text = log.read_bytes()
    assert b"\0" not in text and text.endswith(b"after the cut\n")  # appended at the new end, no hole


def test_logs_of_instances_gone_for_days_are_deleted(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    folder = instances.state_dir() / "logs"
    folder.mkdir(parents=True)
    names = ["gone@8080.log", "gone@8080.log.1", "gone@8080.install.log", "live@8081.log", "recent@8082.log",
             "proxy.log", "sns.log"]
    for name in names:
        (folder / name).write_text("x")
    week_ago = time.time() - 8 * 86400
    for name in names:
        if name != "recent@8082.log":
            os.utime(folder / name, (week_ago, week_ago))
    instances.save({"live@8081": instances.Instance(
        key="live@8081", pid=1, service="/x", host="", port=8081, user="u", db="d", reload=False,
        log=str(folder / "live@8081.log"), started_at=datetime.now().isoformat())})
    assert sorted(instances.forget_old_logs()) == ["gone@8080.install.log", "gone@8080.log", "gone@8080.log.1"]
    assert sorted(p.name for p in folder.iterdir()) == ["live@8081.log", "proxy.log", "recent@8082.log", "sns.log"]


HEALTH = """
import asyncio, json, logging
import starlette.applications as app
lines = []
class Keep(logging.Handler):
    def emit(self, record):
        lines.append(self.format(record))
access = logging.getLogger("uvicorn.access")
access.addHandler(Keep())
access.setLevel(logging.INFO)
logging.getLogger("uvicorn.access").handlers[0].setFormatter(logging.Formatter("%(message)s"))
called, sent = [], []
class Starlette:
    async def __call__(self, scope, receive, send):
        called.append(scope["path"])
app.Starlette = Starlette
import pdms_trace
pdms_trace.follow_requests()
async def send(message):
    sent.append(message.get("status"))
async def run():
    for path in ("/__pdms_health", "/api/v1/x"):
        await app.Starlette()({"type": "http", "path": path, "headers": []}, None, send)
        access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", path, "1.1", 204)
asyncio.run(run())
print(json.dumps({"called": called, "sent": sent, "lines": lines}))
"""


def test_health_checks_are_answered_without_the_app_and_not_logged(tmp_path) -> None:
    (tmp_path / "starlette").mkdir()
    (tmp_path / "starlette" / "__init__.py").write_text("")
    (tmp_path / "starlette" / "applications.py").write_text("class Starlette:\n    pass\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PDMS_", "PYTHONPATH"))}
    env["PYTHONPATH"] = os.pathsep.join([str(events.PATCH_DIR), str(tmp_path)])
    out = subprocess.run([sys.executable, "-c", HEALTH], env=env, capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode == 0, out.stderr
    import json

    result = json.loads(out.stdout)
    assert result["called"] == ["/api/v1/x"]  # the health check never reached the app (nor its middleware's log)
    assert result["sent"][0] == 204
    assert result["lines"] == ['127.0.0.1:1 - "GET /api/v1/x HTTP/1.1" 204']  # only the real request


def test_instances_are_checked_on_the_quiet_path(monkeypatch) -> None:
    asked = []
    monkeypatch.setattr(instances, "responds", lambda host, port, timeout=1.5, path="/": asked.append(path) or True)
    inst = instances.Instance(key="svc@1", pid=os.getpid(), service="/x", host="", port=1, user="u", db="d",
                              reload=False, log="", started_at="", created=instances.creation_time(os.getpid()))
    assert instances.health(inst).state == "ok" and asked == [instances.HEALTH_PATH]
