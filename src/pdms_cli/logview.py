"""Follow one or several instance logs at once, prefixing each line with its instance (like docker compose logs)."""

from __future__ import annotations

import os
import time
from typing import Callable

from rich.console import Console
from rich.text import Text

from . import instances

COLORS = ["cyan", "magenta", "green", "yellow", "blue", "bright_red", "bright_cyan", "bright_magenta"]


class LogFollower:
    """Incremental reader of a log file that survives truncation and re-creation (e.g. after a restart)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.position = 0
        self.inode: int | None = None
        self.pending = ""

    def skip_to_tail(self, lines: int) -> list[str]:
        """Return the last ``lines`` lines and continue from the end of the file."""
        text = instances.tail(self.path, lines)
        try:
            stat = os.stat(self.path)
        except FileNotFoundError:
            return []
        self.inode, self.position = stat.st_ino, stat.st_size
        return text.splitlines()

    def read_new(self) -> list[str]:
        try:
            stat = os.stat(self.path)
        except FileNotFoundError:
            return []
        if stat.st_ino != self.inode or stat.st_size < self.position:
            self.inode, self.position, self.pending = stat.st_ino, 0, ""
        if stat.st_size == self.position:
            return []
        with open(self.path, "rb") as fh:
            fh.seek(self.position)
            data = fh.read()
        self.position += len(data)
        lines = (self.pending + data.decode(errors="replace")).split("\n")
        self.pending = lines.pop()
        return [line.rstrip("\r") for line in lines]  # Windows logs end lines with \r\n


class MultiLog:
    def __init__(self, console: Console, prefix: bool) -> None:
        self.console = console
        self.prefix = prefix
        self.followers: dict[str, LogFollower] = {}
        self.styles: dict[str, str] = {}
        self.width = 0

    def add(self, inst: instances.Instance, lines: int) -> None:
        if inst.key in self.followers:
            return
        self.styles[inst.key] = COLORS[len(self.styles) % len(COLORS)]
        self.width = max(self.width, len(inst.key))
        follower = LogFollower(inst.log)
        self.followers[inst.key] = follower
        self.emit(inst.key, follower.skip_to_tail(lines))

    def emit(self, key: str, lines: list[str]) -> None:
        for line in lines:
            if self.prefix:
                text = Text.assemble((f"{key:<{self.width}} │ ", self.styles[key]), line)
            else:
                text = Text(line)
            self.console.print(text, highlight=False, soft_wrap=True)

    def poll(self) -> None:
        for key, follower in self.followers.items():
            self.emit(key, follower.read_new())


def follow(
    console: Console,
    targets: list[instances.Instance],
    lines: int,
    discover: Callable[[], list[instances.Instance]] | None = None,
    interval: float = 0.25,
) -> None:
    """Print the logs of ``targets`` until Ctrl+C. ``discover`` adds instances started while following."""
    view = MultiLog(console, prefix=len(targets) > 1 or discover is not None)
    for inst in targets:
        view.add(inst, lines)
    last_scan = time.monotonic()
    try:
        while True:
            view.poll()
            if discover and time.monotonic() - last_scan > 2:
                for inst in discover():
                    view.add(inst, lines)
                last_scan = time.monotonic()
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print()
