"""Look and feel from the Claude Design canvas (docs/design): one stylesheet + tiny HTML helpers.

Colors: ink #111827, muted #4B5563, ground #F6F7F9, surface #FFFFFF, border #E5E7EB,
accent #1D4ED8; status green/amber/red/gray always paired with a word.
"""
from html import escape

import streamlit as st

TONE = {"green": ("#DCFCE7", "#14532D"), "amber": ("#FEF3C7", "#78350F"), "red": ("#FEE2E2", "#7F1D1D"),
        "gray": ("#F3F4F6", "#374151"), "blue": ("#E8EEFC", "#1E3A8A")}
STATE_TONE = {"live": "green", "testing": "amber", "pending_approval": "blue", "provisioning": "blue",
              "provision_failed": "red", "paused": "gray", "budget_paused": "amber", "archived": "gray",
              "draft": "gray", "deleted": "gray"}

CSS = """
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=JetBrains+Mono&display=swap" rel="stylesheet">
<style>
html, body, [class*="css"], .stMarkdown, button, input, textarea, select { font-family: 'Instrument Sans', system-ui, sans-serif !important; }
code { font-family: 'JetBrains Mono', monospace !important; }
h1 { font-weight: 700 !important; letter-spacing: -0.01em; }
[data-testid="stSidebar"] { background: #FFFFFF; border-right: 1px solid #E5E7EB; }
[data-testid="stVerticalBlockBorderWrapper"] { background: #FFFFFF; border-radius: 10px !important; border-color: #E5E7EB !important; }
.stButton button, .stDownloadButton button, .stPageLink a { border-radius: 8px; min-height: 44px; font-weight: 600; }
.cf-pill { display: inline-flex; align-items: center; padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; white-space: nowrap; margin-right: 6px; }
.cf-muted { color: #4B5563; font-size: 13px; }
.cf-tile { border: 1px solid #E5E7EB; border-radius: 10px; padding: 12px 14px; background: #fff; margin-bottom: 8px; }
.cf-tile b { font-size: 24px; }
.cf-bar { height: 6px; border-radius: 3px; background: #EEF0F3; overflow: hidden; margin: 6px 0 2px; }
.cf-bar div { height: 6px; }
.cf-steps { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 8px; margin: 4px 0 18px; }
.cf-steps div { border-top: 4px solid #E5E7EB; padding-top: 6px; font-size: 12px; color: #4B5563; }
.cf-steps div.done { border-color: #1D4ED8; }
.cf-steps div.on { border-color: #1D4ED8; color: #111827; font-weight: 700; }
.cf-quote { border-left: 3px solid #93C5FD; padding: 6px 12px; margin: 4px 0 10px; color: #1F2937; }
.cf-locked { background: #F6F7F9; border-radius: 10px; padding: 14px 18px; margin-bottom: 12px; }
.cf-locked li { margin: 2px 0; }
</style>
"""


def style() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def pill(text: str, tone: str = "gray") -> str:
    bg, fg = TONE[tone]
    return f'<span class="cf-pill" style="background:{bg};color:{fg}">{escape(str(text))}</span>'


def html(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


def bar(used: float, total: float) -> str:
    pct = min(100, 100 * used / total) if total else 0
    color = "#B91C1C" if pct >= 100 else "#B45309" if pct >= 70 else "#15803D"
    return f'<div class="cf-bar"><div style="width:{pct:.0f}%;background:{color}"></div></div>'


def tile(label: str, value: float, target: float | None) -> str:
    ok = target is None or value >= target
    need = f'<div class="cf-muted">needs {target:.0%}</div>' if target is not None else ""
    return (f'<div class="cf-tile"><b>{value:.0%}</b> {pill("Pass" if ok else "Below", "green" if ok else "red")}'
            f'<div>{escape(label)}</div>{need}</div>')


def steps(current: int, labels: list[str]) -> None:
    cells = "".join(f'<div class="{"on" if i == current else "done" if i < current else ""}">{i + 1}. {escape(x)}</div>'
                    for i, x in enumerate(labels))
    html(f'<div class="cf-steps">{cells}</div>')


def target_chart(df, title: str = "", target: float | None = None, target_label: str = "target",
                 above_is_bad: bool = True, fmt: str = ",.2f"):
    """Line chart with values on hover, a dashed target line and red markers where it was breached
    (UX-3, OBS-27). `df` is indexed by day with one column per series."""
    import altair as alt
    data = df.reset_index().melt(df.index.name or "index", var_name="series", value_name="value").dropna()
    x = df.index.name or "index"
    base = alt.Chart(data).encode(x=alt.X(f"{x}:T", title=None), y=alt.Y("value:Q", title=title),
                                  color=alt.Color("series:N", title=None))
    hover = alt.selection_point(fields=[x], nearest=True, on="pointerover", empty=False)
    layers = [base.mark_line(),
              base.mark_point(size=60, filled=True).encode(
                  opacity=alt.condition(hover, alt.value(1), alt.value(0)),
                  tooltip=[alt.Tooltip(f"{x}:T", title="Day"), "series:N",
                           alt.Tooltip("value:Q", format=fmt)]).add_params(hover)]
    if target is not None:
        layers.append(alt.Chart({"values": [{"t": target, "label": target_label}]}).mark_rule(
            strokeDash=[5, 4], color="#6B7280").encode(y="t:Q", tooltip=["label:N", alt.Tooltip("t:Q", format=fmt)]))
        bad = data[data.value > target] if above_is_bad else data[data.value < target]
        if not bad.empty:
            layers.append(alt.Chart(bad).mark_point(color="#B91C1C", size=90, shape="triangle-up", filled=True)
                          .encode(x=f"{x}:T", y="value:Q", tooltip=[alt.Tooltip(f"{x}:T", title="Day"), "series:N",
                                                                     alt.Tooltip("value:Q", format=fmt)]))
    st.altair_chart(alt.layer(*layers).properties(height=280), use_container_width=True)


@st.dialog("Details", width="large")
def drill(title: str, rows, note: str = "") -> None:
    """Click-through popup (OBS-24): the events behind a bar or cell, with CSV download."""
    import pandas as pd
    df = pd.DataFrame(rows)
    st.markdown(f"**{title}** · {len(df):,} row(s)")
    if note:
        st.caption(note)
    if df.empty:
        st.info("Nothing to show.")
        return
    st.dataframe(df, hide_index=True, use_container_width=True, height=min(38 * (len(df) + 1), 520))
    st.download_button("Download CSV", df.to_csv(index=False), file_name=f"{title.lower().replace(' ', '_')[:60]}.csv",
                       mime="text/csv")


def clickable_bars(df, label: str, value: str, key: str, horizontal: bool = True):
    """Bar chart whose bars can be clicked; returns the clicked label (or None). Values show on hover."""
    import altair as alt
    pick = alt.selection_point(fields=[label], name="pick")
    enc = dict(y=alt.Y(f"{label}:N", sort="-x", title=None), x=alt.X(f"{value}:Q", title=None)) if horizontal else \
        dict(x=alt.X(f"{label}:N", title=None), y=alt.Y(f"{value}:Q", title=None))
    chart = alt.Chart(df).mark_bar(cursor="pointer").encode(
        **enc, opacity=alt.condition(pick, alt.value(1), alt.value(0.6)),
        tooltip=[label, alt.Tooltip(f"{value}:Q", format=",")]).add_params(pick)
    ev = st.altair_chart(chart, use_container_width=True, on_select="rerun", key=key)
    sel = (ev or {}).get("selection", {}).get("pick") or []
    return fresh(key, sel[0].get(label) if sel else None)


def fresh(key: str, value):
    """`value` only the first time it's selected, so a closed popup doesn't reopen on every rerun."""
    seen = st.session_state.get(f"_seen_{key}")
    st.session_state[f"_seen_{key}"] = value
    return value if value is not None and value != seen else None
