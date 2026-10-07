"""All chatbots (OBS-21, MLOps): every chatbot combined or filtered to one, with Arize-style views
built from our own tables and MLflow traces: per-step latency, slow-answer heatmap, question
topics worst first, slice comparison, document gaps, conversation health, guardrails, and a
paginated table of every chatbot."""
import altair as alt
import pandas as pd
import streamlit as st

from common import STATE_LABEL, app_client, cp, is_admin, load_traces, settings, sql, trace_experiment_id
from factory.explain import help_text
from factory.flow import aggregate
from ui import clickable_bars, drill, fresh

if not is_admin():
    st.error("This page is for MLOps admins only.")
    st.stop()
s, control = settings(), cp()
st.title("All chatbots")
st.markdown("Every chatbot combined, or filter to one.", help=help_text("monitoring"))


def open_trace(trace_id: str, key: str) -> None:
    """Jump to the Traces page showing this trace (OBS-25); clears the row selection first."""
    st.session_state.pop(key, None)
    st.session_state["open_trace"] = trace_id
    st.switch_page("pages/traces.py")


def pick(df: pd.DataFrame, key: str, **kw) -> int | None:
    ev = st.dataframe(df, hide_index=True, use_container_width=True, on_select="rerun", selection_mode="single-row",
                      key=key, **kw)
    rows = ev.selection.rows if ev and ev.selection else []
    return rows[0] if rows else None
bots = {b["bot_id"]: b for b in control.list_bots() if b["state"] not in ("deleted", "purged")}
c1, c2 = st.columns([3, 2])
bot = c1.selectbox("Chatbot", ["all"] + sorted(bots), format_func=lambda b: f"All chatbots ({len(bots)})"
                   if b == "all" else bots[b]["display_name"])
days = c2.segmented_control("Period", [7, 30, 90], default=30, format_func=lambda d: f"{d} days") or 30
P = {"b": "" if bot == "all" else bot, "d": int(days)}
if s.get("observability.genie_space_id"):
    st.markdown(f"[Ask Genie about usage, quality and cost]({app_client().config.host.rstrip('/')}/genie/rooms/"
                f"{s.get('observability.genie_space_id')})", help=help_text("genie"))


def where(p: str = "") -> str:
    return (f"WHERE {p}ts >= current_date() - INTERVAL {int(days)} DAYS AND {p}channel = 'live' "
            f"AND NOT {p}synthetic AND (:b = '' OR {p}bot_id = :b)")


WHERE = where()
LOG = s.fq("request_log")


def frame(query: str) -> pd.DataFrame:
    try:
        df = pd.DataFrame(sql().query(query, P))
        for col in df.columns:  # the SQL API returns strings
            try:
                df[col] = pd.to_numeric(df[col])
            except (ValueError, TypeError):
                pass
        return df
    except Exception as e:  # noqa: BLE001 - tables fill in after the first questions
        st.caption(f"Not available yet: {str(e)[:120]}")
        return pd.DataFrame()


k = frame(f"""SELECT count(*) AS q, sum(cost_usd) AS cost, avg(CASE WHEN outcome = 'answered' THEN 1.0 ELSE 0 END) AS ans,
              percentile_approx(latency_ms, 0.95) AS p95, percentile_approx(ttft_ms, 0.95) AS ttft,
              count(DISTINCT bot_id) AS bots FROM {LOG} {WHERE}""")
if k.empty or not k.loc[0, "q"]:
    st.info("No live questions in this period yet.")
    st.stop()
k = k.loc[0]
judge = frame(f"SELECT avg(value) AS v FROM {s.fq('judge_results')} WHERE ts >= current_date() - INTERVAL {int(days)} DAYS "
              "AND (:b = '' OR bot_id = :b)")
guard = frame(f"SELECT rule, sum(events) AS events FROM {s.fq('v_guardrails')} WHERE day >= current_date() - INTERVAL "
              f"{int(days)} DAYS AND (:b = '' OR bot_id = :b) GROUP BY rule ORDER BY events DESC")
alerts = frame(f"SELECT count(*) AS n FROM {s.fq('v_alerts_open')} WHERE (:b = '' OR bot_id = :b)")
c = st.columns(8)
c[0].metric("Questions", f"{int(k.q):,}", f"{int(k.bots)} chatbots", delta_color="off")
c[1].metric("Cost", f"${float(k.cost or 0):,.2f}", f"${float(k.cost or 0) / k.q:.4f} per question", delta_color="off")
c[2].metric("Answered", f"{float(k.ans):.0%}")
c[3].metric("Judges pass", f"{float(judge.loc[0, 'v']):.0%}" if not judge.empty and judge.loc[0, "v"] is not None else "—",
            help=help_text("judges"))
c[4].metric("Slow answers (p95)", f"{float(k.p95 or 0) / 1000:.1f}s")
c[5].metric("First token (p95)", f"{float(k.ttft or 0) / 1000:.1f}s" if k.ttft else "—")
c[6].metric("Guardrail events", f"{int(guard.events.sum()) if not guard.empty else 0:,}", help=help_text("guardrails"))
c[7].metric("Open alerts", int(alerts.loc[0, "n"]) if not alerts.empty else 0)

left, right = st.columns(2)
daily = frame(f"SELECT CAST(ts AS DATE) AS day, bot_id, count(*) AS questions, sum(cost_usd) AS cost FROM {LOG} {WHERE} GROUP BY ALL")
with left:
    st.subheader("Questions per day")
    if not daily.empty:
        top = daily.groupby("bot_id").questions.sum().nlargest(5).index
        daily["chatbot"] = daily.bot_id.where(daily.bot_id.isin(top), "Others").map(
            lambda b: bots.get(b, {}).get("display_name", b))
        st.bar_chart(daily.pivot_table(index="day", columns="chatbot", values="questions", aggfunc="sum").fillna(0))
with right:
    st.subheader("Cost per day")
    if not daily.empty:
        st.bar_chart(daily.pivot_table(index="day", columns="chatbot", values="cost", aggfunc="sum").fillna(0))

left, right = st.columns(2)
with left:
    st.subheader("Where the time goes, per step", help=help_text("flow"))
    rows = aggregate([t["spans"] for t in load_traces(None if bot == "all" else bot, 300)])
    rows = sorted([r for r in rows if r["depth"] > 0], key=lambda r: -r["p95_ms"])[:8]
    if rows:
        st.bar_chart(pd.DataFrame({"typical (p50)": [r["p50_ms"] for r in rows], "slow (p95)": [r["p95_ms"] for r in rows]},
                                  index=[r["name"] for r in rows]), horizontal=True, stack=False)
        st.caption("Milliseconds, from the last 300 traces. Open Traces for the full flow.")
with right:
    st.subheader("Slow answers by day and hour")
    heat = frame(f"""SELECT date_format(ts, 'E') AS weekday, dayofweek(ts) AS dow, hour(ts) AS hour,
                     avg(CASE WHEN latency_ms > {int(float(s.get('monitoring.latency_p95_s', 12)) * 1000)} THEN 1.0 ELSE 0 END) AS slow,
                     count(*) AS n FROM {LOG} {WHERE} GROUP BY ALL""")
    if not heat.empty:
        cell = alt.selection_point(fields=["dow", "hour", "weekday"], name="cell")
        ev = st.altair_chart(alt.Chart(heat).mark_rect(cursor="pointer").encode(
            x=alt.X("hour:O", title="Hour"), y=alt.Y("weekday:N", sort=alt.EncodingSortField("dow"), title=None),
            color=alt.Color("slow:Q", title="Share slow", scale=alt.Scale(scheme="reds")),
            tooltip=["weekday", "hour", alt.Tooltip("slow:Q", format=".1%"), "n"]).add_params(cell),
            use_container_width=True, on_select="rerun", key="heat")
        st.caption(f"Share of answers slower than {s.get('monitoring.latency_p95_s', 12)} s. Workspace time zone. "
                   "Click a cell for its slow answers.")
        sel = (ev or {}).get("selection", {}).get("cell") or []
        c0 = fresh("heat", sel[0] if sel else None)
        if c0:
            drill(f"Slow answers, {c0['weekday']} {int(c0['hour'])}:00", sql().query(
                f"""SELECT ts, bot_id, latency_ms, ttft_ms, outcome, trace_id FROM {LOG} {WHERE}
                    AND dayofweek(ts) = {int(c0['dow'])} AND hour(ts) = {int(c0['hour'])}
                    AND latency_ms > {int(float(s.get('monitoring.latency_p95_s', 12)) * 1000)}
                    ORDER BY latency_ms DESC LIMIT 5000""", P))

st.subheader("Question topics, worst first", help=help_text("clusters"))
topics = frame(f"SELECT * FROM {s.fq('v_topics')} WHERE (:b = '' OR bot_id = :b)")
if topics.empty:
    st.caption("Topics appear the day after questions are embedded by the daily drift job.")
else:
    topics["Trend"] = topics.apply(lambda r: "New" if not r.prior_7_days else f"{r.last_7_days / r.prior_7_days - 1:+.0%}", axis=1)
    topics["score"] = (1 - topics.answered_rate) * topics.last_7_days
    topics = topics.sort_values("score", ascending=False).reset_index(drop=True)
    st.caption("Select a topic to see its questions and open any of them as a trace.")
    i = pick(topics.rename(columns={
        "topic_label": "Topic", "bot_id": "Chatbot", "questions": "Questions (28 days)",
        "answered_rate": "Answered", "no_source_rate": "Not in documents", "judge_pass_rate": "Judges pass",
        "mean_top_score": "Best search score", "p95_latency_ms": "Slow (p95, ms)"})[
        ["Topic", "Chatbot", "Questions (28 days)", "Trend", "Answered", "Not in documents",
         "Judges pass", "Best search score", "Slow (p95, ms)"]], "topics", column_config={
            c: st.column_config.NumberColumn(format="percent") for c in ("Answered", "Not in documents", "Judges pass")})
    if i is not None:
        t = topics.iloc[i]
        qs = frame(f"""SELECT r.ts, r.question, r.outcome, r.top_score, r.trace_id FROM {s.fq('question_embeddings')} e
                       JOIN {LOG} r USING (request_id) WHERE e.bot_id = '{t.bot_id}' AND e.topic = {int(t.topic)}
                       AND e.day >= current_date() - 28 ORDER BY r.ts DESC LIMIT 200""")
        st.markdown(f"**{t.topic_label}** · {len(qs)} questions. Select one to open its trace.")
        j = pick(qs.drop(columns=["trace_id"]), "topic_questions")
        if j is not None and qs.trace_id.iloc[j]:
            open_trace(qs.trace_id.iloc[j], "topic_questions")

left, right = st.columns(2)
with left:
    st.subheader("Compare slices")
    MEASURES = {"Judges pass": "avg(j.value)", "Answered": "avg(CASE WHEN r.outcome = 'answered' THEN 1.0 ELSE 0 END)",
                "Cost per question": "avg(r.cost_usd)", "Slow (p95, ms)": "percentile_approx(r.latency_ms, 0.95)"}
    DIMS = {"Chatbot": "r.bot_id", "Prompt version": "r.prompt_uri", "Release": "r.release_id", "Model": "r.model",
            "Caller (person or app)": "coalesce(r.identity_source, 'obo')", "Business function": "r.user_function"}
    m = st.selectbox("Measure", list(MEASURES))
    d1, d2 = st.columns(2)
    rows_dim = d1.selectbox("Rows", list(DIMS), index=1)
    cols_dim = d2.selectbox("Columns", list(DIMS), index=4)
    piv = frame(f"""SELECT {DIMS[rows_dim]} AS r1, {DIMS[cols_dim]} AS c1, {MEASURES[m]} AS v, count(*) AS n
                    FROM {LOG} r LEFT JOIN (SELECT request_id, avg(value) AS value FROM {s.fq('judge_results')}
                    GROUP BY request_id) j USING (request_id)
                    {where('r.')}
                    GROUP BY ALL""")
    if not piv.empty:
        piv["cell"] = piv.apply(lambda x: f"{x.v:.0%}" if m in ("Judges pass", "Answered") and pd.notna(x.v) else
                                (f"${x.v:.4f}" if m == "Cost per question" else f"{x.v:,.0f}") + f" (n={int(x.n):,})", axis=1)
        st.dataframe(piv.pivot_table(index="r1", columns="c1", values="cell", aggfunc="first").fillna("—"),
                     use_container_width=True)
        st.caption("Each cell shows how many answers it's based on.")
with right:
    st.subheader("Gaps in the documents", help=help_text("search"))
    gaps = frame(f"SELECT top_score FROM {LOG} {WHERE} AND top_score IS NOT NULL")
    if not gaps.empty:
        counts = pd.cut(gaps.top_score.astype(float), bins=[i / 20 for i in range(21)]).value_counts().sort_index()
        st.bar_chart(pd.DataFrame({"questions": counts.values}, index=[f"{b.left:.2f}" for b in counts.index]))
        low = frame(f"""SELECT r.question, r.top_score, r.bot_id, t.topic_label, r.trace_id FROM {LOG} r
                        LEFT JOIN {s.fq('question_embeddings')} t USING (request_id)
                        {where('r.')}
                        AND r.outcome = 'no_source' ORDER BY r.top_score LIMIT 10""")
        st.caption("Best search score per question (1 = exact match). Lowest-scoring unanswered questions "
                   "(select one to open its trace):")
        if not low.empty:
            j = pick(low.drop(columns=["trace_id"]), "gaps")
            if j is not None:
                open_trace(low.trace_id.iloc[j], "gaps")
            if st.button("Send these to each chatbot's review queue", help=help_text("review_queue")):
                import mlflow
                from factory.reviews import queue_traces
                mlflow.set_tracking_uri("databricks")
                mlflow.set_experiment(experiment_id=trace_experiment_id())
                try:
                    n = sum(queue_traces(cp().get_config(b), g.trace_id.dropna().tolist())
                            for b, g in low.groupby("bot_id"))
                    st.success(f"{n} question(s) queued for review.")
                except Exception as e:  # noqa: BLE001 - Review Queues is a Beta preview
                    st.error(f"Couldn't queue them: {str(e)[:200]}")

left, right = st.columns(2)
with left:
    st.subheader("Conversations")
    conv = frame(f"""WITH t AS (SELECT conversation_id, ts, outcome,
                        lag(outcome) OVER (PARTITION BY conversation_id ORDER BY ts) AS prev,
                        row_number() OVER (PARTITION BY conversation_id ORDER BY ts DESC) AS last
                     FROM {LOG} {WHERE} AND conversation_id IS NOT NULL)
                     SELECT count(*) / count(DISTINCT conversation_id) AS per_conv,
                            avg(CASE WHEN prev IN ('no_source', 'refused', 'unverified') THEN 1.0 ELSE 0 END) AS reasked,
                            count_if(last = 1 AND outcome IN ('no_source', 'refused', 'blocked')) / count(DISTINCT conversation_id) AS left_after_miss
                     FROM t""")
    if not conv.empty:
        x = conv.loc[0]
        c = st.columns(3)
        c[0].metric("Questions per conversation", f"{float(x.per_conv or 0):.1f}")
        c[1].metric("Asked again after a miss", f"{float(x.reasked or 0):.0%}")
        c[2].metric("Left after a miss", f"{float(x.left_after_miss or 0):.0%}")
with right:
    st.subheader("Guardrails", help=help_text("guardrails"))
    if not guard.empty:
        hit = clickable_bars(guard, "rule", "events", "guard_all")
        st.caption("Click a bar to see and download every event behind it.")
        if hit:
            drill(f"Guardrail: {hit}", sql().query(
                f"""SELECT ts, bot_id, action, reason, trace_id FROM {s.fq('guardrail_events')} WHERE rule = :r
                    AND ts >= current_date() - INTERVAL {int(days)} DAYS AND (:b = '' OR bot_id = :b)
                    ORDER BY ts DESC LIMIT 5000""", {**P, "r": hit}))

st.subheader("Recurring problems", help=help_text("issues"))
issues = frame(f"""SELECT bot_id, scorer, topic, failures, last_7_days, prior_7_days, latest_reason, example_traces
                   FROM {s.fq('v_failure_patterns')} WHERE (:b = '' OR bot_id = :b)
                   ORDER BY last_7_days DESC, failures DESC LIMIT 50""")
if issues.empty:
    st.caption("No judge failures in the last 28 days.")
else:
    issues["Trend"] = issues.apply(lambda r: "New" if not r.prior_7_days else f"{r.last_7_days / r.prior_7_days - 1:+.0%}", axis=1)
    st.caption("Judge failures grouped by judge and topic. Select one to open an example trace.")
    k = pick(issues.rename(columns={"bot_id": "Chatbot", "scorer": "Judge", "topic": "Topic", "failures": "Failures",
                                    "last_7_days": "Last 7 days", "latest_reason": "Latest judge reason"})[
        ["Chatbot", "Judge", "Topic", "Failures", "Last 7 days", "Trend", "Latest judge reason"]], "issues")
    if k is not None:
        ex = issues.example_traces.iloc[k]
        ex = ex if isinstance(ex, list) else str(ex).strip("[]").replace('"', "").split(",")
        if ex and ex[0].strip():
            open_trace(ex[0].strip(), "issues")

st.subheader("Every chatbot")
table = frame(f"""SELECT bot_id, count(*) AS questions, avg(CASE WHEN outcome = 'answered' THEN 1.0 ELSE 0 END) AS answered,
                  percentile_approx(latency_ms, 0.95) / 1000 AS p95_s, sum(cost_usd) AS cost
                  FROM {LOG} WHERE ts >= current_date() - INTERVAL {int(days)} DAYS AND channel = 'live' AND NOT synthetic
                  GROUP BY bot_id""")
base = pd.DataFrame([{"bot_id": b, "Chatbot": v["display_name"], "Status": STATE_LABEL.get(v["state"], v["state"]),
                      "Owner": v.get("owner_user")} for b, v in bots.items()])
if not base.empty:
    allrows = base.merge(table, on="bot_id", how="left").fillna({"questions": 0}).sort_values("questions", ascending=False)
    find = st.text_input("Search chatbots")
    if find:
        allrows = allrows[allrows.Chatbot.str.contains(find, case=False)]
    per = 25
    pages = max(1, -(-len(allrows) // per))
    page = st.number_input(f"Page (of {pages})", 1, pages, 1)
    st.dataframe(allrows.iloc[(page - 1) * per: page * per].drop(columns=["bot_id"]), hide_index=True,
                 use_container_width=True, column_config={"answered": st.column_config.NumberColumn("Answered", format="percent"),
                                                          "cost": st.column_config.NumberColumn("Cost", format="dollar"),
                                                          "p95_s": st.column_config.NumberColumn("Slow (p95, s)", format="%.1f")})
    st.download_button("Download CSV", allrows.to_csv(index=False), file_name=f"chatbots_{days}d.csv")
