"""Who the signed-in person is and which groups they are in (ADM-8).

The app decides admin, owner-group and reviewer-group rights from group membership. Membership
is read with the person's own token, and anything short of a clean answer for that same person
grants nothing: a failed call, a missing token, or a token that resolves to a different account
than the sign-in header. Every outcome is logged so the cases can be told apart.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Iterable, Optional

log = logging.getLogger("factory.access")
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


def _names(groups: Optional[Iterable]) -> set[str]:
    return {g.display for g in (groups or []) if getattr(g, "display", None)}


def _error(e: Exception) -> str:
    """The exception's type and, when the SDK supplies one, its error code. The message is left
    out on purpose: SDK messages can carry request details."""
    code = getattr(e, "error_code", None)
    return (
        f"{type(e).__name__} [{code}]"
        if isinstance(code, str) and _CODE.match(code)
        else type(e).__name__
    )


def resolve_groups(
    email: str,
    as_user: Optional[Callable[[], object]],
    from_directory: Optional[Callable[[str], Iterable]] = None,
) -> set[str]:
    """Group names for the signed-in person; an empty set when they can't be established.

    as_user        returns the SCIM "me" record using the person's own token, or None when the
                   request carries no user token.
    from_directory local development only: looks the email up with the caller's own credentials
                   and returns the matching SCIM user records. Used only when there is no user
                   token. The deployed app never passes it, so there a missing or failing token
                   lookup grants nothing instead of falling back to the app's service principal.
    """
    if as_user is not None:
        try:
            me = as_user()
        except Exception as e:  # noqa: BLE001 - no evidence of membership means no rights
            log.warning(
                "group lookup with the user token failed for %r: %s; granting no groups",
                email,
                _error(e),
            )
            return set()
        name = me.user_name or ""
        if name.lower() != (email or "").lower():
            log.warning(
                "identity mismatch: the sign-in header says %r but the user token resolves to %r; "
                "granting no groups",
                email,
                name,
            )
            return set()
        groups = _names(me.groups)
        log.info(
            "groups for %r resolved with the user token: %d group(s): %s",
            name,
            len(groups),
            sorted(groups)[:50],
        )
        return groups
    if from_directory is None:
        log.warning("no user token on this request for %r; granting no groups", email)
        return set()
    try:
        users = list(from_directory(email))
    except Exception as e:  # noqa: BLE001
        log.warning(
            "local directory lookup failed for %r: %s; granting no groups", email, _error(e)
        )
        return set()
    if not users:
        log.warning("local directory lookup found no Databricks user named %r", email)
        return set()
    groups = {name for u in users for name in _names(u.groups)}
    log.info(
        "groups for %r resolved by the local directory lookup: %d user record(s), %d group(s): %s",
        email,
        len(users),
        len(groups),
        sorted(groups)[:50],
    )
    return groups
