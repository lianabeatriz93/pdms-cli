"""config.toml is written in one step: the proxy, pdms ui and the CLI read it while another of them writes it."""

from __future__ import annotations

import os
import sys
import threading

import pytest

from pdms_cli import config
from pdms_cli.config import Config, Database, DevUser


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "pdms" / "config.toml"
    monkeypatch.setenv("PDMS_CONFIG", str(path))
    return path


def test_a_reader_never_sees_a_half_written_config(config_file) -> None:
    cfg = Config(users={f"user{n}": DevUser(f"u{n}", f"{n}@x.com") for n in range(200)},
                 dbs={"local": Database("localhost", password="secret")})
    cfg.save()
    done = threading.Event()

    def write() -> None:
        while not done.is_set():
            cfg.save()

    writer = threading.Thread(target=write)
    writer.start()
    try:
        for _ in range(300):
            seen = Config.load()  # an empty or truncated file read as defaults, or failed to parse
            assert len(seen.users) == 200 and seen.dbs["local"].password == "secret"
    finally:
        done.set()
        writer.join()
    assert [p.name for p in config_file.parent.iterdir()] == ["config.toml"]  # no temporary file left


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_the_config_stays_readable_only_by_its_user(config_file) -> None:
    Config(dbs={"local": Database("localhost", password="secret")}).save()
    assert config_file.stat().st_mode & 0o777 == 0o600


def test_replacing_retries_while_windows_keeps_the_file_open(tmp_path, monkeypatch) -> None:
    source, target = tmp_path / "new", tmp_path / "old"
    source.write_text("new")
    target.write_text("old")
    refusals = iter([PermissionError(), PermissionError()])
    real = os.replace

    def replace(src, dst):
        error = next(refusals, None)
        if error:
            raise error
        real(src, dst)

    monkeypatch.setattr(config.os, "replace", replace)
    monkeypatch.setattr(config.time, "sleep", lambda seconds: None)
    config.replace_file(source, target)
    assert target.read_text() == "new" and not source.exists()


def test_a_failed_write_leaves_the_config_as_it_was(config_file, monkeypatch) -> None:
    Config(dbs={"local": Database("localhost", password="secret")}).save()
    monkeypatch.setattr(config, "replace_file", lambda source, target: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        Config().save()
    assert Config.load().dbs["local"].password == "secret"
    assert [p.name for p in config_file.parent.iterdir()] == ["config.toml"]
