"""Creator wizard: a one-page fast path or six advanced steps (UX-1, WIZ-1..10, CNV-1, IDN-2, DCL-1,
CST-8, REL-1, GRD-16). The fast path asks only what can't be decided for the owner (name and purpose,
reviewer, who can use it, documents) and uses the defaults below for the rest. Every option has a
plain-English (i) explainer naming the Databricks feature behind it (UX-2).

After Go, provisioning, parsing, checks, test questions, strategy comparison and the first
evaluation run in the background; the result lands as a test version the owner can try.
"""
import datetime as dt
from html import escape as esc

import pandas as pd
import streamlit as st

from common import (STATE_LABEL, all_groups, app_client, check_emails, cp, current_user, docs, is_owner, run_job,
                    settings, sql, user_groups)
from factory.config import EVERYONE, MAX_TESTERS, BotConfig
from factory.explain import help_text
from factory.guardrails import platform_rules
from factory.lifecycle import State
from factory.naming import NamingError, validate
from factory.ingestion import BotPaths
from factory.provisioning import Provisioner
from factory.wizard import UNFINISHED_STATES, answers_from_config
from ui import html, steps

STEPS = ["Name and purpose", "People", "Who can use it", "Documents", "Answers and safety", "Alerts and review"]
BUILT_IN = ("harmful content filter, personal data masking, jailbreak blocking, removal of hidden "
            "instructions in documents, exact source quotes, \"I don't know\" when the documents don't say, "
            "conflict disclosure, no invented links")

FAST_DEFAULTS = {"access_mode": "restricted", "channel": "chat", "sensitivity": "internal", "source_type": "upload",
                 "golden_set_mode": "generate", "answer_style": "short",
                 "limit_quotes": True, "identity_mode": "named", "history_mode": "session",
                 "traffic_alerts": True, "testers": [], "business_function": "Other"}

s = settings()
w = st.session_state.setdefault("wizard", {"step": 0})


def config() -> BotConfig:
    return BotConfig(
        bot_id=w.get("bot_id", ""), display_name=w.get("display_name", ""), purpose=w.get("purpose", ""),
        owner_user=w.get("owner_user", ""), owner_group=w.get("owner_group", ""),
        business_function=w.get("business_function", ""), access_mode=w.get("access_mode", "restricted"),
        allowed_principals=w.get("allowed_principals", []),
        channels=["chat", "api"] if w.get("channel") == "both" else [w.get("channel", "chat")],
        refuse_topics=w.get("refuse_topics", []), answer_style=w.get("answer_style", "short"),
        history_mode=w.get("history_mode", "session"), identity_mode=w.get("identity_mode", "named"),
        sensitivity=w.get("sensitivity", "internal"), source_type=w.get("source_type", "upload"),
        source_uri=w.get("source_uri", ""),
        golden_set_mode=w.get("golden_set_mode", "generate"), reviewer=w.get("reviewer", ""),
        testers=w.get("testers", []), team=w.get("owner_group", ""),
        answer_model=s.get("models.default_answer_model"), limit_quotes=w.get("limit_quotes", False),
        alert_prefs={"traffic": bool(w.get("traffic_alerts", True))},
    )


def saved_documents(bot_id: str) -> int:
    """Documents already stored for a chatbot whose setup is being resumed (0 if none yet)."""
    try:
        return int(sql().query(f"SELECT count(*) AS n FROM {BotPaths(s, bot_id).t('manifest')} "
                               "WHERE status <> 'superseded'")[0]["n"])
    except Exception:  # noqa: BLE001 - setup stopped before the chatbot's tables existed
        return 0


def create(cfg: BotConfig) -> None:
    """Create the chatbot, or carry on creating one whose setup stopped. Every part is safe to
    repeat: the saved answers are updated, finished setup steps are skipped and files already
    stored are recognised."""
    user = current_user()
    control = cp()
    try:
        with st.status("Creating workspace…", expanded=True) as status:
            existing = control.get_bot(cfg.bot_id)
            control.save_config(cfg, user, "setup resumed" if existing else "created",
                                state=None if existing else State.DRAFT.value)
            w["resume"] = cfg.bot_id  # restore point: from here the wizard continues this chatbot
            Provisioner(sql(), s, control, app_client()).run(
                cfg, user, progress=st.write, only=["create_objects", "tag_objects", "grant_access", "write_config"])
            for name, data in w.get("files", {}).items():
                exp = w.get("expiry", {}).get(name)  # per document: a date, or valid until replaced (DCL-1)
                res = docs().register(cfg.bot_id, name, data, user, no_expiry=not exp,
                                      expires_at=str(exp) if exp else None, doc_owner=cfg.owner_user)
                if not res.ok:
                    st.warning(f"**{name}** wasn't added: {' '.join(res.reasons)}")
            st.write("Reading documents, checking quality, writing test questions and choosing the best "
                     "search method. This continues in the background.")
            run_job("provision", bot_id=cfg.bot_id, actor=user)
            status.update(label="Your chatbot is being built", state="complete")
    except Exception as e:  # noqa: BLE001 - keep the answers and say how to carry on
        st.error(f"Setup stopped before it finished: {type(e).__name__}: {str(e)[:400]}")
        st.info("Your answers are saved. Change anything that needs fixing and press **Create chatbot** "
                "again; steps that already finished are skipped. If you leave this page, the chatbot is "
                "listed under **Unfinished chatbots** on **Create a chatbot**.")
        st.stop()
    st.session_state["bot_id"] = cfg.bot_id
    st.session_state.pop("wizard", None)
    st.page_link("pages/documents.py", label="Next: check your documents", icon=":material/description:")
    st.stop()


def name_fields(problems: list[str]) -> None:
    if w.get("resume"):  # the chatbot already exists under this name; the technical name can't change
        st.text_input("What should we call your chatbot?", w.get("display_name", ""), disabled=True,
                      help="The name was fixed when setup started.")
        w["purpose"] = st.text_area("In one sentence, what will it help people with?", w.get("purpose", ""),
                                    help=help_text("purpose"))
        st.caption(f"Technical name: `{w['bot_id']}` (can't be changed later)")
        if not w["purpose"]:
            problems.append("Add a purpose.")
        return
    w["display_name"] = st.text_input("What should we call your chatbot?", w.get("display_name", ""),
                                      placeholder="Claims Chatbot")
    w["purpose"] = st.text_area("In one sentence, what will it help people with?", w.get("purpose", ""),
                                placeholder="Answers adjusters' questions about claims procedures.",
                                help=help_text("purpose"))
    try:
        w["bot_id"] = validate(w["display_name"], cp().existing_bot_ids()) if w["display_name"] else ""
        if w["bot_id"]:
            st.caption(f"Technical name: `{w['bot_id']}` (can't be changed later)")
    except NamingError as e:
        problems.append(str(e))
    if not (w["display_name"] and w["purpose"]):
        problems.append("Add a name and a purpose.")


def upload(label: str) -> None:
    exts = s.get("ingestion.parse_extensions") + s.get("ingestion.text_extensions") + (
        s.get("ingestion.tabular_extensions") if s.get("ingestion.enable_tabular") else [])
    files = st.file_uploader(label, accept_multiple_files=True, type=exts, help=help_text("documents"))
    if files:
        w["files"] = {f.name: f.getvalue() for f in files}
    if w.get("files"):
        st.markdown("**Does each document expire?** Leave the date empty if it's valid until you upload a newer "
                    "version.", help=help_text("expiry"))
        exp = w.setdefault("expiry", {})
        table = st.data_editor(pd.DataFrame({"Document": list(w["files"]),
                                             "Expires on": [exp.get(n) for n in w["files"]]}),
                               hide_index=True, use_container_width=True, disabled=["Document"],
                               column_config={"Expires on": st.column_config.DateColumn(
                                   "Expires on (empty = valid until replaced)", min_value=dt.date.today())})
        w["expiry"] = {r["Document"]: r["Expires on"] for _, r in table.iterrows() if pd.notna(r["Expires on"])}


def group_picker(label: str, key: str, multi: bool = True, **kw):
    """Groups from the company directory, the person's own groups first (WIZ-11)."""
    mine = sorted(g for g in user_groups(current_user()) if g not in ("users", "admins"))
    options = mine + [g for g in all_groups() if g not in mine]
    current = w.get(key) or ([] if multi else "")
    if multi:
        w[key] = st.multiselect(label, options, [g for g in current if g in options], help=help_text("groups"),
                                placeholder="Type to search groups (yours are listed first)", **kw)
    else:
        w[key] = st.selectbox(label, options, index=options.index(current) if current in options else None,
                              help=help_text("groups"), placeholder="Type to search groups (yours are listed first)",
                              **kw)


def emails(label: str, key: str, problems: list[str], what: str) -> list[str]:
    txt = st.text_input(label, ", ".join(w.get(key, [])), placeholder="jane.doe@company.com, raj.patel@company.com",
                        help=help_text("people_ids"))
    w[key] = [x.strip() for x in txt.replace(";", ",").split(",") if x.strip()]
    problems += check_emails(w[key], what)
    return w[key]


def reviewer(problems: list[str]) -> None:
    kind = st.radio("Who approves new versions before they go live?", ["A group", "A person"], horizontal=True,
                    index=1 if "@" in w.get("reviewer", "") else 0, help=help_text("review"))
    if kind == "A group":
        group_picker("Reviewer group", "reviewer_group", multi=False)
        w["reviewer"] = w.get("reviewer_group") or ""
    else:
        w["reviewer"] = st.text_input("Reviewer's work email", w.get("reviewer", "") if "@" in w.get("reviewer", "")
                                      else "", placeholder="jane.doe@company.com", help=help_text("people_ids"))
        problems += check_emails([w["reviewer"]] if w["reviewer"] else [], "Reviewer")


def who_can_use(problems: list[str]) -> None:
    everyone = st.radio("Who can use it?", ["Everyone at the company", "Specific groups and people"], horizontal=True,
                        index=0 if EVERYONE in w.get("allowed_principals", []) else 1, help=help_text("everyone"))
    if everyone == "Everyone at the company":
        w["allowed_principals"] = [EVERYONE]
        return
    group_picker("Which groups can use it?", "allowed_groups")
    people = emails("Individual people (optional, work emails, comma separated)", "allowed_people", problems, "People")
    w["allowed_principals"] = w.get("allowed_groups", []) + people
    if not w["allowed_principals"]:
        problems.append("Add at least one group or person.")


def has_documents() -> bool:
    """Files chosen now, or already stored for a chatbot whose setup is being resumed."""
    return bool(w.get("files")) or bool(w.get("resume") and saved_documents(w["resume"]))


# Path choice ---------------------------------------------------------------------------------
if not w.get("mode"):
    st.title("Create a chatbot")
    if w.get("resume"):
        st.info(f"You're finishing the setup of **{w.get('display_name')}**. Pick a path below to carry on.")
        if st.button("Start a new chatbot instead", type="tertiary"):
            st.session_state["wizard"] = {"step": 0}
            st.rerun()
    unfinished = [b for b in cp().list_bots() if b["state"] in UNFINISHED_STATES and is_owner(b)
                  and b["bot_id"] != w.get("resume")]
    if unfinished:
        with st.container(border=True):
            st.subheader("Unfinished chatbots")
            st.caption("Setup started but didn't finish. Your answers were saved, so you can pick up where it stopped.")
            for b in unfinished:
                c1, c2 = st.columns([5, 2])
                c1.markdown(f"**{b['display_name']}** `{b['bot_id']}`  \n"
                            f"{STATE_LABEL.get(b['state'], b['state'])} · {b['owner_user']}")
                if c2.button("Continue setup", key=f"resume_{b['bot_id']}", type="primary"):
                    st.session_state["wizard"] = answers_from_config(cp().get_config(b["bot_id"]))
                    st.rerun()
    st.write("Choose how much you want to set up yourself. You can switch at any time and change any setting later.")
    a, b = st.columns(2)
    with a.container(border=True):
        st.subheader("Fast path", help=help_text("fast_path"))
        st.write("Answer four questions and upload your documents. About 3 minutes. We set the rest: short answers "
                 "with sources, conversation memory, named logs, short quotes, alerts, "
                 "test questions written for you and the best search method.")
        if st.button("Start fast path", type="primary"):
            w["mode"] = "fast"
            st.rerun()
    with b.container(border=True):
        st.subheader("Advanced mode", help=help_text("advanced_mode"))
        st.write("Six steps with every option: business function, testers, how people reach it, sensitivity, "
                 "expiry, answer style, extra topics to refuse, quoting, privacy, memory and alerts.")
        if st.button("Start advanced mode"):
            w["mode"] = "advanced"
            st.rerun()
    st.stop()

if w["mode"] == "fast":
    st.caption("New chatbot · fast path")
    st.title("Four questions and your documents")
    if st.button("Switch to advanced mode", type="tertiary"):
        w["mode"] = "advanced"
        st.rerun()
    for k, v in FAST_DEFAULTS.items():
        w.setdefault(k, v)
    w.setdefault("owner_user", current_user())
    problems: list[str] = []
    with st.container(border=True):
        st.markdown("**1. Name and purpose**")
        name_fields(problems)
        st.markdown("**2. Who approves new versions, and your team**", help=help_text("people"))
        reviewer(problems)
        group_picker("Your team (owner group): everyone in it can manage the chatbot", "owner_group", multi=False)
        st.markdown("**3. Who can use it?**", help=help_text("access"))
        who_can_use(problems)
        st.markdown("**4. Your documents**")
        upload("Upload your documents (up to 100 MB / 500 pages each)")
    with st.expander("What we set for you (change any of these later)"):
        st.markdown("\n".join([
            "- **Answers:** short and direct, always with sources",
            "- **Safety:** platform rules and all built-in checks; quoted text kept short",
            "- **Memory:** remembers the current conversation",
            "- **Logs:** named; personal data masked for everyone but Security",
            "- **Test questions:** written for you; approve 50 before launch",
            "- **Documents:** internal, valid until replaced"]))
    cfg = config()
    if not has_documents():
        problems.append("Upload at least one document.")
    problems += [] if problems else cfg.validate()
    for p in problems:
        st.error(p)
    c1, _, c2 = st.columns([1, 3, 1])
    if c1.button("Back", use_container_width=True):
        w["mode"] = None
        st.rerun()
    if c2.button("Create chatbot", type="primary", use_container_width=True, disabled=bool(problems)):
        create(cfg)
    st.stop()

st.caption(("Finishing setup" if w.get("resume") else "New chatbot") + " · advanced mode"
           + (f" · `{w['bot_id']}`" if w.get("bot_id") else ""))
st.title(STEPS[w["step"]])
if st.button("Switch to fast path", type="tertiary"):
    w["mode"] = "fast"
    st.rerun()
steps(w["step"], STEPS)


def nav(problems: list[str]) -> None:
    for p in problems:
        st.error(p)
    c1, _, c2 = st.columns([1, 3, 1])
    if c1.button("Back", use_container_width=True):
        if w["step"] == 0:
            w["mode"] = None
        else:
            w["step"] -= 1
        st.rerun()
    if w["step"] < len(STEPS) - 1 and c2.button("Next", type="primary", use_container_width=True,
                                                disabled=bool(problems)):
        w["step"] += 1
        st.rerun()


def radio(label: str, options: dict[str, str], key: str, default: str, **kw) -> None:
    labels = list(options)
    current = next((k for k, v in options.items() if v == w.get(key, default)), labels[0])
    w[key] = options[st.radio(label, labels, index=labels.index(current), **kw)]


step, problems = STEPS[w["step"]], []

if step == "Name and purpose":
    name_fields(problems)
    funcs = s.get("wizard.business_functions")
    w["business_function"] = st.selectbox("Which business function is it for?", funcs,
                                          index=funcs.index(w["business_function"])
                                          if w.get("business_function") in funcs else 0,
                                          help="Used for cost reports and tags.")

elif step == "People":
    c1, c2 = st.columns(2)
    w["owner_user"] = c1.text_input("Owner (work email)", w.get("owner_user", current_user()), help=help_text("people"))
    problems += check_emails([w["owner_user"]] if w["owner_user"] else [], "Owner")
    with c2:
        group_picker("Owner group: everyone in it can manage the chatbot", "owner_group", multi=False)
    reviewer(problems)
    emails(f"Testers who can try the test version (up to {MAX_TESTERS} work emails, comma separated)", "testers",
           problems, "Testers")
    st.caption(f"{len(w['testers'])} of {MAX_TESTERS}")
    if not (w["owner_user"] and w["owner_group"] and w["reviewer"]):
        problems.append("Add an owner, an owner group and a reviewer.")
    if len(w["testers"]) > MAX_TESTERS:
        problems.append(f"You can invite up to {MAX_TESTERS} testers.")

elif step == "Who can use it":
    radio("Who will use this chatbot?", {
        "People sign in themselves (the chat page, also as a Microsoft Teams tab)": "restricted",
        "Another app asks on their behalf (a Teams bot, a portal, a CRM)": "middleware"}, "access_mode", "restricted",
        help=help_text("teams"))
    if w["access_mode"] == "restricted":
        who_can_use(problems)
    else:
        w["allowed_principals"] = []
        st.info("Chatbots used through another app can only use Public documents for now.")
    radio("How will people reach it?", {"Chat page": "chat", "API": "api", "Both": "both"}, "channel", "chat",
          horizontal=True)

elif step == "Documents":
    radio("How sensitive are these documents?", {"Public": "public", "Internal": "internal",
                                                 "Confidential": "confidential"}, "sensitivity", "internal",
          horizontal=True, help=help_text("masking"))
    if w.get("access_mode") == "middleware" and w["sensitivity"] != "public":
        problems.append("Chatbots used through another app can only use Public documents for now.")
    sources = {"Upload files": "upload", "Git repository": "git"}
    if s.get("ingestion.enable_sharepoint"):
        sources["SharePoint"] = "sharepoint"
    radio("Where do the documents live?", sources, "source_type", "upload", horizontal=True)
    if w["source_type"] == "upload":
        upload("Drag and drop documents (up to 100 MB / 500 pages each)")
    else:
        w["source_uri"] = st.text_input("Repository or SharePoint link", w.get("source_uri", ""))
    radio("Test questions", {"Write them for me": "generate", "I'll write my own": "manual",
                             "Later": "skip"}, "golden_set_mode", "generate", horizontal=True,
          help=help_text("test_questions"))
    if w.get("resume") and saved_documents(w["resume"]):
        st.caption(f"{saved_documents(w['resume'])} document(s) are already stored for this chatbot. "
                   "Add more here, or later on the Documents page.")
    if not (has_documents() or w.get("source_uri")):
        problems.append("Add documents (or a link) to continue.")

elif step == "Answers and safety":
    radio("How should answers sound?", {"Short and direct": "short", "Detailed": "detailed"},
          "answer_style", "short", horizontal=True, help=help_text("search"))
    rules = "".join(f"<li>{esc(r)}</li>" for r in platform_rules(s.get("guardrails.platform_rules", {}), []))
    html(f'<div class="cf-locked"><b>Rules for every chatbot</b> <span class="cf-muted">· set by MLOps, '
         f'always on</span><ul>{rules}</ul><span class="cf-muted">Also built in: {BUILT_IN}.</span></div>')
    extra = st.text_input("Add your own topics to refuse (optional, comma separated)",
                          ", ".join(w.get("refuse_topics", [])), placeholder="e.g. Reinsurance treaties",
                          help=help_text("refuse"))
    w["refuse_topics"] = [x.strip() for x in extra.split(",") if x.strip()]
    forced = w.get("access_mode") == "middleware"
    w["limit_quotes"] = st.toggle("Keep quoted text short" + (" (required for chatbots used through another app)"
                                  if forced else ""), value=w.get("limit_quotes", False) or forced, disabled=forced,
                                  help=help_text("quotes"))
    radio("Who appears in the logs?", {"Named: logs show who asked": "named",
                                       "Pseudonymous: only Security can look up the person": "pseudonymous"},
          "identity_mode", "named", help=help_text("identity"))
    memory = {"No": "off", "This conversation only": "session"}
    if w["identity_mode"] == "named":
        memory["Saved: people can come back to past chats"] = "saved"
    elif w.get("history_mode") == "saved":
        w["history_mode"] = "session"
    radio("Should it remember the conversation?", memory, "history_mode", "session", horizontal=True,
          help=help_text("memory"))
    if w["identity_mode"] == "pseudonymous":
        st.caption("Saved conversations aren't available when people are pseudonymous.")

else:  # Alerts and review (budgets are set by MLOps in Admin, WIZ-13)
    w["traffic_alerts"] = st.toggle("Alert me about unusual traffic", value=w.get("traffic_alerts", True),
                                    help=help_text("monitoring"))
    st.caption("Always on: alerts for outages, slow answers, errors and expiring documents. "
               "MLOps looks after costs and budgets.")
    cfg = config()
    with st.container(border=True):
        st.markdown("**Review**", help=help_text("review"))
        for label, i, value in [
            ("Name", 0, cfg.display_name), ("Purpose", 0, cfg.purpose),
            ("Owner", 1, f"{cfg.owner_user} ({cfg.owner_group})"), ("Reviewer", 1, cfg.reviewer),
            ("Who can use it", 2, "Through another app" if cfg.access_mode == "middleware"
             else ", ".join(cfg.allowed_principals)),
            ("Documents", 3, f"{len(w.get('files', {})) + (saved_documents(w['resume']) if w.get('resume') else 0)}"
                             f" file(s), {cfg.sensitivity}"),
            ("Extra topics to refuse", 4, ", ".join(cfg.refuse_topics) or "None"),
        ]:
            c1, c2, c3 = st.columns([2, 5, 1])
            c1.caption(label)
            c2.write(value or "—")
            if c3.button("Edit", key=f"edit_{label}"):
                w["step"] = i
                st.rerun()
    problems = cfg.validate()
    for p in problems:
        st.error(p)
    c1, _, c2 = st.columns([1, 3, 1])
    if c1.button("Back", use_container_width=True):
        w["step"] -= 1
        st.rerun()
    if c2.button("Create chatbot", type="primary", use_container_width=True, disabled=bool(problems)):
        create(cfg)
    st.stop()

nav(problems)
