import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _checkout_version() -> str | None:
    """Version in the pyproject.toml of a source checkout (src/pdms_cli -> ../../pyproject.toml).

    An editable install keeps the metadata of the moment it was installed, so after `cz bump` it would keep
    reporting the old version; the checkout's pyproject.toml is the truth there. Wheels have no pyproject.toml.
    """
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return None
    if not re.search(r'^name\s*=\s*"pdms-cli"', text, re.M):
        return None
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    return match.group(1) if match else None


try:
    __version__ = _checkout_version() or version("pdms-cli")
except PackageNotFoundError:  # a source tree that is not installed
    __version__ = "0.0.0"
