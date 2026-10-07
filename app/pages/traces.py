"""Traces (OBS-18, OBS-19): the typical flow of every answer for a chatbot, and any single trace with
complete transparency: the question, what the AI was sent and returned, tokens, time to first
token, cost, search and reranker results, and every guardrail decision."""
import json

import numpy as np
import pandas as pd
import streamlit as st

from common import ROOT, app_client, current_user, is_admin, load_traces, pick_bot, settings, sql, trace_experiment_id, user_groups
from factory.explain import help_text
from factory.flow import aggregate, code_excerpt, describe, tree
from ui import html, pill

ICON = {"AGENT": "AG", "RETRIEVER": "RT", "RERANKER": "RR", "LLM": "LM", "CHAIN": "CH", "GUARDRAIL": "GR",
        "TOOL": "TL", "MEMORY": "ME", "PARSER": "PR"}
st.title("Traces")
st.markdown("Every step of every answer, in order.", help=help_text("traces"))
bot, cfg = pick_bot()
if not bot:
    st.stop()
SOURCE = (ROOT / "agent" / "agent.py").read_text() if (ROOT / "agent" / "agent.py").exists() else ""


def fmt(ms: float) -> str:
    return f"{ms / 1000:.2f}s" if ms >= 1000 else f"{ms:.0f}ms"


def code(name: str) -> None:
    found = code_excerpt(SOURCE, name)
    st.caption(f"agent/agent.py, line {found[0]}" if found else "Added automatically by mlflow.openai.autolog()")
    st.code(found[1] if found else "mlflow.openai.autolog()", language="python")


def pick_row(df: pd.DataFrame, key: str) -> int:
    ev = st.dataframe(df, hide_index=True, use_container_width=True, on_select="rerun",
                      selection_mode="single-row", key=key, height=min(38 * (len(df) + 1), 700))
    rows = ev.selection.rows if ev and ev.selection else []
    return rows[0] if rows else 0


# Single traces hold raw questions and answers: MLOps and Security only (PRV-1).
raw_ok = is_admin() or settings().get("access.security_group") in user_groups(current_user())
jump = st.session_state.pop("open_trace", None) if raw_ok else None  # from All chatbots or Slowest runs
one = raw_ok and st.segmented_control("View", ["Typical flow", "One trace"],
                                      default="One trace" if jump else "Typical flow",
                                      help=help_text("flow")) == "One trace"
exp = trace_experiment_id()
if exp:
    st.markdown(f"[Open in MLflow]({app_client().config.host.rstrip('/')}/ml/experiments/{exp}/traces)")

if not one:
    traces = load_traces(cfg.bot_id, 300)
    if not traces:
        st.info("No traces yet. They appear after the first questions.")
        st.stop()
    rows = aggregate([t["spans"] for t in traces])
    st.caption(f"Typical time per step over the last {len(traces)} questions. Select a step for details.")
    df = pd.DataFrame({"Step": ["    " * r["depth"] + f"[{ICON.get(r['type'], 'ST')}] {r['name']}" for r in rows],
                       "Typical": [fmt(r["p50_ms"]) for r in rows], "Slow (p95)": [fmt(r["p95_ms"]) for r in rows],
                       "Runs on": [f"{min(r['runs'], 1):.0%}" for r in rows],
                       "Cost / question": [f"${r['cost_per_question']:.4f}" if r["cost_per_question"] else "—"
                                           for r in rows]})
    left, right = st.columns([5, 7])
    with left:
        r = rows[pick_row(df, "flow")]
    with right, st.container(border=True):
        plain, feature = describe(r["name"], r["type"])
        st.subheader(r["name"])
        st.caption(f"Databricks: {feature}")
        st.write(plain)
        c = st.columns(4)
        c[0].metric("Typical", fmt(r["p50_ms"]))
        c[1].metric("Slow (p95)", fmt(r["p95_ms"]))
        c[2].metric("Runs on", f"{min(r['runs'], 1):.0%} of questions")
        c[3].metric("Cost / question", f"${r['cost_per_question']:.4f}")
        counts, edges = np.histogram(r["durations"], bins=min(20, max(5, len(r["durations"]) // 5)))
        st.markdown("**How long it takes** (milliseconds)")
        st.bar_chart(pd.DataFrame({"questions": counts}, index=[f"{e:.0f}" for e in edges[:-1]]))
        if raw_ok:  # OBS-25: the slowest runs of this step, one click from their traces
            slow = sorted(((sp["end_ns"] - sp["start_ns"]) / 1e6, t["trace_id"]) for t in traces
                          for sp in t["spans"] if sp["name"] == r["name"])[::-1][:10]
            st.markdown("**Slowest runs of this step** (select one to open its trace)")
            k = pick_row(pd.DataFrame([{"Time": fmt(ms), "Trace": tid} for ms, tid in slow]), f"slow_{r['name']}")
            if slow and st.session_state.get(f"slow_{r['name']}", {}).get("selection", {}).get("rows"):
                st.session_state.pop(f"slow_{r['name']}", None)
                st.session_state["open_trace"] = slow[k][1]
                st.rerun()
        st.markdown("**Code that runs this step**")
        code(r["name"])
    st.stop()

# One trace -------------------------------------------------------------------------------------
s = settings()
q = st.text_input("Find a trace", jump or "", placeholder="Words from the question, or a trace ID")
recent = sql().query(
    f"""SELECT trace_id, ts, question, outcome, latency_ms, ttft_ms, input_tokens, output_tokens, cost_usd,
               retakes, channel, release_id, prompt_uri, user_hash, user_id
        FROM {s.fq('request_log')} WHERE (:b = '' OR bot_id = :b) AND trace_id IS NOT NULL
          AND (:q = '' OR trace_id = :q OR lower(question) LIKE concat('%', lower(:q), '%'))
        ORDER BY ts DESC LIMIT 50""", {"b": "" if q.strip().startswith("tr-") else cfg.bot_id, "q": q.strip()})
if not recent:
    st.info("No matching traces.")
    st.stop()
pick = pick_row(pd.DataFrame([{"When": r["ts"], "Question": (r["question"] or "")[:90], "Outcome": r["outcome"],
                               "Time": fmt(float(r["latency_ms"] or 0))} for r in recent]), "recent")
meta = recent[pick]
found = load_traces(None, trace_id=meta["trace_id"])
if not found:
    st.warning("This trace isn't readable yet; traces land within a few minutes.")
    st.stop()
with st.container(border=True):
    st.markdown(f"**Question:** {meta['question']}")
    c = st.columns(6)
    c[0].metric("Outcome", meta["outcome"])
    c[1].metric("Total time", fmt(float(meta["latency_ms"] or 0)))
    c[2].metric("First token", fmt(float(meta["ttft_ms"])) if meta["ttft_ms"] else "—")
    c[3].metric("Tokens in / out", f"{meta['input_tokens'] or 0} / {meta['output_tokens'] or 0}")
    c[4].metric("Cost", f"${float(meta['cost_usd'] or 0):.4f}")
    c[5].metric("Rewrites", meta["retakes"] or 0)
    st.caption(f"Trace {meta['trace_id']} · {meta['channel']} · release {meta['release_id']} · "
               f"{meta['prompt_uri']} · user {meta['user_id'] or meta['user_hash']}")
spans = tree(found[0]["spans"])
left, right = st.columns([5, 7])
with left:
    sp = spans[pick_row(pd.DataFrame({
        "Step": ["    " * x["depth"] + f"[{ICON.get(x['type'], 'ST')}] {x['name']}" for x in spans],
        "Time": [fmt(x["ms"]) for x in spans]}), "spans")]
with right, st.container(border=True):
    plain, feature = describe(sp["name"], sp["type"])
    st.subheader(sp["name"])
    st.caption(f"Databricks: {feature} · {fmt(sp['ms'])}")
    st.write(plain)
    a = sp["attributes"]
    if "llm.input_tokens" in a:
        c = st.columns(4)
        c[0].metric("Tokens in / out", f"{a.get('llm.input_tokens')} / {a.get('llm.output_tokens')}")
        c[1].metric("First token", fmt(float(a["llm.ttft_ms"])) if a.get("llm.ttft_ms") else "—")
        c[2].metric("Cost", f"${float(a.get('llm.cost_usd') or 0):.4f}")
        c[3].metric("Model", str(a.get("llm.model", "")).split(".")[-1])
    docs = sp["outputs"] if isinstance(sp["outputs"], list) and sp["outputs"] and "page_content" in sp["outputs"][0] else None
    if docs:
        st.markdown("**Search results**")
        st.dataframe(pd.DataFrame([{"Rank": d["metadata"].get("rank"), "Before rerank": d["metadata"].get("pre_rerank_rank"),
                                    "Document": d["metadata"].get("doc_name"), "Pages": d["metadata"].get("pages"),
                                    "Score": d["metadata"].get("score"), "Text": d["page_content"][:300]} for d in docs]),
                     hide_index=True, use_container_width=True)
    messages = (sp["inputs"] or {}).get("messages") if isinstance(sp["inputs"], dict) else None
    if messages:
        st.markdown("**Sent to the AI model**")
        for m in messages:
            with st.expander(f"{m.get('role', '')} · {len(str(m.get('content', '')))} characters", expanded=m.get("role") == "user"):
                st.text(m.get("content", ""))
    st.markdown("**Inputs**")
    st.json(sp["inputs"] if sp["inputs"] is not None else {}, expanded=not messages)
    st.markdown("**Outputs**")
    st.json(sp["outputs"] if sp["outputs"] is not None else {}, expanded=not docs)
    with st.expander("All recorded attributes"):
        st.json(json.loads(json.dumps(a, default=str)))
    st.markdown("**Code that runs this step**")
    code(sp["name"])
html(pill("Personal numbers are masked in traces before they're stored.", "blue"))
