"""Who the signed-in person is and which groups they are in (ADM-8).

The app decides admin, owner-group and reviewer-group rights from group membership. The lookup
can fail or come back incomplete for reasons outside the app (missing scope, a service principal
that can't read the directory, an email that isn't the Databricks user name), so every outcome
is logged with enough detail to tell those apart. A failed lookup grants nothing.
"""
from __future__ import annotations

import logging
from typing import Callable, Iterable, Optional

log = logging.getLogger("factory.access")


def _names(groups: Optional[Iterable]) -> set[str]:
    return {g.display for g in (groups or []) if getattr(g, "display", None)}


def resolve_groups(email: str, as_user: Optional[Callable[[], object]],
                   from_directory: Callable[[str], Iterable]) -> set[str]:
    """Group names for the signed-in person.

    as_user        returns the SCIM "me" record using the person's own token, or None when the
                   request carries no user token (local development). Preferred: it needs no
                   directory rights and can't pick the wrong account.
    from_directory looks the email up with the app's service principal and returns the matching
                   SCIM user records. Used only when the first source is missing or fails.
    """
    if as_user is not None:
        try:
            me = as_user()
            groups, name = _names(me.groups), (me.user_name or "")
            if name.lower() != (email or "").lower():
                log.warning("identity mismatch: signed-in header says %r but the user token resolves to %r",
                            email, name)
            log.info("groups for %r resolved with the user token: %d group(s): %s",
                     name, len(groups), sorted(groups)[:50])
            return groups
        except Exception as e:  # noqa: BLE001 - fall back to the directory lookup, and say why
            log.warning("group lookup with the user token failed for %r: %s: %s",
                        email, type(e).__name__, str(e)[:300])
    else:
        log.info("no user token on this request for %r; using the app's directory lookup", email)
    try:
        users = list(from_directory(email))
    except Exception as e:  # noqa: BLE001 - no evidence of membership means no rights
        log.warning("directory lookup by the app's service principal failed for %r: %s: %s",
                    email, type(e).__name__, str(e)[:300])
        return set()
    if not users:
        log.warning("directory lookup found no Databricks user named %r", email)
        return set()
    groups = {name for u in users for name in _names(u.groups)}
    log.info("groups for %r resolved by the app's service principal: %d user record(s), %d group(s): %s",
             email, len(users), len(groups), sorted(groups)[:50])
    return groups
