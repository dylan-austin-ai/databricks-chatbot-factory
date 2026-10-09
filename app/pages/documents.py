"""Document review and maintenance (QA-2..14, DOC-1..13, DCL-1..8).
Every change lands in the test version (candidate release); it reaches users only after
approval on the Launch page (REL-5)."""
import io
import json

import streamlit as st

from common import (ALL_BOTS, BADGE, app_client, cp, current_user, docs, is_admin, pick_owned_bot, run_job,
                    settings, sql)
from factory.explain import help_text
from factory.documents import FLAG_REASONS
from factory.ingestion import BotPaths

st.title("Documents", help=help_text("documents"))
# Documents are visible to a chatbot's owner (the owner person or a member of the owner group)
# and to MLOps. Reviewers and testers don't see them here.
choice, owned = pick_owned_bot()
if choice is None:
    st.stop()
s, user, D = settings(), current_user(), docs()

if choice == ALL_BOTS:
    found, not_ready = [], []
    for b in owned:
        try:
            for r in sql().query(f"""SELECT doc_name, doc_version, readability, status, expires_at, no_expiry,
                                            uploaded_by FROM {BotPaths(s, b['bot_id']).t('manifest')}
                                     WHERE status <> 'superseded'"""):
                found.append({"Chatbot": b["display_name"], "Document": r["doc_name"], "Version": r["doc_version"],
                              "Readability": BADGE.get(r["readability"]),
                              "Status": (r["status"] or "").replace("_", " "),
                              "Expires": "Until replaced" if str(r["no_expiry"]).lower() != "false"
                              else str(r["expires_at"] or ""), "Uploaded by": r["uploaded_by"]})
        except Exception:  # noqa: BLE001 - setup hasn't created this chatbot's document table yet
            not_ready.append(b["display_name"])
    st.caption(f"Documents across {len(owned)} chatbot(s) you "
               + ("administer" if is_admin() else "own")
               + ". Choose one chatbot in the sidebar to review, add or change its documents.")
    c1, c2, c3 = st.columns(3)
    c1.metric("Chatbots", len(owned))
    c2.metric("Documents", len(found))
    c3.metric("Need review", len([r for r in found if r["Status"] == "pending review"]))
    if found:
        statuses = sorted({r["Status"] for r in found})
        keep = st.multiselect("Status", statuses, default=statuses)
        find = st.text_input("Search by document or chatbot name", "").strip().lower()
        shown = [r for r in found if r["Status"] in keep
                 and (not find or find in r["Document"].lower() or find in r["Chatbot"].lower())]
        st.dataframe(sorted(shown, key=lambda r: (r["Chatbot"], r["Document"])), hide_index=True,
                     use_container_width=True)
    else:
        st.info("No documents yet.")
    if not_ready:
        st.caption("Setup not finished, so no documents to show yet: " + ", ".join(sorted(not_ready)))
    st.stop()

bot = next(b for b in owned if b["bot_id"] == choice)
cfg = cp().get_config(choice)
p = BotPaths(s, cfg.bot_id)

rows = sql().query(f"""
    SELECT * FROM {p.t('manifest')} WHERE status <> 'superseded'
    ORDER BY CASE readability WHEN 'red' THEN 0 WHEN 'yellow' THEN 1 WHEN 'green' THEN 2 ELSE 3 END,
             CASE status WHEN 'flagged' THEN 0 WHEN 'pending_review' THEN 1 ELSE 2 END, doc_name""")
pending = [r for r in rows if r["status"] == "pending_review"]
in_progress = [r for r in rows if not r["readability"] and r["status"] == "pending_review"]

c1, c2, c3, c4 = st.columns(4)
c1.metric("Documents", len([r for r in rows if r["status"] != "archived"]))
c2.metric("Need review", len(pending))
c3.metric("Approved", len([r for r in rows if r["status"] == "approved"]))
c4.metric("Archived", len([r for r in rows if r["status"] == "archived"]))
if in_progress:
    st.info(f"{len(in_progress)} document(s) are still being read. Refresh in a few minutes.")
st.caption("Changes here update the test version. Publish them to users from the Launch page.")


def republish():
    run_job("ingest", bot_id=cfg.bot_id, publish_only="true")


if pending and st.button("Approve all Good documents"):
    n = D.bulk_approve_green(cfg.bot_id, user)
    republish()
    st.success(f"Approved {n} document(s).")
    st.rerun()

with st.expander("Add documents"):
    files = st.file_uploader("Drag and drop", accept_multiple_files=True, key="add_docs")
    keep = st.checkbox("These stay valid until I upload a new version", value=True, key="add_noexp")
    exp = None if keep else st.date_input("They expire on", key="add_exp")
    if files and st.button("Upload"):
        added = []
        for f in files:
            res = D.register(cfg.bot_id, f.name, f.getvalue(), user,
                             no_expiry=keep, expires_at=str(exp) if exp else None)
            if res.ok:
                added.append(f.name)
            else:
                st.warning(f"**{f.name}** wasn't added: {' '.join(res.reasons)}")
        if added:
            if not settings().get("ingestion.auto_trigger", True):  # otherwise the file-arrival trigger runs it
                run_job("ingest", bot_id=cfg.bot_id)
            st.success(f"Added {len(added)} file(s). Reading starts within a minute; same-named files "
                       "become new versions.")

st.divider()
names = {r["doc_id"]: r for r in rows}
doc_id = st.selectbox("Review a document", list(names),
                      format_func=lambda d: f"{BADGE.get(names[d]['readability'])} · "
                                            f"{names[d]['doc_name']} · {names[d]['status'].replace('_', ' ')}")
if not doc_id:
    st.stop()
doc = names[doc_id]
qa = json.loads(doc["qa_json"] or "{}")

st.subheader(doc["doc_name"])
st.caption(f"Version {doc['doc_version']} · uploaded by {doc['uploaded_by']} · "
           f"{doc['chunk_count'] or 0} sections · review by {doc['review_by']}")
for m in qa.get("messages", []):
    (st.error if doc["readability"] == "red" else st.warning if doc["readability"] == "yellow"
     else st.caption)(m)

tab_view, tab_chunks, tab_details, tab_fix = st.tabs(
    ["What the chatbot sees", "Sections", "Details", "Fix a problem"])

with tab_details:  # metadata only: no re-reading or re-splitting (DCL-3)
    owner = st.text_input("Document owner", doc["doc_owner"] or "")
    no_exp = st.checkbox("Valid until a new version is uploaded", value=str(doc["no_expiry"]).lower() != "false")
    expires = None if no_exp else st.date_input("Expires on")
    effective = st.date_input("Effective from", value=None)
    if st.button("Save details"):
        D.update_metadata(cfg.bot_id, doc_id, user, doc_owner=owner, no_expiry=no_exp,
                          **({"expires_at": str(expires)} if expires else {}),
                          **({"effective_date": str(effective)} if effective else {}))
        republish()
        st.success("Saved to the test version.")
    if st.button("⬇️ Get original file"):
        name, data = D.download(cfg.bot_id, doc_id)
        st.download_button("Save file", data, file_name=name)

with tab_view:
    if doc["parser"] == "text_reader":
        text = sql().query(f"SELECT text_content FROM {p.t('parsed_elements')} WHERE doc_id = :d "
                           "ORDER BY parsed_at DESC LIMIT 1", {"d": doc_id})
        st.text_area("Text", text[0]["text_content"] if text else "", height=500, disabled=True)
    else:
        els = sql().query(f"""
            SELECT e.value:bbox[0]:page_id::INT AS page, e.value:type::STRING AS type,
                   e.value:content::STRING AS content, to_json(e.value:bbox[0]:coord) AS coord
            FROM {p.t('parsed_elements')} pe, LATERAL variant_explode(pe.parsed:document:elements) e
            WHERE pe.doc_id = :d AND pe.parsed IS NOT NULL
            QUALIFY DENSE_RANK() OVER (ORDER BY pe.parsed_at DESC) = 1""", {"d": doc_id})
        pages = sorted({e["page"] for e in els if e["page"] is not None})
        if not pages:
            st.info("Nothing extracted yet.")
        else:
            page = st.select_slider("Page", options=pages, format_func=lambda x: str(x + 1))
            page_els = [e for e in els if e["page"] == page]
            left, right = st.columns(2)
            pick = right.radio("Click a block to highlight it on the page", range(len(page_els)),
                               format_func=lambda i: f"[{page_els[i]['type']}] "
                                                     f"{(page_els[i]['content'] or '')[:90]}")
            try:
                from PIL import Image, ImageDraw
                folder = f"{p.images}/{doc_id}/v{doc['doc_version']}"
                imgs = sorted(f.path for f in app_client().files.list_directory_contents(folder)
                              if not f.is_directory)
                img = Image.open(io.BytesIO(app_client().files.download(imgs[page]).contents.read()))
                coord = json.loads(page_els[pick]["coord"] or "[]") if page_els else []
                if len(coord) == 4:
                    ImageDraw.Draw(img).rectangle(coord, outline="red", width=4)
                left.image(img, use_container_width=True)
            except Exception as e:  # noqa: BLE001
                left.info(f"Page image unavailable ({e}).")
            if page_els:
                right.markdown(page_els[pick]["content"] or "_(image or empty block)_",
                               unsafe_allow_html=False)

with tab_chunks:
    chunks = sql().query(f"""SELECT chunk_id, chunk_position, section, page_ids, chunk_to_retrieve, flagged
                             FROM {p.t('chunked')} WHERE doc_id = :d ORDER BY chunk_position""",
                         {"d": doc_id})
    for c in chunks:
        pages = ", ".join(str(int(x) + 1) for x in json.loads(c["page_ids"] or "[]")) \
            if isinstance(c["page_ids"], str) else c["page_ids"]
        with st.container(border=True):
            st.caption(f"Section {int(c['chunk_position']) + 1} · page(s) {pages} · "
                       f"{c['section'] or ''} · {len(c['chunk_to_retrieve'] or '')} characters"
                       + (" · flagged" if str(c["flagged"]).lower() == "true" else ""))
            st.write(c["chunk_to_retrieve"])
            flagged = str(c["flagged"]).lower() == "true"
            if st.button("Unflag" if flagged else "Flag this section", key=c["chunk_id"]):
                D.flag_chunk(cfg.bot_id, c["chunk_id"], user, not flagged)
                republish()
                st.rerun()

with tab_fix:
    st.markdown("**Re-read with some pages skipped**")
    pr = st.text_input("Pages to keep (e.g. 1-3,5,7-40)", doc["page_range"] or "")
    if st.button("Re-read document"):
        try:
            D.set_page_range(cfg.bot_id, doc_id, pr or None, user)
            run_job("ingest", bot_id=cfg.bot_id, doc_ids=doc_id, reparse="true", generate_golden="false")
            st.success("Re-reading in the background.")
        except ValueError as e:
            st.error(str(e))
    st.markdown("**Upload a cleaner copy** (becomes a new version)")
    newf = st.file_uploader("Replacement file", key="replace")
    if newf and st.button("Replace"):
        res = D.register(cfg.bot_id, doc["doc_name"], newf.getvalue(), user)
        if res.ok:
            if not settings().get("ingestion.auto_trigger", True):
                run_job("ingest", bot_id=cfg.bot_id)
            st.success("New version uploaded.")
        else:
            st.error(" ".join(res.reasons))
    st.markdown("**Correct the text by hand**")
    override = st.text_area("Corrected text (replaces what was extracted)", height=200)
    if override and st.button("Save corrected text"):
        D.save_override(cfg.bot_id, doc_id, override, user)
        run_job("ingest", bot_id=cfg.bot_id, doc_ids=doc_id, rechunk_only="true", generate_golden="false")
        st.success("Saved. Sections will update shortly.")

st.divider()
a1, a2, a3 = st.columns(3)
if doc["status"] in ("pending_review", "flagged") and a1.button("Approve", type="primary"):
    D.approve(cfg.bot_id, doc_id, user)
    republish()
    st.rerun()
if doc["status"] != "archived":
    reason = a2.selectbox("Flag reason", FLAG_REASONS, label_visibility="collapsed")
    if a2.button("Flag for fixing"):
        D.flag(cfg.bot_id, doc_id, user, reason)
        republish()
        st.rerun()
    if a3.button("Archive (can be restored)"):
        D.archive(cfg.bot_id, doc_id, user)
        republish()
        st.rerun()
else:
    if a3.button("Restore"):
        D.restore(cfg.bot_id, doc_id, user)
        republish()
        st.rerun()

if is_admin():
    with st.expander("MLOps: permanently delete (legal/retention only)"):
        why = st.text_input("Legal reason (required)")
        if why and st.button("Delete permanently", type="secondary"):
            D.hard_delete(cfg.bot_id, doc_id, user, True, why)
            republish()
            st.rerun()
