"""Platform settings and per-bot configuration.

* PlatformSettings: admin-only values (ADM-2). YAML defaults, overridden by rows
  in <catalog>.<platform_schema>.platform_settings.
* BotConfig: what the creator wizard produces (WIZ-3). Persisted as the bot's
  config.yml in its volume and as a row in the bots table.
"""
from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULTS_PATH = Path(__file__).resolve().parent / "platform_defaults.yml"


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _set_path(d: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = value


class PlatformSettings:
    """Read-only view over merged platform settings."""

    def __init__(self, data: dict):
        self._data = data

    @classmethod
    def load(cls, overrides: dict[str, Any] | None = None, path: Path = DEFAULTS_PATH):
        with open(path) as f:
            data = yaml.safe_load(f)
        nested: dict = {}
        for dotted, value in (overrides or {}).items():
            _set_path(nested, dotted, value)
        return cls(_deep_merge(data, nested))

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def as_dict(self) -> dict:
        return copy.deepcopy(self._data)

    # Convenience accessors ------------------------------------------------
    @property
    def catalog(self) -> str:
        return self.get("catalog")

    @property
    def platform_schema(self) -> str:
        return self.get("platform_schema")

    def fq(self, table: str) -> str:
        """Fully qualified platform table name."""
        return f"{self.catalog}.{self.platform_schema}.{table}"

    def answer_service(self, model_label: str) -> str:
        """Full name of the guardrailed Unity Gateway model service for a label (ARC-8)."""
        from .gateway import service_name
        return service_name(self, model_label)

    def thresholds(self) -> dict[str, float]:
        return dict(self.get("quality.go_live_thresholds", {}))


MAX_TESTERS = 10
# Per-bot quality and observability options (admin-only). Built-in judges, version linking,
# issue detection and trace views default on; custom judge, simulator, multi-turn default off.
DEFAULT_JUDGES = {"correctness": True, "groundedness": True, "retrieval_relevance": True,
                  "retrieval_sufficiency": True, "relevance": True, "safety": True,
                  "platform_rules": True, "expectations_guidelines": True,
                  "multi_turn": False, "custom": False}
DEFAULT_OBSERVABILITY = {"link_versions": True, "issue_detection": True, "custom_trace_view": True,
                         "simulate_conversations": False}
DEFAULT_RETRIEVAL = {"query_type": "hybrid", "rerank": True, "num_results": 8, "context_results": 6}


EVERYONE = "account users"  # built-in group of every account user: "everyone at the company" (WIZ-15)


@dataclass
class BotConfig:
    """The answers to the 13 wizard questions plus derived fields."""

    bot_id: str                         # == schema name, immutable (WIZ-6)
    display_name: str
    purpose: str                        # Q2, feeds prompt + valid topics
    owner_user: str                     # Q3 person: primary owner and fallback contact
    owner_group: str                    # Q3 group (GOV-6)
    business_function: str              # Q4 -> function tag
    access_mode: str = "restricted"     # Q5: restricted (UC grants) | middleware (GOV-4, IDN-1)
    allowed_principals: list[str] = field(default_factory=list)  # Q5 groups/people
    channels: list[str] = field(default_factory=lambda: ["chat"])  # Q6: chat | api
    refuse_topics: list[str] = field(default_factory=list)  # Q7: owner's extra topics (empty by default)
    answer_style: str = "short"         # Q8: short | detailed
    history_mode: str = "session"       # CNV-1: off | session | saved
    identity_mode: str = "named"        # IDN-2: named | pseudonymous
    sensitivity: str = "internal"       # Q9: public | internal | confidential
    source_type: str = "upload"         # Q10: upload | git | sharepoint
    source_uri: str = ""                # repo URL or SharePoint path
    sync_schedule: str = ""             # cron for live sources
    golden_set_mode: str = "generate"   # Q12: generate | manual | skip
    reviewer: str = ""                  # Q13 (LCY-4)
    testers: list[str] = field(default_factory=list)  # up to 10 candidate testers (REL-1)
    team: str = ""                      # team tag, derived from owner group
    answer_model: str = "haiku"         # admin-only (ARC-8)
    critical: bool = False              # admin-only (ADM-3)
    limit_quotes: bool = False          # GRD-13; always on for middleware bots
    disabled_rules: list[str] = field(default_factory=list)  # admin-only: platform rules off for this bot
    visual_judge: bool = True           # QA-6, optional per bot
    query_rewrite: bool = False         # RET-2, default off
    retrieval: dict = field(default_factory=lambda: dict(DEFAULT_RETRIEVAL))  # RET-3 winner
    budget_usd: float = 1000.0          # CST-8 monthly ceiling
    budget_action: str = "continue"     # CST-8: continue | pause
    alert_prefs: dict = field(default_factory=lambda: {"traffic": True})  # OBS-3 optional alerts
    git_branch: str = "main"
    judges: dict = field(default_factory=lambda: dict(DEFAULT_JUDGES))          # JDG-2..4
    custom_judge_instructions: str = ""                                          # make_judge, when judges.custom
    observability: dict = field(default_factory=lambda: dict(DEFAULT_OBSERVABILITY))

    def judge_on(self, name: str) -> bool:
        return bool({**DEFAULT_JUDGES, **(self.judges or {})}.get(name))

    def obs_on(self, name: str) -> bool:
        return bool({**DEFAULT_OBSERVABILITY, **(self.observability or {})}.get(name))

    def validate(self) -> list[str]:
        """Return a list of plain-English problems (empty means valid)."""
        problems = []
        if self.access_mode not in {"restricted", "middleware"}:
            problems.append("Access must be 'people in my organization' or 'through another app'.")
        if self.access_mode == "middleware" and self.sensitivity != "public":
            problems.append("Chatbots reached through another app can only use Public documents "
                            "until document-level access is available (GOV-9).")
        if self.access_mode == "restricted" and not self.allowed_principals:
            problems.append("Pick at least one group or person who can use the chatbot.")
        if not self.owner_user or not self.owner_group:
            problems.append("A chatbot needs both an owner person and an owner group.")
        if not self.reviewer:
            problems.append("Choose who approves the chatbot before it goes live.")
        if self.source_type in {"git", "sharepoint"} and not self.source_uri:
            problems.append("Tell us where the documents live (repository or SharePoint link).")
        if self.history_mode not in {"off", "session", "saved"}:
            problems.append("Conversation history must be off, session-only, or saved.")
        if self.identity_mode == "pseudonymous" and self.history_mode == "saved":
            problems.append("Saved conversation history isn't available when identities are "
                            "pseudonymous (CNV-1).")
        if len(self.testers) > MAX_TESTERS:
            problems.append(f"You can invite up to {MAX_TESTERS} testers.")
        if self.budget_action not in {"continue", "pause"}:
            problems.append("Choose what happens when the budget is reached.")
        if self.budget_usd <= 0:
            problems.append("The monthly budget must be more than $0.")
        return problems

    @property
    def dedicated_index(self) -> bool:
        """Confidential bots get their own AI Search index (ARC-4)."""
        return self.sensitivity == "confidential"

    def to_yaml(self) -> str:
        return yaml.safe_dump(asdict(self), sort_keys=False)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_dict(cls, d: dict) -> "BotConfig":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})
