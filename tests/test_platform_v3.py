"""Unity Gateway, evals on genai.evaluate, lifecycle, sessions, query tags, prompt filling."""
import json

import pytest

from factory import evals, gateway
from factory import tracing as tr
from factory.answering import SYSTEM_PROMPT, fill_system
from factory.lifecycle import TransitionError, check_transition
from factory.memory import merge_history
from factory.sql import WarehouseRunner


def test_model_service_config(settings):
    body = gateway.service_config(settings, "haiku")
    routing = body["config"]["routing"]
    assert routing["destinations"][0]["pay_per_token_config"]["model"] == "models/system.ai.databricks-claude-haiku-4-5"
    assert routing["fallback"]["destinations"], "fallback model configured"
    assert set(body["config"]) == {"routing"}, "rate limits, inference table and policies are set in the UI"


def test_request_tags_and_policy_block():
    h = gateway.request_tags({"bot_id": "b", "release_id": "r1", "channel": "live", "request_id": "q",
                              "synthetic": False})
    tags = json.loads(h["Databricks-Ai-Gateway-Request-Tags"])
    assert tags == {"bot_id": "b", "release_id": "r1", "channel": "live", "request_id": "q", "synthetic": "false"}
    assert gateway.blocked_reason({"databricks_service_policy": {"result": "DENY", "reason": "unsafe"}}) == "unsafe"
    assert gateway.blocked_reason({"databricks_service_policy": {"result": "ALLOW"}}) is None
    assert gateway.blocked_reason({"choices": []}) is None


def test_ui_checklist_lists_limits_table_and_policies(settings):
    items = {i["setting"]: i["value"] for i in gateway.ui_checklist(settings)}
    assert "Policy system.ai.detect_sensitive_data" in items and "Inference table" in items
    assert "· Log ·" in items["Policy system.ai.block_hallucination"]
    assert items["Rate limit (user default)"] == "30 requests per minute"
    assert "evaluator chatbots_test._platform.guardrail_evaluator" in items["Policy system.ai.block_hallucination"]
    assert "evaluator" not in items["Policy system.ai.block_jailbreak"]
    assert gateway.labels(settings) == ["haiku", "evaluator"]


def test_judge_model(settings):
    assert settings.get("models.judge_endpoint") == "databricks-claude-haiku-4-5"


def test_deterministic_scores_and_dataset_records():
    custom = {"outcome": "answered", "answer": 'Per page 2, "Claims over $10,000 need approval" [1].',
              "retrieved_chunk_ids": ["c1"], "citations": [{"n": 1, "chunk_id": "c1",
                                                             "excerpt": "Claims over $10,000 need approval"}],
              "retrieval": [{"rank": 1, "doc_id": "d1", "pages": "2"}]}
    s = evals.deterministic_scores("in_scope", {"response": custom["answer"], "custom": custom},
                                   {"source_doc_id": "d1"}, {"c1": "Claims over $10,000 need approval."}, {"c1"})
    assert s["hit_at_1"] == 1 and s["citation_accuracy"] == 1 and s["leakage"] == 1
    leak = evals.deterministic_scores("in_scope", {"response": "x", "custom": {**custom}}, {}, {}, set())
    assert evals.zero_failures([leak])["leakage"] == 1
    oos = evals.deterministic_scores("out_of_scope", {"response": "no", "custom": {"outcome": "refused"}}, {}, {}, set())
    assert oos["refusal_accuracy"] == 1
    rec = evals.dataset_record({"eval_id": "e", "kind": "out_of_scope", "question": "q"})
    assert rec["inputs"] == {"question": "q"} and rec["expectations"]["guidelines"]


def test_purge_lifecycle():
    check_transition("archived", "deleted")
    check_transition("deleted", "archived")      # restore before purge
    check_transition("deleted", "purged")
    with pytest.raises(TransitionError):
        check_transition("purged", "archived")
    with pytest.raises(TransitionError):
        check_transition("live", "purged")


def test_session_history_merge():
    stored = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    assert merge_history(stored, [{"role": "user", "content": "c"}]) == stored + [{"role": "user", "content": "c"}]
    full = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}, {"role": "user", "content": "z"}]
    assert merge_history(stored, full) == full     # the caller sent history: use it


def test_trace_masking():
    out = tr.mask({"messages": [{"content": "SSN 123-45-6789"}], "n": 3})
    assert "123-45-6789" not in json.dumps(out) and out["n"] == 3


def test_fill_system(cfg):
    text = fill_system(SYSTEM_PROMPT, cfg, ["Do not give legal advice."])
    assert cfg.display_name in text and "- Do not give legal advice." in text and "<<" not in text


def test_warehouse_query_tags():
    class Api:
        def do(self, method, path, body=None, **_):
            self.body = body
            return {"status": {"state": "SUCCEEDED"}, "manifest": {"schema": {"columns": [{"name": "x"}]}},
                    "result": {"data_array": [["1"]]}}

    class W:
        api_client = Api()

    r = WarehouseRunner(W(), "wh", tags={"source": "app"}).tagged(bot_id="claims")
    assert r.query("SELECT 1 AS x") == [{"x": "1"}]
    tags = {t["key"]: t["value"] for t in W.api_client.body["query_tags"]}
    assert tags == {"component": "chatbot-factory", "source": "app", "bot_id": "claims"}


def test_per_bot_options_default(cfg):
    assert cfg.judge_on("correctness") and cfg.judge_on("retrieval_sufficiency")
    assert not cfg.judge_on("multi_turn") and not cfg.judge_on("custom")
    assert cfg.obs_on("link_versions") and not cfg.obs_on("simulate_conversations")
