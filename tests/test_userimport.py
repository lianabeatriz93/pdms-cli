"""Importing development users from pdms_user."""

from __future__ import annotations

from pdms_cli.config import DevUser
from pathlib import Path

import pytest

from pdms_cli.userimport import (
    INTERNAL_TO_EXTERNAL_ROLES, DbUser, alias_for, internal_role, merge_users, repo_role_mapping, role_mapping,
)


def db_user(uid: str, email: str, roles: list[str], first: str = "A", last: str = "B") -> DbUser:
    return DbUser(uid, email, first, last, roles, True)


def test_internal_roles_become_dev_roles():
    user = db_user("1", "a@x.com", ["TRANSPORTATION_PR_SUPERVISOR", "SPECIALTY_PR_AGENT", "NEW_ROLE"])
    assert user.dev_roles() == "TPR.Supervisor,SPR.Agent,NEW_ROLE"
    assert user.unknown_roles() == ["NEW_ROLE"]


def test_role_filter_accepts_both_forms():
    assert internal_role("TPR.Agent") == "TRANSPORTATION_PR_AGENT"
    assert internal_role("TRANSPORTATION_PR_AGENT") == "TRANSPORTATION_PR_AGENT"


def test_alias_from_email():
    assert alias_for("supervisor.nemt1@gmail.com") == "supervisor-nemt1"
    assert alias_for("Ana+QA@alivi.com") == "ana-qa"


def test_merge_adds_updates_by_user_id_and_avoids_alias_clashes():
    existing = {
        "supervisor": DevUser("1", "sup@x.com", roles="TPR.Supervisor"),
        "ana": DevUser("9", "other@x.com"),
    }
    picked = [
        db_user("1", "sup@x.com", ["TRANSPORTATION_PR_AGENT"]),  # same id: updated under its alias
        db_user("2", "ana@x.com", []),  # alias "ana" taken by someone else
    ]
    users, added, updated = merge_users(existing, picked)
    assert updated == ["supervisor"] and users["supervisor"].roles == "TPR.Agent"
    assert added == ["ana-2"] and users["ana-2"].username == "ana@x.com"
    assert users["ana"].username == "other@x.com"


def test_merge_reports_nothing_when_unchanged():
    existing = {"sup": db_user("1", "sup@x.com", ["TRANSPORTATION_PR_SUPERVISOR"]).to_dev_user()}
    _, added, updated = merge_users(existing, [db_user("1", "sup@x.com", ["TRANSPORTATION_PR_SUPERVISOR"])])
    assert added == [] and updated == []


MODELS = """
from enum import Enum


class UserRoleEnum(str, Enum):
    TRANSPORTATION_PR_SUPERVISOR = "TRANSPORTATION_PR_SUPERVISOR"

    NEW_AUDITOR = "NEW_AUDITOR"
    LEGACY_ADMIN = "LEGACY_ADMIN"


class UserRolePPEnum(str, Enum):
    TRANSPORTATION_PR_SUPERVISOR = "TPR.Supervisor"
    NEW_AUDITOR = "Audit.Reader"
    ADMIN = "Portal.Admin"
"""

SETTINGS = """
from core.domain.models import UserRoleEnum, UserRolePPEnum

MAP_INTERNAL_ROLES = {
    UserRoleEnum.TRANSPORTATION_PR_SUPERVISOR: UserRolePPEnum.TRANSPORTATION_PR_SUPERVISOR,
    UserRoleEnum.NEW_AUDITOR: UserRolePPEnum.NEW_AUDITOR,
    UserRoleEnum.LEGACY_ADMIN: UserRolePPEnum.ADMIN,  # names do not have to match
}
"""


def fake_repo(root: Path, settings: str | None = SETTINGS) -> Path:
    core = root / "backend/common/core/core"
    (core / "domain").mkdir(parents=True)
    (core / "domain/models.py").write_text(MODELS)
    if settings is not None:
        (core / "settings.py").write_text(settings)
    repo_role_mapping.cache_clear()
    return root


def test_mapping_is_read_from_the_repo_settings(tmp_path):
    mapping, source = role_mapping(fake_repo(tmp_path))
    assert mapping == {
        "TRANSPORTATION_PR_SUPERVISOR": "TPR.Supervisor", "NEW_AUDITOR": "Audit.Reader", "LEGACY_ADMIN": "Portal.Admin",
    }
    assert source.endswith("settings.py")
    user = DbUser("1", "a@x.com", "A", "B", ["NEW_AUDITOR", "LEGACY_ADMIN"], True)
    assert user.dev_roles(mapping) == "Audit.Reader,Portal.Admin"
    assert internal_role("Portal.Admin", mapping) == "LEGACY_ADMIN"


def test_without_map_members_with_the_same_name_are_paired(tmp_path):
    mapping, _ = role_mapping(fake_repo(tmp_path, settings="X = 1\n"))
    assert mapping == {"TRANSPORTATION_PR_SUPERVISOR": "TPR.Supervisor", "NEW_AUDITOR": "Audit.Reader"}


def test_falls_back_to_the_built_in_copy(tmp_path):
    repo_role_mapping.cache_clear()
    assert role_mapping(tmp_path) == (INTERNAL_TO_EXTERNAL_ROLES, "built-in")
    assert role_mapping(None) == (INTERNAL_TO_EXTERNAL_ROLES, "built-in")


@pytest.mark.parametrize("repo", ["pdms", "pdms_v2"])
def test_real_checkouts_match_the_built_in_copy(repo):
    root = Path.home() / "Code" / "Alivi" / repo
    if not (root / "backend").is_dir():
        pytest.skip(f"{root} not available")
    repo_role_mapping.cache_clear()
    mapping, source = role_mapping(root)
    assert source != "built-in"
    assert mapping == INTERNAL_TO_EXTERNAL_ROLES
