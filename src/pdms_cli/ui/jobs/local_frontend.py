"""The PDMS frontend: starting it and its options."""

from __future__ import annotations

from ... import actions, frontend
from ...config import Config
from ...i18n import _
from . import repo_info
from .core import Job, JobsCore

__all__ = ["FrontendJobs", "fix_frontend_api", "frontend_options"]


def fix_frontend_api() -> dict:
    return {"done": actions.fix_frontend_api(Config.load())}


def frontend_options(cfg: Config) -> dict:
    """What the frontend's start form shows: node and yarn, whether node_modules is up to date, where the API goes
    in each mode and whether a build would be made (and why)."""
    root = repo_info.repo_root(cfg)
    if not frontend.exists(root):
        return {"available": False}
    tool = frontend.yarn()
    return {
        "available": True, "node": frontend.node_version() or "", "yarn": bool(tool), "port": frontend.PORT,
        "dependencies_ok": frontend.dependencies_ok(root) if tool else False,
        "api": {mode: frontend.api_url(root, mode) for mode in frontend.MODES},
        "build_needed": frontend.build_needed(root), "last_build": frontend.last_build(),
    }


class FrontendJobs(JobsCore):
    def start_frontend(
        self, *, mode: str = "dev", port: int | None = None, install: bool | None = None, rebuild: bool = False,
        restart: bool = False,
    ) -> Job:
        """Start the web app like ``pdms front -b``: yarn install when needed, the build in build mode, then the
        server; a busy port is raised before anything starts. The job ends once it answers. With ``restart`` the
        running one stops first, on the same port (Rebuild)."""
        if self.busy(frontend.KEY):
            raise actions.ActionError(_("{key} is busy.", key=frontend.KEY))
        if restart and (current := frontend.running()):
            port = port or int(current["port"])
            actions.stop_frontend(current)
        plan = actions.plan_frontend(Config.load(), mode=mode, port=port, install=install, rebuild=rebuild)
        return self.run(frontend.KEY, "start", "starting", lambda job: self.frontend_steps(job, plan))

    def frontend_steps(self, job: Job, plan: actions.FrontendLaunch) -> None:
        """Install, build and start the frontend, writing the install and build output to their own logs."""
        if plan.install:
            self.phase(job, "installing frontend", installed=True, log_key=frontend.KEY)
            log = frontend.install_log_path()
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "w", encoding="utf-8", errors="replace") as output:
                actions.install_frontend(plan.root, output, log)
        if plan.mode == "build" and (plan.build or frontend.build_needed(plan.root)):
            self.phase(job, "building frontend", log_key=frontend.KEY)
            log = frontend.build_log_path()
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "w", encoding="utf-8", errors="replace") as output:
                actions.build_frontend(plan.root, output)
        self.phase(job, "starting frontend")
        started = actions.start_frontend(plan)
        if actions.wait_for_frontend(started) == "stopped":
            raise actions.ActionError(_("The frontend exited while starting: {line}",
                                        line=frontend.last_line(frontend.log_path()) or "?"))
