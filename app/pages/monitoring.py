"""Monitoring and drift (OBS-6..12): speed, cost, quality, retrieval and question drift, with
links to the MLflow traces and the Databricks drift dashboard.

Works at both levels: "All my chatbots" compares them side by side so the one that needs
attention stands out; choosing one chatbot opens its charts."""
import altair as alt
import pandas as pd
import streamlit as st
import streamlit.components.v1  # noqa: F401 - embedded dashboard

from common import (METRIC_LABEL, OUTCOME_LABEL, app_client, drift_dashboard_id, is_admin, my_bots, pick_bot,
                    read_many, settings, soft, sql, trace_experiment_id)
from factory.explain import help_text
from factory.sql import json_rows
from ui import clickable_bars, drill, html, pill, target_chart

st.title("Monitoring", help=help_text("monitoring"))
s = settings()
SLOW_S = float(s.get("monitoring.latency_p95_s", 12))
ALL, ONE = "All my chatbots", "One chatbot"
mine = my_bots()
scope = ONE
if "monitoring_open" in st.session_state:  # a row was chosen in the all-chatbots table: open that chatbot
    st.session_state["bot_id"] = st.session_state.pop("monitoring_open")
    st.session_state["monitoring_scope"] = ONE
if len(mine) > 1:
    st.session_state.setdefault("monitoring_scope", ONE if st.session_state.get("bot_id") else ALL)
    scope = st.segmented_control("Show", [ALL, ONE], key="monitoring_scope") or ALL
c_days, _ = st.columns([2, 3])
days = c_days.segmented_control("Period", [7, 30, 90], default=30, format_func=lambda d: f"{d} days") or 30

if scope == ALL:
    # Side by side, so the chatbot that needs attention stands out. One statement for all of them.
    live = [b for b in mine if b.get("live_release_id")]
    if not live:
        st.info("None of your chatbots is live yet, so there is nothing to monitor. Monitoring starts once a "
                "chatbot has been published on the **Launch** page.")
        st.stop()
    source, params = json_rows([{"bot_id": b["bot_id"]} for b in live], {"bot_id": "STRING"})
    found, failed = read_many({
        "usage": (f"""SELECT bot_id, sum(requests) AS questions,
                             sum(answered_rate * requests) / nullif(sum(requests), 0) AS answered,
                             sum(no_source_rate * requests) / nullif(sum(requests), 0) AS not_covered,
                             percentile_approx(p95_latency_ms, 0.5) / 1000 AS slow_s
                      FROM {s.fq('v_observability_daily')}
                      WHERE day >= current_date() - INTERVAL {int(days)} DAYS AND bot_id IN (SELECT bot_id FROM {source})
                      GROUP BY bot_id""", params),
        "alerts": (f"SELECT bot_id, count(*) AS alerts FROM {s.fq('v_alerts_open')} "
                   f"WHERE bot_id IN (SELECT bot_id FROM {source}) GROUP BY bot_id", params)})
    usage = {r["bot_id"]: r for r in found["usage"]}
    open_alerts = {r["bot_id"]: int(r["alerts"]) for r in found["alerts"]}

    def look_at(row: dict | None, alerts: int) -> str:
        """The first thing worth the owner's attention for one chatbot, in plain words."""
        if not row or not float(row["questions"] or 0):
            return "Nobody has asked it anything in this period."
        if alerts:
            return f"{alerts} open alert(s). They are listed on Home with what to do."
        if float(row["answered"] or 0) < 0.8:
            return "Many questions go unanswered. Open it and look at Quality."
        if float(row["slow_s"] or 0) > SLOW_S:
            return "Answers are slower than the target. Open it and look at Speed."
        return "Nothing needs your attention."

    table = pd.DataFrame([{
        "Chatbot": b["display_name"],
        "Questions": int(float((usage.get(b["bot_id"]) or {}).get("questions") or 0)),
        "Answered": float((usage.get(b["bot_id"]) or {}).get("answered") or 0) if b["bot_id"] in usage else None,
        "Not in documents": float((usage.get(b["bot_id"]) or {}).get("not_covered") or 0) if b["bot_id"] in usage else None,
        "Slow answers (seconds)": float((usage.get(b["bot_id"]) or {}).get("slow_s") or 0) if b["bot_id"] in usage else None,
        "Open alerts": open_alerts.get(b["bot_id"], 0),
        "What to look at": look_at(usage.get(b["bot_id"]), open_alerts.get(b["bot_id"], 0)),
        "bot_id": b["bot_id"]} for b in live]).sort_values(["Open alerts", "Questions"], ascending=False)
    st.caption(f"Your {len(live)} live chatbot(s) over the last {days} days. **Select a row** to open that chatbot's "
               "charts. The last column says what, if anything, needs a look.")
    ev = st.dataframe(table.drop(columns=["bot_id"]), hide_index=True, width="stretch", on_select="rerun",
                      selection_mode="single-row", key="monitoring_all",
                      column_config={"Answered": st.column_config.NumberColumn(format="percent"),
                                     "Not in documents": st.column_config.NumberColumn(format="percent"),
                                     "Slow answers (seconds)": st.column_config.NumberColumn(format="%.1f")})
    chosen = ev.selection.rows if ev and ev.selection else []
    if chosen:
        st.session_state["monitoring_open"] = table.iloc[chosen[0]]["bot_id"]
        st.session_state.pop("monitoring_all", None)
        st.rerun()
    not_live = [b["display_name"] for b in mine if not b.get("live_release_id")]
    if not_live:
        st.caption("Not live yet, so not monitored: " + ", ".join(not_live))
    for name, error in failed.items():
        soft({"usage": "Usage numbers", "alerts": "Open alerts"}[name], error)
    st.stop()

bot, cfg = pick_bot()
if not bot:
    st.stop()
b = {"b": cfg.bot_id}


def frame(query: str) -> pd.DataFrame:
    try:
        return pd.DataFrame(sql().query(query, b))
    except Exception:  # noqa: BLE001 - a table that isn't populated yet just shows as empty
        return pd.DataFrame()


daily = frame(f"SELECT * FROM {s.fq('v_observability_daily')} WHERE bot_id = :b "
              f"AND day >= current_date() - INTERVAL {int(days)} DAYS ORDER BY day")
if daily.empty:
    if not bot.get("live_release_id"):
        st.info(f"**{cfg.display_name}** isn't live yet, so there is nothing to monitor. Monitoring starts once it "
                "has been published on the **Launch** page.")
    else:
        st.info("Nobody has asked the live version a question in this period yet. Charts appear here once people "
                "use it. Try a longer period above, or check that people know where to find the chatbot.")
    st.stop()
daily = daily.set_index("day").apply(pd.to_numeric, errors="coerce")
last7 = daily.tail(7)

# Native Databricks tools, linked and embedded (OBS-16) ------------------------------------------
host = app_client().config.host.rstrip("/")
links, experiment, dashboard_id = [], trace_experiment_id(), drift_dashboard_id()
if experiment:
    links.append(f"[Review queue]({host}/ml/experiments/{experiment}/reviews) "
                 f"(`review_{cfg.bot_id}`: failed and thumbs-down answers waiting for a person)")
    links.append(f"[Traces in MLflow]({host}/ml/experiments/{experiment}/traces) "
                 f"(filter `tags.bot_id = '{cfg.bot_id}'`; run issue detection from there) · "
                 "open **Traces** in the menu for the step-by-step flow")
if s.get("observability.genie_space_id"):
    links.append(f"[Ask Genie about usage and quality]({host}/genie/rooms/{s.get('observability.genie_space_id')})")
if links:
    with st.expander("Databricks tools for a deeper look (for MLOps and analysts)"):
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

# What, if anything, needs the owner's attention, in plain words.
answered, slow = float(last7["answered_rate"].mean()), float(last7["p95_latency_ms"].median()) / 1000
if answered < 0.8:
    st.warning(f"**Only {answered:.0%} of questions were answered in the last 7 days.** Open **Quality** below to see "
               "what happened to the rest. The usual cause is questions about things your documents don't cover: "
               "**Question drift** shows which topics, and adding a document for them fixes it.")
elif slow > SLOW_S:
    st.warning(f"**Answers are slower than the target** ({slow:.1f}s against {SLOW_S:.0f}s). This is for MLOps to fix, "
               "not you. If it lasts more than a day, tell them.")
else:
    st.success("Nothing here needs your attention: most questions are answered and answers arrive on time.")

# One view open at a time, so only the charts on screen are loaded.
VIEWS = {"Speed": "How long people wait for an answer.",
         "Quality": "What happened to each question, and how the automatic judges rate the answers.",
         "Guardrails": "Every time a safety check stepped in.",
         "Retrieval": "How well the search finds the right part of your documents.",
         "Question drift": "Whether people ask about different things than when the chatbot launched."}
if admin:
    VIEWS["Cost and tokens"] = "Spending per day and the size of questions and answers."
view = st.segmented_control("Look closer at", list(VIEWS), default="Speed", key="monitoring_view") or "Speed"
st.caption(VIEWS[view])

if view == "Speed":
    st.caption("Answer time and time to first token from the answer model, in seconds. Dashed line: the slow-answer "
               "target; red markers: days over it.")
    target_chart(daily[["p50_latency_ms", "p95_latency_ms", "p50_ttft_ms", "p95_ttft_ms"]].div(1000).rename(
        columns={"p50_latency_ms": "Answer (typical)", "p95_latency_ms": "Answer (p95)",
                 "p50_ttft_ms": "First token (typical)", "p95_ttft_ms": "First token (p95)"}), "Seconds",
        SLOW_S, "Slow-answer target (p95)", fmt=".1f")
    st.caption("Share of answers the safety checks sent back for a rewrite.")
    st.line_chart(daily[["retake_rate"]].rename(columns={"retake_rate": "Rewritten by guardrails"}))

if view == "Cost and tokens":  # admins only
    st.line_chart(daily[["cost_usd"]].rename(columns={"cost_usd": "Cost per day ($)"}))
    st.line_chart(daily[["avg_input_tokens", "avg_output_tokens"]].rename(
        columns={"avg_input_tokens": "Input tokens per question", "avg_output_tokens": "Output tokens per question"}))

if view == "Quality":
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
            ["Judge", "Topic", "Failures", "Last 7 days", "Trend"]], hide_index=True, width="stretch")

if view == "Guardrails":  # GW-7: people asking never see which check fired; it's all recorded here
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
            st.dataframe(recent, hide_index=True, width="stretch")

if view == "Retrieval":
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
        st.dataframe(psi, hide_index=True, width="stretch")

if view == "Question drift":
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
            width="stretch")
