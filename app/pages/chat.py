"""Chat (CON-1, UI-004/005). Calls the shared agent as the signed-in user so UC grants
apply (IDN-1). Owners, the Reviewer and testers can switch to the test version (REL-1)."""
import uuid

import streamlit as st

from common import (APP_VERSION, OUTCOME_LABEL, STATE_LABEL, can_manage, cp, current_user, is_tester,
                    settings, sql, user_client)
from ui import html, pill

st.title("Chat")
s, user = settings(), current_user()
bots = [b for b in cp().list_bots()
        if b["state"] == "live" or ((can_manage(b) or is_tester(b)) and b.get("candidate_release_id"))]
if not bots:
    st.info("No chatbots are available yet.")
    st.stop()
ids = [b["bot_id"] for b in bots]
bot_id = st.selectbox("Chatbot", ids, index=ids.index(st.session_state["bot_id"])
                      if st.session_state.get("bot_id") in ids else 0,
                      format_func=lambda i: next(f"{b['display_name']} ({STATE_LABEL[b['state']]})"
                                                 for b in bots if b["bot_id"] == i))
bot = next(b for b in bots if b["bot_id"] == bot_id)
bot_cfg = __import__("json").loads(bot.get("config_json") or "{}")
channel = "live"
if (can_manage(bot) or is_tester(bot)) and bot.get("candidate_release_id"):
    if bot["state"] != "live" or st.toggle("Use the test version", value=True):
        channel = "candidate"
        st.warning("Test version. Only owners, the Reviewer and invited testers see this.")

key = f"chat_{bot_id}_{channel}"
if key not in st.session_state or st.sidebar.button("New conversation"):
    st.session_state[key] = {"id": str(uuid.uuid4()), "messages": []}
conv = st.session_state[key]


def feedback(m, rating):
    sql().execute(f"INSERT INTO {s.fq('feedback')} VALUES (:r, :b, :u, :rt, NULL, current_timestamp(), false)",
                  {"r": m["request_id"], "b": bot_id, "u": user, "rt": rating})
    if m.get("trace_id"):  # OBS-10: the thumb is attached to the answer's MLflow trace
        try:
            import mlflow
            from mlflow.entities import AssessmentSource
            mlflow.set_tracking_uri("databricks")
            who = "anonymous" if bot_cfg.get("identity_mode") == "pseudonymous" else user
            mlflow.log_feedback(trace_id=m["trace_id"], name="user_thumbs", value=rating == "up",
                                source=AssessmentSource(source_type="HUMAN", source_id=who))
        except Exception:  # noqa: BLE001 - the table row above is the record of truth
            pass
    st.toast("Thanks for the feedback!")


for i, m in enumerate(conv["messages"]):
    with st.chat_message(m["role"]):
        if m.get("label"):
            html(pill(m["label"], "gray"))
        st.markdown(m["content"])
        for cf in m.get("conflicts", []):
            st.warning(f"**Sources disagree ({' and '.join(f'[{n}]' for n in cf.get('sources', []))}):** "
                       f"{cf.get('note', '')}")
        if m.get("citations"):
            with st.expander(f"Sources ({len(m['citations'])})"):
                for c in m["citations"]:
                    where = f"page(s) {c['pages']}" + (f", section \"{c['section']}\"" if c.get("section") else "")
                    st.markdown(f"**[{c['n']}] {c['doc_name']}** (v{c.get('doc_version')}), {where}")
                    st.markdown(f"> {c['excerpt']}")  # Markdown quote, rendered safely
        if m["role"] == "assistant" and m.get("request_id"):
            c1, c2, _ = st.columns([1, 1, 10])
            if c1.button("", icon=":material/thumb_up:", key=f"up_{i}", help="Helpful"):
                feedback(m, "up")
            if c2.button("", icon=":material/thumb_down:", key=f"down_{i}", help="Not helpful"):
                feedback(m, "down")

if question := st.chat_input("Ask a question"):
    conv["messages"].append({"role": "user", "content": question})
    request_id = str(uuid.uuid4())
    with st.spinner("Looking through the documents…"):
        try:
            resp = user_client().serving_endpoints.get_open_ai_client().responses.create(
                model=s.get("agent.serving_endpoint"),
                input=[{"role": m["role"], "content": m["content"]} for m in conv["messages"]],
                extra_body={"custom_inputs": {"bot_id": bot_id, "conversation_id": conv["id"],
                                              "request_id": request_id, "channel": channel,
                                              "app_version": APP_VERSION}})
            custom = getattr(resp, "custom_outputs", None) or (resp.model_extra or {}).get("custom_outputs", {})
            text = custom.get("answer") or resp.output_text
        except Exception:  # noqa: BLE001
            text, custom = "Sorry, something went wrong. Please try again.", {"outcome": "error"}
    conv["messages"].append({"role": "assistant", "content": text, "request_id": request_id,
                             "trace_id": custom.get("trace_id"),
                             "label": OUTCOME_LABEL.get(custom.get("outcome")),
                             "citations": custom.get("citations", []),
                             "conflicts": custom.get("conflicts", [])})
    st.rerun()
