"""Monitoring and drift for one chatbot (OBS-6..12): speed, cost, quality, retrieval and
question drift, with links to the MLflow traces and the Databricks drift dashboard."""
import os

import altair as alt
import pandas as pd
import streamlit as st
import streamlit.components.v1  # noqa: F401 - embedded dashboard

from common import METRIC_LABEL, OUTCOME_LABEL, app_client, is_admin, pick_bot, settings, sql
from factory.explain import help_text
from ui import clickable_bars, drill, html, pill, target_chart

st.title("Monitoring", help=help_text("monitoring"))
bot, cfg = pick_bot()
if not bot:
    st.stop()
s, b = settings(), {"b": cfg.bot_id}
days = st.segmented_control("Period", [7, 30, 90], default=30, format_func=lambda d: f"{d} days") or 30


def frame(query: str) -> pd.DataFrame:
    try:
        return pd.DataFrame(sql().query(query, b))
    except Exception:  # noqa: BLE001 - a table that isn't populated yet just shows as empty
        return pd.DataFrame()


daily = frame(f"SELECT * FROM {s.fq('v_observability_daily')} WHERE bot_id = :b "
              f"AND day >= current_date() - INTERVAL {int(days)} DAYS ORDER BY day")
if daily.empty:
    st.info("No live questions in this period yet. Charts appear once people use the live version.")
    st.stop()
daily = daily.set_index("day").apply(pd.to_numeric, errors="coerce")
last7 = daily.tail(7)

# Native Databricks tools, linked and embedded (OBS-16) ------------------------------------------
host = app_client().config.host.rstrip("/")
links, dashboard_id = [], None
try:
    exp = app_client().experiments.get_by_name(f"/Shared/chatbot-factory/{os.environ.get('FACTORY_ENV', 'qa')}/traces")
    links.append(f"[Review queue]({host}/ml/experiments/{exp.experiment.experiment_id}/reviews) "
                 f"(`review_{cfg.bot_id}`: failed and thumbs-down answers waiting for a person)")
    links.append(f"[Traces in MLflow]({host}/ml/experiments/{exp.experiment.experiment_id}/traces) "
                 f"(filter `tags.bot_id = '{cfg.bot_id}'`; run issue detection from there) · "
                 "open **Traces** in the menu for the step-by-step flow")
except Exception:  # noqa: BLE001
    pass
try:
    dashboard_id = app_client().quality_monitors.get(s.fq("request_log")).dashboard_id
except Exception:  # noqa: BLE001
    pass
if s.get("observability.genie_space_id"):
    links.append(f"[Ask Genie about usage and quality]({host}/genie/rooms/{s.get('observability.genie_space_id')})")
if links:
    st.markdown(" · ".join(links))
if dashboard_id:
    with st.expander("Databricks drift dashboard"):
        # Needs the workspace embed policy to allow *.databricksapps.com (Admin › Setup checklist).
        st.components.v1.iframe(f"{host}/embed/dashboardsv3/{dashboard_id}", height=820, scrolling=True)

# Headline numbers (last 7 days) ---------------------------------------------------------------
admin = is_admin()  # costs are MLOps' concern, not owners' (WIZ-13)
c = st.columns(6 if admin else 5)
c[0].metric("Questions (7 days)", f"{int(last7['requests'].sum()):,}")
c[1].metric("Typical answer time", f"{last7['p50_latency_ms'].median() / 1000:.1f}s")
c[2].metric("Slow answers (p95)", f"{last7['p95_latency_ms'].median() / 1000:.1f}s")
c[3].metric("First token (p95)", f"{last7['p95_ttft_ms'].median() / 1000:.1f}s"
            if last7["p95_ttft_ms"].notna().any() else "—")
c[4].metric("Answered", f"{last7['answered_rate'].mean():.0%}")
if admin:
    c[5].metric("Cost per question", f"${last7['cost_usd'].sum() / max(last7['requests'].sum(), 1):.4f}")

names = ["Speed", "Quality", "Guardrails", "Retrieval", "Question drift"] + (["Cost and tokens"] if admin else [])
t_speed, t_quality, t_guard, t_retrieval, t_drift, *rest = st.tabs(names)

with t_speed:
    st.caption("Answer time and time to first token from the answer model, in seconds. Dashed line: the slow-answer "
               "target; red markers: days over it.")
    target_chart(daily[["p50_latency_ms", "p95_latency_ms", "p50_ttft_ms", "p95_ttft_ms"]].div(1000).rename(
        columns={"p50_latency_ms": "Answer (typical)", "p95_latency_ms": "Answer (p95)",
                 "p50_ttft_ms": "First token (typical)", "p95_ttft_ms": "First token (p95)"}), "Seconds",
        float(s.get("monitoring.latency_p95_s", 12)), "Slow-answer target (p95)", fmt=".1f")
    st.caption("Share of answers the safety checks sent back for a rewrite.")
    st.line_chart(daily[["retake_rate"]].rename(columns={"retake_rate": "Rewritten by guardrails"}))

for t in rest:  # admins only
    with t:
        st.line_chart(daily[["cost_usd"]].rename(columns={"cost_usd": "Cost per day ($)"}))
        st.line_chart(daily[["avg_input_tokens", "avg_output_tokens"]].rename(
            columns={"avg_input_tokens": "Input tokens per question", "avg_output_tokens": "Output tokens per question"}))

with t_quality:
    st.caption("What happened to each question.")
    st.area_chart(daily[["answered_rate", "no_source_rate", "refused_rate", "failed_rate"]].rename(columns={
        "answered_rate": "Answered", "no_source_rate": "Not in documents", "refused_rate": "Refused or blocked",
        "failed_rate": "Couldn't verify or error"}))
    judges = frame(f"SELECT day, scorer, pass_rate FROM {s.fq('v_quality_daily')} WHERE bot_id = :b "
                   f"AND day >= current_date() - INTERVAL {int(days)} DAYS")
    if not judges.empty:
        judges["scorer"] = judges["scorer"].map(lambda k: METRIC_LABEL.get(k, k.replace("_", " ").capitalize()))
        st.markdown("Judge pass rates on sampled live answers (100% for critical chatbots), and thumbs up.",
                    help=help_text("judges"))
        target_chart(judges.pivot_table(index="day", columns="scorer", values="pass_rate").apply(
            pd.to_numeric, errors="coerce"), "Pass rate", float(s.get("quality.go_live_thresholds.groundedness", 0.9)),
            "Go-live target", above_is_bad=False, fmt=".0%")
    issues = frame(f"""SELECT scorer, topic, failures, last_7_days, prior_7_days FROM {s.fq('v_failure_patterns')}
                       WHERE bot_id = :b ORDER BY last_7_days DESC, failures DESC LIMIT 20""")
    if not issues.empty:
        st.markdown("**Recurring problems** (judge failures grouped by judge and topic, last 28 days)",
                    help=help_text("issues"))
        issues["scorer"] = issues["scorer"].map(lambda k: METRIC_LABEL.get(k, k.replace("_", " ").capitalize()))
        issues["trend"] = issues.apply(lambda r: "New" if not r.prior_7_days else
                                       f"{r.last_7_days / r.prior_7_days - 1:+.0%}", axis=1)
        st.dataframe(issues.rename(columns={"scorer": "Judge", "topic": "Topic", "failures": "Failures",
                                            "last_7_days": "Last 7 days", "trend": "Trend"})[
            ["Judge", "Topic", "Failures", "Last 7 days", "Trend"]], hide_index=True, use_container_width=True)

with t_guard:  # GW-7: people asking never see which check fired; it's all recorded here
    st.markdown("Every safety check that fired.", help=help_text("guardrails"))
    RULE = {"gateway_guardrail": "Unity Gateway policy blocked", "topic_rules": "Off-topic refused",
            "unverified": "Answer couldn't be verified", "sensitive_output": "Sensitive data redacted",
            "retrieved_injection": "Instructions removed from documents", "access": "Access denied",
            "conflict_disclosed": "Conflicting sources disclosed", "citation_mapping": "Wrong citation, rewritten",
            "quote_check": "Excerpt not in source, rewritten", "verbatim_limit": "Too much quoted, rewritten",
            "prompt_leak": "Instructions leaked, rewritten", "no_invented_links": "Invented link, rewritten",
            "citation_range": "Citation out of range, rewritten"}
    g = frame(f"SELECT day, rule, sum(events) AS events FROM {s.fq('v_guardrails')} WHERE bot_id = :b "
              f"AND day >= current_date() - INTERVAL {int(days)} DAYS GROUP BY ALL")
    v = frame(f"""SELECT CAST(event_time AS DATE) AS day, count(*) AS checks, count_if(flagged) AS flagged
                  FROM {s.fq('v_guardrail_verdicts')} WHERE bot_id = :b
                  AND event_time >= current_date() - INTERVAL {int(days)} DAYS GROUP BY ALL ORDER BY day""")
    if not v.empty:
        st.caption("Unity Gateway policies in Log mode (for example the hallucination check): they flag "
                   "answers without blocking them, so they're counted here.")
        st.line_chart(v.set_index("day")[["checks", "flagged"]].apply(pd.to_numeric, errors="coerce").rename(
            columns={"checks": "Policy checks", "flagged": "Flagged"}))
    if g.empty:
        st.info("No guardrail activity in this period.")
    else:
        g["rule"] = g["rule"].map(lambda r: RULE.get(r, r.replace("_", " ").capitalize()))
        st.caption("Every guardrail that fired, per day: Unity Gateway blocks and the factory's own checks "
                   "(rewrites, refusals, redactions). Each event has its MLflow trace ID.")
        st.bar_chart(g.pivot_table(index="day", columns="rule", values="events", aggfunc="sum").fillna(0))
        st.markdown("**Totals this period.** Click a bar to see and download every event behind it.")
        hit = clickable_bars(g.groupby("rule", as_index=False)["events"].sum(), "rule", "events", "guard_bars")
        if hit:
            key = next((k for k, v in RULE.items() if v == hit), hit)
            drill(f"{hit} · {cfg.display_name}", sql().query(
                f"""SELECT ts, action, reason, trace_id FROM {s.fq('guardrail_events')} WHERE bot_id = :b
                    AND rule = :r AND ts >= current_date() - INTERVAL {int(days)} DAYS ORDER BY ts DESC LIMIT 5000""",
                {"b": cfg.bot_id, "r": key}), "Open a trace ID on the Traces page (MLOps and Security).")
        recent = frame(f"""SELECT ts, rule, action, left(reason, 200) AS reason, trace_id
                           FROM {s.fq('guardrail_events')} WHERE bot_id = :b
                           AND ts >= current_date() - INTERVAL {int(days)} DAYS ORDER BY ts DESC LIMIT 200""")
        if not recent.empty:
            recent["rule"] = recent["rule"].map(lambda r: RULE.get(r, r))
            st.dataframe(recent, hide_index=True, use_container_width=True)

with t_retrieval:
    st.caption("Best search score per question (a falling line means documents match questions less well) "
               "and how many results the reranker moved.")
    st.line_chart(daily[["mean_top_score"]].rename(columns={"mean_top_score": "Best search score"}))
    st.line_chart(daily[["mean_rerank_moved"]].rename(columns={"mean_rerank_moved": "Results moved by reranker"}))
    psi = frame(f"""SELECT column_name, population_stability_index AS psi, js_distance
                    FROM {s.fq('request_log_drift_metrics')}
                    WHERE slice_key = 'bot_id' AND slice_value = :b AND drift_type = 'CONSECUTIVE'
                    QUALIFY row_number() OVER (PARTITION BY column_name ORDER BY window.start DESC) = 1""")
    if not psi.empty:
        st.caption("Latest day-over-day drift per field (Databricks data quality monitor). "
                   "PSI above 0.2 is a meaningful shift.")
        st.dataframe(psi, hide_index=True, use_container_width=True)

with t_drift:
    st.markdown("Are people asking about different things than at launch?", help=help_text("drift"))
    qd = frame(f"SELECT * FROM {s.fq('question_drift')} WHERE bot_id = :b "
               f"AND day >= current_date() - INTERVAL {int(days)} DAYS ORDER BY day")
    if qd.empty:
        st.info("Question drift starts the day after the first live questions.")
    else:
        qd = qd.set_index("day").apply(pd.to_numeric, errors="coerce")
        latest = qd["drift_vs_launch"].dropna().iloc[-1] if qd["drift_vs_launch"].notna().any() else 0
        limit = s.get("monitoring.question_drift_alert", 0.15)
        html(pill("Shifted from launch" if latest > limit else "Similar to launch", "amber" if latest > limit else "green")
             + '<span class="cf-muted">How different today\'s questions are from the first two weeks live '
               '(0 = same topics).</span>')
        target_chart(qd[["drift_vs_launch", "drift_vs_last_week"]].rename(
            columns={"drift_vs_launch": "vs launch", "drift_vs_last_week": "vs last week"}), "Drift",
            float(limit), "Alert level")
    pts = frame(f"""SELECT x, y, outcome, coalesce(topic_label, 'Not grouped yet') AS topic,
                           CAST(date_trunc('WEEK', day) AS DATE) AS week
                    FROM {s.fq('question_embeddings')} WHERE bot_id = :b AND x IS NOT NULL
                    AND day >= current_date() - INTERVAL 28 DAYS LIMIT 3000""")
    if not pts.empty:
        st.markdown("**Question map, last 4 weeks**", help=help_text("question_map"))
        st.caption("Each dot is one question. Similar questions sit close together, so groups of dots are topics. "
                   "Hover a dot to see its topic, week and what happened. Look for groups colored \"not in "
                   "documents\" (add a document that covers them) and for groups that only appear in recent weeks "
                   "(new topics). Position has no units; only closeness matters.")
        color = st.radio("Color the question map by", ["week", "outcome"], horizontal=True)
        pts["outcome"] = pts["outcome"].map(lambda o: OUTCOME_LABEL.get(o) or "Answered")
        st.altair_chart(alt.Chart(pts).mark_circle(size=40, opacity=0.75).encode(
            x=alt.X("x:Q", axis=None), y=alt.Y("y:Q", axis=None),
            color=alt.Color("week:O" if color == "week" else "outcome:N", title=color.capitalize()),
            tooltip=[alt.Tooltip("topic:N", title="Topic"), alt.Tooltip("week:T", title="Week of"),
                     alt.Tooltip("outcome:N", title="What happened")]).properties(height=420),
            use_container_width=True)
