"""Launch, for one chatbot: the path to launch, the test version and its quality check, approval,
the live version, roll back and the documentation (REL-1..11, EVG-1..5, LCY-1..7, CON-1/2/5).

Always for a single chatbot: a version is tested, approved and published per chatbot."""
import json

import streamlit as st

from common import (METRIC_LABEL, PAGES, RETRIEVAL_METRICS, STATE_LABEL, cp, current_user, fresh_sql, is_admin,
                    job_statuses, pick_bot, read_many, run_job, settings, soft, sql, user_groups, watch_job)
from factory import jobruns, journey
from factory.config import MAX_TESTERS
from factory.explain import help_text
from factory.ingestion import BotPaths
from factory.lifecycle import State
from factory.modelcard import model_card
from factory.problems import check_advice
from factory.provisioning import Provisioner
from ui import STATE_TONE, checklist, html, pill, tile

st.title("Launch")
bot, cfg = pick_bot(during_setup=True)
if not bot:
    st.stop()
s, user, control = settings(), current_user(), cp()
p = BotPaths(s, cfg.bot_id)
state, b = bot["state"], {"b": cfg.bot_id}
groups = user_groups(user)
is_owner = is_admin() or user == cfg.owner_user or cfg.owner_group in groups
is_reviewer = is_admin() or user == cfg.reviewer or cfg.reviewer in groups
setting_up = state in ("provisioning", "provision_failed")


def start(kind: str, **params) -> None:
    """Start a background job and reload, so the page shows the run itself: waiting, running with
    a progress bar, or stopped with an error. Nothing here claims a job is running; the status
    comes from Databricks each time (common.watch_job)."""
    run_job(kind, bot_id=cfg.bot_id, **params)
    st.rerun()


# Everything the page needs, asked for at the same time ---------------------------------------
queries = {"history": (f"SELECT * FROM {s.fq('releases')} WHERE bot_id = :b ORDER BY seq DESC", b)}
if setting_up:
    queries["setup"] = (f"""SELECT step, status FROM {s.fq('provisioning_steps')} WHERE bot_id = :b
                            QUALIFY ROW_NUMBER() OVER (PARTITION BY step ORDER BY updated_at DESC) = 1""", b)
else:
    queries.update({
        "documents": (f"SELECT doc_name, doc_version, status, readability FROM {p.t('manifest')} "
                      "WHERE status <> 'superseded'", None),
        "questions": (f"SELECT count(*) AS questions, count_if(approved) AS questions_ok "
                      f"FROM {p.t('golden_set')} WHERE active", None),
        "search": (f"""SELECT explanation FROM {s.fq('strategy_results')} WHERE bot_id = :b AND chosen
                       ORDER BY ts DESC LIMIT 1""", b),
        "checks": (f"""SELECT * FROM {s.fq('eval_runs')} WHERE run_id IN (
                         SELECT eval_run_id FROM {s.fq('releases')} WHERE release_id IN (:c, :l))""",
                   {"c": bot.get("candidate_release_id") or "", "l": bot.get("live_release_id") or ""}),
    })
found, failed = read_many(queries)
history = found["history"]
by_id = {r["release_id"]: r for r in history}
cand, live = by_id.get(bot.get("candidate_release_id")), by_id.get(bot.get("live_release_id"))
checks = {r["run_id"]: r for r in found.get("checks", [])}


def ver(r):
    """A version's number as people say it: release claims_bot-r0003 is version 3."""
    return str(int(r["release_id"].rsplit("-r", 1)[-1])) if r else None


html(pill(STATE_LABEL.get(state, state), STATE_TONE.get(state, "gray"))
     + (pill(f"Live v{ver(live)}", "green") if live else "") + (pill(f"Test v{ver(cand)}", "amber") if cand else ""))

# Setup still running, or stopped ----------------------------------------------------------------
if setting_up:
    st.subheader("Setting up")
    setup = watch_job("provision", cfg.bot_id, quiet=True)   # the failed step below says what went wrong
    if state == "provisioning" and not setup.active:
        st.info(f"**{cfg.display_name}** is being set up in the background. This usually takes about 10 minutes. "
                "You don't need to stay on this page. Press **Refresh** in the menu on the left to see progress.")
    for r in found["setup"]:
        html(pill("Done" if r["status"] == "done" else "Failed", "green" if r["status"] == "done" else "red")
             + r["step"].replace("_", " ").capitalize())
    if state == "provision_failed" and not setup.active:
        st.error("Setup stopped at the step marked **Failed**. Press **Try again**: steps that already finished are "
                 "skipped, and nothing you entered is lost. If it stops at the same step a second time, send MLOps "
                 f"the chatbot's technical name (`{cfg.bot_id}`) and the name of the failed step.")
        if st.button("Try again", type="primary"):
            start("provision", actor=user)
    elif setup.active:
        st.button("Setup running…", disabled=True)
    for name, error in failed.items():
        soft("Setup progress", error)
    st.stop()

# What to do next ------------------------------------------------------------------------------
doc_rows = found["documents"]
docs_count = {"reading": sum(d["status"] == "pending_review" and not d["readability"] for d in doc_rows),
              "waiting": sum(d["status"] == "pending_review" and bool(d["readability"]) for d in doc_rows),
              "flagged": sum(d["status"] == "flagged" for d in doc_rows),
              "approved": sum(d["status"] == "approved" for d in doc_rows)}
q = (found["questions"] or [{}])[0]
needed = int(s.get("quality.min_golden_questions", 50))
jobs_now = job_statuses(cfg.bot_id)   # where each background job stands, as Databricks reports it
checking = jobs_now.get("evals", jobruns.RunStatus()).active
path = journey.steps(state, docs_count, {"total": q.get("questions"), "approved": q.get("questions_ok")}, needed,
                     cand, bool(live), cfg.reviewer, is_reviewer, checking=checking)
now = journey.next_step(state, path, bool(cand))
if now.status == journey.DONE or state in ("paused", "budget_paused", "archived"):
    (st.success if now.status == journey.DONE else st.info)(now.detail)
else:
    st.subheader("What to do next" + (" for the new version" if live else ""))
    st.caption("The steps to get this chatbot live. The one marked **Your turn** is what to do now.")
    checklist(path, journey.STATUS_LABEL, PAGES, here="launch")
watch_job("ingest_publish", cfg.bot_id)   # the test version being rebuilt after a change, if it is

# Test version: checks and approval ------------------------------------------------------
if cand:
    st.subheader(f"Test version {ver(cand)}", help=help_text("review"))
    st.caption("Only owners, the Reviewer and invited testers can use it. Try it on the **Chat** page with "
               "**Use the test version** switched on.")
    if found["search"]:
        st.info(found["search"][0]["explanation"])
    ev = checks.get(cand.get("eval_run_id"))
    passed = str(cand.get("eval_passed")).lower() == "true"
    check = watch_job("evals", cfg.bot_id)   # progress while it runs; what to do if it stopped
    if not ev:
        if not check.active:
            st.write("The quality check hasn't run on this version yet. It asks the chatbot every approved test "
                     "question and scores the answers.")
            if check.state == jobruns.SUCCEEDED:
                st.warning("A quality check finished earlier but left no result for this version. Either the test "
                           "version has changed since, or that run found nothing to check. Run it again.")
        if st.button("Quality check running…" if check.active else "Run the quality check", type="primary",
                     disabled=check.active, key="run_check"):
            start("evals", trigger="manual", channel="candidate")
    else:
        data = json.loads(ev["metrics_json"])
        th, m = s.thresholds(), data.get("metrics", {})
        (st.success if passed else st.error)(
            "**The quality check passed.** Every result below meets its target." if passed else
            "**The quality check didn't pass.** The results marked **Below** need work; what to do about each is "
            "listed underneath.")
        for title, keys, why in [("Finding the right document", RETRIEVAL_METRICS, "search"),
                                 ("Answer quality", [k for k in METRIC_LABEL if k not in RETRIEVAL_METRICS], "judges")]:
            st.markdown(f"**{title}**", help=help_text(why))
            shown = [k for k in keys if k in m]
            for col, k in zip(st.columns(max(len(shown), 1)), shown):
                with col:
                    html(tile(METRIC_LABEL[k], m[k], th.get(k)))
        if data.get("failures"):
            with st.container(border=True):
                st.markdown("**What needs fixing**")
                for f in data["failures"]:
                    st.markdown(f"- **{f}**  \n  What to do: {check_advice(f)}")
        else:
            st.caption("No wrong citations, no leaked content, no unmasked personal data.")
        if (not passed or check.active) and st.button(
                "Quality check running…" if check.active else "Run the quality check again", disabled=check.active,
                key="run_check_again"):
            start("evals", trigger="manual", channel="candidate")
        if str(ev["needs_safety_review"]).lower() == "true" and is_reviewer:
            unsafe = [c for c in data.get("per_case", []) if c["scores"].get("safety") == 0]
            with st.expander(f"Review {len(unsafe)} answer(s) flagged as possibly unsafe"):
                st.caption("Reviewer only. Read each one. If they are all acceptable, say why and press the button; "
                           "your reason is recorded. If any is not acceptable, don't override: the documents or "
                           "questions need changing first.")
                for c in unsafe:
                    st.markdown(f"**Q:** {c['question']}\n\n*{c['rationales'].get('safety', '')}*")
                why = st.text_input("Why these are acceptable (required to override)")
                if why and st.button("Override and re-check"):
                    sql().execute(f"""UPDATE {s.fq('releases')} SET safety_override_by = :u,
                                      safety_override_reason = :w WHERE release_id = :r""",
                                  {"u": user, "w": why, "r": cand["release_id"]})
                    control.audit(user, cfg.bot_id, "safety_override", cand["release_id"], {"reason": why})
                    start("evals", trigger="safety_override", channel="candidate")

    with st.container(border=True):
        st.markdown("**Approval**")
        publishing = watch_job("promote", cfg.bot_id)   # progress while publishing; what to do if it stopped
        if publishing.active:
            st.caption("Approved. A final test runs before the switch; if it fails, the current version stays "
                       "live and an alert appears on Home.")
        elif cand.get("review_requested_at") is None:
            st.write(f"When the quality check has passed, send this version to **{cfg.reviewer}** for approval. "
                     "Nothing reaches users until they approve.")
            if is_owner and st.button("Send for approval", type="primary", disabled=not passed,
                                      help=None if passed else "Available once the quality check passes."):
                sql().execute(f"UPDATE {s.fq('releases')} SET review_requested_at = current_timestamp() "
                              "WHERE release_id = :r", {"r": cand["release_id"]})
                control.audit(user, cfg.bot_id, "submitted_for_approval", cand["release_id"])
                if state == State.TESTING.value:
                    control.set_state(cfg.bot_id, State.PENDING_APPROVAL.value, user)
                st.rerun()
            if not is_owner:
                st.caption("Only the chatbot's owner can send it for approval.")
        elif not is_reviewer:
            st.write(f"Sent for approval. Waiting for **{cfg.reviewer}** to decide; there is nothing for you to do "
                     "until they do. They see it on their Home page.")
        else:
            st.write("This version is waiting for **your** decision. Choose one:")
            a1, a2 = st.columns(2)
            a1.markdown("**Approve and publish**  \nThis version replaces the live one and people start getting "
                        "its answers. A final test runs first; if it fails, the current version stays live.")
            a2.markdown("**Send back for changes**  \nNothing is published. The owner can change documents or "
                        "questions and send it to you again.")
            if a1.button("Approve and publish", type="primary", disabled=not passed,
                         help=None if passed else "Available once the quality check passes."):
                control.audit(user, cfg.bot_id, "approved", cand["release_id"])
                start("promote", actor=user, action="promote")
            if a2.button("Send back for changes"):
                sql().execute(f"UPDATE {s.fq('releases')} SET review_requested_at = NULL WHERE release_id = :r",
                              {"r": cand["release_id"]})
                control.audit(user, cfg.bot_id, "approval_withdrawn", cand["release_id"])
                if state == State.PENDING_APPROVAL.value:
                    control.set_state(cfg.bot_id, State.TESTING.value, user)
                st.rerun()

    if is_owner:
        with st.expander(f"Testers ({len(cfg.testers)} of {MAX_TESTERS})"):
            st.caption("People who may try the test version before it is published. One work email per line.")
            txt = st.text_area("One email per line", "\n".join(cfg.testers))
            if st.button("Save testers"):
                try:
                    Provisioner(fresh_sql(), s, control).set_testers(cfg, txt.splitlines(), user)
                    st.success("Saved. Testers can pick \"Test version\" on the Chat page.")
                except ValueError as e:
                    st.error(str(e))

# Live version ---------------------------------------------------------------------
if live:
    st.subheader(f"Live version {ver(live)}")
    st.caption(f"Published {live['promoted_at']} · approved by {live['approved_by']} · "
               f"{live['doc_count']} documents")
    if "api" in cfg.channels:
        st.code(f"""POST /serving-endpoints/{s.get('agent.serving_endpoint')}/invocations
{{"input": [{{"role": "user", "content": "your question"}}],
 "custom_inputs": {{"bot_id": "{cfg.bot_id}", "end_user": "<user email, trusted callers only>"}}}}""",
                language="json")
    if is_owner or is_reviewer:
        b1, b2 = st.columns(2)
        if state == "live" and b1.button("Pause", help="Stops the chatbot answering questions until you press "
                                                       "Resume. Nothing is deleted."):
            control.set_state(cfg.bot_id, State.PAUSED.value, user)
            st.rerun()
        if state in ("paused", "budget_paused") and b1.button("Resume", type="primary",
                                                              help="Turns the chatbot back on."):
            control.set_state(cfg.bot_id, State.LIVE.value, user)
            st.rerun()
        previous = next((r for r in history if r["status"] == "superseded"), None)
        with b2:
            rolling = watch_job("rollback", cfg.bot_id)
        if rolling.active:
            pass
        elif previous:
            with b2.popover("Roll back to the previous version"):
                st.markdown(f"This takes version **{ver(live)}** out of service and puts version "
                            f"**{ver(previous)}** back. People get the older answers within a few minutes. Use it "
                            "when the new version is giving wrong answers. Your documents and test questions are "
                            "not changed.")
                if st.button(f"Yes, go back to version {ver(previous)}", type="primary"):
                    start("promote", actor=user, action="rollback", reason="owner rollback")
        else:
            b2.caption("There is no earlier version to roll back to yet.")

if is_owner and state in ("live", "paused", "testing"):
    with st.expander("Archive this chatbot"):
        st.write("An archived chatbot stops answering questions straight away. Its documents, test questions and "
                 "history are kept, and MLOps can restore it. Do this when the chatbot is no longer needed.")
        if st.checkbox(f"Yes, I want to archive {cfg.display_name}", key="archive_sure") and \
                st.button("Archive", type="primary"):
            control.set_state(cfg.bot_id, State.ARCHIVED.value, user)
            st.rerun()

# Documentation: model card + release history (CON-5, REL-8) ------------------------------
latest_metrics = {}
if live and live.get("eval_run_id") in checks:
    latest_metrics = json.loads(checks[live["eval_run_id"]]["metrics_json"])
if history:
    st.subheader("Release history")
    st.dataframe([{"Version": f"v{ver(r)}", "Status": r["status"].replace("_", " ").title(),
                   "Published": r.get("promoted_at") or "—", "Approved by": r.get("approved_by") or "—",
                   "Quality check": "Passed" if str(r.get("eval_passed")).lower() == "true" else "Not passed",
                   "Smoke test": {"true": "Passed", "false": "Failed"}.get(str(r.get("smoke_passed")).lower(), "—")}
                  for r in history], hide_index=True, width="stretch")
st.subheader("Documentation")
st.caption("A one-page description of this chatbot for auditors and anyone who asks what it does: its purpose, "
           "owners, documents and latest quality results.")
card = model_card(cfg, s, STATE_LABEL.get(state, state), doc_rows, latest_metrics, bot["config_version"],
                  history)
with st.expander("View model card"):
    st.markdown(card)
st.download_button("Download model card", card, file_name=f"{cfg.bot_id}_model_card.md")

WHAT = {"history": "The list of versions", "documents": "The document list", "questions": "The test question count",
        "search": "The search method note", "checks": "The quality check results"}
for name, error in failed.items():
    soft(WHAT.get(name, name), error)
