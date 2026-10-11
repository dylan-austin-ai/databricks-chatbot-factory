"""MLOps-only admin (ADM-2..8, ARC-8, MON-6, CAS-6/7, IDN-1/3, LCY-6/7, JDG-2..4, PRM-2, GW-2).

Platform level: settings, reports, costs, the answer prompt, the setup checklist, user access,
alerts and the audit log cover every chatbot. Two sections work on one chatbot at a time:
Chatbots (its options) and Lifecycle (archive, restore, delete).

One section is open at a time, so only that section's data is loaded.
"""
import io
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st
import yaml

from common import (JOB_IDS, STATE_LABEL, all_bots, app_client, bot_config, cp, current_user, forget, fresh_sql,
                    is_admin, run_job, settings, soft, sql)
from factory.explain import help_text
from factory.config import DEFAULT_JUDGES, DEFAULT_OBSERVABILITY
from factory.gateway import ui_checklist
from factory.lifecycle import State, TransitionError, retire_path
from factory.provisioning import Provisioner
from factory.sql import json_rows

if not is_admin():
    st.error("This page is for MLOps admins only.")
    st.stop()

st.title("Admin")
s, user, control = settings(), current_user(), cp()
SECTIONS = {"Platform": "Settings that apply to every chatbot.",
            "Chatbots": "Options for one chatbot: budget, answer model, judges, platform rules.",
            "Lifecycle": "Archive, restore or delete a chatbot.",
            "Reports": "Usage, quality and cost for every chatbot, and idle chatbots.",
            "Costs": "Estimated cost next to what Databricks recorded and billed.",
            "Answer prompt": "The prompt every chatbot answers with, and its optimizer.",
            "Setup checklist": "Steps that have to be done in Databricks screens.",
            "User access": "Load the table that says which users may see what.",
            "Alerts": "Open alerts for every chatbot and the platform.",
            "Audit log": "Who did what, and when."}
section = st.segmented_control("Section", list(SECTIONS), default="Platform", key="admin_section") or "Platform"
st.caption(SECTIONS[section])
JUDGE_LABEL = {"correctness": "Correct (vs expected answer)", "groundedness": "Sticks to sources",
               "retrieval_relevance": "Search results relevant", "retrieval_sufficiency": "Search found enough",
               "relevance": "Answers the question", "safety": "Safe", "platform_rules": "Follows platform rules",
               "expectations_guidelines": "Refuses out-of-scope questions properly",
               "multi_turn": "Conversation judges (multi-turn)", "custom": "Custom judge (make_judge)"}
OBS_LABEL = {"link_versions": "Link traces to release versions (MLflow LoggedModel)",
             "issue_detection": "Automatic issue detection (run from the MLflow UI)",
             "custom_trace_view": "Custom trace view (create in the MLflow UI)",
             "simulate_conversations": "Simulated multi-turn conversations in quality checks"}


def provisioner() -> Provisioner:
    return Provisioner(fresh_sql(), s, control, app_client(), ingest_job_id=JOB_IDS.get("ingest"))


def frame(query: str, params: dict | None = None) -> pd.DataFrame:
    try:
        return pd.DataFrame(sql().query(query, params or {}))
    except Exception as e:  # noqa: BLE001 - optional views (system tables) may not exist yet
        soft("This table", e)
        return pd.DataFrame()


def ask_first(where, label: str, key: str, text: str, yes: str) -> bool:
    """A button that asks before it acts: opens a small box saying what will happen."""
    with where.popover(label):
        st.write(text)
        return st.button(yes, key=key, type="primary")


if section == "Platform":  # Platform ------------------------------------------------------------------
    current = s.as_dict()
    current.pop("catalog", None)

    def save(new: dict) -> None:
        if new["models"]["default_answer_model"] not in new["models"]["answer_services"]:
            raise ValueError("The answer model must be one of the Unity Gateway answer services.")
        changed = [k for k in new if new[k] != current.get(k)]
        for k in changed:
            control.set_setting(k, new[k], user)
        forget(settings_too=True)
        if changed:
            run_job("evals", bot_id="all", trigger="settings_changed")  # EVL-6
        st.success(f"Saved {len(changed)} section(s): {', '.join(changed) or 'none'}. "
                   "The agent picks them up within a minute; all chatbots are being re-checked.")

    # ADM-11: the everyday settings as a form; everything else in the YAML editor below.
    FIELDS = [  # (section heading, dotted key, label, kind)
        ("Go-live targets", "quality.go_live_thresholds.hit_at_1", "Right document first", "pct"),
        ("Go-live targets", "quality.go_live_thresholds.hit_at_3", "In top 3", "pct"),
        ("Go-live targets", "quality.go_live_thresholds.hit_at_5", "In top 5", "pct"),
        ("Go-live targets", "quality.go_live_thresholds.mrr", "Overall ranking", "pct"),
        ("Go-live targets", "quality.go_live_thresholds.correctness", "Correct", "pct"),
        ("Go-live targets", "quality.go_live_thresholds.groundedness", "Sticks to sources", "pct"),
        ("Go-live targets", "quality.go_live_thresholds.relevance", "Answers the question", "pct"),
        ("Go-live targets", "quality.go_live_thresholds.citation_accuracy", "Citations accurate", "pct"),
        ("Go-live targets", "quality.go_live_thresholds.refusal_accuracy", "Refuses correctly", "pct"),
        ("Go-live targets", "quality.min_golden_questions", "Approved test questions required", "int"),
        ("Go-live targets", "quality.judge_sample_rate", "Production judging sample", "pct"),
        ("Monitoring", "monitoring.latency_p50_s", "Typical answer (p50), seconds", "float"),
        ("Monitoring", "monitoring.latency_p95_s", "Slow answer (p95), seconds", "float"),
        ("Monitoring", "monitoring.latency_p99_s", "Slowest (p99), seconds", "float"),
        ("Monitoring", "monitoring.ttft_p95_s", "First token (p95), seconds", "float"),
        ("Monitoring", "monitoring.error_rate_max", "Error rate (15 minutes)", "pct"),
        ("Monitoring", "monitoring.question_drift_alert", "Question drift alert level", "float"),
        ("Monitoring", "monitoring.expiry_lead_days", "Expiry warning, days", "int"),
        ("Monitoring", "monitoring.alert_emails_mlops", "MLOps alert emails (comma separated)", "list"),
        ("Models and cost", "models.default_answer_model", "Answer model", "answer"),
        ("Models and cost", "models.judge_endpoint", "Judge model endpoint", "text"),
        ("Models and cost", "models.generation_endpoint", "Test-question writer endpoint", "text"),
        ("Models and cost", "pricing.answer_input_per_mtok", "Answer input $ per 1M tokens", "float"),
        ("Models and cost", "pricing.answer_output_per_mtok", "Answer output $ per 1M tokens", "float"),
        ("Models and cost", "pricing.judge_input_per_mtok", "Judge input $ per 1M tokens", "float"),
        ("Models and cost", "pricing.judge_output_per_mtok", "Judge output $ per 1M tokens", "float"),
        ("Access and retention", "access.admin_group", "Admins group", "text"),
        ("Access and retention", "access.security_group", "Security group", "text"),
        ("Access and retention", "access.support_group", "Support group", "text"),
        ("Access and retention", "access.trusted_callers", "Trusted apps (may say who the user is)", "list"),
        ("Access and retention", "access.synthetic_callers", "Synthetic test callers", "list"),
        ("Access and retention", "access.app_service_principal", "App service principal (application ID)", "text"),
        ("Access and retention", "access.jobs_run_as", "Jobs run-as (email or application ID)", "text"),
        ("Access and retention", "retention_years", "Keep logs for, years", "int"),
        ("Access and retention", "idle_days_before_flag", "Flag idle chatbots after, days", "int"),
        ("Access and retention", "soft_delete_retention_days", "Purge deleted chatbots after, days", "int"),
        ("Unity Gateway tables (after setup)", "unity_gateway.inference_table", "Answer service inference table", "text"),
        ("Unity Gateway tables (after setup)", "unity_gateway.evaluator_inference_table", "Evaluator inference table", "text"),
        ("Unity Gateway tables (after setup)", "unity_gateway.trace_table", "Unified trace table", "text"),
    ]
    with st.form("platform_form"):
        values, section = {}, None
        cols = None
        for i, (head, key, label, kind) in enumerate(FIELDS):
            if head != section:
                section, n = head, 0
                st.subheader(head)
                cols = st.columns(3)
            v, col = s.get(key), cols[n % 3]
            n += 1
            if kind == "pct":
                values[key] = col.number_input(f"{label} (%)", 0.0, 100.0, float(v or 0) * 100, 1.0, key=key) / 100
            elif kind == "int":
                values[key] = int(col.number_input(label, 0, 100000, int(v or 0), key=key))
            elif kind == "float":
                values[key] = col.number_input(label, 0.0, 100000.0, float(v or 0), key=key)
            elif kind == "list":
                values[key] = [x.strip() for x in col.text_input(label, ", ".join(v or []), key=key).split(",") if x.strip()]
            elif kind == "answer":
                opts = list(s.get("models.answer_services", {}))
                values[key] = col.selectbox(label, opts, opts.index(v) if v in opts else 0, key=key)
            else:
                values[key] = col.text_input(label, v or "", key=key)
        if st.form_submit_button("Save settings", type="primary"):
            import copy
            new = copy.deepcopy(current)
            for key, val in values.items():
                node, parts = new, key.split(".")
                for part in parts[:-1]:
                    node = node.setdefault(part, {})
                node[parts[-1]] = val
            try:
                save(new)
            except Exception as e:  # noqa: BLE001
                st.error(f"Not saved: {e}")
    with st.expander("All settings (YAML, for anything not in the form)"):
        text = st.text_area("Platform settings (YAML)", yaml.safe_dump(current, sort_keys=False), height=600)
        if st.button("Save YAML"):
            try:
                save(yaml.safe_load(text))
            except Exception as e:  # noqa: BLE001
                st.error(f"Not saved: {e}")
    st.subheader("Re-index after a parser or chunker change")
    c1, c2 = st.columns(2)
    parser = c1.selectbox("Affected parser", ["all", "ai_parse_document", "text_reader"])
    exts = c2.text_input("Only these file types (optional, e.g. md,txt)")
    if st.button("Re-index affected documents now"):
        run_job("reindex", parser=parser, extensions=exts)
        st.success("Re-index started.")

if section == "Chatbots":  # Chatbots ------------------------------------------------------------------
    models = list(s.get("models.answer_services"))
    listed = {b["bot_id"]: b for b in all_bots() if b["state"] not in ("deleted", "purged")}
    chosen = st.selectbox("Chatbot", list(listed), index=None, placeholder="Choose a chatbot to configure",
                          format_func=lambda k: f"{listed[k]['display_name']} · "
                                                f"{STATE_LABEL.get(listed[k]['state'], listed[k]['state'])}")
    for b in [listed[chosen]] if chosen else []:
        cfg = bot_config(b)
        with st.container(border=True):
            k = b["bot_id"]
            c1, c2 = st.columns(2)
            budget = c1.number_input("Monthly budget ($)", 0, 1_000_000, int(cfg.budget_usd), step=50, key=f"bu_{k}",
                                     help="Set by MLOps; owners don't see costs. Alerts at 70/90/100% go to MLOps.")
            action = c2.selectbox("When the budget is reached", ["continue", "pause"], ["continue", "pause"].index(
                cfg.budget_action), key=f"ba_{k}", format_func={"continue": "Keep running, alert MLOps",
                                                                 "pause": "Pause until the 1st"}.get)
            crit = st.checkbox("Critical (100% production judging)", cfg.critical, key=f"c_{k}")
            model = st.selectbox("Answer model", models, key=f"m_{k}",
                                 index=models.index(cfg.answer_model) if cfg.answer_model in models else 0)
            vis = st.checkbox("Visual extraction check", cfg.visual_judge, key=f"v_{k}")
            rules = s.get("guardrails.platform_rules", {})
            off = st.multiselect("Platform rules turned OFF for this chatbot", list(rules),
                                 [r for r in cfg.disabled_rules if r in rules], key=f"o_{k}",
                                 format_func=lambda r: rules[r])
            rewrite = st.checkbox("Rewrite follow-up questions with the LLM", cfg.query_rewrite, key=f"r_{k}")
            judges = st.multiselect("Judges", list(DEFAULT_JUDGES), [j for j in DEFAULT_JUDGES if cfg.judge_on(j)],
                                    key=f"j_{k}", format_func=JUDGE_LABEL.get,
                                    help="Quality checks and production judging. Custom, conversation judges "
                                         "and simulation are off by default.")
            custom = st.text_area("Custom judge instructions (make_judge)", cfg.custom_judge_instructions, key=f"cj_{k}",
                                  placeholder="e.g. The answer must state the claim limit in dollars when asked about limits.") \
                if "custom" in judges else cfg.custom_judge_instructions
            obs = st.multiselect("Observability", list(DEFAULT_OBSERVABILITY),
                                 [o for o in DEFAULT_OBSERVABILITY if cfg.obs_on(o)], key=f"ob_{k}",
                                 format_func=OBS_LABEL.get)
            pin = st.text_input("Pin to platform version (temporary)", b.get("platform_pin") or "", key=f"p_{k}")
            days = st.number_input("Pin expires in (days)", 1, 30, 7, key=f"d_{k}")
            if st.button("Save", key=f"s_{k}"):
                cfg.critical, cfg.answer_model, cfg.visual_judge, cfg.query_rewrite = crit, model, vis, rewrite
                cfg.budget_usd, cfg.budget_action = float(budget or 1), action
                cfg.disabled_rules, cfg.custom_judge_instructions = off, custom
                cfg.judges = {j: j in judges for j in DEFAULT_JUDGES}
                cfg.observability = {o: o in obs for o in DEFAULT_OBSERVABILITY}
                control.save_config(cfg, user, "admin update")
                if pin:
                    sql().execute(f"UPDATE {s.fq('bots')} SET platform_pin = :p, pin_expires_at = "
                                  "CAST(:e AS TIMESTAMP) WHERE bot_id = :b",
                                  {"p": pin, "e": (datetime.utcnow() + timedelta(days=int(days))).isoformat(), "b": k})
                    control.audit(user, k, "platform_pinned", pin, {"days": int(days)})
                run_job("ingest", bot_id=k, publish_only="true")  # stage as a candidate (REL-5)
                st.success("Saved. Answer changes go to the test version; judge and observability "
                           "options apply from the next check and the next question.")

if section == "Lifecycle":  # Lifecycle (LCY-6/7) -------------------------------------------------------
    keep = int(s.get("soft_delete_retention_days", 90))
    st.caption("**Archive** stops a chatbot answering and keeps everything; it can be restored. **Delete** also "
               f"hides it, and its data is removed automatically after {keep} days; until then it can be restored. "
               "**Delete completely** removes its documents, sections, test questions and search entries now and "
               "can't be undone. Audit history, request logs and traces are always kept for 7 years.")

    def retire(bot_id: str, state: str, target: str) -> None:
        """Archive or delete from any state, through the allowed state changes."""
        for step in retire_path(state, target):
            control.set_state(bot_id, step, user)
        if target == State.DELETED.value:
            provisioner().remove_trigger(bot_id)

    everything = all_bots(include_deleted=True)
    groups = {"Active": [b for b in everything if b["state"] not in ("archived", "deleted", "purged")],
              "Archived": [b for b in everything if b["state"] == "archived"],
              "Deleted": [b for b in everything if b["state"] == "deleted"],
              "Removed completely": [b for b in everything if b["state"] == "purged"]}
    view = st.segmented_control("Show", list(groups), default="Active", key="lifecycle_view",
                                format_func=lambda g: f"{g} ({len(groups[g])})") or "Active"
    if not groups[view]:
        st.info("No chatbots here.")
    for b in groups[view]:
        k, state = b["bot_id"], b["state"]
        c1, c2, c3 = st.columns([4, 2, 2])
        left = ""
        if state == "deleted" and b.get("deleted_at"):
            deleted = pd.to_datetime(b["deleted_at"], utc=True)
            left = f" · removed completely in {max(0, keep - (datetime.now(timezone.utc) - deleted).days)} days"
        c1.markdown(f"**{b['display_name']}** `{k}`  \n{b['owner_user']} · {STATE_LABEL.get(state, state)}{left}")
        if state == "purged":
            c2.caption("Record kept for audit")
            continue
        if view == "Active":
            try:
                retire_path(state, State.ARCHIVED.value)
                if ask_first(c2, "Archive", f"ar2_{k}", f"{b['display_name']} stops answering questions straight "
                           "away. Everything is kept and it can be restored.", "Yes, archive it"):
                    retire(k, state, State.ARCHIVED.value)
                    st.rerun()
            except TransitionError:  # never finished setup: nothing to keep answering from
                c2.caption("Not set up: delete only")
            if ask_first(c3, "Delete", f"de2_{k}", f"{b['display_name']} stops answering and is hidden from its "
                       f"owners. It can be restored for {keep} days; after that its data is removed.",
                       "Yes, delete it"):
                retire(k, state, State.DELETED.value)
                st.rerun()
        elif state == "archived":
            if c2.button("Restore", key=f"ra_{k}"):
                control.set_state(k, State.TESTING.value, user)
                provisioner().create_trigger(control.get_config(k))
                st.rerun()
            if ask_first(c3, "Delete", f"de_{k}", f"{b['display_name']} is hidden from its owners. It can be "
                       f"restored for {keep} days; after that its data is removed.", "Yes, delete it"):
                retire(k, state, State.DELETED.value)
                st.rerun()
        elif state == "deleted":
            if c2.button("Restore", key=f"rd_{k}"):
                control.set_state(k, State.ARCHIVED.value, user)
                st.rerun()
        with st.expander(f"Delete {b['display_name']} completely"):
            st.write("Removes this chatbot's documents, sections, test questions and search entries now. "
                     "This can't be undone.")
            confirm = st.text_input("Type the technical name to confirm", key=f"pc_{k}", placeholder=k)
            if st.button("Delete completely", key=f"pu_{k}", type="primary", disabled=confirm != k):
                retire(k, state, State.DELETED.value)
                run_job("maintenance", purge_bot_id=k, actor=user)
                control.audit(user, k, "purge_requested", k)
                st.success("Deleting. It moves to **Removed completely** when the cleanup job finishes.")

if section == "Reports":  # Reports ---------------------------------------------------------------------
    days = st.segmented_control("Period", [7, 30, 90], default=30, format_func=lambda d: f"{d} days", key="rep") or 30
    rep = frame(f"""
        SELECT b.bot_id, b.display_name, b.state, b.owner_user, b.owner_group, b.created_at,
               r.last_question, coalesce(r.questions, 0) AS questions, coalesce(r.cost_usd, 0) AS cost_usd,
               r.answered_rate, r.p95_latency_ms, r.p95_ttft_ms, q.judge_pass_rate, coalesce(al.open_alerts, 0) AS open_alerts
        FROM {s.fq('bots')} b
        LEFT JOIN (SELECT bot_id, max(ts) AS last_question, count(*) AS questions, sum(cost_usd) AS cost_usd,
                          avg(CASE WHEN outcome = 'answered' THEN 1.0 ELSE 0.0 END) AS answered_rate,
                          percentile_approx(latency_ms, 0.95) AS p95_latency_ms, percentile_approx(ttft_ms, 0.95) AS p95_ttft_ms
                   FROM {s.fq('request_log')} WHERE channel = 'live' AND NOT synthetic
                     AND ts >= current_timestamp() - INTERVAL {int(days)} DAYS GROUP BY bot_id) r USING (bot_id)
        LEFT JOIN (SELECT bot_id, avg(value) AS judge_pass_rate FROM {s.fq('judge_results')}
                   WHERE ts >= current_timestamp() - INTERVAL {int(days)} DAYS GROUP BY bot_id) q USING (bot_id)
        LEFT JOIN (SELECT bot_id, count(*) AS open_alerts FROM {s.fq('v_alerts_open')} GROUP BY bot_id) al USING (bot_id)
        WHERE b.state <> 'purged' ORDER BY questions DESC""")
    if not rep.empty:
        num = rep[["questions", "cost_usd"]].apply(pd.to_numeric, errors="coerce")
        c = st.columns(4)
        c[0].metric("Chatbots", len(rep))
        c[1].metric("Live", int((rep["state"] == "live").sum()))
        c[2].metric(f"Questions ({days} days)", f"{int(num['questions'].sum()):,}")
        c[3].metric(f"Answer cost ({days} days)", f"${num['cost_usd'].sum():,.2f}")
        st.dataframe(rep, hide_index=True, width="stretch")
        st.download_button("Download CSV", rep.to_csv(index=False), file_name=f"chatbots_{days}d.csv")
        idle_days = int(s.get("idle_days_before_flag", 45))
        idle = rep[(rep["state"] == "live") & (pd.to_datetime(rep["last_question"], utc=True, errors="coerce").isna()
                   | (pd.to_datetime(rep["last_question"], utc=True, errors="coerce")
                      < datetime.now(timezone.utc) - timedelta(days=idle_days)))]
        if not idle.empty:
            st.subheader(f"Idle for {idle_days}+ days")
            for _, r in idle.iterrows():
                c1, c2 = st.columns([5, 1])
                c1.write(f"**{r['display_name']}** · {r['owner_user']} · last question: {r['last_question'] or 'never'}")
                if ask_first(c2, "Archive", f"ar_{r['bot_id']}", f"{r['display_name']} stops answering questions "
                           "straight away. Everything is kept and it can be restored.", "Yes, archive it"):
                    control.set_state(r["bot_id"], State.ARCHIVED.value, user)
                    st.rerun()
    st.subheader("Maintenance")
    if st.button("Run nightly maintenance now (drift checks, idle flags, purges, retention)"):
        run_job("maintenance")
        st.success("Maintenance started.")

if section == "Costs":  # Costs (CST-9..12) -------------------------------------------------------------
    st.caption("Estimated answer cost per bot (from tokens) next to the tokens Unity Gateway recorded, "
               "warehouse work per bot (query tags) and billed platform spend by tag (system tables).")
    st.markdown("**Estimate vs Unity Gateway usage**")
    st.dataframe(frame(f"SELECT * FROM {s.fq('v_cost_reconciliation')} WHERE day >= current_date() - 30 "
                       "ORDER BY day DESC, bot_id"), hide_index=True, width="stretch")
    st.markdown("**SQL warehouse work by bot**")
    st.dataframe(frame(f"SELECT * FROM {s.fq('v_warehouse_by_bot')} WHERE day >= current_date() - 30 "
                       "ORDER BY day DESC"), hide_index=True, width="stretch")
    st.markdown("**Billed spend by tag**")
    st.dataframe(frame(f"SELECT * FROM {s.fq('v_cost_by_tag')} WHERE usage_date >= current_date() - 30 "
                       "ORDER BY usage_date DESC"), hide_index=True, width="stretch")

if section == "Answer prompt":  # Answer prompt (PRM-1/2) --------------------------------------------------------
    st.subheader("Answer prompt", help=help_text("prompt_optimizer"))
    st.caption("The answer prompt lives in the MLflow Prompt Registry; the agent serves the @production "
               "version. Optimization rewrites it against approved test questions (GEPA) into @optimized; "
               "promoting it re-checks every chatbot.")
    st.dataframe(frame(f"SELECT * FROM {s.fq('prompt_optimizations')} ORDER BY ts DESC LIMIT 20"),
                 hide_index=True, width="stretch")
    c1, c2 = st.columns(2)
    if c1.button("Optimize the answer prompt", help=help_text("prompt_optimizer")):
        run_job("optimize", action="optimize", actor=user)
        st.success("Optimization started; results appear here when it finishes.")
    if c2.button("Promote @optimized to production", type="primary"):
        run_job("optimize", action="promote", actor=user)  # the job re-checks every bot
        st.success("Promoted. Every chatbot is being re-checked.")

if section == "Setup checklist":  # Setup checklist: Databricks steps with no API today ---------------------------
    st.caption("These steps are done in Databricks screens. docs/SETUP.md has click-by-click steps.")
    st.markdown("**Unity Gateway model service settings.** Set on each model service in AI Gateway "
                "(rate limits, inference table, guardrail policies with phase and mode):")
    st.dataframe(pd.DataFrame(ui_checklist(s)), hide_index=True, width="stretch")
    obs = s.get("observability", {})
    items = [
        ("Put the evaluator's inference table name in unity_gateway.evaluator_inference_table (every "
         "Log-mode policy verdict in v_guardrail_verdicts)",
         bool(s.get("unity_gateway.evaluator_inference_table"))),
        ("Turn on the MLflow Review Queues preview (workspace admin › Previews)", None),
        ("Put the answer service's inference table name in unity_gateway.inference_table (every "
         "gateway call in v_gateway_calls)", bool(s.get("unity_gateway.inference_table"))),
        ("Enable the unified Unity Gateway trace table (AI Gateway › Govern › Traces) and put its name "
         "in unity_gateway.trace_table", bool(s.get("unity_gateway.trace_table"))),
        ("Allow dashboard embedding for *.databricksapps.com (workspace admin › Security › Embed dashboards)", None),
        ("Create a serverless usage policy and pass its ID as the usage_policy_id bundle variable", None),
        ("Account admin: create the platform budget (account console › Usage › Budgets, filter tag "
         "component=chatbot-factory, alerts at 70/90/100%)", None),
        ("Set the app's instance count (1-5) in Apps › chatbot-factory › Compute for horizontal scaling", None),
        ("Genie agent over observability tables (created by setup_observability)", bool(obs.get("genie_space_id"))),
    ]
    for text, done in items:
        st.markdown(f"{'✅' if done else '☐'} {text}")
    st.markdown("**Per-chatbot MLflow UI steps** (issue detection and custom trace views have no API):")
    for b in all_bots():
        cfg = bot_config(b)
        todo = [OBS_LABEL[o] for o in ("issue_detection", "custom_trace_view") if cfg.obs_on(o)]
        if todo:
            st.markdown(f"- **{b['display_name']}**: {'; '.join(todo)}. Filter traces with "
                        f"`tags.bot_id = '{b['bot_id']}'`.")

if section == "User access":  # User access (IDN-3) ------------------------------------------------------------
    st.write("Upload a CSV with columns: user_id, country, state, business_function, security_scopes "
             "(semicolon-separated), active, valid_from, valid_to. Rows replace existing ones per user.")
    f = st.file_uploader("user_access.csv", type=["csv"])
    if f and st.button("Load"):
        df = pd.read_csv(io.BytesIO(f.getvalue()), dtype=str).fillna("")
        df.columns = [c.strip().lower() for c in df.columns]
        if "user_id" not in df.columns or not df["user_id"].str.strip().all():
            st.error("Nothing was loaded. The file needs a **user_id** column with a value on every row. "
                     f"Columns found: {', '.join(df.columns) or 'none'}.")
        else:
            # The whole file in two statements (replace, then insert), however many rows it has.
            COLS = ["user_id", "country", "state", "business_function", "security_scopes", "active", "valid_from",
                    "valid_to"]
            rows = [{**{c: r.get(c) for c in COLS}, "user_id": r["user_id"].strip().lower(),
                     "security_scopes": r.get("security_scopes", ""), "active": r.get("active") or "true"}
                    for r in df.to_dict("records")]
            source, params = json_rows(rows, {c: "STRING" for c in COLS})
            typed = f"""SELECT user_id, country, state, business_function, split(security_scopes, ';') AS scopes,
                               CAST(active AS BOOLEAN) AS active, CAST(nullif(valid_from, '') AS DATE) AS valid_from,
                               CAST(nullif(valid_to, '') AS DATE) AS valid_to FROM {source}"""
            try:  # read every value before anything is removed, so a bad file changes nothing
                sql().query(f"SELECT count(*) AS n FROM ({typed}) WHERE active IS NOT NULL", params)
            except Exception as e:  # noqa: BLE001
                st.error("Nothing was loaded: a value in the file couldn't be read. Dates must look like "
                         "2026-01-31 and **active** must be true or false. Fix the file and load it again.")
                soft("The file check", e)
            else:
                sql().execute(f"DELETE FROM {s.fq('user_access')} "
                              f"WHERE lower(user_id) IN (SELECT user_id FROM {source})", params)
                sql().execute(f"INSERT INTO {s.fq('user_access')} "
                              f"SELECT t.*, 'csv', current_timestamp() FROM ({typed}) t", params)
                control.audit(user, None, "user_access_loaded", f.name, {"rows": len(rows)})
                st.success(f"Loaded {len(rows)} row(s) for {len({r['user_id'] for r in rows})} user(s).")

if section == "Alerts":  # Alerts -------------------------------------------------------------------------
    try:
        alerts = sql().query(f"SELECT * FROM {s.fq('v_alerts_open')} ORDER BY ts DESC LIMIT 200")
    except Exception as e:  # noqa: BLE001
        alerts = []
        soft("The alert list", e)
    else:
        if not alerts:
            st.success("No open alerts.")
    for a in alerts:
        c1, c2 = st.columns([6, 1])
        c1.write(f"**{a['severity']}** · {a['bot_id'] or 'platform'} · {a['kind']} · {a['ts']}  \n{a['message']}")
        if c2.button("Resolve", key=a["alert_id"]):
            sql().execute(f"UPDATE {s.fq('alerts')} SET resolved = true WHERE alert_id = :a", {"a": a["alert_id"]})
            control.audit(user, a["bot_id"], "alert_resolved", a["alert_id"])
            st.rerun()

if section == "Audit log":  # Audit log ----------------------------------------------------------------------
    bot_filter = st.text_input("Filter by chatbot (technical name)")
    q = f"SELECT ts, actor, bot_id, action, target, detail_json FROM {s.fq('audit_log')}"
    params = {}
    if bot_filter:
        q, params = q + " WHERE bot_id = :b", {"b": bot_filter}
    st.dataframe(frame(q + " ORDER BY ts DESC LIMIT 500", params), hide_index=True, width="stretch")
