"""Restore point for the creator wizard (WIZ-1).

A chatbot's answers are saved to the control plane the moment creation starts. If setup stops
part-way, or the browser session is lost, the wizard is refilled from that saved configuration
so the owner can correct an answer and finish, instead of starting again under a new name.
"""
from __future__ import annotations

from .config import EVERYONE, BotConfig

# Setup never finished: creation stopped in the app (draft) or in the background job.
UNFINISHED_STATES = ("draft", "provision_failed")


def answers_from_config(cfg: BotConfig) -> dict:
    """The wizard's answer dictionary for a saved chatbot, opened on the review step."""
    allowed = list(cfg.allowed_principals or [])
    channels = set(cfg.channels or ["chat"])
    reviewer_is_person = "@" in (cfg.reviewer or "")
    return {
        "mode": "advanced", "step": 5, "resume": cfg.bot_id,
        "bot_id": cfg.bot_id, "display_name": cfg.display_name, "purpose": cfg.purpose,
        "owner_user": cfg.owner_user, "owner_group": cfg.owner_group,
        "business_function": cfg.business_function, "access_mode": cfg.access_mode,
        "allowed_principals": allowed,
        "allowed_groups": [p for p in allowed if "@" not in p and p != EVERYONE],
        "allowed_people": [p for p in allowed if "@" in p],
        "channel": "both" if {"chat", "api"} <= channels else next(iter(sorted(channels))),
        "refuse_topics": list(cfg.refuse_topics or []), "answer_style": cfg.answer_style,
        "history_mode": cfg.history_mode, "identity_mode": cfg.identity_mode,
        "sensitivity": cfg.sensitivity, "source_type": cfg.source_type, "source_uri": cfg.source_uri,
        "golden_set_mode": cfg.golden_set_mode, "reviewer": cfg.reviewer,
        "reviewer_group": "" if reviewer_is_person else cfg.reviewer,
        "testers": list(cfg.testers or []), "limit_quotes": cfg.limit_quotes,
        "traffic_alerts": bool((cfg.alert_prefs or {}).get("traffic", True)),
    }
