"""The CI runs the installer jobs (macOS runners can queue for long) only when what they try changed."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("tomllib")  # Python 3.11+, like the runner's python3

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("installers_changed", ROOT / ".github" / "installers_changed.py")
changed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(changed)

PYPROJECT = '[project]\nname = "pdms-cli"\ndependencies = ["typer"]\n\n[dependency-groups]\ndev = ["pytest"]\n'
CI = "jobs:\n  tests:\n    runs-on: ubuntu-latest\n  installers:\n    runs-on: macos-latest\n  flyway:\n    x: 1\n"


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    for args in (["init", "-q"], ["config", "user.email", "t@x"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], check=True)
    write(tmp_path, {"pyproject.toml": PYPROJECT, ".github/workflows/ci.yml": CI, "src/pdms_cli/proxy.py": "a = 1\n",
                     "src/pdms_cli/desktop.py": "b = 1\n", "uv.lock": "lock 1\n"})
    return tmp_path


def write(root: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "commit", "-qm", "x"], check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


def test_code_the_tests_cover_a_dev_dependency_or_the_lock_leave_the_installers_alone(repo) -> None:
    base = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    write(repo, {"src/pdms_cli/proxy.py": "a = 2\n", "uv.lock": "lock 2\n",
                 "pyproject.toml": PYPROJECT.replace('dev = ["pytest"]', 'dev = ["pytest", "playwright"]')
                 + "\n[tool.ruff]\nline-length = 120\n",
                 ".github/workflows/ci.yml": CI.replace("tests:\n    runs-on: ubuntu-latest",
                                                        "tests:\n    runs-on: windows-latest")})
    assert changed.reasons(base) == []


def test_what_the_installers_try_runs_them(repo) -> None:
    base = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    write(repo, {"src/pdms_cli/desktop.py": "b = 2\n", "installer/install.sh": "echo\n",
                 "pyproject.toml": PYPROJECT.replace('["typer"]', '["typer", "rich"]'),
                 ".github/workflows/ci.yml": CI.replace("macos-latest", "macos-15")})
    assert changed.reasons(base) == ["installer/install.sh", "src/pdms_cli/desktop.py", "pyproject.toml (the package)",
                                     ".github/workflows/ci.yml (the installers job)"]


def test_run_by_hand_or_on_a_new_branch_they_always_run(repo, capsys) -> None:
    for base in ("", "0" * 40):
        changed.sys.argv = ["installers_changed.py", base]
        changed.main()
        assert capsys.readouterr().out.strip().endswith("installers=true")
