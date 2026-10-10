"""Chatbot Factory app entry point (Databricks App, Streamlit). Look: docs/design."""
import logging
import traceback

import streamlit as st

from common import is_admin, start_probe
from ui import style

st.set_page_config(page_title="Chatbot Factory", page_icon=":material/forum:", layout="wide")
style()


def report(where: str, error: Exception) -> None:
    """One line saying something went wrong, with the full detail folded away for whoever needs
    it. The trace also goes to the app log."""
    logging.getLogger("factory.app").error("%s failed: %s", where, "".join(
        traceback.format_exception(type(error), error, error.__traceback__)))
    st.error(f"Something went wrong {where} ({type(error).__name__}). The rest of the app still works; "
             "you can try again or pick another page.")
    with st.expander("Show technical details"):
        st.code("".join(traceback.format_exception(type(error), error, error.__traceback__)), language=None)


try:
    start_probe()
except Exception as e:  # noqa: BLE001 - the health probe is optional; never block the app on it
    report("starting the background health check", e)

pages = [
    st.Page("pages/home.py", title="Home", icon=":material/home:", default=True),
    st.Page("pages/create.py", title="Create chatbot", icon=":material/add:"),
    st.Page("pages/documents.py", title="Documents", icon=":material/description:"),
    st.Page("pages/test_questions.py", title="Test questions", icon=":material/checklist:"),
    st.Page("pages/launch.py", title="Launch", icon=":material/rocket_launch:"),
    st.Page("pages/chat.py", title="Chat", icon=":material/chat:"),
    st.Page("pages/monitoring.py", title="Monitoring", icon=":material/monitoring:"),
    st.Page("pages/traces.py", title="Traces", icon=":material/account_tree:"),
]
try:
    admin = is_admin()
except Exception as e:  # noqa: BLE001 - without a clear answer the admin pages stay hidden
    admin = False
    report("checking your access", e)
if admin:
    pages += [st.Page("pages/overview.py", title="All chatbots", icon=":material/dashboard:"),
              st.Page("pages/admin.py", title="Admin", icon=":material/settings:")]

st.sidebar.markdown("**Chatbot Factory**")
page = st.navigation(pages)
try:
    page.run()
except Exception as e:  # noqa: BLE001 - st.stop() and st.rerun() are not Exceptions and pass through
    report("on this page", e)
