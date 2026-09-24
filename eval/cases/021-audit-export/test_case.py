import pytest

from before import check_permission, export_audit

USER = {"permissions": ["audit:export"]}
ENTRIES = [{"ts": "t1", "actor": "ana", "action": "login"}]


def test_exports_a_header_and_a_row():
    body = export_audit(USER, ENTRIES)
    assert body.splitlines() == ["timestamp,actor,action", "t1,ana,login"]


def test_refuses_a_user_without_the_permission():
    with pytest.raises(Exception):
        export_audit({"permissions": []}, ENTRIES)


def test_check_permission_allows_a_permitted_action():
    check_permission(USER, "audit:export")
