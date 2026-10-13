"""Home: what needs you, then your chatbots with the next step for each (design: Main).

Shows every chatbot the person manages at once; choosing one opens its pages."""
import json
from html import escape as esc

import streamlit as st

from common import (PAGES, STATE_LABEL, bot_progress, checks_in_progress, current_user, is_admin, my_bots, read_many,
                    settings, soft, user_groups)
from factory import journey
from factory.monitoring import split_alerts, unresolved_alerts_sql
from factory.problems import group_alerts
from ui import STATE_TONE, STEP_TONE, bar, html, pill, plain

s, user = settings(), current_user()
c1, c2 = st.columns([4, 1])
c1.title(f"Hello, {user.split('@')[0].split('.')[0].title()}")
c2.page_link("pages/create.py", label="Create a chatbot", icon=":material/add:")

bots = my_bots()
if not bots:
    st.info("You don't have any chatbots yet. Press **Create a chatbot** (top right) to make your first one. "
            "You'll answer a few questions and upload your documents; it takes about 10 minutes.")
    st.stop()

admin, groups = is_admin(), user_groups(user)
ids = {b["bot_id"]: b for b in bots}
queries = {
    "alerts": (unresolved_alerts_sql(s), None),
    "usage": (f"""SELECT a.bot_id, a.questions, f.thumbs FROM
          (SELECT bot_id, sum(requests) AS questions FROM {s.fq('v_bot_activity')}
           WHERE day >= current_date() - INTERVAL 7 DAYS GROUP BY bot_id) a
        LEFT JOIN
          (SELECT bot_id, avg(CASE rating WHEN 'up' THEN 1.0 WHEN 'down' THEN 0.0 END) AS thumbs
           FROM {s.fq('feedback')} WHERE ts >= current_date() - INTERVAL 7 DAYS GROUP BY bot_id) f
        USING (bot_id)""", None),
    "versions": (f"""SELECT r.bot_id, r.release_id, r.eval_run_id, r.eval_passed, r.review_requested_at
                     FROM {s.fq('releases')} r JOIN {s.fq('bots')} b ON r.release_id = b.candidate_release_id""", None),
}
if admin:  # owners don't manage costs (WIZ-13)
    queries["spending"] = (f"SELECT bot_id, variable_cost_usd, budget_usd FROM {s.fq('v_budget')} "
                           "WHERE month = date_trunc('MONTH', current_date())", None)
found, failed = read_many(queries)   # all at once; a part that fails is reported at the bottom
# Only alerts that are still true, with repeats folded together. A chatbot whose setup stopped
# says so on its own card, so that one isn't listed twice.
still_true = split_alerts(found["alerts"], int(s.get("monitoring.expiry_lead_days", 30)))[0]
alerts = group_alerts([a for a in still_true if a["kind"] != "provision_failed"
                       and (a["bot_id"] in ids or (admin and not a["bot_id"]))])
yours, for_mlops = [g for g in alerts if g["for_owner"]], [g for g in alerts if not g["for_owner"]]
week = {r["bot_id"]: r for r in found["usage"]}
candidates = {r["bot_id"]: r for r in found["versions"]}
spend = {r["bot_id"]: r for r in found.get("spending", [])}
progress = bot_progress(bots)
checking = checks_in_progress()   # chatbots whose quality check is waiting or running right now
needed = int(s.get("quality.min_golden_questions", 50))


def reviews(b: dict) -> bool:
    return user == b.get("reviewer") or b.get("reviewer") in groups


def next_for(b: dict) -> journey.Step:
    """The one thing to do next for this chatbot."""
    cand = candidates.get(b["bot_id"]) if b.get("candidate_release_id") else None
    path = journey.steps(b["state"], *journey.counts(progress.get(b["bot_id"])), needed, cand,
                         bool(b.get("live_release_id")), b.get("reviewer") or "", reviews(b),
                         checking=b["bot_id"] in checking)
    return journey.next_step(b["state"], path, bool(b.get("candidate_release_id")))


waiting = [b for b in bots if b["state"] == "pending_approval" and reviews(b)]
if yours or waiting or (admin and for_mlops):
    with st.container(border=True):
        st.markdown("**Needs your attention**")
        for b in waiting:
            st.markdown(f'{pill("Approval", "blue")} **{esc(b["display_name"])}** · a new version is waiting for '
                        "your approval. Press **Go to Launch** on its card below.", unsafe_allow_html=True)
        for g in yours[:6]:   # one calm line each: what it is in plain words, then what to do
            repeats = f' <span class="cf-muted">(raised {g["count"]} times)</span>' if g["count"] > 1 else ""
            st.markdown(f'{pill(g["label"], "red" if g["severity"] == "high" else "amber")} '
                        f'**{esc(ids[g["bot_id"]]["display_name"])}** · {plain(g["text"])}{repeats}  \n'
                        f'<span class="cf-muted">What to do:</span> {g["advice"]}', unsafe_allow_html=True)
        if len(yours) > 6:
            st.caption(f"And {len(yours) - 6} more. Each one clears on its own once the checks stop reporting it.")
        if admin and for_mlops:
            c1, c2 = st.columns([5, 1])
            c1.markdown(f'{pill("For MLOps", "gray")} {len(for_mlops)} alert(s) for MLOps to look at: '
                        + ", ".join(sorted({g["label"].lower() for g in for_mlops})) + ".", unsafe_allow_html=True)
            if c2.button("Open them", key="open_admin_alerts"):
                st.session_state["admin_section"] = "Alerts"
                st.switch_page("pages/admin.py")
if for_mlops and not admin:
    st.caption(f"MLOps has been told about {len(for_mlops)} other issue(s) with your chatbots ("
               + ", ".join(sorted({g["label"].lower() for g in for_mlops})) + "). There is nothing for you to do.")

st.subheader("Your chatbots")
st.caption("Each card shows what to do next for that chatbot. Press its button to go straight there.")
cols = st.columns(3)
for i, b in enumerate(bots):
    sp, wk, step = spend.get(b["bot_id"], {}), week.get(b["bot_id"], {}), next_for(b)
    used, budget = float(sp.get("variable_cost_usd") or 0), float(sp.get("budget_usd") or 1000)
    versions = (pill("Live", "green") if b.get("live_release_id") else "") + \
        (pill("Test version", "amber") if b.get("candidate_release_id") else "")
    thumbs = f'{float(wk["thumbs"]):.0%}' if wk.get("thumbs") is not None else "—"
    with cols[i % 3].container(border=True):
        html(f'<b style="font-size:17px">{esc(b["display_name"])}</b> '
             f'{pill(STATE_LABEL.get(b["state"], b["state"]), STATE_TONE.get(b["state"], "gray"))}<br>'
             f'<span class="cf-muted">{esc(json.loads(b.get("config_json") or "{}").get("purpose", ""))}</span><div style="margin:8px 0">{versions}</div>'
             f'<b>{int(wk.get("questions") or 0):,}</b> <span class="cf-muted">questions this week</span> · '
             f'<b>{thumbs}</b> <span class="cf-muted">thumbs up</span>'
             + (f'<div class="cf-muted" style="margin-top:8px">This month: &#36;{used:,.0f} of &#36;{budget:,.0f}</div>'
                f'{bar(used, budget)}' if admin and "spending" not in failed else ""))
        if step.status == journey.DONE:
            st.markdown(f'{pill("Nothing to do", "green")} {step.detail}', unsafe_allow_html=True)
        else:
            st.markdown(f'{pill(journey.STATUS_LABEL[step.status], STEP_TONE[step.status])} **Next: {step.title}**'
                        f'\n\n{step.detail}', unsafe_allow_html=True)
        page, name = PAGES.get(step.page or "launch", PAGES["launch"])
        if st.button(f"Go to {name}", key=f"open_{b['bot_id']}", width="stretch",
                     type="primary" if step.status in (journey.NOW, journey.PROBLEM) else "secondary"):
            st.session_state["bot_id"] = b["bot_id"]
            st.session_state["docs_bot_id"] = b["bot_id"]
            st.switch_page(page)

WHAT = {"alerts": "Alerts", "usage": "This week's usage numbers", "versions": "The status of test versions",
        "spending": "This month's spending"}
for name, error in failed.items():
    soft(WHAT[name], error)
