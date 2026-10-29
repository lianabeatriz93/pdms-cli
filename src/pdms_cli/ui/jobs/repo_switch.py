"""Switching the current repo, and what the page needs of a repo."""

from __future__ import annotations

from ... import actions, instances
from ...config import Config
from ...i18n import _
from .core import REPO_KEY, SWITCH_CHOICES, Job, LeftRunning
from .services import ServiceJobs

__all__ = ["RepoJobs"]


class RepoJobs(ServiceJobs):
    def use_repo(self, alias: str, running: str | None = None) -> dict:
        """Make ``alias`` the current repo, like ``pdms repo use``. What runs from the old one is kept, stopped or
        restarted from the new one (``running``); without an answer :class:`LeftRunning` asks first."""
        cfg = Config.load()
        switch = actions.plan_repo_switch(cfg, alias)
        if running is not None and running not in SWITCH_CHOICES:
            raise actions.ActionError(f"running must be one of: {', '.join(SWITCH_CHOICES)}")
        if switch.running and running is None:
            raise LeftRunning(switch)
        if self.busy(REPO_KEY):
            raise actions.ActionError(_("{key} is busy.", key=REPO_KEY))
        actions.use_repo(cfg, alias)
        answer = {"proxy": switch.proxy, "old": switch.old, "job": None, "left": []}
        if not switch.running or running == "keep":
            return answer
        if running == "move":
            answer["left"] = [inst.key for inst, target in switch.running if target is None]

        def work(job: Job) -> None:
            for inst, target in switch.running:
                if running == "move" and target is None:
                    continue  # not in the new repo: it keeps running
                self.phase(job, f"stopping {inst.name}")
                actions.stop_service(inst)
                if running == "move":
                    self.phase(job, f"starting {inst.name}")
                    launch = actions.plan_service(cfg, target, user_name=inst.user, db_name=inst.db, port=inst.port,
                                                  host=inst.host, reload=inst.reload, on_demand=inst.on_demand)
                    key = instances.make_key(launch.service, launch.port)
                    self.install(cfg, job, launch.service, key, None, inst.name)
                    actions.start_service(cfg, launch, awake=True)

        answer["job"] = self.run(REPO_KEY, "move" if running == "move" else "stop", "stopping", work).key
        return answer
