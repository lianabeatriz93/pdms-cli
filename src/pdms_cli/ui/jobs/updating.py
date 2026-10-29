"""Updating pdms itself from the page."""

from __future__ import annotations

from ... import __version__, actions, proxy, update
from ...config import Config
from ...i18n import _
from .. import updates as ui_updates
from ..control import Control
from .core import Job, JobsCore

__all__ = ["UpdateJobs"]


class UpdateJobs(JobsCore):
    def update(self, control: Control, *, restart_proxy: bool = True, version: str = "") -> Job:
        """Install the latest version, or ``version`` (an earlier one too), and restart pdms ui with it (see
        :mod:`.updates`)."""
        if not update.updates_itself():
            raise actions.ActionError(_("This pdms was not installed with uv tool, so it cannot update itself; "
                                        "run the command shown."))
        if version:
            if not update.VERSION.match(version):
                raise actions.ActionError(_("'{version}' is not a pdms version.", version=version))
            if version == __version__:
                raise actions.ActionError(_("pdms {version} is the version you have.", version=version))
            target = version
        else:
            target, _checked = update.cached_latest(ui_updates.channel(Config.load()))
            if not target or not update.is_newer(target):
                raise actions.ActionError(_("pdms {version} is the latest version.", version=__version__))
        return self.run(ui_updates.KEY, "update", "starting", lambda job: ui_updates.install(
            control, target, restart_proxy, lambda phase: self.phase(job, phase, log_key=ui_updates.KEY)))

    def after_update(self, note: dict | None) -> None:
        """Right after restarting for an update: say if it failed and start the proxy it stopped."""
        error, saved_proxy = ui_updates.after_restart(note)
        if error:
            self.failed(ui_updates.KEY, "update", error, log_key=ui_updates.KEY)
        if saved_proxy:
            self.run(proxy.KEY, "start", "starting", lambda _job: ui_updates.start_proxy_again(saved_proxy))
