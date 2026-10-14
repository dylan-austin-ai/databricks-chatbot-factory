"""One statement for many rows, independent statements side by side, and saving only what changed."""
import json
import threading
import time

import pytest

from factory import golden
from factory.ingestion import BotPaths
from factory.jobruns import covered_by
from factory.sql import in_list, json_rows, run_lanes, union_by_key


def test_json_rows_carries_a_whole_batch_in_one_parameter():
    source, params = json_rows([{"a": "x", "n": 1, "ok": True, "extra": "ignored"}, {"a": "it's", "n": None, "ok": False}],
                               {"a": "STRING", "n": "INT", "ok": "BOOLEAN"})
    assert source == ("(SELECT inline(from_json(:rows, 'ARRAY<STRUCT<a: STRING, n: INT, ok: BOOLEAN>>', "
                      "map('mode', 'FAILFAST'))))")
    assert json.loads(params["rows"]) == [{"a": "x", "n": 1, "ok": True}, {"a": "it's", "n": None, "ok": False}]
    assert list(params) == ["rows"]


def test_json_rows_refuses_unsafe_names_and_types():
    for schema in ({"a; DROP": "STRING"}, {"a": "STRING); DROP TABLE x; --"}, {"a": "MAP<STRING,STRING>"}):
        with pytest.raises(ValueError):
            json_rows([{}], schema)
    with pytest.raises(ValueError):
        json_rows([{}], {"a": "STRING"}, name="rows; x")
    assert json_rows([{"p": [1, 2]}], {"p": "ARRAY<INT>"})[1] == {"rows": '[{"p": [1, 2]}]'}


class _Runner:
    def __init__(self, parallel, fail_once=()):
        self.parallel, self.log, self.threads, self.fail_once = parallel, [], set(), set(fail_once)
        self.lock = threading.Lock()

    def execute(self, statement, params=None):
        with self.lock:
            self.threads.add(threading.get_ident())
            if statement in self.fail_once:
                self.fail_once.discard(statement)
                raise RuntimeError("concurrent change")
        time.sleep(0.01)
        with self.lock:
            self.log.append(statement)


def test_lanes_keep_their_order_and_run_side_by_side():
    lanes = [[f"a{i}" for i in range(4)], [f"b{i}" for i in range(4)], [f"c{i}" for i in range(4)], []]
    sql = _Runner(parallel=True)
    run_lanes(sql, lanes)
    assert sorted(sql.log) == sorted(s for lane in lanes for s in lane) and len(sql.threads) > 1
    for lane in lanes[:3]:
        assert [s for s in sql.log if s[0] == lane[0][0]] == lane          # order inside a lane is kept


def test_a_spark_session_runs_lanes_one_after_another():
    sql = _Runner(parallel=False)
    run_lanes(sql, [["a0", "a1"], ["b0"]])
    assert sql.log == ["a0", "a1", "b0"] and len(sql.threads) == 1


def test_a_lane_that_stumbles_is_finished_in_order_and_a_real_failure_raises():
    sql = _Runner(parallel=True, fail_once={"a1"})
    run_lanes(sql, [["a0", "a1", "a2"], ["b0", "b1"]])
    assert [s for s in sql.log if s.startswith("a")] == ["a0", "a1", "a2"] and sorted(sql.log)[-2:] == ["b0", "b1"]

    class Broken(_Runner):
        def execute(self, statement, params=None):
            if statement == "a1":
                raise RuntimeError("no permission")
            super().execute(statement, params)

    with pytest.raises(RuntimeError, match="no permission"):
        run_lanes(Broken(parallel=True), [["a0", "a1"], ["b0"]])


def test_audit_many_is_one_statement(settings, fake_sql):
    from factory.controlplane import ControlPlane

    ControlPlane(fake_sql, settings).audit_many("ana@corp.com", "claims_chatbot", "doc_approved",
                                               [("d1", None), ("d2", {"reason": "x"})])
    (stmt, params), = fake_sql.statements
    assert stmt.startswith("INSERT INTO chatbots_test._platform.audit_log SELECT id, current_timestamp(), :actor,")
    rows = json.loads(params["rows"])
    assert [(r["target"], r["detail"]) for r in rows] == [("d1", "{}"), ("d2", '{"reason": "x"}')]
    assert len({r["id"] for r in rows}) == 2 and params["action"] == "doc_approved"
    ControlPlane(fake_sql, settings).audit_many("a", None, "x", [])
    assert len(fake_sql.statements) == 1


# --- test questions: only what changed is written -----------------------------------------------

LOADED = [{"eval_id": "e1", "question": "How do I enroll?", "expected_answer": "Click Benefits.", "kind": "in_scope", "approved": False},
          {"eval_id": "e2", "question": "What is the limit?", "expected_answer": "$500", "kind": "in_scope", "approved": True},
          {"eval_id": "e3", "question": "Give me legal advice", "expected_answer": "", "kind": "out_of_scope", "approved": False}]


def _edited(**changes_by_id):
    rows = [{**r, "kind": golden.KIND_LABEL[r["kind"]], "delete": False} for r in LOADED]
    for r in rows:
        r.update(changes_by_id.get(r["eval_id"], {}))
    return rows


def test_nothing_changed_means_nothing_to_write(settings):
    delta = golden.changes(LOADED, _edited())
    assert delta == {"add": [], "update": [], "remove": []} and golden.counts(delta) == "nothing to save"
    assert golden.save_statements(BotPaths(settings, "claims_chatbot"), delta, "ana@corp.com") == []


def test_only_the_differences_are_saved(settings):
    edited = _edited(e1={"approved": True}, e3={"delete": True})
    edited.append({"eval_id": float("nan"), "question": " What about dental? ", "expected_answer": float("nan"),
                   "kind": "Should answer", "approved": True, "delete": False})
    edited.append({"eval_id": None, "question": "", "expected_answer": "", "kind": None, "approved": False})   # blank row
    delta = golden.changes(LOADED, edited)
    assert delta["add"] == [{"question": "What about dental?", "expected_answer": "", "kind": "in_scope", "approved": True,
                             "source_doc_id": None}]
    assert delta["update"] == [{"eval_id": "e1", "question": "How do I enroll?", "expected_answer": "Click Benefits.",
                                "kind": "in_scope", "approved": True, "source_doc_id": None}]
    assert delta["remove"] == ["e3"] and golden.counts(delta) == "1 added, 1 changed, 1 removed"
    statements = golden.save_statements(BotPaths(settings, "claims_chatbot"), delta, "ana@corp.com")
    assert [s.split()[0] for s, _ in statements] == ["INSERT", "MERGE", "UPDATE"]      # three, whatever the size
    assert all(p["u"] == "ana@corp.com" for _, p in statements)
    assert statements[2][0].endswith("WHERE eval_id IN (:e0)") and statements[2][1]["e0"] == "e3"   # a plain list


def test_a_row_removed_from_the_table_is_removed_and_unknown_rows_are_left_alone():
    edited = [r for r in _edited() if r["eval_id"] != "e2"] + [
        {"eval_id": "someone-elses", "question": "x", "expected_answer": "", "kind": "Should answer", "approved": True}]
    delta = golden.changes(LOADED, edited)
    assert delta == {"add": [], "update": [], "remove": ["e2"]}


def test_a_question_cannot_be_blanked_by_an_edit():
    assert golden.changes(LOADED, _edited(e1={"question": "   "}))["update"] == []


# --- one statement across chatbots, and no duplicate job runs ---------------------------------

def test_union_by_key_tags_each_row_with_its_chatbot():
    statement, params = union_by_key({"a_bot": "SELECT 1 AS n", "b_bot": "SELECT 2 AS n"})
    assert statement == ("SELECT :k0 AS bot_id, x.* FROM (SELECT 1 AS n) x\nUNION ALL\n"
                         "SELECT :k1 AS bot_id, x.* FROM (SELECT 2 AS n) x")
    assert params == {"k0": "a_bot", "k1": "b_bot"}
    with pytest.raises(ValueError):
        union_by_key({"a": "SELECT 1"}, key="bot id")


def _run(run_id, state, new_shape=True, **params):
    from types import SimpleNamespace as NS

    values = [NS(name=k, value=v, default=None) for k, v in params.items()]
    if new_shape:
        return NS(run_id=run_id, status=NS(state=NS(value=state)), state=None, job_parameters=values)
    return NS(run_id=run_id, status=None, state=NS(life_cycle_state=NS(value=state)), job_parameters=values)


def test_a_request_is_covered_only_by_an_identical_run_that_has_not_started():
    want = {"catalog": "qa", "bot_id": "claims_chatbot", "publish_only": "true"}
    assert covered_by([], want) is None
    assert covered_by([_run(1, "RUNNING", **want)], want) is None                      # may have read old data
    assert covered_by([_run(2, "QUEUED", **want)], want) == 2
    assert covered_by([_run(3, "PENDING", new_shape=False, **want)], want) == 3        # older API shape
    assert covered_by([_run(4, "QUEUED", **{**want, "bot_id": "hr_chatbot"})], want) is None
    assert covered_by([_run(5, "QUEUED", catalog="qa", bot_id="claims_chatbot")], want) is None   # a full ingest
    # a queued run that does more than was asked (re-reads documents) is a different job
    assert covered_by([_run(6, "QUEUED", **want, reparse="true")], want) is None
    assert covered_by([_run(7, "QUEUED", **want, reparse="false", doc_ids="")], want) == 7
    assert covered_by([_run(8, "TERMINATED", **want), _run(9, "QUEUED", **want)], want) == 9


def test_empty_checkbox_cells_are_not_ticks():
    nan = float("nan")
    edited = _edited(e1={"approved": nan, "delete": nan}, e2={"delete": 1})
    edited.append({"eval_id": None, "question": "Typed then removed", "expected_answer": "", "kind": "Should answer",
                   "approved": nan, "delete": True})
    edited.append({"eval_id": None, "question": "New one", "expected_answer": "", "kind": "Should answer",
                   "approved": nan, "delete": nan})
    delta = golden.changes(LOADED, edited)
    assert delta["update"] == [] and delta["remove"] == ["e2"]
    assert delta["add"] == [{"question": "New one", "expected_answer": "", "kind": "in_scope", "approved": False,
                             "source_doc_id": None}]


def test_in_list_is_a_plain_parameter_list():
    assert in_list(["a", "b"], "d") == ("(:d0, :d1)", {"d0": "a", "d1": "b"})
    with pytest.raises(ValueError):
        in_list(["a"], "d; DROP")
