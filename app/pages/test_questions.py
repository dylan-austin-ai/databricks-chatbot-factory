"""Test questions for one chatbot: approve, edit, remove, add (EVL-1..4, EVL-7).

Always for a single chatbot: each chatbot has its own questions, checked against its own
documents."""
import pandas as pd
import streamlit as st

from common import cp, current_user, job_statuses, pick_bot, run_job, settings, sql, update_in_progress, watch_job
from factory import golden
from factory.explain import help_text
from factory.ingestion import BotPaths
from ui import bar, html

st.title("Test questions", help=help_text("test_questions"))
bot, cfg = pick_bot()  # test questions always belong to one chatbot
if not bot:
    st.stop()
p, user = BotPaths(settings(), cfg.bot_id), current_user()

latest = sql().query(f"""SELECT eval_id, kind, difficulty, origin, question, expected_answer, approved
                         FROM {p.t('golden_set')} WHERE active ORDER BY kind, origin, question""")
for r in latest:
    r["approved"] = str(r["approved"]).lower() == "true"
# The table's unsaved ticks and edits are remembered by row position. While there are any, keep
# showing the rows they were made on: questions written in the background must not slide in
# underneath and move a tick onto a different question.
editor_key = f"questions_{cfg.bot_id}_{st.session_state.get('questions_round', 0)}"
shown_key = f"questions_shown_{cfg.bot_id}"
edits = st.session_state.get(editor_key) or {}
if any(edits.get(k) for k in ("edited_rows", "added_rows", "deleted_rows")) and shown_key in st.session_state:
    rows = st.session_state[shown_key]
else:
    rows = st.session_state[shown_key] = latest
arrived = len({r["eval_id"] for r in latest} - {r["eval_id"] for r in rows})
n_req, n_ok = int(settings().get("quality.min_golden_questions", 50)), sum(r["approved"] for r in rows)

st.caption(f"Before **{cfg.display_name}** can go live, and after every change, it is asked these questions and its "
           "answers are compared with the expected ones. Your job here is to make sure the questions and expected "
           "answers are right.")
html(f'<b style="font-size:24px">{n_ok}</b> <span class="cf-muted">of {n_req} approved</span>{bar(n_ok, n_req)}'
     + (f'<span class="cf-muted">{n_req - n_ok} more needed before the quality check can pass</span>'
        if n_ok < n_req else '<span class="cf-muted">Enough approved questions to go live.</span>'))
if st.session_state.get("questions_saved"):
    st.success(st.session_state.pop("questions_saved"))
watch_job("ingest_publish", cfg.bot_id)   # the test version being rebuilt after a save, if it is

with st.container(border=True):
    st.markdown("**How to use this page**")
    st.markdown(
        "1. **Read each row.** Is the question something a real person would ask, and is the expected answer "
        "correct?\n"
        "2. **Right?** Tick **Approved**. **Nearly right?** Double-click the cell, fix the wording, then tick "
        "**Approved**. **Wrong or pointless?** Tick **Remove**.\n"
        "3. **Add your own** in the empty row at the bottom of the table, the way people really ask. Questions about "
        "tables, footnotes and answers spread over several pages are the most useful.\n"
        "4. Press **Save changes**. Nothing is saved until you do.")

if not rows:
    st.info("There are no questions yet. Press **Suggest questions from my approved documents** below, wait a few "
            "minutes, then press **Refresh** in the menu on the left. You can also type your own in the table.")

df = pd.DataFrame(rows, columns=["eval_id", "kind", "difficulty", "origin", "question", "expected_answer", "approved"])
df["kind"] = df["kind"].map(lambda k: golden.KIND_LABEL.get(k, k))
df["delete"] = False
edited = st.data_editor(
    df, hide_index=True, width="stretch", num_rows="dynamic",
    key=editor_key,
    column_config={
        "eval_id": None, "origin": st.column_config.TextColumn("Source", disabled=True),
        "difficulty": st.column_config.TextColumn("Difficulty", disabled=True),
        "kind": st.column_config.SelectboxColumn(
            "Type", options=list(golden.KIND_LABEL.values()), required=True, default="Should answer",
            help="\"Should answer\": the answer is in the documents. \"Should refuse\": the chatbot must "
                 "politely decline (for example a request for legal advice)."),
        "question": st.column_config.TextColumn("Question", width="large", required=True),
        "expected_answer": st.column_config.TextColumn("Expected answer", width="large",
                                                       help="Leave empty for questions the chatbot should refuse."),
        "approved": st.column_config.CheckboxColumn("Approved", default=False),
        "delete": st.column_config.CheckboxColumn("Remove", default=False),
    })

delta = golden.changes(rows, edited.to_dict("records"))
pending = golden.counts(delta)
unsaved = pending != "nothing to save"
if unsaved:
    st.warning(f"You have unsaved changes: {pending}. Press **Save changes** to keep them.")
if arrived:
    st.info(f"{arrived} new question(s) have arrived. They will appear after you save your changes.")


def save(change: dict, message: str) -> None:
    """Write only what changed (at most three statements), then rebuild the test version once."""
    for statement, params in golden.save_statements(p, change, user):
        sql().execute(statement, params)
    cp().audit(user, cfg.bot_id, "golden_set_saved", "", {k: len(v) for k, v in change.items()})
    run_job("ingest", bot_id=cfg.bot_id, publish_only="true")  # new question set on the test version
    st.session_state["questions_saved"] = message
    st.session_state["questions_round"] = st.session_state.get("questions_round", 0) + 1  # a clean table
    st.rerun()


b1, b2, _ = st.columns([1, 1, 2])
if b1.button("Save changes", type="primary", disabled=not unsaved, width="stretch"):
    save(delta, f"Saved: {pending}. The test version is being updated, and a quality check runs at the end of "
                "the update once enough questions are approved. Progress is shown at the top of this page.")

not_yet = [r for r in rows if not r["approved"]]
with b2.popover(f"Approve all {len(not_yet)} remaining", disabled=not not_yet or unsaved, width="stretch"):
    st.markdown(f"This marks **all {len(not_yet)} unapproved question(s)** as correct without you reading them. "
                "The quality check will then hold the chatbot to these expected answers, so a wrong one makes a "
                "good chatbot look bad. Only do this if you have read them.")
    if st.checkbox("I have read them and they are right", key="approve_all_sure") and \
            st.button("Approve them all", type="primary"):
        everything = [{**r, "approved": True} for r in rows]
        save(golden.changes(rows, everything), f"Approved {len(not_yet)} question(s).")
if unsaved and not_yet:
    b2.caption("Save your changes first.")

st.divider()
st.markdown("**More you can do**")
c1, c2 = st.columns(2)
with c1.container(border=True):
    st.markdown("**Need more questions?**")
    st.caption("We read your approved documents and write questions with expected answers. They arrive unapproved, "
               "for you to check. Takes a few minutes.")
    writing = watch_job("ingest_questions", cfg.bot_id)   # real progress, and the new questions when it ends
    if st.button("Writing questions…" if writing.active else "Suggest questions from my approved documents",
                 disabled=writing.active, key="suggest_questions"):
        run_job("ingest", bot_id=cfg.bot_id, golden_only="true")
        st.rerun()
with c2.container(border=True):
    st.markdown("**Ready to see how the chatbot does?**")
    cap = int(settings().get("quality.check.max_questions", 50) or 0)
    st.caption("Asks the chatbot the approved questions and scores the answers"
               + (f" (a fixed sample of {max(cap, n_req)} spread across your documents, when more are approved)"
                  if cap else "")
               + ". It runs by itself at the end of every update of the test version; this starts it on demand. The "
                 "results appear on the **Launch** page.")
    has_version = bool(bot.get("candidate_release_id"))
    checking = watch_job("evals", cfg.bot_id)   # real progress while it runs; what to do if it stopped
    updating = update_in_progress(job_statuses(cfg.bot_id))   # an update ends with a check of its own
    busy = "Quality check running…" if checking.active else "Runs when the update finishes" if updating else ""
    if st.button(busy or "Run the quality check", key="run_check",
                 disabled=bool(busy) or n_ok < n_req or not has_version,
                 help=None if busy else f"Available once {n_req} questions are approved." if n_ok < n_req
                 else None if has_version else "There is no test version to check yet. One is built a few minutes "
                                               "after you approve documents or save questions."):
        run_job("evals", bot_id=cfg.bot_id, trigger="manual", channel="candidate")
        st.rerun()
