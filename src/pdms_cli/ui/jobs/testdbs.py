"""The test databases of the Tests screen."""

from __future__ import annotations

from ... import actions, images, localdb, testruns
from ...config import Config
from ..testing import Tests
from . import repo_info, settings_screen
from .core import TESTS_DB_KEY, Job, JobsCore

__all__ = ["TestDbJobs", "tests_info"]


def tests_info(cfg: Config, tests: Tests) -> dict:
    """The Tests screen: every project with its last result, the ones the branch's changes touch, and pdms's test
    databases (the only ones tests run on: they drop and create every table)."""
    root = repo_info.backend(cfg)
    port = localdb.state()["port"] or localdb.PORT
    found = testruns.results(root)
    tests.count_failing(root)
    return {
        "projects": [{"project": p, "kind": testruns.kind(root / p), "result": found.get(p),
                      "env_dev_mode": testruns.env_dev_mode(root / p)}
                     for p in testruns.projects(root)],
        "affected": testruns.affected(root),
        "dbs": [{"name": name, "where": f"localhost:{port}/{name}"} for name in testruns.test_dbs()],
        "port": localdb.PORT,
        "root": str(root),
    }


class TestDbJobs(JobsCore):
    def run_tests(self, body: dict) -> dict:
        cfg = Config.load()
        projects, dbs = body.get("projects"), body.get("dbs")
        if not settings_screen._strings(projects) or not projects or not settings_screen._strings(dbs):
            raise actions.ActionError("projects and dbs must be lists of names")
        dev_mode = body.get("dev_mode", False)
        if not isinstance(dev_mode, bool):
            raise actions.ActionError("dev_mode must be true or false")
        return self.tests.run(cfg, repo_info.backend(cfg), projects, dbs, dev_mode)

    def start_test_db(self) -> Job:
        """Start pdms's Postgres and create its test databases, like ``pdms db local up``."""
        if not localdb.state()["exists"]:
            images.require([localdb.IMAGE])

        def work(job: Job) -> None:
            self.phase(job, "starting")
            testruns.prepare_test_dbs()

        return self.run(TESTS_DB_KEY, "start", "starting", work)
