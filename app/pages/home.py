"""Home: what needs you, then your chatbots (design: Main)."""
import json
from html import escape as esc

import streamlit as st

from common import STATE_LABEL, current_user, my_bots, settings, sql, is_admin
from ui import STATE_TONE, bar, html, pill

s = settings()
c1, c2 = st.columns([4, 1])
c1.title(f"Hello, {current_user().split('@')[0].split('.')[0].title()}")
c2.page_link("pages/create.py", label="Create a chatbot", icon=":material/add:")

bots = my_bots()
if not bots:
    st.info("You don't have any chatbots yet. Create one to get started.")
    st.stop()

ids = {b["bot_id"]: b for b in bots}
alerts = [a for a in sql().query(f"SELECT * FROM {s.fq('v_alerts_open')} ORDER BY ts DESC LIMIT 50")
          if a["bot_id"] in ids]
spend = {r["bot_id"]: r for r in sql().query(
    f"SELECT bot_id, variable_cost_usd, budget_usd FROM {s.fq('v_budget')} "
    "WHERE month = date_trunc('MONTH', current_date())")}
week = {r["bot_id"]: r for r in sql().query(
    f"""SELECT a.bot_id, a.questions, f.thumbs FROM
          (SELECT bot_id, sum(requests) AS questions FROM {s.fq('v_bot_activity')}
           WHERE day >= current_date() - INTERVAL 7 DAYS GROUP BY bot_id) a
        LEFT JOIN
          (SELECT bot_id, avg(CASE rating WHEN 'up' THEN 1.0 WHEN 'down' THEN 0.0 END) AS thumbs
           FROM {s.fq('feedback')} WHERE ts >= current_date() - INTERVAL 7 DAYS GROUP BY bot_id) f
        USING (bot_id)""")}

waiting = [b for b in bots if b["state"] == "pending_approval" and current_user() == b.get("reviewer")]
if alerts or waiting:
    with st.container(border=True):
        st.markdown("**Needs your attention**")
        for b in waiting:
            html(f'{pill("Approval", "blue")} <b>{esc(b["display_name"])}</b> · a new version is waiting for you.')
        for a in alerts[:8]:
            tone = {"high": "red", "medium": "amber"}.get(a["severity"], "gray")
            html(f'{pill(a["kind"].replace("_", " ").title(), tone)} <b>{esc(ids[a["bot_id"]]["display_name"])}</b>'
                 f' · {esc(a["message"])}')

admin = is_admin()
st.subheader("Your chatbots")
cols = st.columns(3)
for i, b in enumerate(bots):
    sp, wk = spend.get(b["bot_id"], {}), week.get(b["bot_id"], {})
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
             + (f'<div class="cf-muted" style="margin-top:8px">This month: ${used:,.0f} of ${budget:,.0f}</div>'
                f'{bar(used, budget)}' if admin else ""))  # owners don't manage costs (WIZ-13)
        if st.button("Open", key=f"open_{b['bot_id']}"):
            st.session_state["bot_id"] = b["bot_id"]
            st.switch_page("pages/launch.py")
