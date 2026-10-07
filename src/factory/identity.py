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


_PRINCIPAL = re.compile(r"^[A-Za-z0-9._%+@-]{1,320}$")  # emails, user names, application IDs


@dataclass
class Identity:
    user: str                    # the end user (email or principal name)
    source: str                  # obo | middleware
    caller: str                  # the authenticated caller (user or service principal)


class IdentityError(PermissionError):
    pass


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
