"""Version handling and self-update helpers."""

from __future__ import annotations

from pdms_cli import __version__, update


def test_version_comes_from_the_package_metadata():
    assert update.parse_version(__version__) >= (0, 1, 0)


def test_versions_compare_numerically():
    assert update.is_newer("0.10.0", "0.9.3")
    assert not update.is_newer("0.2.0", "0.2.0")
    assert update.parse_version("v1.2.3") == (1, 2, 3)


def test_release_asset_urls():
    assert update.wheel_url("0.2.0") == (
        "https://github.com/lianabeatriz93/pdms-cli/releases/download/v0.2.0/pdms_cli-0.2.0-py3-none-any.whl"
    )
    cmd = update.upgrade_command("0.2.0")
    assert cmd[1:4] == ["tool", "install", "--force"] and cmd[-1].endswith("pdms_cli-0.2.0-py3-none-any.whl")


def test_development_checkout_is_editable():
    assert update.install_kind() == "editable"  # tests run from `uv sync`, an editable install
