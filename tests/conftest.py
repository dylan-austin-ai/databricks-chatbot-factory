import re

import pytest

from factory.config import BotConfig, PlatformSettings


class FakeSql:
    """Records statements; answers queries from a simple rules list."""

    def __init__(self, answers=None):
        self.statements: list[tuple[str, dict]] = []
        self.answers = answers or []  # list of (regex, rows)

    def execute(self, statement, params=None):
        self.statements.append((statement, params or {}))

    def query(self, statement, params=None):
        self.statements.append((statement, params or {}))
        for pattern, rows in self.answers:
            if re.search(pattern, statement, re.S):
                return rows(params) if callable(rows) else rows
        return []

    def find(self, pattern):
        return [s for s, _ in self.statements if re.search(pattern, s, re.S)]


@pytest.fixture
def settings():
    return PlatformSettings.load({"catalog": "chatbots_test"})


@pytest.fixture
def cfg():
    return BotConfig(
        bot_id="claims_chatbot", display_name="Claims Chatbot",
        purpose="Answers adjusters' questions about claims procedures.",
        owner_user="ana@corp.com", owner_group="claims-team", business_function="Claims",
        allowed_principals=["claims-adjusters"], reviewer="lead@corp.com",
        refuse_topics=["legal advice"],
    )


@pytest.fixture
def fake_sql():
    return FakeSql()
