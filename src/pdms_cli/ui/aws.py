"""AWS while pdms ui runs: whether the chosen profile's session still works (every :data:`EVERY` seconds), the
Lambdas' buckets read again once a day (:data:`awsenv.READ_EVERY`), and reading them or logging in from the page.

The summary (bucket names, counts, what changed) is worked out after each of those and kept: building it walks the
repo and its Terraform, too slow for every state the page gets.
"""

from __future__ import annotations

import re
import subprocess
import threading
from collections.abc import Callable
from datetime import datetime

from .. import actions, awsenv, instances
from ..config import Config
from ..i18n import _

EVERY = 600
LOGIN_WAIT = 600  # seconds aws sso login may wait for the browser
KEY = "aws"  # the log of the last login
URL = re.compile(r"https://\S+")
CODE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b")


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Aws:
    def __init__(self, on_change: Callable[[], None] = lambda: None) -> None:
        self.on_change = on_change
        self._lock = threading.Lock()
        self._checking = False
        self._reading = False
        self._login: dict = {"running": False, "url": "", "code": "", "error": ""}
        self._session: dict = {"state": "", "account": "", "detail": "", "at": ""}
        self._error = ""
        self._summary: dict = {}

    def summary(self) -> dict:
        with self._lock:
            return {**self._summary, "session": dict(self._session), "reading": self._reading,
                    "checking": self._checking, "login": dict(self._login), "error": self._error}

    def _summarize(self, cfg: Config) -> None:
        try:
            found = awsenv.summary(cfg)
        except Exception:  # noqa: BLE001 - a broken repo or state file must not stop the monitor
            return
        with self._lock:
            self._summary = found

    def check(self, read_if_due: bool = True) -> None:
        """The session now (here, in this thread); then the Lambdas when a read is due and the session works."""
        cfg = Config.load()
        profile = cfg.defaults.aws_profile
        with self._lock:
            if self._checking:
                return
            self._checking = True
        try:
            who = awsenv.session(profile, timeout=30) if profile and awsenv.cli() else None
            with self._lock:
                self._session = ({"state": who.state, "account": who.account, "detail": who.detail, "at": now()}
                                 if who else {"state": "", "account": "", "detail": "", "at": ""})
            self._summarize(cfg)
            self.on_change()
            if who and who.state == "ok" and read_if_due and awsenv.due(awsenv.load(), profile):
                self._read(cfg)
        finally:
            with self._lock:
                self._checking = False
        self.on_change()

    def _read(self, cfg: Config) -> None:
        with self._lock:
            self._reading = True
            self._error = ""
        self.on_change()
        try:
            actions.read_aws(cfg)
            error = ""
        except actions.ActionError as exc:
            error = exc.message
        with self._lock:
            self._reading = False
            self._error = error
        self._summarize(cfg)
        self.on_change()

    def read(self) -> bool:
        """Read the Lambdas now, in the background; False when a read is already going."""
        cfg = Config.load()
        if not cfg.defaults.aws_profile:
            raise actions.ActionError(_("Choose an AWS profile first (pdms aws profile)."))
        with self._lock:
            if self._reading:
                return False
            self._reading = True
        threading.Thread(target=self._read, args=(cfg,), name="pdms-ui-aws-read", daemon=True).start()
        return True

    def choose(self, profile: str) -> None:
        """Use ``profile`` ("" = none), then check it (and read its Lambdas when they were not read for it)."""
        actions.set_aws_profile(Config.load(), profile)
        with self._lock:
            self._session = {"state": "", "account": "", "detail": "", "at": ""}
            self._error = ""
        self._summarize(Config.load())
        self.on_change()
        threading.Thread(target=self.check, name="pdms-ui-aws-check", daemon=True).start()

    def login(self) -> bool:
        """``aws sso login`` for the chosen profile, in the background (it opens the browser and waits there)."""
        cfg = Config.load()
        profile = cfg.defaults.aws_profile
        if not profile:
            raise actions.ActionError(_("Choose an AWS profile first (pdms aws profile)."))
        if not awsenv.cli():
            raise actions.ActionError(_("The AWS CLI (aws) is not installed."))
        with self._lock:
            if self._login["running"]:
                return False
            self._login = {"running": True, "url": "", "code": "", "error": ""}
        threading.Thread(target=self._work_login, args=(profile,), name="pdms-ui-aws-login", daemon=True).start()
        self.on_change()
        return True

    def _work_login(self, profile: str) -> None:
        log = instances.log_path(KEY)
        log.parent.mkdir(parents=True, exist_ok=True)
        error = ""
        try:
            with open(log, "w", encoding="utf-8", errors="replace") as output:
                proc = subprocess.Popen(awsenv.login_command(profile), stdout=output, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, text=True)
                try:
                    while proc.poll() is None:
                        try:
                            proc.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            self._found_in(log)
                    code = proc.returncode
                except Exception:
                    proc.kill()
                    raise
            if code != 0:
                lines = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()
                error = lines[-1] if lines else f"aws sso login exited with code {code}"
        except OSError as exc:
            error = str(exc)
        with self._lock:
            self._login = {"running": False, "url": "", "code": "", "error": error}
        self.on_change()
        self.check()

    def _found_in(self, log) -> None:
        """The page shows the address and code aws sso login prints, for when the browser did not open."""
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        url, code = URL.search(text), CODE.search(text)
        with self._lock:
            changed = (url and url.group(0) != self._login["url"]) or (code and code.group(0) != self._login["code"])
            if url:
                self._login["url"] = url.group(0)
            if code:
                self._login["code"] = code.group(0)
        if changed:
            self.on_change()

    def watch(self, stopped: threading.Event) -> None:
        while not stopped.is_set():
            try:
                self.check()
            except Exception:  # noqa: BLE001 - next time it may work
                pass
            stopped.wait(EVERY)
