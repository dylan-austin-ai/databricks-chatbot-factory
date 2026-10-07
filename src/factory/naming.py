"""Bot naming rules (WIZ-2, WIZ-6, WIZ-7).

The display name is free text and editable. The schema name is derived once,
at creation, and never changes.
"""
from __future__ import annotations

import re
import unicodedata

MAX_SCHEMA_LEN = 60  # leave headroom under UC's 255 for suffixes like _index
RESERVED = {
    "default", "information_schema", "_platform", "platform", "system",
    "admin", "public", "global", "temp", "tmp",
}


class NamingError(ValueError):
    """Raised with a message that can be shown to a non-technical user."""


def normalize(display_name: str) -> str:
    """'Claims Chatbot' -> 'claims_chatbot'.

    Strips accents, lowercases, turns any run of non-alphanumerics into one
    underscore, trims underscores, and prefixes 'bot_' if it starts with a digit.
    """
    if not display_name or not display_name.strip():
        raise NamingError("Please give your chatbot a name.")
    text = unicodedata.normalize("NFKD", display_name)
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    if not text:
        raise NamingError("The name needs at least one letter or number.")
    if text[0].isdigit():
        text = f"bot_{text}"
    return text


def validate(display_name: str, existing_schemas: set[str]) -> str:
    """Return the schema name or raise NamingError with a friendly message."""
    schema = normalize(display_name)
    if len(schema) > MAX_SCHEMA_LEN:
        raise NamingError(
            f"That name is too long. Please keep it under {MAX_SCHEMA_LEN} characters."
        )
    if schema in RESERVED:
        raise NamingError(f"'{display_name}' is reserved. Please choose another name.")
    if schema in {s.lower() for s in existing_schemas}:
        raise NamingError(
            f"A chatbot called '{schema}' already exists (names are compared "
            "ignoring spaces, dashes and capitals). Please choose another name."
        )
    return schema
