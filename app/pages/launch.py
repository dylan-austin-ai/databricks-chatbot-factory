"""Test version vs live version: checks, testers, approval, publish, roll back, docs
(REL-1..11, EVG-1..5, LCY-1..7, CON-1/2/5)."""
import json

import streamlit as st

from common import (METRIC_LABEL, RETRIEVAL_METRICS, STATE_LABEL, cp, current_user, is_admin, pick_bot,
                    run_job, settings, sql, user_groups)
from factory.explain import help_text
from factory.config import MAX_TESTERS
from factory.ingestion import BotPaths
from factory.lifecycle import State
from factory.modelcard import model_card
from factory.provisioning import Provisioner
from ui import STATE_TONE, html, pill, tile

st.title("Launch")
bot, cfg = pick_bot()
if not bot:
    st.stop()
s, user, control = settings(), current_user(), cp()
p = BotPaths(s, cfg.bot_id)
state = bot["state"]
groups = user_groups(user)
is_owner = is_admin() or user == cfg.owner_user or cfg.owner_group in groups
is_reviewer = is_admin() or user == cfg.reviewer or cfg.reviewer in groups


def release(rid):
    rows = sql().query(f"SELECT * FROM {s.fq('releases')} WHERE release_id = :r", {"r": rid}) if rid else []
    return rows[0] if rows else None


cand, live = release(bot.get("candidate_release_id")), release(bot.get("live_release_id"))
def ver(r):
    return r["release_id"].rsplit("-", 1)[-1] if r else None


html(pill(STATE_LABEL.get(state, state), STATE_TONE.get(state, "gray"))
     + (pill(f"Live v{ver(live)}", "green") if live else "") + (pill(f"Test v{ver(cand)}", "amber") if cand else ""))

if state in ("provisioning", "provision_failed"):
    for r in sql().query(f"""SELECT step, status FROM {s.fq('provisioning_steps')} WHERE bot_id = :b
                             QUALIFY ROW_NUMBER() OVER (PARTITION BY step ORDER BY updated_at DESC) = 1""",
                         {"b": cfg.bot_id}):
        html(pill("Done" if r["status"] == "done" else "Failed", "green" if r["status"] == "done" else "red")
             + r["step"].replace("_", " ").capitalize())
    if state == "provision_failed" and st.button("Try again"):
        run_job("provision", bot_id=cfg.bot_id, actor=user)
    st.stop()

# Test version: checks and approval ------------------------------------------------------
if cand:
    st.subheader(f"Test version {ver(cand)}", help=help_text("review"))
    st.caption("Only owners, the Reviewer and invited testers can use it.")
    strat = sql().query(f"""SELECT explanation FROM {s.fq('strategy_results')} WHERE bot_id = :b AND chosen
                            ORDER BY ts DESC LIMIT 1""", {"b": cfg.bot_id})
    if strat:
        st.info(strat[0]["explanation"])
    ev = sql().query(f"SELECT * FROM {s.fq('eval_runs')} WHERE run_id = :r", {"r": cand["eval_run_id"]}) \
        if cand.get("eval_run_id") else []
    if not ev:
        st.write("Quality check is running or hasn't run yet.")
        if st.button("Run the quality check"):
            run_job("evals", bot_id=cfg.bot_id, trigger="manual", channel="candidate")
    else:
        data = json.loads(ev[0]["metrics_json"])
        th, m = s.thresholds(), data.get("metrics", {})
        for title, keys, why in [("Finding the right document", RETRIEVAL_METRICS, "search"),
                                 ("Answer quality", [k for k in METRIC_LABEL if k not in RETRIEVAL_METRICS], "judges")]:
            st.markdown(f"**{title}**", help=help_text(why))
            shown = [k for k in keys if k in m]
            for col, k in zip(st.columns(max(len(shown), 1)), shown):
                with col:
                    html(tile(METRIC_LABEL[k], m[k], th.get(k)))
        for f in data.get("failures", []):
            st.error(f)
        if not data.get("failures"):
            st.success("No wrong citations, no leaked content, no unmasked personal data.")
        if str(ev[0]["needs_safety_review"]).lower() == "true" and is_reviewer:
            unsafe = [c for c in data.get("per_case", []) if c["scores"].get("safety") == 0]
            with st.expander(f"Review {len(unsafe)} answer(s) flagged as possibly unsafe"):
                for c in unsafe:
                    st.markdown(f"**Q:** {c['question']}\n\n*{c['rationales'].get('safety', '')}*")
                why = st.text_input("Why these are acceptable (required to override)")
                if why and st.button("Override and re-check"):
                    sql().execute(f"""UPDATE {s.fq('releases')} SET safety_override_by = :u,
                                      safety_override_reason = :w WHERE release_id = :r""",
                                  {"u": user, "w": why, "r": cand["release_id"]})
                    control.audit(user, cfg.bot_id, "safety_override", cand["release_id"], {"reason": why})
                    run_job("evals", bot_id=cfg.bot_id, trigger="safety_override", channel="candidate")
                    st.rerun()

    passed = str(cand.get("eval_passed")).lower() == "true"
    if cand.get("review_requested_at") is None:
        if is_owner and st.button("Send for approval", type="primary", disabled=not passed):
            sql().execute(f"UPDATE {s.fq('releases')} SET review_requested_at = current_timestamp() "
                          "WHERE release_id = :r", {"r": cand["release_id"]})
            control.audit(user, cfg.bot_id, "submitted_for_approval", cand["release_id"])
            if state == State.TESTING.value:
                control.set_state(cfg.bot_id, State.PENDING_APPROVAL.value, user)
            st.rerun()
        if not passed:
            st.caption("Available once the quality check passes.")
    else:
        st.write(f"Waiting for **{cfg.reviewer}** to approve.")
        if is_reviewer:
            a1, a2 = st.columns(2)
            if a1.button("Approve and publish", type="primary", disabled=not passed):
                control.audit(user, cfg.bot_id, "approved", cand["release_id"])
                run_job("promote", bot_id=cfg.bot_id, actor=user, action="promote")
                st.success("Publishing. A quick smoke test runs; if it fails, the previous version stays live.")
            if a2.button("Send back for changes"):
                sql().execute(f"UPDATE {s.fq('releases')} SET review_requested_at = NULL WHERE release_id = :r",
                              {"r": cand["release_id"]})
                control.audit(user, cfg.bot_id, "approval_withdrawn", cand["release_id"])
                if state == State.PENDING_APPROVAL.value:
                    control.set_state(cfg.bot_id, State.TESTING.value, user)
                st.rerun()

    if is_owner:
        with st.expander(f"Testers ({len(cfg.testers)} of {MAX_TESTERS})"):
            txt = st.text_area("One email per line", "\n".join(cfg.testers))
            if st.button("Save testers"):
                try:
                    Provisioner(sql(), s, control).set_testers(cfg, txt.splitlines(), user)
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
        if state == "live" and b1.button("Pause"):
            control.set_state(cfg.bot_id, State.PAUSED.value, user)
            st.rerun()
        if state in ("paused", "budget_paused") and b1.button("Resume"):
            control.set_state(cfg.bot_id, State.LIVE.value, user)
            st.rerun()
        if b2.button("Roll back to the previous version"):
            run_job("promote", bot_id=cfg.bot_id, actor=user, action="rollback", reason="owner rollback")
            st.success("Rolling back.")

if is_owner and state in ("live", "paused", "testing"):
    with st.expander("Archive this chatbot"):
        st.write("Archived chatbots stop answering but keep all documents and history.")
        if st.button("Archive"):
            control.set_state(cfg.bot_id, State.ARCHIVED.value, user)
            st.rerun()

# Documentation: model card + release history (CON-5, REL-8) ------------------------------
docs_rows = sql().query(f"SELECT doc_name, doc_version, status, readability FROM {p.t('manifest')} "
                        "WHERE status <> 'superseded'")
history = sql().query(f"SELECT * FROM {s.fq('releases')} WHERE bot_id = :b ORDER BY seq DESC",
                      {"b": cfg.bot_id})
latest_metrics = {}
if live and live.get("eval_run_id"):
    r = sql().query(f"SELECT metrics_json FROM {s.fq('eval_runs')} WHERE run_id = :r", {"r": live["eval_run_id"]})
    latest_metrics = json.loads(r[0]["metrics_json"]) if r else {}
if history:
    st.subheader("Release history")
    st.dataframe([{"Version": f"v{ver(r)}", "Status": r["status"].replace("_", " ").title(),
                   "Published": r.get("promoted_at"), "Approved by": r.get("approved_by"),
                   "Quality check": "Passed" if str(r.get("eval_passed")).lower() == "true" else "Not passed",
                   "Smoke test": {"true": "Passed", "false": "Failed"}.get(str(r.get("smoke_passed")).lower(), "—")}
                  for r in history], hide_index=True, use_container_width=True)
st.subheader("Documentation")
card = model_card(cfg, s, STATE_LABEL.get(state, state), docs_rows, latest_metrics, bot["config_version"],
                  history)
with st.expander("View model card"):
    st.markdown(card)
st.download_button("Download model card", card, file_name=f"{cfg.bot_id}_model_card.md")
