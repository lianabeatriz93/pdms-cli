"""Service actions: no prompts, decisions come back as exceptions."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pdms_cli import actions, events
from pdms_cli.config import Config, Database, Defaults, DevUser, Stack
from pdms_cli.instances import Instance


@pytest.fixture
def cfg(monkeypatch) -> Config:
    monkeypatch.setattr(Config, "save", lambda self: None)
    return Config(
        users={"supervisor": DevUser("u1", "s@x.com"), "agent": DevUser("u2", "a@x.com")},
        dbs={"local": Database("localhost"), "shared": Database("db.example.com", protected=True)},
    )


@pytest.fixture
def ports(monkeypatch):
    """Ports in use on the machine (``busy``) and held by background instances (``taken``)."""
    state = {"busy": set(), "taken": set()}
    monkeypatch.setattr(actions.runner, "port_is_free", lambda host, port: port not in state["busy"])
    monkeypatch.setattr(actions.instances, "running_ports", lambda: set(state["taken"]))
    return state


@pytest.fixture
def http_service(monkeypatch, tmp_path) -> Path:
    monkeypatch.setattr(actions, "consumer_of", lambda cfg, service: None)
    monkeypatch.setattr(actions.events, "running", lambda port: False)
    return tmp_path / "lead-tp-list"


def test_free_port_keeps_a_free_port_and_hands_back_the_next_one(ports) -> None:
    assert actions.free_port("0.0.0.0", 8080) == 8080
    ports["busy"].add(8080)
    ports["taken"].add(8081)  # a background instance still booting
    with pytest.raises(actions.PortBusy) as busy:
        actions.free_port("0.0.0.0", 8080)
    assert (busy.value.port, busy.value.free) == (8080, 8082)


def test_check_database_asks_only_for_protected_ones(cfg) -> None:
    actions.check_database(cfg, "local", confirmed=False)
    actions.check_database(cfg, "shared", confirmed=True)
    with pytest.raises(actions.ProtectedDatabase) as asked:
        actions.check_database(cfg, "shared", confirmed=False)
    assert asked.value.name == "shared"


def test_events_setup_decides_without_asking(cfg, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(actions.events, "running", lambda port: False)
    with pytest.raises(actions.ActionError):
        actions.events_setup(cfg, tmp_path, "sqs")
    assert actions.events_setup(cfg, tmp_path, "auto").kind == "aws"
    with pytest.raises(actions.LocalEventsDown):
        actions.events_setup(cfg, tmp_path, "local")


def test_plan_service_for_an_api(cfg, ports, http_service) -> None:
    launch = actions.plan_service(cfg, http_service, user_name="supervisor", db_name="local", port=8081)
    assert (launch.port, launch.host, launch.reload, launch.queue) == (8081, "0.0.0.0", True, "")
    assert launch.cmd[-4:] == ["--host", "0.0.0.0", "--port", "8081"]
    assert launch.events.kind == "aws"


def test_plan_service_rejects_unknown_names_and_busy_ports(cfg, ports, http_service) -> None:
    with pytest.raises(actions.ActionError) as error:
        actions.plan_service(cfg, http_service, user_name="nobody", db_name="local")
    assert "nobody" in error.value.message
    ports["busy"].add(8080)
    with pytest.raises(actions.PortBusy):
        actions.plan_service(cfg, http_service, user_name="supervisor", db_name="local")


def test_plan_service_for_an_sqs_consumer(cfg, ports, monkeypatch, tmp_path) -> None:
    queue, handler = events.Queue("broker-sqs-queue.fifo"), events.Consumer("broker/broker-sqs-event", "main.handler")
    monkeypatch.setattr(actions, "consumer_of", lambda cfg, service: (queue, handler))
    modes = []
    monkeypatch.setattr(actions, "events_setup",
                        lambda cfg, service, mode: modes.append(mode) or actions.EventsSetup({}, "local", "local"))
    ports["busy"].add(8080)  # consumers do not listen, so a busy default port does not matter
    launch = actions.plan_service(cfg, tmp_path, user_name="supervisor", db_name="local")
    assert (launch.port, launch.queue, modes) == (0, "broker-sqs-queue.fifo", ["local"])
    assert launch.is_consumer


def test_start_service_installs_first_and_records_the_instance(cfg, ports, http_service, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(actions, "installed_parts", lambda service: {"(service)": "abc"})
    monkeypatch.setattr(actions.instances, "start", lambda service, cmd, env, **kw: calls.append(("start", kw)) or SimpleNamespace(**kw))
    launch = actions.plan_service(cfg, http_service, user_name="agent", db_name="local", port=8090)
    started = actions.start_service(cfg, launch, install=lambda service: calls.append(("install", service)))
    assert [c[0] for c in calls] == ["install", "start"]
    assert (started.port, started.user, started.db, started.deps) == (8090, "agent", "local", {"(service)": "abc"})


def _instance(tmp_path: Path, **kw) -> Instance:
    return Instance(key="lead-tp-list@8081", pid=1, service=str(tmp_path / "lead-tp-list"), host="0.0.0.0",
                    port=8081, user="supervisor", db="local", reload=True, log="x.log", started_at="", **kw)


def test_restart_asks_for_a_protected_database_before_stopping(cfg, ports, http_service, monkeypatch,
                                                               tmp_path) -> None:
    stopped = []
    monkeypatch.setattr(actions.instances, "stop", stopped.append)
    with pytest.raises(actions.ProtectedDatabase):
        actions.restart_service(cfg, _instance(tmp_path), db_name="shared")
    assert not stopped  # a refused confirmation leaves the instance running


def test_restart_keeps_port_user_and_database(cfg, ports, http_service, monkeypatch, tmp_path) -> None:
    order = []
    monkeypatch.setattr(actions.events, "running", lambda port: True)  # it published to the local ElasticMQ
    monkeypatch.setattr(actions.instances, "stop", lambda inst: order.append("stop"))
    monkeypatch.setattr(actions, "installed_parts", lambda service: None)
    monkeypatch.setattr(actions.instances, "start",
                        lambda service, cmd, env, **kw: order.append("start") or SimpleNamespace(**kw))
    started = actions.restart_service(cfg, _instance(tmp_path, events="local"), db_name="shared", confirmed=True)
    assert order == ["stop", "start"]
    assert (started.port, started.user, started.db, started.events) == (8081, "supervisor", "shared", "local")


# --------------------------------------------------------------------------- stacks


@pytest.fixture
def backend(tmp_path, monkeypatch) -> Path:
    """A backend with four services; ``broker/sqs`` is an SQS consumer."""
    for name in ("lead/a", "lead/b", "lead/c", "broker/sqs"):
        (tmp_path / name).mkdir(parents=True)
    monkeypatch.setattr(actions.runner, "is_service", lambda path: path.is_dir())
    queue, handler = events.Queue("broker-sqs-queue.fifo"), events.Consumer("broker/sqs", "main.handler")
    monkeypatch.setattr(actions, "consumer_of",
                        lambda cfg, service: (queue, handler) if service.name == "sqs" else None)
    return tmp_path


def _live(monkeypatch, *items: tuple[Path, int]) -> None:
    live = [Instance(key=f"{p.name}@{port}", pid=1, service=str(p), host="0.0.0.0", port=port, user="supervisor",
                     db="local", reload=True, log="x.log", started_at="") for p, port in items]
    monkeypatch.setattr(actions.instances, "load", lambda: {i.key: i for i in live})
    monkeypatch.setattr(Instance, "alive", lambda self: True)


def test_plan_stack_skips_running_services_and_gives_each_a_free_port(cfg, ports, backend, monkeypatch) -> None:
    cfg.stacks["prospecting"] = Stack(services=["lead/a", "lead/b", "lead/c"])
    _live(monkeypatch, (backend / "lead/b", 8080))
    ports["taken"].add(8080)
    ports["busy"].add(8081)  # DynamoDB or anything else outside pdms
    monkeypatch.setattr(actions.events, "running", lambda port: False)
    plan = actions.plan_stack(cfg, "prospecting", backend, user_name="supervisor", db_name="local")
    assert plan.running == {backend / "lead/b": [8080]}
    assert [(s.service.name, s.port) for s in plan.services] == [("a", 8082), ("c", 8083)]
    assert plan.events is not None and plan.events.kind == "aws"


def test_plan_stack_with_a_consumer_publishes_locally(cfg, ports, backend, monkeypatch) -> None:
    cfg.stacks["broker"] = Stack(services=["broker/sqs", "lead/a"])
    _live(monkeypatch)
    monkeypatch.setattr(actions.events, "running", lambda port: False)
    with pytest.raises(actions.LocalEventsDown):
        actions.plan_stack(cfg, "broker", backend, user_name="supervisor", db_name="local")
    monkeypatch.setattr(actions.events, "running", lambda port: True)
    plan = actions.plan_stack(cfg, "broker", backend, user_name="supervisor", db_name="local")
    assert [(s.service.name, s.port, s.events.kind) for s in plan.services] == [("sqs", 0, "local"),
                                                                               ("a", 8080, "local")]


def test_plan_stack_when_everything_runs_and_when_the_stack_is_stale(cfg, ports, backend, monkeypatch) -> None:
    cfg.stacks["one"] = Stack(services=["lead/a"])
    _live(monkeypatch, (backend / "lead/a", 8080))
    plan = actions.plan_stack(cfg, "one", backend, user_name="supervisor", db_name="local")
    assert (plan.services, plan.events) == ([], None)
    cfg.stacks["stale"] = Stack(services=["lead/gone"])
    with pytest.raises(actions.ActionError) as error:
        actions.plan_stack(cfg, "stale", backend, user_name="supervisor", db_name="local")
    assert "lead/gone" in error.value.message


def test_stack_instances_lists_only_its_live_services(cfg, backend, monkeypatch) -> None:
    cfg.stacks["prospecting"] = Stack(services=["lead/a", "lead/b"])
    _live(monkeypatch, (backend / "lead/a", 8080), (backend / "lead/c", 8081))
    assert [i.key for i in actions.stack_instances(cfg, "prospecting", backend)] == ["a@8080"]


def test_save_and_remove_stack_validate_first(cfg) -> None:
    with pytest.raises(actions.ActionError):
        actions.save_stack(cfg, "empty", Stack())
    with pytest.raises(actions.ActionError):
        actions.save_stack(cfg, "bad", Stack(services=["lead/a"], user="nobody"))
    actions.save_stack(cfg, "ok", Stack(services=["lead/a"], user="agent", db="local"))
    assert cfg.stacks["ok"].user == "agent"
    actions.remove_stack(cfg, "ok")
    assert "ok" not in cfg.stacks
    with pytest.raises(actions.ActionError):
        actions.remove_stack(cfg, "ok")


# --------------------------------------------------------------------------- databases, users and defaults


@pytest.mark.parametrize("name, reason", [
    ("  ", "Required field"), ("local", "That name already exists"), ("a b", "Use only letters, numbers, '-' or '_'"),
])
def test_check_alias_says_what_is_wrong(name, reason) -> None:
    with pytest.raises(actions.InvalidValue) as invalid:
        actions.check_alias(name, ["local"])
    assert (invalid.value.field, invalid.value.reason) == ("name", reason)
    assert actions.check_alias(" new-db_2 ", ["local"]) == "new-db_2"


def test_save_db_checks_every_field_and_the_alias(cfg) -> None:
    with pytest.raises(actions.InvalidValue) as invalid:
        actions.save_db(cfg, "local", Database("localhost"), new=True)
    assert invalid.value.field == "name"
    with pytest.raises(actions.ActionError):
        actions.save_db(cfg, "nope", Database("localhost", user="pdm"))  # editing one that does not exist
    for db, field in [
        (Database(" ", user="pdm"), "host"), (Database("h", user=""), "user"), (Database("h", database="", user="pdm"),
         "database"), (Database("h", port=70000, user="pdm"), "port"), (Database("h", port="x", user="pdm"), "port"), (Database("h", port=True, user="pdm"), "port"),
    ]:
        with pytest.raises(actions.InvalidValue) as invalid:
            actions.save_db(cfg, "new", db, new=True)
        assert invalid.value.field == field
    assert actions.save_db(cfg, " new ", Database(" h ", port="5433", user=" pdm ", password=" s3cret "), new=True) == "new"
    assert cfg.dbs["new"] == Database("h", port=5433, user="pdm", password=" s3cret ")


def test_remove_db_and_user_let_the_stacks_ask_again(cfg) -> None:
    cfg.stacks = {"lead": Stack(["lead/a"], user="agent", db="local"), "other": Stack(["lead/b"], db="shared")}
    cfg.last_db, cfg.last_user = "local", "agent"
    assert actions.remove_db(cfg, "local") == ["lead"]
    assert (cfg.stacks["lead"].db, cfg.stacks["other"].db, cfg.last_db) == ("", "shared", "")
    assert actions.remove_user(cfg, "agent") == ["lead"]
    assert (cfg.stacks["lead"].user, cfg.last_user) == ("", "")
    assert "local" not in cfg.dbs and "agent" not in cfg.users
    with pytest.raises(actions.ActionError):
        actions.remove_user(cfg, "agent")


def test_check_connection_gives_the_version_or_the_driver_error(monkeypatch) -> None:
    monkeypatch.setattr(actions.runner, "test_connection", lambda db, timeout: "PostgreSQL 16.4, compiled by gcc")
    assert actions.check_connection(Database("h"), 5) == "PostgreSQL 16.4"

    def refuse(db, timeout):
        raise OSError("connection refused\n")

    monkeypatch.setattr(actions.runner, "test_connection", refuse)
    with pytest.raises(actions.ActionError, match="^connection refused$"):
        actions.check_connection(Database("h"), 5)


def test_save_user_needs_its_id_and_username(cfg) -> None:
    with pytest.raises(actions.InvalidValue) as invalid:
        actions.save_user(cfg, "new", DevUser(" ", "n@x.com"), new=True)
    assert invalid.value.field == "user_id"
    with pytest.raises(actions.InvalidValue) as invalid:
        actions.save_user(cfg, "new", DevUser("u3", ""), new=True)
    assert invalid.value.field == "username"
    actions.save_user(cfg, "agent", DevUser(" u2 ", "a@x.com", roles=" TPR.Agent "))
    assert cfg.users["agent"] == DevUser("u2", "a@x.com", roles="TPR.Agent")


def test_read_db_users_reports_the_driver_error(cfg, monkeypatch) -> None:
    def fetch(db, **kwargs):
        raise OSError("timeout")

    monkeypatch.setattr(actions.userimport, "fetch_users", fetch)
    with pytest.raises(actions.ActionError, match="Could not read the users from local: timeout"):
        actions.read_db_users(cfg, "local", {})
    with pytest.raises(actions.ActionError):
        actions.read_db_users(cfg, "nope", {})


@pytest.mark.parametrize("change, field", [
    ({"language": "fr"}, "language"), ({"host": ""}, "host"), ({"port": 0}, "port"),
    ({"logging_level": "TRACE"}, "logging_level"), ({"events": "sqs"}, "events"), ({"events_port": "x"}, "events_port"),
    ({"db_timeout": 0}, "db_timeout"), ({"env": {"BAD-NAME": "1"}}, "env"),
])
def test_save_defaults_rejects_bad_values(cfg, change, field) -> None:
    with pytest.raises(actions.InvalidValue) as invalid:
        actions.save_defaults(cfg, Defaults(**change))
    assert invalid.value.field == field
    assert cfg.defaults == Defaults()


def test_save_defaults_and_set_language(cfg) -> None:
    actions.save_defaults(cfg, Defaults(language="es", port="9000", env={"FEATURE_X": "on"}))
    assert (cfg.defaults.language, cfg.defaults.port, cfg.defaults.env) == ("es", 9000, {"FEATURE_X": "on"})
    actions.set_language(cfg, "en")
    assert cfg.defaults.language == "en"
    with pytest.raises(actions.ActionError):
        actions.set_language(cfg, "fr")


# --------------------------------------------------------------------------- proxy


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "repo" / "frontend").mkdir(parents=True)
    return tmp_path / "repo"


def test_plan_proxy_hands_back_a_busy_port(cfg, ports, repo) -> None:
    ports["busy"].add(8000)
    with pytest.raises(actions.PortBusy) as busy:
        actions.plan_proxy(cfg, repo, [], port=8000, frontend=False)
    assert (busy.value.port, busy.value.free) == (8000, 8001)


def test_plan_proxy_asks_whether_to_point_the_frontend(cfg, ports, repo) -> None:
    with pytest.raises(actions.PointFrontend) as asked:
        actions.plan_proxy(cfg, repo, [], port=8000)
    assert asked.value.url == "http://localhost:8000"
    plan = actions.plan_proxy(cfg, repo, [], port=8000, frontend=True, user_name="agent")
    assert plan.frontend and plan.user == cfg.users["agent"]
    assert actions.point_frontend(plan) == str(repo / "frontend" / ".env.local")
    # Already pointing to the proxy (or no frontend at all): nothing to ask or change.
    assert not actions.plan_proxy(cfg, repo, [], port=8000).frontend


def test_plan_proxy_rejects_an_unknown_user(cfg, ports, repo) -> None:
    with pytest.raises(actions.ActionError):
        actions.plan_proxy(cfg, repo, [], port=8000, user_name="nobody", frontend=False)


def test_proxy_routes_needs_the_terraform_environment(repo) -> None:
    with pytest.raises(actions.ActionError):
        actions.proxy_routes(repo, "dev")


def test_proxy_remote_saves_what_it_detects(cfg, repo) -> None:
    from pdms_cli.config import Repo

    cfg.repos = {"pdms": Repo(path=str(repo))}
    (repo / "frontend" / ".env").write_text("VITE_APP_API_URL=https://abc.execute-api.us-east-1.amazonaws.com/dev\n")
    assert actions.proxy_remote(cfg, repo, None, True) == (None, False)
    url, detected = actions.proxy_remote(cfg, repo, None, False)
    assert detected and url == cfg.repos["pdms"].remote
    assert actions.proxy_remote(cfg, repo, None, False) == (url, False)  # saved: not detected again
    assert actions.proxy_remote(cfg, repo, "https://other/", False) == ("https://other", False)


def test_clear_proxy_leftovers_fails_while_a_proxy_runs(repo, monkeypatch) -> None:
    monkeypatch.setattr(actions.proxy, "running_proxy", lambda: {"pid": 1, "port": 8000})
    with pytest.raises(actions.ActionError):
        actions.clear_proxy_leftovers()


def test_local_events_give_each_service_its_own_sns_topics(cfg, monkeypatch):
    event_map = events.EventMap(topic_variables={"lead/account-publish-ev": {"SNS_ACCOUNT_PUBLISH_ARN": "sns-account-publish.fifo"}})
    monkeypatch.setattr(actions, "repo_event_map", lambda cfg, service: (event_map, f"lead/{service.name}"))

    def env(service: str, kind: str) -> dict[str, str]:
        launch = actions.ServiceLaunch(
            Path(service), "agent", cfg.users["agent"], "local", cfg.dbs["local"], "0.0.0.0", 8081, False,
            actions.EventsSetup({"PDMS_SNS_QUEUE_URL": "q"} if kind == "local" else {}, "", kind), [],
        )
        return actions.service_env(cfg, launch)

    assert env("account-publish-ev", "local")["SNS_ACCOUNT_PUBLISH_ARN"] == \
        "arn:aws:sns:us-east-1:000000000000:sns-account-publish.fifo"
    assert "SNS_ACCOUNT_PUBLISH_ARN" not in env("lead-tp-list", "local")
    assert "SNS_ACCOUNT_PUBLISH_ARN" not in env("account-publish-ev", "aws")  # AWS mode keeps the real topic


# --------------------------------------------------------------------------- local events


def event_map() -> events.EventMap:
    return events.EventMap(
        queues={"broker.fifo": events.Queue("broker.fifo"), "email.fifo": events.Queue("email.fifo"),
                "plain": events.Queue("plain", fifo=False)},
        consumers={"email.fifo": events.Consumer("notification/email-notify", "main.handler")},
        routes={"email-notify": "email.fifo"}, broker_queue="broker.fifo",
    )


def test_plan_send_goes_through_the_broker_unless_direct() -> None:
    queue, message = actions.plan_send(event_map(), "email-notify", '{"to": "a@x.com"}')
    body = json.loads(message)
    assert queue == "broker.fifo" and body["type"] == "email-notify" and body["to"] == "a@x.com" and body["event_id"]
    assert actions.plan_send(event_map(), "email-notify", None, direct=True)[0] == "email.fifo"
    assert actions.plan_send(event_map(), "plain", "[1, 2]") == ("plain", "[1, 2]")  # a queue gets the body as is
    assert actions.plan_send(event_map(), "plain", None) == ("plain", "{}")


@pytest.mark.parametrize(("target", "raw", "error"), [
    ("email", None, "Did you mean: email-notify, email.fifo"),
    ("nothing", None, "pdms events map"),
    ("email-notify", "{", "Invalid JSON"),
    ("email-notify", "[]", "must be a JSON object"),
    ("plain", "not json", "Invalid JSON"),
])
def test_plan_send_says_what_is_wrong(target, raw, error) -> None:
    with pytest.raises(actions.ActionError, match=error):
        actions.plan_send(event_map(), target, raw)


def test_start_events_checks_docker_and_the_port(cfg, ports, monkeypatch) -> None:
    monkeypatch.setattr(actions.events, "docker_available", lambda: (False, "Cannot connect to the Docker daemon"))
    with pytest.raises(actions.ActionError, match="Docker is not available: Cannot connect"):
        actions.start_events(cfg, event_map().queues)

    monkeypatch.setattr(actions.events, "docker_available", lambda: (True, "27.0"))
    monkeypatch.setattr(actions.events, "container_state", lambda: None)
    ports["busy"].add(cfg.defaults.events_port)
    with pytest.raises(actions.ActionError, match="in use by something else"):
        actions.start_events(cfg, event_map().queues)

    ports["busy"].clear()
    started = []
    monkeypatch.setattr(actions.events, "start", lambda queues, port: started.append(sorted(queues)) or "created")
    answers = iter([False, True])
    monkeypatch.setattr(actions.events, "is_up", lambda port: next(answers))
    monkeypatch.setattr(actions.time, "sleep", lambda seconds: None)
    assert actions.start_events(cfg, event_map().queues) == "created"
    assert started == [["broker.fifo", "email.fifo", "plain"]]

    monkeypatch.setattr(actions.events, "is_up", lambda port: False)
    with pytest.raises(actions.ActionError, match="did not answer"):
        actions.start_events(cfg, event_map().queues, wait=0)

    def broken(queues, port):
        raise RuntimeError("pull access denied")

    monkeypatch.setattr(actions.events, "start", broken)
    with pytest.raises(actions.ActionError, match="Could not start ElasticMQ: pull access denied"):
        actions.start_events(cfg, event_map().queues)
