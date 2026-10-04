"""The Docker images pdms uses, and the rule that it never downloads one without asking: they are hundreds of MB.

What needs an image checks it first (:func:`require`) and stops with :class:`Missing`; the CLI asks before
:func:`pull`, pdms ui shows a dialog and pulls it in a job, and Doctor lists them with a Download button.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import IO

from . import actions, migrations
from .i18n import _

POSTGRES = "postgres:18.0"  # backend/docker-compose_dev.yml and docker-compose_tests.yml
FLYWAY = migrations.IMAGE  # the pipeline's flyway_migrate.yml


@dataclass(frozen=True)
class Image:
    name: str
    download_mb: int

    @property
    def use(self) -> str:
        """What needs it, in the user's language."""
        return {
            POSTGRES: _("pdms's Postgres: test databases, the local copy and its snapshots"),
            FLYWAY: _("Flyway: pdms migrate and the migrations of the local copy"),
        }.get(self.name, "")


IMAGES = {POSTGRES: Image(POSTGRES, 155), FLYWAY: Image(FLYWAY, 360)}


class Missing(actions.Decision):
    """Images to download before the action can go on (answer: pull them, then retry)."""

    def __init__(self, images: list[Image]) -> None:
        super().__init__(describe(images))
        self.images = images


def describe(images: list[Image]) -> str:
    total = sum(image.download_mb for image in images)
    names = ", ".join(f"{image.name} (~{image.download_mb} MB)" for image in images)
    return _("pdms needs to download {names} from Docker: about {total} MB, only once.", names=names, total=total)


def present(name: str) -> bool:
    try:
        result = subprocess.run(["docker", "image", "inspect", name], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def missing(names: list[str]) -> list[Image]:
    return [IMAGES[name] for name in names if not present(name)]


def require(names: list[str]) -> None:
    """:class:`Missing` when one of the images is not there yet (never pulled here)."""
    found = missing(names)
    if found:
        raise Missing(found)


def pull(name: str, output: IO[str] | None = None, attempts: int = 3, wait: float = 10) -> None:
    """Download one image, retrying (public ECR throttles anonymous pulls); its progress goes to ``output``."""
    last = ""
    for attempt in range(1, attempts + 1):
        if output:
            output.write(f"$ docker pull {name}\n")
            output.flush()
        try:
            result = subprocess.run(["docker", "pull", name], stdout=output or subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, timeout=3600)
        except (OSError, subprocess.TimeoutExpired) as exc:
            last = str(exc)
        else:
            if result.returncode == 0:
                return
            last = ((result.stdout or "").strip().splitlines() or [f"exit code {result.returncode}"])[-1]
        if attempt < attempts:
            time.sleep(wait * attempt)
    raise actions.ActionError(_("Could not download {image}: {error}. Check the connection and try again.",
                                image=name, error=last))
