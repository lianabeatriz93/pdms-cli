"""Importing development users from pdms_user."""

from __future__ import annotations

from pdms_cli.config import DevUser
from pdms_cli.userimport import DbUser, alias_for, internal_role, merge_users


def db_user(uid: str, email: str, roles: list[str], first: str = "A", last: str = "B") -> DbUser:
    return DbUser(uid, email, first, last, roles, True)


def test_internal_roles_become_dev_roles():
    user = db_user("1", "a@x.com", ["TRANSPORTATION_PR_SUPERVISOR", "SPECIALTY_PR_AGENT", "NEW_ROLE"])
    assert user.dev_roles == "TPR.Supervisor,SPR.Agent,NEW_ROLE"
    assert user.unknown_roles == ["NEW_ROLE"]


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
