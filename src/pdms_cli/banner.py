"""The big "PDMS" shown when the interactive menu opens."""

from __future__ import annotations

from rich.console import Console
from rich.text import Text

# "ANSI Shadow" letters; plain ASCII for terminals that cannot draw box characters (e.g. the classic Windows cmd).
BLOCKS = [
    "██████╗ ██████╗ ███╗   ███╗███████╗",
    "██╔══██╗██╔══██╗████╗ ████║██╔════╝",
    "██████╔╝██║  ██║██╔████╔██║███████╗",
    "██╔═══╝ ██║  ██║██║╚██╔╝██║╚════██║",
    "██║     ██████╔╝██║ ╚═╝ ██║███████║",
    "╚═╝     ╚═════╝ ╚═╝     ╚═╝╚══════╝",
]
ASCII = [
    " ____  ____  __  __ ____  ",
    "|  _ \\|  _ \\|  \\/  / ___| ",
    "| |_) | | | | |\\/| \\___ \\ ",
    "|  __/| |_| | |  | |___) |",
    "|_|   |____/|_|  |_|____/ ",
]
# One colour per line: a teal-to-blue gradient that reads on light and dark terminals.
GRADIENT = ["#1fb5a0", "#1aa6a8", "#1797b0", "#1488b8", "#1179c0", "#0e6ac8"]


def can_draw_blocks(console: Console) -> bool:
    try:
        "".join(BLOCKS).encode(console.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def render(console: Console, version: str, tagline: str) -> None:
    lines = BLOCKS if can_draw_blocks(console) else ASCII
    width = max(len(line) for line in lines)
    if console.width < width + 2:  # too narrow for the letters: just the name
        console.print(Text.assemble(("pdms ", "bold #1488b8"), (f"v{version}", "dim")))
        return
    console.print()
    for line, colour in zip(lines, GRADIENT):
        console.print(Text(line, style=f"bold {colour}"), highlight=False, soft_wrap=False)
    console.print(Text.assemble((f"v{version}", "bold"), ("  ·  ", "dim"), (tagline, "dim")), highlight=False)
    console.print()
