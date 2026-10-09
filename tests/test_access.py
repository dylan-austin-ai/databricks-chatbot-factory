"""Group resolution for the app's admin/owner/reviewer checks."""
import logging
from types import SimpleNamespace

from factory.access import resolve_groups


def _user(name, *groups):
    return SimpleNamespace(user_name=name, groups=[SimpleNamespace(display=g) for g in groups])


def _never(email):
    raise AssertionError("the directory lookup should not be used")


def test_groups_come_from_the_users_own_token(caplog):
    caplog.set_level(logging.INFO, logger="factory.access")
    groups = resolve_groups("ana@corp.com", lambda: _user("ana@corp.com", "mlops", "claims-team"), _never)
    assert groups == {"mlops", "claims-team"}
    assert "resolved with the user token" in caplog.text and "2 group(s)" in caplog.text


def test_identity_mismatch_is_logged(caplog):
    resolve_groups("ana@corp.com", lambda: _user("ana.silva@corp.com", "mlops"), _never)
    assert "identity mismatch" in caplog.text and "ana.silva@corp.com" in caplog.text


def test_header_and_user_name_differing_only_by_case_is_not_a_mismatch(caplog):
    resolve_groups("Ana@Corp.com", lambda: _user("ana@corp.com", "mlops"), _never)
    assert "identity mismatch" not in caplog.text


def test_token_failure_falls_back_to_the_directory_and_says_why(caplog):
    def denied():
        raise PermissionError("required scopes: iam.current-user:read")

    groups = resolve_groups("ana@corp.com", denied, lambda email: [_user(email, "mlops")])
    assert groups == {"mlops"}
    assert "user token failed" in caplog.text and "PermissionError" in caplog.text and "iam.current-user" in caplog.text


def test_no_token_uses_the_directory(caplog):
    caplog.set_level(logging.INFO, logger="factory.access")
    assert resolve_groups("ana@corp.com", None, lambda email: [_user(email, "mlops")]) == {"mlops"}
    assert "no user token" in caplog.text


def test_failed_lookups_grant_nothing_and_are_logged(caplog):
    def broken(email):
        raise RuntimeError("403 Forbidden")

    assert resolve_groups("ana@corp.com", None, broken) == set()
    assert "directory lookup by the app's service principal failed" in caplog.text and "403" in caplog.text


def test_unknown_user_is_distinguished_from_a_user_with_no_groups(caplog):
    caplog.set_level(logging.INFO, logger="factory.access")
    assert resolve_groups("ghost@corp.com", None, lambda email: []) == set()
    assert "found no Databricks user" in caplog.text
    caplog.clear()
    assert resolve_groups("new@corp.com", None, lambda email: [_user(email)]) == set()
    assert "0 group(s)" in caplog.text and "found no Databricks user" not in caplog.text
