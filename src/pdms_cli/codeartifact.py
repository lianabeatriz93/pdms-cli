"""The ``@alivi`` npm packages of the PDMS web app come from AWS CodeArtifact, with a token that lasts 12 hours.

The repo's ``frontend/codeartifact-login.sh`` logs in by hand (bash only, and it asks for a profile). pdms does the
same before ``yarn install`` when it is needed: it reads the domains from that script (so they stay in one place),
asks npm whether the token still works (``npm view``, under a second) and, when it does not, runs
``aws codeartifact login --tool npm`` with a profile of the right account. That writes the token to the user's
``~/.npmrc``, as the script does.
"""

from __future__ import annotations

import configparser
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from . import awsenv
from .config import Config
from .i18n import _

SCRIPT = "codeartifact-login.sh"
ROLE = "DeveloperTools"  # the script's REQUIRED_ROLE: profiles with it come first
NPM_TIMEOUT = 60
LOGIN_TIMEOUT = 120


class LoginError(Exception):
    """The login could not happen; ``expired`` when an ``aws sso login`` of ``profile`` fixes it."""

    def __init__(self, message: str, profile: str = "", expired: bool = False) -> None:
        super().__init__(message)
        self.message, self.profile, self.expired = message, profile, expired


@dataclass(frozen=True)
class Domain:
    domain: str
    repository: str
    namespace: str  # @alivi
    region: str
    account: str


def _value(text: str, name: str, default: str = "") -> str:
    match = re.search(rf'^\s*{name}="([^"]*)"', text, re.MULTILINE)
    return match.group(1) if match else default


def domains(frontend: Path) -> list[Domain]:
    """The CodeArtifact repositories ``frontend/codeartifact-login.sh`` logs in to (none without the script)."""
    try:
        text = (frontend / SCRIPT).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    region, account = _value(text, "REGION", "us-east-1"), _value(text, "REQUIRED_ACCOUNT")
    block = re.search(r"DOMAINS=\((.*?)\)", text, re.DOTALL)
    found = []
    for entry in re.findall(r'"([^":\s]+):([^":\s]+):(@[^":\s]+)"', block.group(1) if block else ""):
        found.append(Domain(entry[0], entry[1], entry[2], region, account))
    return found


def packages(frontend: Path, namespace: str) -> list[str]:
    """The app's dependencies in ``namespace`` (``@alivi/ui-kit-core``...)."""
    try:
        data = json.loads((frontend / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    names = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
    return sorted(name for name in names if name.startswith(namespace + "/"))


def needed(frontend: Path) -> list[Domain]:
    """The repositories the app installs packages from."""
    return [domain for domain in domains(frontend) if packages(frontend, domain.namespace)]


def authorized(frontend: Path, domain: Domain) -> bool | None:
    """Whether npm can read the domain's packages with the token it has (None: npm cannot tell, e.g. no npm)."""
    npm = shutil.which("npm")
    names = packages(frontend, domain.namespace)
    if not npm or not names:
        return None
    try:
        result = subprocess.run([npm, "view", names[0], "version"], cwd=frontend, capture_output=True, text=True,
                                timeout=NPM_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 0:
        return True
    text = (result.stdout + result.stderr).lower()
    return False if any(code in text for code in ("e401", "e403", "401", "403", "unauthorized", "forbidden")) else None


def profile_for(cfg: Config, account: str) -> str:
    """A profile of the user's AWS config for ``account``: the one pdms uses when it is of that account, else one
    with the script's role, else any ("" when there is none)."""
    parser = configparser.RawConfigParser()
    try:
        parser.read(awsenv.config_file(), encoding="utf-8")
    except (OSError, configparser.Error):
        return ""
    matching: list[tuple[str, str]] = []
    for section in parser.sections():
        name = section.removeprefix("profile ").strip() if section.startswith("profile ") else section
        if section.startswith("sso-session") or parser.get(section, "sso_account_id", fallback="") != account:
            continue
        matching.append((name, parser.get(section, "sso_role_name", fallback="")))
    names = [name for name, _role in matching]
    if cfg.defaults.aws_profile in names:
        return cfg.defaults.aws_profile
    with_role = [name for name, role in matching if role == ROLE]
    return (with_role or names or [""])[0]


def login_command(domain: Domain, profile: str) -> list[str]:
    return [awsenv.cli() or "aws", "codeartifact", "login", "--tool", "npm", "--domain", domain.domain,
            "--repository", domain.repository, "--namespace", domain.namespace, "--region", domain.region,
            "--profile", profile]


def login(cfg: Config, domain: Domain, output: IO[str] | None = None) -> str:
    """``aws codeartifact login --tool npm`` for ``domain``: the profile it used, or :class:`LoginError`."""
    if not awsenv.cli():
        raise LoginError(_("The AWS CLI (aws) is not installed: pdms needs it to log in to AWS CodeArtifact."))
    profile = profile_for(cfg, domain.account) if domain.account else cfg.defaults.aws_profile
    if not profile:
        raise LoginError(_("No profile of AWS account {account} in {path} to log in to CodeArtifact. Add one with "
                           "aws configure sso (role {role}).", account=domain.account, path=awsenv.config_file(),
                           role=ROLE))
    cmd = login_command(domain, profile)
    if output:
        output.write(f"$ {' '.join(cmd)}\n")
        output.flush()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=LOGIN_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise LoginError(_("AWS did not answer in {seconds} s.", seconds=LOGIN_TIMEOUT), profile) from exc
    except OSError as exc:
        raise LoginError(str(exc), profile) from exc
    if output:
        output.write(result.stdout + result.stderr)
        output.flush()
    if result.returncode != 0:
        lines = (result.stderr or result.stdout).strip().splitlines()
        message = lines[-1] if lines else _("aws exited with code {code}", code=result.returncode)
        raise LoginError(message, profile, expired=bool(awsenv.EXPIRED.search(result.stderr or "")))
    return profile
