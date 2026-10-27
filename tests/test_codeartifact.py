"""The @alivi packages of the web app: pdms logs in to AWS CodeArtifact when npm's token is over."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from pdms_cli import actions, awsenv, codeartifact, frontend
from pdms_cli.config import Config, Defaults

SCRIPT = '''#!/bin/bash
REGION="us-east-1"
REQUIRED_ACCOUNT="609837931903"
REQUIRED_ROLE="DeveloperTools"
declare -a DOMAINS=(
    "alivi:alivi-ui-kit:@alivi"
)
'''
AWS_CONFIG = """[sso-session default]
sso_region = us-east-1

[profile pdm-dev]
sso_account_id = 339713060902
sso_role_name = AdministratorAccess

[profile epic-admin]
sso_account_id = 609837931903
sso_role_name = AdministratorAccess

[profile epic-dev]
sso_account_id = 609837931903
sso_role_name = DeveloperTools
"""
DOMAIN = codeartifact.Domain("alivi", "alivi-ui-kit", "@alivi", "us-east-1", "609837931903")


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A repo whose web app installs @alivi packages, and an AWS config with profiles of two accounts."""
    folder = tmp_path / "frontend"
    folder.mkdir()
    (folder / codeartifact.SCRIPT).write_text(SCRIPT, encoding="utf-8")
    (folder / "package.json").write_text(json.dumps({
        "dependencies": {"@alivi/ui-kit-core": "3.0.0", "react": "18"}, "devDependencies": {"@alivi/ui-kit-react": "3"},
    }), encoding="utf-8")
    (tmp_path / "aws-config").write_text(AWS_CONFIG, encoding="utf-8")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "aws-config"))
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: Config(defaults=Defaults(aws_profile="pdm-dev"))))
    return tmp_path


def test_the_repositories_come_from_the_repos_script(app) -> None:
    folder = app / "frontend"
    assert codeartifact.domains(folder) == [DOMAIN]
    assert codeartifact.packages(folder, "@alivi") == ["@alivi/ui-kit-core", "@alivi/ui-kit-react"]
    assert codeartifact.needed(folder) == [DOMAIN]
    (folder / "package.json").write_text("{}", encoding="utf-8")
    assert codeartifact.needed(folder) == []  # nothing of @alivi to install
    assert codeartifact.domains(app) == []  # no script: no CodeArtifact


def test_a_profile_of_the_account_with_the_scripts_role(app) -> None:
    assert codeartifact.profile_for(Config(defaults=Defaults(aws_profile="pdm-dev")), DOMAIN.account) == "epic-dev"
    assert codeartifact.profile_for(Config(defaults=Defaults(aws_profile="epic-admin")), DOMAIN.account) == "epic-admin"
    assert codeartifact.profile_for(Config(), "123") == ""


def test_npm_tells_whether_the_token_still_works(app, monkeypatch) -> None:
    folder = app / "frontend"
    answers = {"code": 1, "err": "npm error code E401\nUnable to authenticate"}
    monkeypatch.setattr(codeartifact.shutil, "which", lambda name: "/usr/bin/npm")
    monkeypatch.setattr(codeartifact.subprocess, "run", lambda cmd, **kw: SimpleNamespace(
        returncode=answers["code"], stdout="", stderr=answers["err"]))
    assert codeartifact.authorized(folder, DOMAIN) is False
    answers.update(code=0, err="")
    assert codeartifact.authorized(folder, DOMAIN) is True
    answers.update(code=1, err="npm error code ENOTFOUND")  # offline: npm cannot tell
    assert codeartifact.authorized(folder, DOMAIN) is None


class Aws:
    """The AWS CLI: an expired SSO session until ``aws sso login``; then the login writes the token."""

    def __init__(self, expired: bool = False) -> None:
        self.expired, self.calls = expired, []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd[1:3])
        if self.expired:
            return SimpleNamespace(returncode=255, stdout="", stderr="Error when retrieving token from sso: Token has expired")
        return SimpleNamespace(returncode=0, stdout="Successfully configured npm\n", stderr="")


def test_login_with_the_accounts_profile(app, monkeypatch) -> None:
    aws = Aws()
    monkeypatch.setattr(awsenv, "cli", lambda: "/usr/bin/aws")
    monkeypatch.setattr(codeartifact.subprocess, "run", aws)
    assert codeartifact.login(Config(), DOMAIN) == "epic-dev"
    assert codeartifact.login_command(DOMAIN, "epic-dev")[1:] == [
        "codeartifact", "login", "--tool", "npm", "--domain", "alivi", "--repository", "alivi-ui-kit",
        "--namespace", "@alivi", "--region", "us-east-1", "--profile", "epic-dev"]
    aws.expired = True
    with pytest.raises(codeartifact.LoginError) as error:
        codeartifact.login(Config(), DOMAIN)
    assert error.value.expired and error.value.profile == "epic-dev"
    monkeypatch.setattr(awsenv, "cli", lambda: None)
    with pytest.raises(codeartifact.LoginError, match="AWS CLI"):
        codeartifact.login(Config(), DOMAIN)


def test_an_expired_session_logs_in_first_in_the_terminal(app, monkeypatch) -> None:
    aws = Aws(expired=True)
    monkeypatch.setattr(awsenv, "cli", lambda: "/usr/bin/aws")
    monkeypatch.setattr(codeartifact.subprocess, "run", aws)
    monkeypatch.setattr(codeartifact, "authorized", lambda folder, domain: False)
    asked = []

    def sso(profile: str) -> bool:
        asked.append(profile)
        aws.expired = False
        return True

    assert actions.codeartifact_ready(app / "frontend", sso=sso) == ["epic-dev"]
    assert asked == ["epic-dev"]
    aws.expired = True
    with pytest.raises(actions.ActionError, match="aws sso login --profile epic-dev"):  # pdms ui: says how
        actions.codeartifact_ready(app / "frontend")


def test_install_logs_in_before_and_tries_again_after_a_401(app, monkeypatch) -> None:
    root = app
    log = app / "install.log"
    steps = []
    monkeypatch.setattr(actions, "codeartifact_ready",
                        lambda folder, notes=None, sso=None, force=False: steps.append(("login", force)) or [])

    def yarn(cmd, cwd, output):
        steps.append(("yarn",))
        if len([step for step in steps if step == ("yarn",)]) == 1:
            log.write_text("error https://alivi-609837931903.d.codeartifact.us-east-1.amazonaws.com: 401 Unauthorized")
            raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(actions, "_yarn", yarn)
    actions.install_frontend(root, open(log, "a", encoding="utf-8"), log)
    assert steps == [("login", False), ("yarn",), ("login", True), ("yarn",)]
    steps.clear()

    def broken(cmd, cwd, output):
        steps.append(("yarn",))
        log.write_text("error Couldn't find package left-pad")
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(actions, "_yarn", broken)
    with pytest.raises(actions.ActionError, match="yarn install failed"):
        actions.install_frontend(root, open(log, "a", encoding="utf-8"), log)
    assert steps == [("login", False), ("yarn",)]  # not CodeArtifact's fault: no second login
    assert frontend.needs_codeartifact(log) is False
