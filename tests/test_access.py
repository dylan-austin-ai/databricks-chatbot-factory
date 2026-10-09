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
    groups = resolve_groups(
        "ana@corp.com", lambda: _user("ana@corp.com", "mlops", "claims-team"), _never
    )
    assert groups == {"mlops", "claims-team"}
    assert "resolved with the user token" in caplog.text and "2 group(s)" in caplog.text


def test_identity_mismatch_grants_nothing(caplog):
    groups = resolve_groups("ana@corp.com", lambda: _user("ana.silva@corp.com", "mlops"), _never)
    assert groups == set()
    assert "identity mismatch" in caplog.text and "ana.silva@corp.com" in caplog.text


def test_header_and_user_name_differing_only_by_case_is_not_a_mismatch(caplog):
    assert resolve_groups("Ana@Corp.com", lambda: _user("ana@corp.com", "mlops"), _never) == {
        "mlops"
    }
    assert "identity mismatch" not in caplog.text


def test_token_failure_grants_nothing_and_never_falls_back(caplog):
    class PermissionDenied(Exception):
        error_code = "PERMISSION_DENIED"

    def denied():
        raise PermissionDenied(
            "GET /api/2.0/preview/scim/v2/Me token=dapi-secret required scopes: x"
        )

    assert resolve_groups("ana@corp.com", denied, _never) == set()
    assert (
        "user token failed" in caplog.text and "PermissionDenied [PERMISSION_DENIED]" in caplog.text
    )


def test_exception_messages_are_never_logged(caplog):
    def broken():
        raise RuntimeError("request to https://host/api?token=dapi-secret failed")

    resolve_groups("ana@corp.com", broken)
    assert "RuntimeError" in caplog.text
    assert "dapi-secret" not in caplog.text and "https://host" not in caplog.text

    class Odd(Exception):
        error_code = "contains spaces and a secret"

    def odd():
        raise Odd("x")

    caplog.clear()
    resolve_groups("ana@corp.com", odd)
    assert "Odd" in caplog.text and "secret" not in caplog.text  # only well-formed codes are logged


def test_no_token_grants_nothing_in_the_deployed_app(caplog):
    assert resolve_groups("ana@corp.com", None) == set()
    assert "no user token" in caplog.text


def test_local_development_can_use_the_directory_when_enabled(caplog):
    caplog.set_level(logging.INFO, logger="factory.access")
    assert resolve_groups("ana@corp.com", None, lambda email: [_user(email, "mlops")]) == {"mlops"}
    assert "local directory lookup" in caplog.text


def test_local_directory_failures_grant_nothing(caplog):
    def broken(email):
        raise RuntimeError("403 Forbidden for user ana")

    assert resolve_groups("ana@corp.com", None, broken) == set()
    assert "local directory lookup failed" in caplog.text and "403" not in caplog.text


def test_unknown_user_is_distinguished_from_a_user_with_no_groups(caplog):
    caplog.set_level(logging.INFO, logger="factory.access")
    assert resolve_groups("ghost@corp.com", None, lambda email: []) == set()
    assert "found no Databricks user" in caplog.text
    caplog.clear()
    assert resolve_groups("new@corp.com", None, lambda email: [_user(email)]) == set()
    assert "0 group(s)" in caplog.text and "found no Databricks user" not in caplog.text
