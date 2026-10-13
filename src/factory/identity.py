"""End-user identity for the shared endpoint (IDN-1, IDN-2, IDN-3, IDN-5).

* The App and playground call the endpoint on behalf of the signed-in user (OBO).
* Middleware calls the endpoint as a service principal. If that principal is on the
  admin-managed trusted list, it may assert the end user in custom_inputs.end_user.
  Identity in message text, or asserted by an untrusted caller, is ignored.
* Pseudonymous bots log a keyed hash (for joining and analytics) plus an encrypted
  identity that only Security can decrypt as break-glass. Keys live in a secret scope
  only Security can read; the serving endpoint receives them as secret env vars.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass

from .problems import safe_text


_PRINCIPAL = re.compile(r"^[A-Za-z0-9._%+@-]{1,320}$")  # emails, user names, application IDs


@dataclass
class Identity:
    user: str                    # the end user (email or principal name)
    source: str                  # obo | middleware
    caller: str                  # the authenticated caller (user or service principal)


class IdentityError(PermissionError):
    pass


# What anyone may be told when the agent can't tell who is asking. It names nobody and nothing.
NO_CALLER = ("the agent could not get the asking user's credentials from Model Serving (on-behalf-of-user "
             "authorisation), so it cannot tell who is asking and answers nobody")


class CallerUnknown(PermissionError):
    """The agent could not establish who queried the endpoint. `public` is the reason anyone may
    be told: the fixed sentence and the kind of failure, never its message."""

    def __init__(self, detail: str, kind: str = ""):
        super().__init__(f"{NO_CALLER}: {detail}")
        self.public = f"{NO_CALLER} ({kind})" if kind else NO_CALLER


def obo_caller(as_caller, own_id: str = "") -> tuple[str, object]:
    """(who queried the endpoint, a workspace client acting as them).

    `as_caller` builds the client from the on-behalf-of-user token Model Serving hands the agent
    for each request. If that client can't be built, can't say who it is, or turns out to be the
    agent itself (`own_id`: the agent's own application ID), this raises CallerUnknown. It never
    falls back to another identity: access would then be decided for the wrong principal, which
    lets people in as easily as it keeps them out."""
    try:
        client = as_caller()
        me = client.current_user.me()
    except Exception as e:  # noqa: BLE001 - whatever stopped it, nobody is identified
        first = next((line.strip() for line in str(e).splitlines() if line.strip()), "")
        raise CallerUnknown(f"{type(e).__name__}: {safe_text(first, 400)}", type(e).__name__) from e
    name = (me.user_name or (me.emails[0].value if me.emails else "") or me.display_name or "").strip()
    if not name:
        raise CallerUnknown("the credentials it was given carry no user name", "no name")
    if own_id and name.lower() == own_id.strip().lower():
        raise CallerUnknown("the credentials it was given are the agent's own, not the asking user's",
                            "agent's own identity")
    return name, client


def resolve(caller: str, custom_inputs: dict, trusted_callers: list[str]) -> Identity:
    asserted = (custom_inputs or {}).get("end_user")
    if asserted:
        if caller not in set(trusted_callers or []):
            # Untrusted callers can't impersonate anyone; fall back to who they are.
            return Identity(user=caller, source="obo", caller=caller)
        if not isinstance(asserted, str) or not _PRINCIPAL.match(asserted.strip()):
            raise IdentityError("Invalid end_user asserted by middleware.")
        return Identity(user=asserted.strip().lower(), source="middleware", caller=caller)
    if not caller:
        raise IdentityError("The caller could not be identified.")
    return Identity(user=caller.lower(), source="obo", caller=caller)


def platform_callers(settings) -> set[str]:
    """The identities the platform itself calls the agent with, lower-cased: middleware allowed to
    speak for users (access.trusted_callers), the monitor's test callers
    (access.synthetic_callers) and the one identity the jobs run as (access.jobs_run_as), which is
    what the quality check and its diagnostic call with. Nothing else is on this list."""
    named = [*(settings.get("access.trusted_callers") or []), *(settings.get("access.synthetic_callers") or []),
             settings.get("access.jobs_run_as")]
    return {str(n).strip().lower() for n in named if n and str(n).strip()}


def is_platform_caller(caller: str | None, settings) -> bool:
    """Whether the authenticated caller is one of the platform's own. Only such a caller's
    `synthetic` flag is honoured (its questions skip a chatbot's access list and stay out of the
    usage numbers); the same flag from anyone else is ignored and they are authorised as usual."""
    return bool(caller) and str(caller).strip().lower() in platform_callers(settings)


def pseudonym(user: str, key: bytes) -> str:
    """Stable keyed hash: joinable across logs, not reversible without the key."""
    return hmac.new(key, user.lower().encode(), hashlib.sha256).hexdigest()


def encrypt_identity(user: str, key: bytes) -> str | None:
    """Break-glass: Fernet-encrypted identity, decryptable only with Security's key."""
    if not key:
        return None
    from cryptography.fernet import Fernet
    return Fernet(key).encrypt(user.encode()).decode()


def decrypt_identity(token: str, key: bytes) -> str:
    """For Security's break-glass notebook only."""
    from cryptography.fernet import Fernet
    return Fernet(key).decrypt(token.encode()).decode()


def log_fields(identity: Identity, mode: str, hash_key: bytes, enc_key: bytes,
               profile: dict | None) -> dict:
    """What the request log may store for this user (IDN-2, IDN-5)."""
    profile = profile or {}
    fields = {
        "user_hash": pseudonym(identity.user, hash_key) if hash_key else None,
        "user_country": profile.get("country"),
        "user_state": profile.get("state"),
        "user_function": profile.get("business_function"),
        "identity_source": identity.source,
        "identity_mode": mode,
    }
    if mode == "pseudonymous":
        fields.update(user_id=None, user_enc=encrypt_identity(identity.user, enc_key))
    else:
        fields.update(user_id=identity.user, user_enc=None)
    return fields


def active_profile(rows: list[dict], today) -> dict | None:
    """Pick the active, currently valid user_access row (IDN-3)."""
    for r in rows:
        if not r.get("active"):
            continue
        vf, vt = r.get("valid_from"), r.get("valid_to")
        if (vf is None or str(vf) <= str(today)) and (vt is None or str(today) <= str(vt)):
            return r
    return None
