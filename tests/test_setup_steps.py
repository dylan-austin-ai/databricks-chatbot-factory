"""The setup checklist: done, to do, or for a person to confirm. Never an ambiguous empty box."""
from factory.config import PlatformSettings
from factory.setup_steps import CONFIRM, DONE, TODO, setup_steps, summary

HOME = "chatbots_test._platform"


def _settings(**gateway):
    return PlatformSettings.load({"catalog": "chatbots_test", **{f"unity_gateway.{k}": v for k, v in gateway.items()}})


def _by_key(steps):
    return {s.key: s for s in steps}


def test_a_fresh_platform_says_what_is_to_do_and_what_only_a_person_can_confirm():
    steps = _by_key(setup_steps(_settings(), existing=set(), usage_policy=None))
    for key in ("inference_table", "evaluator_inference_table", "trace_table", "genie"):
        assert steps[key].state == TODO and not steps[key].by_hand
    assert "Answer service inference table" in steps["inference_table"].detail      # the field to fill in
    for key in ("inference_table", "evaluator_inference_table", "trace_table"):     # which catalog, by name
        assert "catalog `chatbots_test`, schema `_platform`" in steps[key].detail, key
    assert "Save settings" in steps["inference_table"].detail and steps["inference_table"].guide == "C2 and C5"
    for key in ("usage_policy", "review_queues", "embed_dashboards", "account_budget", "app_scaling"):
        assert steps[key].state == CONFIRM and steps[key].by_hand, key
    assert steps["app_scaling"].optional and not steps["review_queues"].optional
    assert all(s.why and s.guide and s.detail for s in steps.values())
    assert summary(list(steps.values())) == "0 of 8 done, 4 still to do, 4 for you to confirm."    # optional left out


def test_a_recorded_table_is_done_only_if_it_is_in_this_environment_and_exists():
    s = _settings(inference_table=f"{HOME}.gw_answer_payload", evaluator_inference_table="other_cat._platform.gw_eval",
                  trace_table=f"{HOME}.Gateway_Spans")
    steps = _by_key(setup_steps(s, existing={f"{HOME}.gw_answer_payload"}, usage_policy=True))
    assert steps["inference_table"].state == DONE and "gw_answer_payload" in steps["inference_table"].detail
    assert steps["evaluator_inference_table"].state == TODO                       # another environment's table
    assert "not in this environment" in steps["evaluator_inference_table"].detail
    assert steps["trace_table"].state == TODO and "can't find a table" in steps["trace_table"].detail
    assert steps["usage_policy"].state == DONE and not steps["usage_policy"].by_hand

    # when the app couldn't look, a recorded name counts; a trace table may live elsewhere
    unknown = _by_key(setup_steps(_settings(trace_table="main.gateway.spans",
                                            inference_table=f"{HOME}.gw_answer_payload"), existing=None))
    assert unknown["inference_table"].state == DONE and unknown["trace_table"].state == DONE


def test_the_usage_policy_is_checked_when_it_can_be_and_asked_when_it_cannot():
    assert _by_key(setup_steps(_settings(), usage_policy=False))["usage_policy"].state == TODO
    assert "variable-overrides.json" in _by_key(setup_steps(_settings(), usage_policy=False))["usage_policy"].detail
    assert _by_key(setup_steps(_settings(), usage_policy=None))["usage_policy"].state == CONFIRM


def test_a_tick_records_who_confirmed_and_only_counts_for_steps_done_by_hand():
    ticks = {"review_queues": {"by": "ana@corp.com", "on": "2026-10-12"}, "inference_table": {"by": "x", "on": "y"}}
    steps = _by_key(setup_steps(_settings(), existing=set(), usage_policy=True, confirmed=ticks))
    assert steps["review_queues"].state == DONE and "ana@corp.com on 2026-10-12" in steps["review_queues"].detail
    assert steps["inference_table"].state == TODO          # the app checks this one itself; a tick can't override it
    assert steps["embed_dashboards"].state == CONFIRM
    everything = {k: {"by": "a", "on": "d"} for k in ("review_queues", "embed_dashboards", "account_budget")}
    s = _settings(inference_table=f"{HOME}.a", evaluator_inference_table=f"{HOME}.b", trace_table=f"{HOME}.c")
    s._data["observability"]["genie_space_id"] = "g1"
    done = setup_steps(s, existing={f"{HOME}.a", f"{HOME}.b", f"{HOME}.c"}, usage_policy=True, confirmed=everything)
    assert summary(done) == "8 of 8 done." and _by_key(done)["app_scaling"].state == CONFIRM     # optional stays open
