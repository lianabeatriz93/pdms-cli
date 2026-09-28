"""Version handling and self-update helpers."""

from __future__ import annotations

from pdms_cli import __version__, update


def test_version_comes_from_the_package_metadata():
    assert update.parse_version(__version__) >= (0, 1, 0)


def test_versions_compare_numerically():
    assert update.is_newer("0.10.0", "0.9.3")
    assert not update.is_newer("0.2.0", "0.2.0")
    assert update.parse_version("v1.2.3")[:3] == (1, 2, 3)


def test_release_asset_urls():
    assert update.wheel_url("0.2.0") == (
        "https://github.com/lianabeatriz93/pdms-cli/releases/download/v0.2.0/pdms_cli-0.2.0-py3-none-any.whl"
    )
    cmd = update.upgrade_command("0.2.0")
    assert cmd[1:4] == ["tool", "install", "--force"] and cmd[-1].endswith("pdms_cli-0.2.0-py3-none-any.whl")


def test_development_checkout_is_editable():
    assert update.install_kind() == "editable"  # tests run from `uv sync`, an editable install


def test_pre_releases_sort_before_the_final_version():
    ordered = ["0.2.0", "0.3.0a0", "0.3.0a1", "0.3.0a10", "0.3.0b1", "0.3.0rc1", "0.3.0"]
    assert sorted(reversed(ordered), key=update.parse_version) == ordered
    assert update.is_newer("0.3.0a1", "0.2.0")
    assert update.is_newer("0.3.0", "0.3.0a5")
    assert not update.is_newer("0.3.0a1", "0.3.0")


def test_pre_release_detection():
    assert update.is_prerelease("0.3.0a1") and update.is_prerelease("v1.0.0rc2")
    assert not update.is_prerelease("0.2.0")
    assert update.wheel_url("0.3.0a1").endswith("/v0.3.0a1/pdms_cli-0.3.0a1-py3-none-any.whl")
