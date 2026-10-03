"""Requests in parallel and connections opened at start: the settings, what pdms passes to a service, and the
sqs_patch/pdms_parallel.py module the service loads (tried with stand-ins for Starlette and SQLAlchemy)."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
import textwrap
import threading
import time
import weakref
from dataclasses import replace
from pathlib import Path

import pytest

from pdms_cli import actions, events
from pdms_cli.config import Config, Database, Defaults, DevUser
from pdms_cli.ui import jobs as ui_jobs


@pytest.fixture
def cfg(monkeypatch) -> Config:
    monkeypatch.setattr(Config, "save", lambda self: None)
    monkeypatch.setattr(actions.runner, "port_is_free", lambda host, port: True)
    monkeypatch.setattr(actions.instances, "running_ports", lambda: set())
    monkeypatch.setattr(actions.events, "running", lambda port: False)
    return Config(users={"agent": DevUser("u2", "a@x.com")}, dbs={"local": Database("localhost")}, defaults=Defaults())


def plan(cfg: Config, service: Path, **options) -> actions.ServiceLaunch:
    return actions.plan_service(cfg, service, user_name="agent", db_name="local", **options)


# --------------------------------------------------------------------------- settings and launch


def test_both_are_on_by_default() -> None:
    assert (Defaults().parallel_requests, Defaults().warm_connections) == (True, 2)


def test_a_service_runs_in_parallel_unless_asked_not_to(cfg, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(actions, "consumer_of", lambda cfg, service: None)
    assert plan(cfg, tmp_path).parallel
    assert not plan(cfg, tmp_path, parallel=False).parallel
    cfg.defaults.parallel_requests = False
    assert not plan(cfg, tmp_path).parallel
    assert plan(cfg, tmp_path, parallel=True).parallel


def test_an_sqs_consumer_never_does(cfg, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(actions, "consumer_of",
                        lambda cfg, service: (events.Queue("q.fifo"), events.Consumer("broker/x", "main.handler")))
    monkeypatch.setattr(actions, "events_setup", lambda cfg, service, mode: actions.EventsSetup({}, "local", "local"))
    assert not plan(cfg, tmp_path, parallel=True).parallel


def test_the_service_gets_the_patch_folder_and_its_variables(cfg, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(actions, "consumer_of", lambda cfg, service: None)
    env = actions.service_env(cfg, plan(cfg, tmp_path))
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(events.PATCH_DIR)
    assert (env["PDMS_PARALLEL_REQUESTS"], env["PDMS_WARM_CONNECTIONS"]) == ("1", "2")

    cfg.defaults = replace(cfg.defaults, warm_connections=0)
    env = actions.service_env(cfg, plan(cfg, tmp_path))
    assert "PDMS_WARM_CONNECTIONS" not in env and env["PDMS_PARALLEL_REQUESTS"] == "1"

    env = actions.service_env(cfg, plan(cfg, tmp_path, parallel=False))
    assert not {"PDMS_PARALLEL_REQUESTS", "PDMS_WARM_CONNECTIONS"} & set(env)
    assert env["PDMS_QUERY_STATS"] == "1"  # still measured: the patch folder stays

    cfg.defaults = replace(cfg.defaults, query_stats=False)
    env = actions.service_env(cfg, plan(cfg, tmp_path, parallel=False))
    assert not {"PDMS_PARALLEL_REQUESTS", "PDMS_WARM_CONNECTIONS", "PDMS_QUERY_STATS"} & set(env)
    assert str(events.PATCH_DIR) not in env.get("PYTHONPATH", "")  # nothing to load: no patch folder


def test_the_instance_remembers_it(cfg, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(actions, "consumer_of", lambda cfg, service: None)
    monkeypatch.setattr(actions, "installed_parts", lambda service: None)
    started = {}
    monkeypatch.setattr(actions.instances, "start", lambda service, cmd, env, **kw: started.update(kw))
    actions.start_service(cfg, plan(cfg, tmp_path, parallel=False))
    assert started["parallel"] is False


def test_warm_connections_are_checked_when_saved(cfg) -> None:
    for wrong in (-1, actions.MAX_WARM_CONNECTIONS + 1, "many"):
        with pytest.raises(actions.InvalidValue) as error:
            actions.save_defaults(cfg, replace(cfg.defaults, warm_connections=wrong))
        assert error.value.field == "warm_connections"
    actions.save_defaults(cfg, replace(cfg.defaults, warm_connections="0", parallel_requests=False))
    assert (cfg.defaults.warm_connections, cfg.defaults.parallel_requests) == (0, False)


def test_the_ui_form_sends_them_like_the_other_defaults() -> None:
    values = ui_jobs.defaults_from({"parallel_requests": False, "warm_connections": "3"}, Defaults())
    assert (values.parallel_requests, values.warm_connections) == (False, "3")  # the action turns it into a number
    with pytest.raises(actions.InvalidValue):
        ui_jobs.defaults_from({"parallel_requests": "no"}, Defaults())


# --------------------------------------------------------------------------- the module the service loads


@pytest.fixture
def parallel(monkeypatch, tmp_path):
    """pdms_parallel.py loaded fresh, with a stand-in ``starlette`` whose app blocks like a slow synchronous query."""
    fake = tmp_path / "starlette"
    fake.mkdir()
    (fake / "__init__.py").write_text("")
    (fake / "applications.py").write_text(textwrap.dedent("""
        import threading, time

        class Starlette:
            async def __call__(self, scope, receive, send):
                if scope["type"] == "http":
                    time.sleep(scope.get("block", 0))  # synchronous, like SQLAlchemy inside an async endpoint
                message = await receive()
                await send({"thread": threading.current_thread().name, "got": message})
    """))
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in ("starlette", "starlette.applications"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location("pdms_parallel_under_test", events.PATCH_DIR / "pdms_parallel.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def serve(app, *scopes) -> tuple[list[dict], list[float], set[str]]:
    """Run the requests at once on one event loop, like uvicorn would; what each sent, when, and the loop threads
    ``receive``/``send`` ran on."""
    sent, finished, loop_threads = [], [], set()

    async def main():
        start = time.monotonic()

        async def one(scope):
            async def receive():
                loop_threads.add(threading.current_thread().name)
                return {"type": "http.request", "body": b"x"}

            async def send(message):
                loop_threads.add(threading.current_thread().name)
                sent.append(message)

            await app(scope, receive, send)
            finished.append(time.monotonic() - start)

        await asyncio.gather(*(one(scope) for scope in scopes))

    asyncio.run(main())
    return sent, finished, loop_threads


def test_without_it_a_slow_request_holds_up_the_others(parallel) -> None:
    import starlette.applications

    _, finished, _ = serve(starlette.applications.Starlette(), {"type": "http", "block": 0.6}, {"type": "http"})
    assert min(finished) >= 0.5  # the quick one waited for the 0.6 s one (margins: Windows' clock ticks every ~16 ms)


def test_with_it_a_slow_request_only_holds_up_itself(parallel) -> None:
    import starlette.applications

    parallel.run_requests_in_threads()
    parallel.run_requests_in_threads()  # loaded twice (uvicorn --reload): patched once
    sent, finished, loop_threads = serve(starlette.applications.Starlette(), {"type": "http", "block": 0.6},
                                         {"type": "http"}, {"type": "lifespan"})
    assert max(finished[:2]) < 0.3 and finished[-1] >= 0.5  # the quick ones did not wait for the 0.6 s one
    assert sorted(message["thread"].startswith("pdms-request") for message in sent) == [False, True, True]
    assert loop_threads == {threading.main_thread().name}  # receive and send stay on uvicorn's loop
    assert all(message["got"] == {"type": "http.request", "body": b"x"} for message in sent)


def test_errors_reach_uvicorn_as_before(parallel) -> None:
    import starlette.applications

    async def broken(self, scope, receive, send):
        raise ValueError("boom")

    starlette.applications.Starlette.__call__ = broken
    parallel.run_requests_in_threads()
    with pytest.raises(ValueError, match="boom"):
        serve(starlette.applications.Starlette(), {"type": "http"})


class FakeEngine:
    def __init__(self, size: int = 5) -> None:
        self.pool = type("Pool", (), {"size": lambda pool: size})()
        self.opened = self.closed = 0
        self.lock = threading.Lock()

    def connect(self):
        with self.lock:
            self.opened += 1
        engine = self

        class Connection:
            def close(self):
                with engine.lock:
                    engine.closed += 1

        return Connection()


def test_warm_opens_connections_and_gives_them_back(parallel, capfd) -> None:
    engine = FakeEngine(size=5)
    parallel.warm(weakref.ref(engine), 3, delay=0)
    assert (engine.opened, engine.closed) == (3, 3)
    assert "opened 3 database connection(s)" in capfd.readouterr().err

    small = FakeEngine(size=1)
    parallel.warm(weakref.ref(small), 3, delay=0)
    assert small.opened == 1  # never more than the pool keeps


def test_warm_skips_engines_that_are_gone_and_reports_failures(parallel, capfd) -> None:
    gone = FakeEngine()
    gone.itself = gone  # dropped engines sit in reference cycles until the garbage collector runs
    ref = weakref.ref(gone)
    del gone
    parallel.warm(ref, 2, delay=0)
    assert ref() is None and "opened" not in capfd.readouterr().err

    engine = FakeEngine()
    engine.connect = lambda: (_ for _ in ()).throw(OSError("timeout expired"))
    parallel.warm(weakref.ref(engine), 2, delay=0)
    assert "could not open 2 database connection(s): timeout expired" in capfd.readouterr().err


def test_install_does_only_what_the_variables_ask(parallel, monkeypatch) -> None:
    import starlette.applications

    calls = []
    monkeypatch.setattr(parallel, "run_requests_in_threads", lambda: calls.append("threads"))
    monkeypatch.setattr(parallel, "warm_new_engines", lambda count: calls.append(("warm", count)))
    monkeypatch.delenv("PDMS_PARALLEL_REQUESTS", raising=False)
    monkeypatch.delenv("PDMS_WARM_CONNECTIONS", raising=False)
    parallel.install()
    monkeypatch.setenv("PDMS_PARALLEL_REQUESTS", "1")
    monkeypatch.setenv("PDMS_WARM_CONNECTIONS", "2")
    parallel.install()
    assert calls == ["threads", ("warm", 2)]
    assert not hasattr(starlette.applications.Starlette.__call__, "_pdms_parallel")


def test_a_service_python_loads_it_through_sitecustomize(tmp_path) -> None:
    fake = tmp_path / "starlette"
    fake.mkdir()
    (fake / "__init__.py").write_text("")
    (fake / "applications.py").write_text(
        "class Starlette:\n    async def __call__(self, scope, receive, send):\n        pass\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PDMS_", "PYTHONPATH"))}
    env.update(PYTHONPATH=os.pathsep.join([str(events.PATCH_DIR), str(tmp_path)]), PDMS_PARALLEL_REQUESTS="1")
    check = "import starlette.applications as a; print(getattr(a.Starlette.__call__, '_pdms_parallel', False))"
    result = subprocess.run([sys.executable, "-c", check], env=env, capture_output=True, text=True, timeout=60)
    assert result.stdout.strip() == "True", result.stderr
    assert "[pdms] requests run in parallel" in result.stderr
