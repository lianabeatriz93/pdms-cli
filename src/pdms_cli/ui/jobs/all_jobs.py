"""Jobs: every kind of job pdms ui runs, in one object."""

from __future__ import annotations

from .data import DataJobs
from .home import HomeJobs
from .local_events import EventJobs
from .local_proxy import ProxyJobs
from .repo_switch import RepoJobs
from .updating import UpdateJobs

__all__ = ["Jobs"]


class Jobs(HomeJobs, DataJobs, EventJobs, RepoJobs, ProxyJobs, UpdateJobs):
    """Every job of pdms ui."""
