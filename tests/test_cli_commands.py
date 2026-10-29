"""The interactive commands as a user answers them: pdms setup, pdms repo, pdms config and pdms events."""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from pdms_cli import actions, events, transfer
from pdms_cli.commands import events as events_cmd
from pdms_cli.commands import repos as repos_cmd
from pdms_cli.commands import settings
from pdms_cli.commands import setup as setup_cmd
from pdms_cli.config import Config, Database, DevUser, Stack, config_path


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """An empty home, as the current folder, with no pdms configuration yet."""
    for name, sub in (("HOME", ""), ("USERPROFILE", ""), ("XDG_CONFIG_HOME", ".config"),
                      ("XDG_STATE_HOME", ".local/state"), ("XDG_DATA_HOME", ".local/share")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setenv("PDMS_CONFIG", str(tmp_path / ".config/pdms/config.toml"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def make_repo(path: Path, services: tuple[str, ...] = ("lead/lead-tp-create",)) -> Path:
    (path / "backend" / "snakesdk").mkdir(parents=True)
    for service in services:
        (path / "backend" / service).mkdir(parents=True)
        (path / "backend" / service / "pyproject.toml").write_text('[tool.poetry]\nname = "x"\n')
        (path / "backend" / service / "main.py").write_text("app = None\n")
    return path


def teammate_config() -> Config:
    return Config(
        users={"agent": DevUser("u1", "agent@x.com")},
        dbs={"web-dev": Database("db.example.com", user="pdm", password="secret", protected=True)},
        stacks={"tp": Stack(services=["lead/lead-tp-create"])},
    )


# --------------------------------------------------------------------------- pdms setup


def test_setup_on_a_new_computer_skipping_every_step(home, answers, capsys) -> None:
    answers(
        False,  # a configuration to import?
        "es",  # language
        "",  # folder of the PDMS checkout: skip
        False,  # add a database now?
        "later",  # users
        False,  # review the defaults?
        False,  # pdms doctor now?
    )
    setup_cmd.setup_cmd()
    out = capsys.readouterr().out
    cfg = Config.load()
    assert config_path().exists() and cfg.defaults.language == "es"
    assert "pdms repo add" in out and "pdms db add" in out  # what to do later
    assert not cfg.repos and not cfg.dbs and not cfg.users


def test_setup_imports_a_teammates_configuration_and_completes_it(home, answers, capsys) -> None:
    exported = home / "pdms-config-2026-10-29.toml"
    exported.write_text(transfer.export_document(teammate_config(), list(transfer.SECTIONS), secrets=False))
    repo = make_repo(home / "pdms")
    (repo / "backend" / "lead").mkdir(exist_ok=True)
    answers(
        True,  # a configuration to import? (one was found)
        str(exported),  # which file
        list(transfer.SECTIONS),  # what to import
        True,  # apply it
        "",  # folder of the PDMS checkout (the current folder is not one): skip
        False,  # another database? (web-dev came with the file)
        "s3cret",  # the password of web-dev, which the file leaves out
        False,  # test the connection?
        False,  # pdms doctor now?
    )
    setup_cmd.setup_cmd()
    cfg = Config.load()
    assert cfg.users["agent"].username == "agent@x.com" and cfg.dbs["web-dev"].password == "s3cret"
    assert "tp" in cfg.stacks and not cfg.repos
    assert "Still missing: PDMS repo" in capsys.readouterr().out


def test_setup_takes_the_repo_it_is_run_from(home, answers, monkeypatch) -> None:
    repo = make_repo(home / "pdms")
    monkeypatch.chdir(repo / "backend")
    Config(users={"agent": DevUser("u1", "a@x.com")}, dbs={"local": Database("localhost", password="x")}).save()
    answers(
        False,  # import?
        True,  # use this folder's repo
        "",  # migrations repo: skip
        False,  # another database?
        False,  # test the connection to local?
        False,  # create a stack now?
        False,  # doctor?
    )
    setup_cmd.setup_cmd()
    cfg = Config.load()
    assert cfg.current_repo and cfg.repo.root == repo.resolve()


# --------------------------------------------------------------------------- pdms repo


def test_repos_are_added_listed_switched_edited_and_removed(home, answers, capsys) -> None:
    first, second = make_repo(home / "pdms"), make_repo(home / "pdms-fix")
    repos_cmd.repo_add(first, "main")
    answers(True)  # make the second one the current repo?
    repos_cmd.repo_add(second, "fix")
    cfg = Config.load()
    assert set(cfg.repos) == {"main", "fix"} and cfg.current_repo == "fix"

    repos_cmd.repo_add(second / "backend", None)  # a folder inside a registered repo
    assert "already registered as 'fix'" in capsys.readouterr().out

    repos_cmd.repo_list()
    out = capsys.readouterr().out
    assert "main" in out and "fix" in out

    answers("main")  # which repo becomes the current one
    repos_cmd.repo_use(None)
    assert Config.load().current_repo == "main"

    repos_cmd.repo_edit("fix", None, None, "https://abc.execute-api.us-east-1.amazonaws.com/dev")
    assert Config.load().repos["fix"].remote.endswith("/dev")
    answers("fix", "hotfix", "", "")  # which repo, its new alias, no migrations repo, no remote
    repos_cmd.repo_edit(None, None, None, None)
    assert set(Config.load().repos) == {"main", "hotfix"}

    answers(False)  # delete it? no
    repos_cmd.repo_remove("hotfix")
    answers(True)
    repos_cmd.repo_remove("hotfix")
    assert set(Config.load().repos) == {"main"}


def test_a_repo_that_is_not_one_is_refused(home) -> None:
    with pytest.raises(typer.Exit):
        repos_cmd.repo_add(home, None)


def test_inside_another_repo_pdms_asks_what_to_do(home, answers, monkeypatch) -> None:
    main, other = make_repo(home / "pdms"), make_repo(home / "other")
    cfg = Config()
    actions.add_repo(cfg, main, "main")
    cfg.current_repo = "main"
    cfg.save()
    monkeypatch.chdir(other)
    answers("ignore")
    repos_cmd.check_repo(Config.load())
    assert str(other.resolve()) in Config.load().ignored_repos
    repos_cmd.check_repo(Config.load())  # ignored: nothing asked
    cfg = Config.load()
    cfg.ignored_repos = []
    cfg.save()
    answers("switch")
    repos_cmd.check_repo(Config.load())
    assert Config.load().repo.root == other.resolve()


# --------------------------------------------------------------------------- pdms config


def test_export_then_import_on_another_computer_keeping_what_differs(home, answers, tmp_path, monkeypatch) -> None:
    teammate_config().save()
    file = tmp_path / "shared.toml"
    answers(["users", "dbs"], False)  # what to export; passwords? no
    settings.config_export(file, None, None, False)
    text = file.read_text()
    assert "agent@x.com" in text and 'password = "secret"' not in text and "[stacks" not in text

    Config(users={"agent": DevUser("u9", "me@x.com")}).save()  # this computer: the same user, other values
    answers(..., [], True)  # every section in the file; overwrite none of mine; apply
    settings.config_import(file, None, False, False, False)
    cfg = Config.load()
    assert cfg.users["agent"].username == "me@x.com"  # mine kept
    assert cfg.dbs["web-dev"].host == "db.example.com" and cfg.dbs["web-dev"].password == ""  # new, no password
    assert list(home.glob(".config/pdms/config.toml.bak-*"))  # the previous configuration is kept


def test_export_refuses_to_overwrite_a_file_unless_told(home, answers, tmp_path) -> None:
    teammate_config().save()
    file = tmp_path / "shared.toml"
    file.write_text("old")
    answers(False)  # overwrite it? no
    with pytest.raises(typer.Exit):
        settings.config_export(file, "users", None, False)
    assert file.read_text() == "old"
    settings.config_export(file, "users", False, True)
    assert "agent@x.com" in file.read_text()


def test_unknown_sections_are_refused(home) -> None:
    with pytest.raises(typer.Exit):
        settings.config_export(None, "users,nope", False, False)


def test_defaults_language_and_email(home, answers, capsys) -> None:
    answers(..., ..., "28300", then_defaults=True)  # language, host, a new port; the rest as they are
    settings.config_defaults()
    assert Config.load().defaults.port == 28300
    settings.config_language("es")
    assert Config.load().defaults.language == "es"
    settings.config_language("en")
    with pytest.raises(typer.Exit):
        settings.config_language("xx")
    settings.config_email_to("me@x.com")
    assert Config.load().defaults.email_to == "me@x.com"
    settings.config_email_to(None)
    assert "me@x.com" in capsys.readouterr().out
    settings.config_email_to("")
    assert Config.load().defaults.email_to == ""


# --------------------------------------------------------------------------- pdms events


@pytest.fixture
def event_repo(home, monkeypatch) -> Path:
    from test_events import LOCAL_CONF, MODELS, SETTINGS, TERRAFORM

    root = make_repo(home / "pdms", ("broker/broker-sqs-event", "notification/email-notify"))
    tf = root / "infra/infra_auto/environments/dev"
    tf.mkdir(parents=True)
    for name, text in TERRAFORM.items():
        (tf / name).write_text(text)
    event = root / "backend/common/event/event"
    (event / "domain").mkdir(parents=True)
    (event / "settings.py").write_text(SETTINGS)
    (event / "domain/models.py").write_text(MODELS)
    (root / "infra/local_sqs").mkdir(parents=True)
    (root / "infra/local_sqs/elasticmq.conf").write_text(LOCAL_CONF)
    cfg = Config()
    actions.add_repo(cfg, root, "pdms")
    cfg.current_repo = "pdms"
    cfg.save()
    return root


def test_events_map_lists_each_event_type_with_its_queue_and_consumer(event_repo, capsys) -> None:
    events_cmd.events_map("", "dev")
    out = capsys.readouterr().out
    assert "email-send-sqs-queue.fifo" in out and "broker:" in out
    events_cmd.events_map("no-such-queue", "dev")
    assert "email-send-sqs-queue.fifo" not in capsys.readouterr().out


def test_events_up_starts_elasticmq_and_says_why_the_broker_does_not(event_repo, monkeypatch, capsys) -> None:
    started = []
    monkeypatch.setattr(actions, "start_events", lambda cfg, queues: started.append(sorted(queues)) or "created")
    events_cmd.events_up("dev", True)
    out = capsys.readouterr().out
    assert started and "ElasticMQ started" in out
    assert "Configure a user and a database to run the broker" in out  # none configured yet


def test_events_status_and_down_without_elasticmq(event_repo, monkeypatch, capsys) -> None:
    monkeypatch.setattr(events, "container_state", lambda: None)
    monkeypatch.setattr(events, "stop", lambda: False)
    events_cmd.events_status(False)
    assert "ElasticMQ is not running" in capsys.readouterr().out
    events_cmd.events_down()
    assert "ElasticMQ is not running" in capsys.readouterr().out


def test_events_status_counts_the_messages_of_each_queue(event_repo, monkeypatch, capsys) -> None:
    monkeypatch.setattr(events, "container_state", lambda: {"running": True})
    monkeypatch.setattr(events, "is_up", lambda port: True)
    monkeypatch.setattr(events, "queue_counts", lambda port: {
        "email-send-sqs-queue.fifo": {"visible": 3, "in_flight": 1}, "idle.fifo": {"visible": 0, "in_flight": 0},
        events.SNS_QUEUE: {"visible": 2, "in_flight": 0}})
    events_cmd.events_status(False)
    out = capsys.readouterr().out
    assert "email-send-sqs-queue.fifo" in out and "idle.fifo" not in out and events.SNS_QUEUE in out
    events_cmd.events_status(True)
    assert "idle.fifo" in capsys.readouterr().out
