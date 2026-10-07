"""Chatbot Factory app entry point (Databricks App, Streamlit). Look: docs/design."""
import streamlit as st

from common import is_admin, start_probe
from ui import style

st.set_page_config(page_title="Chatbot Factory", page_icon=":material/forum:", layout="wide")
style()
start_probe()

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
if is_admin():
    pages += [st.Page("pages/overview.py", title="All chatbots", icon=":material/dashboard:"),
              st.Page("pages/admin.py", title="Admin", icon=":material/settings:")]

st.sidebar.markdown("**Chatbot Factory**")
st.navigation(pages).run()
