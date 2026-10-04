"""The real AWS of dev for the services pdms runs: the profile they use and the buckets each one needs.

The user picks one of the profiles of their AWS config (``defaults.aws_profile``; pdms never assumes one). pdms then
reads the configuration of that account's Lambdas (``aws lambda list-functions``, read-only) and keeps, for each
function, only the variables that name a bucket (and its region): the rest of a Lambda's environment holds secrets
and is never kept. The copy lives in ``<state>/aws-env.json``. A service gets the variables of the Lambda with its
name in the repo's dev Terraform (else the name of its folder) when it starts, without asking AWS again; a variable
the service's own ``.env`` or ``defaults.env`` sets keeps that value. pdms ui reads the Lambdas again at most once a
day (:data:`READ_EVERY`) and remembers what changed.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import instances, repos, routes, runner
from .config import Config, write_private
from .i18n import _

KEPT = re.compile(r"BUCKET")  # the only variables copied from a Lambda's environment
DEFAULT_REGION = "us-east-1"
READ_EVERY = 24 * 3600  # seconds between two reads of the Lambdas by pdms ui
# What the AWS CLI says when the SSO session is over (then `aws sso login` fixes it).
EXPIRED = re.compile(r"expired|aws sso login|Error loading SSO Token|refresh failed", re.IGNORECASE)


class AwsError(Exception):
    def __init__(self, message: str, expired: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.expired = expired


# --------------------------------------------------------------------------- profiles


def config_file() -> Path:
    custom = os.environ.get("AWS_CONFIG_FILE")
    return Path(custom).expanduser() if custom else Path.home() / ".aws" / "config"


def _read_config() -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser()
    try:
        parser.read(config_file(), encoding="utf-8")
    except (configparser.Error, UnicodeDecodeError):
        pass
    return parser


def profiles() -> list[str]:
    """The profiles of the user's AWS config, in its order (``default`` included when it is there)."""
    found = []
    for section in _read_config().sections():
        if section == "default":
            found.append("default")
        elif section.startswith("profile "):
            found.append(section.removeprefix("profile ").strip())
    return found


def region_of(profile: str) -> str:
    parser = _read_config()
    section = "default" if profile == "default" else f"profile {profile}"
    return parser.get(section, "region", fallback="") or DEFAULT_REGION


def cli() -> str | None:
    """The AWS CLI, or None when it is not installed."""
    return shutil.which("aws")


def _aws(profile: str, args: list[str], timeout: float) -> str:
    """Run the AWS CLI with ``profile``; its output, or :class:`AwsError` (``expired`` when a login fixes it)."""
    tool = cli()
    if not tool:
        raise AwsError(_("The AWS CLI (aws) is not installed."))
    cmd = [tool, *args, "--profile", profile, "--region", region_of(profile), "--output", "json"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise AwsError(_("AWS did not answer in {seconds} s.", seconds=int(timeout))) from exc
    except OSError as exc:
        raise AwsError(str(exc)) from exc
    if result.returncode != 0:
        error = (result.stderr or result.stdout).strip().splitlines()
        message = error[-1] if error else _("aws exited with code {code}", code=result.returncode)
        raise AwsError(message, expired=bool(EXPIRED.search(result.stderr or "")))
    return result.stdout


@dataclass
class Session:
    """Whether ``profile`` can call AWS now: "ok", "expired" (``aws sso login`` fixes it) or "error"."""

    state: str
    account: str = ""
    detail: str = ""


def session(profile: str, timeout: float = 30) -> Session:
    try:
        identity = json.loads(_aws(profile, ["sts", "get-caller-identity"], timeout))
    except AwsError as exc:
        return Session("expired" if exc.expired else "error", detail=exc.message)
    except ValueError:
        return Session("error", detail=_("AWS answered something that is not JSON."))
    return Session("ok", account=str(identity.get("Account", "")))


def login_command(profile: str) -> list[str]:
    return [cli() or "aws", "sso", "login", "--profile", profile]


# --------------------------------------------------------------------------- the Lambdas' buckets


def read_functions(profile: str, timeout: float = 180) -> dict[str, dict[str, str]]:
    """Every Lambda of the account with only its bucket variables (an empty dict when it has none)."""
    out = _aws(profile, ["lambda", "list-functions"], timeout)  # the CLI goes through every page itself
    try:
        listed = json.loads(out).get("Functions", [])
    except (ValueError, AttributeError) as exc:
        raise AwsError(_("AWS answered something that is not JSON.")) from exc
    functions = {}
    for item in listed:
        variables = (item.get("Environment") or {}).get("Variables") or {}
        functions[item["FunctionName"]] = {key: str(value) for key, value in sorted(variables.items()) if KEPT.search(key)}
    return functions


def state_path() -> Path:
    return instances.state_dir() / "aws-env.json"


def load() -> dict:
    """What was read last: profile, account, region, read_at, checked_at, functions and the last changes."""
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict) -> None:
    write_private(state_path(), json.dumps(data, indent=1, sort_keys=True))


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class Change:
    function: str
    variable: str
    old: str = ""  # "" when the variable is new
    new: str = ""  # "" when it is gone


def compare(old: dict[str, dict[str, str]], new: dict[str, dict[str, str]]) -> list[Change]:
    found = []
    for name in sorted(set(old) | set(new)):
        before, after = old.get(name, {}), new.get(name, {})
        for variable in sorted(set(before) | set(after)):
            if before.get(variable) != after.get(variable):
                found.append(Change(name, variable, before.get(variable, ""), after.get(variable, "")))
    return found


def refresh(profile: str) -> list[Change]:
    """Read the Lambdas of ``profile`` now and keep their buckets; what changed since the last read of the same
    account (nothing on the first read, or when the profile changed)."""
    who = session(profile)
    if who.state != "ok":
        raise AwsError(who.detail, expired=who.state == "expired")
    functions = read_functions(profile)
    saved = load()
    same = saved.get("account") == who.account and saved.get("functions")
    changes = compare(saved["functions"], functions) if same else []
    at = now()
    save({
        "profile": profile, "account": who.account, "region": region_of(profile), "read_at": at, "checked_at": at,
        "functions": functions,
        "changes": [vars(c) for c in changes] if changes else saved.get("changes", []) if same else [],
        "changed_at": at if changes else saved.get("changed_at", "") if same else "",
    })
    return changes


def due(saved: dict, profile: str, seconds: float = READ_EVERY) -> bool:
    """Whether pdms ui should read the Lambdas again: never read, another profile, or older than ``seconds``."""
    if saved.get("profile") != profile or not saved.get("checked_at"):
        return True
    try:
        last = datetime.fromisoformat(saved["checked_at"])
    except ValueError:
        return True
    return (datetime.now().astimezone() - last).total_seconds() >= seconds


# --------------------------------------------------------------------------- services ↔ Lambdas

MODULE = re.compile(r'module\s+"[^"]+"\s*\{(.*?)\n\}', re.DOTALL)
FUNCTION_NAME = re.compile(r'\n\s*function_name\s*=\s*"([^"$]+)"')
LAMBDA_PATH = re.compile(r'\n\s*lambda_path\s*=\s*"[^"]*?backend/([^"]+?)/?"')


def terraform_names(repo_root: Path) -> dict[str, list[str]]:
    """Service (relative to the backend folder, e.g. auth/profile-get) → the names of its Lambdas (auth-profile-get),
    from the dev Terraform: each Lambda module has its ``function_name`` and the ``lambda_path`` of its service. One
    folder may make several Lambdas (lead-sp-export and lead-sp-export-ev)."""
    names: dict[str, list[str]] = {}
    directory = routes.terraform_dir(repo_root, "dev")
    for tf in sorted(directory.glob("*.tf")) if directory.is_dir() else []:
        try:
            text = tf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for block in MODULE.finditer(text):
            name, path = FUNCTION_NAME.search(block.group(1)), LAMBDA_PATH.search(block.group(1))
            if name and path:
                names.setdefault(path.group(1), []).append(name.group(1))
    return names


@dataclass
class Matches:
    """Which Lambda each service of a repo goes with (service path → Lambda): ``named`` by the Terraform with
    another name than the service's folder, ``same`` with its folder's name; ``none`` has no Lambda in the account."""

    same: dict[str, str] = field(default_factory=dict)
    named: dict[str, str] = field(default_factory=dict)
    none: list[str] = field(default_factory=list)

    def of(self, service: str) -> str:
        return self.same.get(service) or self.named.get(service, "")


def match(services: list[str], functions: list[str], terraform: dict[str, list[str]]) -> Matches:
    """Pair services (paths relative to the backend folder) with the account's Lambdas: its folder's name when there
    is a Lambda with it, else the first name the Terraform gives it. Never a guess: a wrong pair would hand a service
    another one's buckets."""
    found = Matches()
    names = set(functions)
    for service in sorted(set(services)):
        folder = service.rsplit("/", 1)[-1]
        named = [name for name in terraform.get(service, []) if name in names and name != folder]
        if folder in names:
            found.same[service] = folder
        elif named:
            found.named[service] = named[0]
        else:
            found.none.append(service)
    return found


def repo_of(cfg: Config, service: Path | None = None) -> tuple[Path, Path] | None:
    """(root, backend folder) of ``service``'s repo, else of the current one."""
    alias = repos.repo_of(cfg, service) if service else cfg.current_repo
    repo = cfg.repos.get(alias or "")
    return (repo.root, repo.backend_dir) if repo and repo.backend_dir.is_dir() else None


def function_of(cfg: Config, service: Path, functions: dict) -> str:
    """The Lambda of ``service`` in the account read ("" when it has none)."""
    found = repo_of(cfg, service)
    if not found:
        return service.name if service.name in functions else ""
    root, backend = found
    try:
        relative = service.resolve().relative_to(backend.resolve()).as_posix()
    except ValueError:
        relative = service.name
    return match([relative], list(functions), terraform_names(root)).of(relative)


def dotenv_keys(service: Path) -> set[str]:
    """The variables the service's own ``.env`` sets (python-decouple reads it when the variable is not given)."""
    try:
        lines = (service / ".env").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return set()
    keys = set()
    for line in lines:
        line = line.strip().removeprefix("export ").strip()
        if line and not line.startswith("#") and "=" in line:
            keys.add(line.split("=", 1)[0].strip())
    return keys


def profile_env(cfg: Config) -> dict[str, str]:
    """``AWS_PROFILE`` when a profile is chosen (unless ``defaults.env`` sets its own)."""
    profile = cfg.defaults.aws_profile
    return {"AWS_PROFILE": profile} if profile and "AWS_PROFILE" not in cfg.defaults.env else {}


def env_for(cfg: Config, service: Path, saved: dict | None = None) -> dict[str, str]:
    """What ``service`` gets from AWS: the profile and its Lambda's buckets (read from the copy, never from AWS)."""
    env = profile_env(cfg)
    if not env:
        return {}
    saved = load() if saved is None else saved
    functions = saved.get("functions") or {}
    if not functions or saved.get("profile") != cfg.defaults.aws_profile:
        return env
    function = function_of(cfg, service, functions)
    own = dotenv_keys(service) | set(cfg.defaults.env)
    env.update({key: value for key, value in functions.get(function, {}).items() if key not in own})
    return env


def running_on(changes: list[dict], changed_at: str, cfg: Config) -> list[str]:
    """The running instances whose Lambda changed and that started before (they keep the old buckets until they
    restart). Both times are local: an instance's has no offset, so only the first 19 characters are compared."""
    changed = {c["function"] for c in changes}
    if not changed:
        return []
    saved = load()
    keys = []
    for inst in instances.load().values():
        if not inst.alive() or (inst.started_at and changed_at and inst.started_at[:19] >= changed_at[:19]):
            continue
        function = function_of(cfg, Path(inst.service), saved.get("functions", {}))
        if function in changed:
            keys.append(inst.key)
    return keys


def summary(cfg: Config) -> dict:
    """What the Data screen and ``pdms aws status`` show (bucket names, never anything else of a Lambda)."""
    saved = load()
    profile = cfg.defaults.aws_profile
    current = saved if saved.get("profile") == profile else {}
    functions = current.get("functions") or {}
    found_repo = repo_of(cfg)
    services = [path.relative_to(found_repo[1]).as_posix() for path in runner.find_services_below(found_repo[1])] \
        if found_repo else []
    found = match(services, list(functions), terraform_names(found_repo[0])) if functions and found_repo else Matches()
    with_buckets = sorted(s for s in services if functions.get(found.of(s)))
    return {
        "profile": profile, "profiles": profiles(), "cli": bool(cli()), "region": region_of(profile) if profile else "",
        "account": current.get("account", ""), "read_at": current.get("read_at", ""),
        "checked_at": current.get("checked_at", ""), "functions": len(functions),
        "services": len(services), "with_buckets": len(with_buckets), "named": {s: found.named[s] for s in with_buckets if s in found.named},
        "unmatched": len(found.none),
        "buckets": sorted({value for f in functions.values() for key, value in f.items() if "REGION" not in key}),
        "changes": current.get("changes", []), "changed_at": current.get("changed_at", ""),
        "restart": running_on(current.get("changes", []), current.get("changed_at", ""), cfg),
    }
