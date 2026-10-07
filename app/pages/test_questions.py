"""Golden set review: approve, edit, delete, add (EVL-1..4, EVL-7)."""
import uuid

import pandas as pd
import streamlit as st

from common import cp, current_user, pick_bot, run_job, settings, sql
from factory.explain import help_text
from factory.ingestion import BotPaths
from ui import bar, html

st.title("Test questions", help=help_text("test_questions"))
st.caption("These questions check your chatbot before launch and after every change. "
           "Approve the ones that are right; edit or delete the rest.")
st.info("Add a few questions of your own, the way real users would ask them. \"Hard\" questions "
        "test tricky content like tables, footnotes and answers spread across pages.")
bot, cfg = pick_bot()
if not bot:
    st.stop()
p, user = BotPaths(settings(), cfg.bot_id), current_user()

rows = sql().query(f"SELECT * FROM {p.t('golden_set')} WHERE active ORDER BY kind, origin, question")
COLS = ["eval_id", "kind", "difficulty", "origin", "question", "expected_answer", "approved"]
df = pd.DataFrame(rows, columns=COLS) if rows else pd.DataFrame(columns=COLS)
df["approved"] = df["approved"].astype(str).str.lower() == "true"
n_req, n_ok = settings().get("quality.min_golden_questions", 50), int(df["approved"].sum())
html(f'<b style="font-size:24px">{n_ok}</b> <span class="cf-muted">of {n_req} approved</span>{bar(n_ok, n_req)}'
     + (f'<span class="cf-muted">{n_req - n_ok} more needed before you can send for approval</span>'
        if n_ok < n_req else '<span class="cf-muted">Enough approved questions to go live.</span>'))
df["delete"] = False
labels = {"in_scope": "Should answer", "out_of_scope": "Should refuse", "feedback": "From user feedback"}
df["kind"] = df["kind"].map(lambda k: labels.get(k, k))

edited = st.data_editor(
    df, hide_index=True, use_container_width=True, num_rows="dynamic",
    column_config={
        "eval_id": None, "origin": st.column_config.TextColumn("Source", disabled=True),
        "difficulty": st.column_config.TextColumn("Difficulty", disabled=True),
        "kind": st.column_config.SelectboxColumn("Type", options=list(labels.values()), required=True),
        "question": st.column_config.TextColumn("Question", width="large", required=True),
        "expected_answer": st.column_config.TextColumn("Expected answer", width="large"),
        "approved": st.column_config.CheckboxColumn("Approved"),
        "delete": st.column_config.CheckboxColumn("Delete"),
    })

st.caption("Double-click any cell to change it in the table, or edit one question at a time below.")
rev = {v: k for k, v in labels.items()}

with st.expander("Edit a question", icon=":material/edit:"):
    pick = st.selectbox("Question", df["eval_id"].tolist(), index=None, placeholder="Choose a question to edit",
                        format_func=lambda i: df.loc[df.eval_id == i, "question"].iloc[0][:120])
    if pick:
        row = df[df.eval_id == pick].iloc[0]
        with st.form(f"edit_{pick}"):
            q = st.text_area("Question", row["question"])
            a = st.text_area("Expected answer", row["expected_answer"] or "")
            k = st.selectbox("Type", list(labels.values()), index=list(labels.values()).index(row["kind"])
                             if row["kind"] in labels.values() else 0)
            ok = st.checkbox("Approved", bool(row["approved"]))
            if st.form_submit_button("Save question", type="primary"):
                sql().execute(f"UPDATE {p.t('golden_set')} SET question = :q, expected_answer = :a, kind = :k, "
                              "approved = CAST(:ap AS BOOLEAN), updated_by = :u, updated_at = current_timestamp() "
                              "WHERE eval_id = :id", {"q": q, "a": a, "k": rev.get(k, "in_scope"), "ap": ok,
                                                      "u": user, "id": pick})
                cp().audit(user, cfg.bot_id, "golden_question_edited", pick)
                run_job("ingest", bot_id=cfg.bot_id, publish_only="true")
                st.success("Saved.")
                st.rerun()

if st.button("Save changes", type="primary"):
    for _, r in edited.iterrows():
        kind = rev.get(r["kind"], "in_scope")
        if not r.get("eval_id") or pd.isna(r.get("eval_id")):
            if r["question"]:
                sql().execute(f"""INSERT INTO {p.t('golden_set')} (eval_id, question, expected_answer, kind,
                              difficulty, question_type, origin, approved, active, updated_by, updated_at)
                              VALUES (:id, :q, :a, :k, 'hard', 'owner', 'manual', CAST(:ap AS BOOLEAN), true,
                              :u, current_timestamp())""",
                              {"id": str(uuid.uuid4()), "q": r["question"], "a": r["expected_answer"] or "",
                               "k": kind, "ap": bool(r["approved"]), "u": user})
        elif r["delete"]:
            sql().execute(f"UPDATE {p.t('golden_set')} SET active = false, updated_by = :u, "
                          "updated_at = current_timestamp() WHERE eval_id = :id",
                          {"u": user, "id": r["eval_id"]})
        else:
            sql().execute(f"UPDATE {p.t('golden_set')} SET question = :q, expected_answer = :a, kind = :k, "
                          "approved = CAST(:ap AS BOOLEAN), updated_by = :u, updated_at = current_timestamp() "
                          "WHERE eval_id = :id",
                          {"q": r["question"], "a": r["expected_answer"] or "", "k": kind,
                           "ap": bool(r["approved"]), "u": user, "id": r["eval_id"]})
    cp().audit(user, cfg.bot_id, "golden_set_saved", "", {"rows": len(edited)})
    run_job("ingest", bot_id=cfg.bot_id, publish_only="true")  # new golden hash on the test version
    st.success("Saved.")
    st.rerun()

c1, c2 = st.columns(2)
if c1.button("Suggest questions from my approved documents"):
    run_job("ingest", bot_id=cfg.bot_id, golden_only="true")
    st.info("Generating in the background. Refresh in a few minutes.")
if c2.button("▶️ Run a check now"):
    run_job("evals", bot_id=cfg.bot_id, trigger="manual", channel="candidate")
    st.info("Checking your chatbot. Results appear on the Launch page.")
