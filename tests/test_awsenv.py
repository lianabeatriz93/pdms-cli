"""The AWS of dev for the services: profiles of the AWS config, the Lambdas' buckets (only those kept), services
paired with their Lambda through the dev Terraform, what a service gets when it starts, and the checks of Doctor,
pdms aws and pdms ui."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from pdms_cli import actions, awsenv, cli, doctor, instances
from pdms_cli.config import Config, Defaults, Repo
from pdms_cli.ui import aws as ui_aws

REAL_AWS = awsenv._aws  # tests/conftest.py replaces it in every test

LAMBDA_TF = '''
module "lambda_{module}" {{
  source          = "../../../modules/lambda_new"
  function_name   = "{function}"
  handler         = "main.handler"
  lambda_path     = "../../../../backend/{service}/"
  environment_variables = {{
    LOGGING_LEVEL = "DEBUG"
  }}
}}
'''


def lambda_listing(functions: dict[str, dict[str, str]]) -> str:
    return json.dumps({"Functions": [{"FunctionName": name, "Environment": {"Variables": env}}
                                     for name, env in functions.items()]})


class FakeAws:
    """The AWS CLI's answers: who the profile is (or that its session is over) and the Lambdas."""

    def __init__(self, functions: dict[str, dict[str, str]], account: str = "111122223333") -> None:
        self.functions = functions
        self.account = account
        self.expired = False
        self.calls: list[list[str]] = []

    def __call__(self, profile: str, args: list[str], timeout: float) -> str:
        self.calls.append(args)
        if self.expired:
            raise awsenv.AwsError("Error when retrieving token from sso: Token has expired and refresh failed",
                                  expired=True)
        if args[:2] == ["sts", "get-caller-identity"]:
            return json.dumps({"Account": self.account, "Arn": "arn:aws:sts::x:assumed-role/Admin/me"})
        if args[:2] == ["lambda", "list-functions"]:
            return lambda_listing(self.functions)
        raise AssertionError(args)


@pytest.fixture
def aws_config(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "aws-config"
    path.write_text(
        "[sso-session default]\nsso_region = us-east-1\n\n"
        "[profile pdm-dev]\nsso_session = default\nregion = us-east-1\n\n"
        "[profile pdm-qa]\nregion = us-west-2\n\n[default]\nregion = eu-west-1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AWS_CONFIG_FILE", str(path))
    monkeypatch.setattr(awsenv, "cli", lambda: "/usr/bin/aws")
    return path


@pytest.fixture
def fake(monkeypatch) -> FakeAws:
    fake = FakeAws({
        "lead-sp-export": {"DOWNLOABLE_FILES_BUCKET_S3": "downloads", "DB_PASSWORD": "secret",
                           "DOWNLOABLE_FILES_BUCKET_REGION_S3": "us-east-1"},
        "lead-sp-export-ev": {"DOWNLOABLE_FILES_BUCKET_S3": "other"},
        "report-download": {"DOWNLOABLE_FILES_BUCKET_S3": "reports", "API_KEY": "secret"},
        "auth-profile-get": {"LEAD_CHANGES_FILES_BUCKET_S3": "history"},
        "util-plain": {"LOGGING_LEVEL": "INFO"},
    })
    monkeypatch.setattr(awsenv, "_aws", fake)
    return fake


@pytest.fixture
def repo(tmp_path) -> Path:
    """A repo with four services and the dev Terraform of their Lambdas (profile-get is auth-profile-get there)."""
    root = tmp_path / "pdms"
    for service in ("lead/lead-sp-export", "report/report-download", "auth/profile-get", "util/util-plain",
                    "util/util-local-only"):
        folder = root / "backend" / service
        folder.mkdir(parents=True)
        (folder / "pyproject.toml").write_text("[tool.poetry]\n", encoding="utf-8")
        (folder / "main.py").write_text("app = None\n", encoding="utf-8")
    tf = root / "infra" / "infra_auto" / "environments" / "dev"
    tf.mkdir(parents=True)
    (tf / "lambdas.tf").write_text("".join(LAMBDA_TF.format(module=m, function=f, service=s) for m, f, s in (
        ("a", "lead-sp-export", "lead/lead-sp-export"), ("b", "lead-sp-export-ev", "lead/lead-sp-export"),
        ("c", "auth-profile-get", "auth/profile-get"), ("d", "report-download", "report/report-download"),
    )), encoding="utf-8")
    return root


@pytest.fixture
def cfg(repo, monkeypatch) -> Config:
    cfg = Config(defaults=Defaults(aws_profile="pdm-dev"), repos={"pdms": Repo(path=str(repo))}, current_repo="pdms")
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(Config, "save", lambda self: None)
    return cfg


def service(repo: Path, path: str) -> Path:
    return repo / "backend" / path


# --------------------------------------------------------------------------- profiles and session


def test_profiles_come_from_the_aws_config_without_sso_sessions(aws_config) -> None:
    assert awsenv.profiles() == ["pdm-dev", "pdm-qa", "default"]
    assert (awsenv.region_of("pdm-qa"), awsenv.region_of("default")) == ("us-west-2", "eu-west-1")
    assert awsenv.region_of("missing") == awsenv.DEFAULT_REGION


def test_no_aws_config_means_no_profiles(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "nothing"))
    assert awsenv.profiles() == []


def test_an_expired_sso_session_is_told_apart(aws_config, fake) -> None:
    assert awsenv.session("pdm-dev") == awsenv.Session("ok", account="111122223333")
    fake.expired = True
    assert awsenv.session("pdm-dev").state == "expired"


def test_the_cli_says_when_the_session_is_over(aws_config, monkeypatch) -> None:
    def run(cmd, **kwargs):
        assert cmd[-6:] == ["--profile", "pdm-dev", "--region", "us-east-1", "--output", "json"]
        return SimpleNamespace(returncode=255, stdout="", stderr="\nError when retrieving token from sso: Token has "
                                                                  "expired and refresh failed\n")

    monkeypatch.setattr(awsenv, "_aws", REAL_AWS)  # the CLI's own call, with subprocess faked
    monkeypatch.setattr(awsenv.subprocess, "run", run)
    who = awsenv.session("pdm-dev")
    assert who.state == "expired" and "Token has expired" in who.detail


# --------------------------------------------------------------------------- the Lambdas' buckets


def test_only_bucket_variables_are_kept(aws_config, fake) -> None:
    functions = awsenv.read_functions("pdm-dev")
    assert functions["lead-sp-export"] == {"DOWNLOABLE_FILES_BUCKET_REGION_S3": "us-east-1",
                                           "DOWNLOABLE_FILES_BUCKET_S3": "downloads"}
    assert functions["util-plain"] == {}
    assert "secret" not in json.dumps(functions)


def test_refresh_saves_the_buckets_and_tells_what_changed(aws_config, fake) -> None:
    assert awsenv.refresh("pdm-dev") == []  # the first read has nothing to compare with
    saved = awsenv.load()
    assert (saved["profile"], saved["account"], saved["region"]) == ("pdm-dev", "111122223333", "us-east-1")
    assert "secret" not in awsenv.state_path().read_text(encoding="utf-8")
    if os.name != "nt":
        assert awsenv.state_path().stat().st_mode & 0o777 == 0o600
    fake.functions["report-download"]["DOWNLOABLE_FILES_BUCKET_S3"] = "reports-2"
    changes = awsenv.refresh("pdm-dev")
    assert changes == [awsenv.Change("report-download", "DOWNLOABLE_FILES_BUCKET_S3", "reports", "reports-2")]
    assert awsenv.load()["changes"] == [vars(changes[0])]
    assert awsenv.refresh("pdm-dev") == [] and awsenv.load()["changes"]  # the last changes are kept until new ones
    fake.account = "999988887777"  # another account: nothing to compare with
    assert awsenv.refresh("pdm-dev") == [] and awsenv.load()["changes"] == []


def test_refresh_needs_a_working_session(aws_config, fake) -> None:
    fake.expired = True
    with pytest.raises(awsenv.AwsError) as error:
        awsenv.refresh("pdm-dev")
    assert error.value.expired and not awsenv.state_path().exists()


def test_a_read_is_due_once_a_day_or_for_another_profile() -> None:
    fresh = {"profile": "pdm-dev", "checked_at": awsenv.now(), "kept": awsenv.KEPT.pattern}
    assert not awsenv.due(fresh, "pdm-dev")
    assert awsenv.due(fresh, "pdm-qa") and awsenv.due({}, "pdm-dev")
    assert awsenv.due({**fresh, "checked_at": "2020-01-01T00:00:00+00:00"}, "pdm-dev")


# --------------------------------------------------------------------------- services ↔ Lambdas


def test_the_terraform_names_each_services_lambdas(repo) -> None:
    assert awsenv.terraform_names(repo) == {
        "lead/lead-sp-export": ["lead-sp-export", "lead-sp-export-ev"], "auth/profile-get": ["auth-profile-get"],
        "report/report-download": ["report-download"],
    }


def test_services_pair_with_their_folders_lambda_else_the_terraforms_never_a_guess(repo) -> None:
    found = awsenv.match(["lead/lead-sp-export", "auth/profile-get", "util/util-plain", "util/util-plan"],
                         ["lead-sp-export", "lead-sp-export-ev", "auth-profile-get", "util-plain"],
                         awsenv.terraform_names(repo))
    assert found.same == {"lead/lead-sp-export": "lead-sp-export", "util/util-plain": "util-plain"}
    assert found.named == {"auth/profile-get": "auth-profile-get"}
    assert found.none == ["util/util-plan"]  # one letter away from util-plain, and still no buckets of another


def test_a_service_gets_the_profile_and_its_lambdas_buckets(aws_config, fake, cfg, repo) -> None:
    awsenv.refresh("pdm-dev")
    assert awsenv.env_for(cfg, service(repo, "lead/lead-sp-export")) == {
        "AWS_PROFILE": "pdm-dev", "DOWNLOABLE_FILES_BUCKET_REGION_S3": "us-east-1",
        "DOWNLOABLE_FILES_BUCKET_S3": "downloads"}
    assert awsenv.env_for(cfg, service(repo, "auth/profile-get")) == {
        "AWS_PROFILE": "pdm-dev", "LEAD_CHANGES_FILES_BUCKET_S3": "history"}
    assert awsenv.env_for(cfg, service(repo, "util/util-local-only")) == {"AWS_PROFILE": "pdm-dev"}


def test_the_services_own_env_and_the_defaults_keep_their_values(aws_config, fake, cfg, repo) -> None:
    awsenv.refresh("pdm-dev")
    (service(repo, "lead/lead-sp-export") / ".env").write_text(
        '# local\nexport DOWNLOABLE_FILES_BUCKET_S3 = "mine"\n', encoding="utf-8")
    cfg.defaults.env = {"DOWNLOABLE_FILES_BUCKET_REGION_S3": "us-east-2", "AWS_PROFILE": "other"}
    assert awsenv.env_for(cfg, service(repo, "lead/lead-sp-export")) == {}
    cfg.defaults.env = {}
    assert awsenv.env_for(cfg, service(repo, "lead/lead-sp-export")) == {
        "AWS_PROFILE": "pdm-dev", "DOWNLOABLE_FILES_BUCKET_REGION_S3": "us-east-1"}


def test_without_a_profile_or_with_another_one_nothing_read_applies(aws_config, fake, cfg, repo) -> None:
    awsenv.refresh("pdm-dev")
    cfg.defaults.aws_profile = "pdm-qa"
    assert awsenv.env_for(cfg, service(repo, "report/report-download")) == {"AWS_PROFILE": "pdm-qa"}
    cfg.defaults.aws_profile = ""
    assert awsenv.env_for(cfg, service(repo, "report/report-download")) == {}


def test_services_started_by_pdms_get_it(aws_config, fake, cfg, repo) -> None:
    awsenv.refresh("pdm-dev")
    launch = SimpleNamespace(service=service(repo, "report/report-download"), events=SimpleNamespace(env={}, kind="aws"),
                             is_consumer=False, parallel=False, user=SimpleNamespace(env=lambda: {}),
                             db=SimpleNamespace(url=lambda: "postgresql://x"))
    env = actions.service_env(cfg, launch)
    assert env["AWS_PROFILE"] == "pdm-dev" and env["DOWNLOABLE_FILES_BUCKET_S3"] == "reports"


def test_running_services_that_started_before_a_change_must_restart(aws_config, fake, cfg, repo, monkeypatch) -> None:
    awsenv.refresh("pdm-dev")
    fake.functions["report-download"]["DOWNLOABLE_FILES_BUCKET_S3"] = "reports-2"
    awsenv.refresh("pdm-dev")
    changed_at = awsenv.load()["changed_at"]
    before = SimpleNamespace(key="report-download@28100", service=str(service(repo, "report/report-download")),
                             started_at="2020-01-01T10:00:00", alive=lambda: True)
    after = SimpleNamespace(key="report-download@28101", service=before.service, started_at=changed_at[:19],
                            alive=lambda: True)
    other = SimpleNamespace(key="profile-get@28102", service=str(service(repo, "auth/profile-get")),
                            started_at="2020-01-01T10:00:00", alive=lambda: True)
    monkeypatch.setattr(instances, "load", lambda: {i.key: i for i in (before, after, other)})
    found = awsenv.summary(cfg)
    assert found["restart"] == ["report-download@28100"]
    assert (found["functions"], found["services"], found["with_buckets"]) == (5, 5, 3)
    assert found["named"] == {"auth/profile-get": "auth-profile-get"}
    assert found["buckets"] == ["downloads", "history", "other", "reports-2"]


# --------------------------------------------------------------------------- actions, CLI, Doctor, pdms ui


def test_only_a_profile_of_the_aws_config_can_be_chosen(aws_config, cfg) -> None:
    actions.set_aws_profile(cfg, "pdm-qa")
    assert cfg.defaults.aws_profile == "pdm-qa"
    with pytest.raises(actions.ActionError, match="Unknown AWS profile 'nope'"):
        actions.set_aws_profile(cfg, "nope")
    actions.set_aws_profile(cfg, "")
    assert cfg.defaults.aws_profile == ""


def test_reading_with_an_expired_session_says_how_to_log_in(aws_config, fake, cfg) -> None:
    fake.expired = True
    with pytest.raises(actions.ActionError, match="pdms aws login"):
        actions.read_aws(cfg)


def test_the_profile_survives_the_config_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PDMS_CONFIG", str(tmp_path / "config.toml"))
    Config(defaults=Defaults(aws_profile="pdm-dev")).save()
    assert Config.load().defaults.aws_profile == "pdm-dev"


def test_pdms_aws_profile_reads_the_lambdas(aws_config, fake, cfg, monkeypatch) -> None:
    monkeypatch.setenv("PDMS_NO_UPDATE_CHECK", "1")
    cfg.defaults.aws_profile = ""
    result = CliRunner().invoke(cli.app, ["aws", "profile", "pdm-dev"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert cfg.defaults.aws_profile == "pdm-dev"
    assert "5 Lambdas read; 3 services of the repo get their buckets." in result.output
    status = CliRunner().invoke(cli.app, ["aws", "status"], env={"COLUMNS": "200"})
    assert "pdm-dev · us-east-1 · account 111122223333" in status.output
    assert "auth/profile-get is the Lambda auth-profile-get" in status.output
    result = CliRunner().invoke(cli.app, ["aws", "profile", "nope"], env={"COLUMNS": "200"})
    assert result.exit_code == 1 and "Unknown AWS profile" in result.output


def test_doctor_without_profiles_is_fine_and_with_an_expired_session_says_so(aws_config, fake, cfg, monkeypatch) -> None:
    monkeypatch.setenv("AWS_CONFIG_FILE", str(aws_config.parent / "none"))
    cfg.defaults.aws_profile = ""
    assert [c.status for c in doctor.check_aws(cfg)] == ["ok"]
    monkeypatch.setenv("AWS_CONFIG_FILE", str(aws_config))
    assert [(c.status, c.fix) for c in doctor.check_aws(cfg)] == [("warn", "aws")]
    cfg.defaults.aws_profile = "pdm-dev"
    awsenv.refresh("pdm-dev")
    checks = doctor.check_aws(cfg)
    assert [c.status for c in checks] == ["ok", "ok"] and "account 111122223333" in checks[0].detail
    fake.expired = True
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA")
    checks = doctor.check_aws(cfg)
    assert [(c.name, c.status) for c in checks] == [("Profile", "warn"), ("Buckets", "ok"), ("AWS_ACCESS_KEY_ID", "warn")]


def test_pdms_ui_checks_the_session_and_reads_when_due(aws_config, fake, cfg) -> None:
    monitor = ui_aws.Aws()
    monitor.check()
    found = monitor.summary()
    assert found["session"]["state"] == "ok" and found["read_at"] and found["with_buckets"] == 3
    reads = sum(1 for call in fake.calls if call[0] == "lambda")
    monitor.check()  # read today: only the session again
    assert sum(1 for call in fake.calls if call[0] == "lambda") == reads
    fake.expired = True
    monitor.check()
    assert monitor.summary()["session"]["state"] == "expired"


def test_pdms_ui_takes_the_code_aws_sso_login_prints(tmp_path) -> None:
    monitor = ui_aws.Aws()
    monitor._login["running"] = True
    log = tmp_path / "aws.log"
    log.write_text("If the browser does not open, open the following URL:\n\nhttps://device.sso.us-east-1.amazonaws.com/"
                   "\n\nThen enter the code:\n\nABCD-EFGH\n", encoding="utf-8")
    monitor._found_in(log)
    assert monitor.summary()["login"] == {"running": True, "url": "https://device.sso.us-east-1.amazonaws.com/",
                                          "code": "ABCD-EFGH", "error": ""}


# --------------------------------------------------------------------------- credentials from the AWS CLI


def test_services_get_the_profiles_credentials_from_the_aws_cli(aws_config, cfg) -> None:
    env = awsenv.credentials_env(cfg)
    text = Path(env["AWS_CONFIG_FILE"]).read_text(encoding="utf-8")
    assert "[profile pdm-dev]" in text and "-m pdms_cli.awscreds pdm-dev" in text
    assert str(awsenv.config_file()) in text  # the CLI reads the user's own config, not this one
    assert awsenv.credentials_env(cfg) == env  # written again only when it changes


@pytest.mark.parametrize("change", [{"aws_profile": ""}, {"env": {"AWS_CONFIG_FILE": "/mine"}}, {"cli": None}])
def test_no_credentials_config_without_a_profile_the_cli_or_with_the_users_own(aws_config, cfg, monkeypatch, change):
    if "cli" in change:
        monkeypatch.setattr(awsenv, "cli", lambda: None)
    if "aws_profile" in change:
        cfg.defaults.aws_profile = ""
    if "env" in change:
        cfg.defaults.env = change["env"]
    assert awsenv.credentials_env(cfg) == {}


def test_the_credential_process_asks_the_cli_with_the_users_config(monkeypatch, tmp_path) -> None:
    from pdms_cli import awscreds

    seen = {}
    monkeypatch.setattr(awscreds.shutil, "which", lambda name: "/usr/bin/aws")
    monkeypatch.setenv("AWS_PROFILE", "pdm-dev")
    monkeypatch.setattr(awscreds.subprocess, "run",
                        lambda cmd, env, stdin: seen.update(cmd=cmd, env=env) or SimpleNamespace(returncode=0))
    assert awscreds.main(["pdm-dev", str(tmp_path / "config")]) == 0
    assert seen["cmd"][1:] == ["configure", "export-credentials", "--profile", "pdm-dev", "--format", "process"]
    assert seen["env"]["AWS_CONFIG_FILE"] == str(tmp_path / "config") and "AWS_PROFILE" not in seen["env"]
