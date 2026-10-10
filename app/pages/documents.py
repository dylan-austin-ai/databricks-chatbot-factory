"""Document review and maintenance (QA-2..14, DOC-1..13, DCL-1..8).
Every change lands in the test version (candidate release); it reaches users only after
approval on the Launch page (REL-5)."""
import io
import json

import streamlit as st

from common import (ALL_BOTS, BADGE, app_client, cp, current_user, docs, is_admin, pick_owned_bot, remember_upload,
                    run_job, settings, show_upload_report, sql)
from factory.explain import help_text
from factory.llm import openai_client
from factory.qa import guidance, next_step, page_issues
from factory.reread import propose_section_text
from factory.documents import FLAG_REASONS
from factory.ingestion import BotPaths, page_images, page_images_sql, page_order
from ui import md_text

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
                                            uploaded_by, qa_json FROM {BotPaths(s, b['bot_id']).t('manifest')}
                                     WHERE status <> 'superseded'"""):
                found.append({"Chatbot": b["display_name"], "Document": r["doc_name"], "Version": r["doc_version"],
                              "Readability": BADGE.get(r["readability"]),
                              "Status": (r["status"] or "").replace("_", " "),
                              "What to do next": next_step(r["readability"], r["status"] or "", json.loads(
                                  r["qa_json"] or "{}").get("messages", [])),
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


if pending:
    good = [r for r in pending if r["readability"] in ("green", "n/a")]
    with st.container(border=True):
        st.markdown(f"**Approve in one go** · {len(pending)} document(s) are waiting for review, "
                    f"{len(good)} of them marked Good")
        b1, b2 = st.columns(2)
        if b1.button(f"Approve the {len(good)} Good document(s)", disabled=not good):
            n = D.bulk_approve_green(cfg.bot_id, user)
            republish()
            st.success(f"Approved {n} document(s).")
            st.rerun()
        sure = b2.checkbox("I've checked the ones with warnings", key="approve_all_sure",
                           help="Approves every document waiting for review, including those marked "
                                "Check it or Problem. Documents blocked for restricted data are never approved.")
        if b2.button(f"Approve all {len(pending)} waiting", type="primary", disabled=not sure):
            n = D.bulk_approve_pending(cfg.bot_id, user)
            republish()
            st.success(f"Approved {n} document(s).")
            st.rerun()

show_upload_report(cfg.bot_id)
with st.container(border=True):
    st.subheader("Add documents")
    st.caption("Add files at any time. A file with the same name as an existing document becomes its new "
               "version. New files go into the test version and reach users after approval on the Launch page.")
    ing = s.get("ingestion")
    kinds = ing["parse_extensions"] + ing["text_extensions"] + (
        ing["tabular_extensions"] if ing.get("enable_tabular") else [])
    batch = st.session_state.setdefault("add_docs_batch", 0)  # a new key empties the uploader after an upload
    files = st.file_uploader("Drag and drop, or browse", accept_multiple_files=True, type=kinds,
                             key=f"add_docs_{batch}")
    keep = st.checkbox("These stay valid until I upload a new version", value=True, key="add_noexp")
    exp = None if keep else st.date_input("They expire on", key="add_exp")
    if st.button("Upload", type="primary", disabled=not files):
        report = D.upload_many(cfg.bot_id, [
            {"name": f.name, "data": f.getvalue(), "no_expiry": keep, "expires_at": str(exp) if exp else None}
            for f in files], user)
        remember_upload(cfg.bot_id, report)
        if any(item["ok"] for item in report) and not s.get("ingestion.auto_trigger", True):
            run_job("ingest", bot_id=cfg.bot_id)  # otherwise the file-arrival trigger starts reading
        st.session_state["add_docs_batch"] = batch + 1
        st.rerun()

missing = D.not_uploaded(cfg.bot_id)
if missing:
    with st.expander(f"Not uploaded in the last 30 days: {len(missing)} file(s)"):
        st.caption("Someone tried to add these and they were refused, and no file with that name has been "
                   "added since. Fix the problem and upload them again above.")
        st.dataframe([{"File": m["file_name"], "Why": " ".join(m["reasons"]), "Tried by": m["by"],
                       "When": str(m["when"])[:16]} for m in missing], hide_index=True, use_container_width=True)

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
valid = "valid until replaced" if str(doc["no_expiry"]).lower() != "false" else f"expires {doc['expires_at'] or 'not set'}"
st.caption(f"Version {doc['doc_version']} · uploaded by {doc['uploaded_by']} · "
           f"{doc['chunk_count'] or 0} sections · {valid}")
# What was found, by how sure the check is, each with what to do about it (QA-7).
notes = [guidance(m) for m in qa.get("messages", [])]
st.info("**What to do next:** " + next_step(doc["readability"], doc["status"], qa.get("messages", [])))
GROUPS = [("problem", "Needs fixing", st.error),
          ("check", "Worth checking", st.warning),
          ("opinion", "Suggestions from the automatic reviewer (quality warnings, not confirmed problems)", st.info)]
for kind, heading, box in GROUPS:
    found = [n for n in notes if n["kind"] == kind]
    if found:
        box(f"**{heading}**\n\n" + "\n\n".join(
            f"- {md_text(n['text'])}\n\n  *What to do:* {n['next']}" for n in found))
for n in notes:
    if n["kind"] == "info" and n["text"]:
        st.caption(n["text"] + (f" {n['next']}" if n["next"] else ""))

# One decision per document: it is the whole document that goes into the chatbot or stays out.
CHOICES = {"approve": "Approve: use this document in the chatbot",
           "skip": "Skip for now: decide later",
           "flag": "Flag for fixing: it needs work before it can be used"}
NOW = {"approved": "approve", "flagged": "flag"}.get(doc["status"], "skip")
blocked = doc["flag_reason"] == "Contains sensitive info"
with st.container(border=True):
    st.markdown("**Your decision for this document**")
    st.caption("This is one decision for the whole document, every page and section together. It decides whether "
               "the document goes into the chatbot's test version; nothing reaches users until the test version "
               "is approved on the Launch page. Pick one of the three and save. You can change it later.")
    if doc["status"] == "archived":
        st.info("This document is archived, so it isn't used. Restore it to make a decision.")
        if st.button("Restore"):
            D.restore(cfg.bot_id, doc_id, user)
            republish()
            st.rerun()
    elif blocked:
        st.error("This document is blocked because it appears to contain restricted data, so it can't be approved "
                 "or skipped. Remove the data from the source file and upload it as a new version.")
    else:
        st.markdown({"approve": ":green[**Now: approved.**] It is in the test version.",
                     "flag": f":red[**Now: flagged for fixing**] ({doc['flag_reason'] or 'no reason given'}). "
                             "It is not used.",
                     "skip": ":orange[**Now: no decision yet.**] It is waiting for review and is not used."}[NOW])
        choice = st.radio("Choose one", list(CHOICES), index=list(CHOICES).index(NOW), key=f"decision_{doc_id}",
                          format_func=lambda c: CHOICES[c],
                          captions=["It goes into the test version.",
                                    "Nothing changes for the chatbot. It stays in Need review so you can come back.",
                                    "It stays out, with a note of what is wrong so you know what to come back to."])
        reason = None
        if choice == "flag":  # only asked when flagging
            reason = st.selectbox("What needs fixing?", FLAG_REASONS, key=f"flag_reason_{doc_id}",
                                  index=FLAG_REASONS.index(doc["flag_reason"])
                                  if doc["flag_reason"] in FLAG_REASONS else 0)
        changed = choice != NOW or (choice == "flag" and reason != doc["flag_reason"])
        if st.button("Save decision", type="primary", disabled=not changed):
            try:
                if choice == "approve":
                    D.approve(cfg.bot_id, doc_id, user)
                elif choice == "flag":
                    D.flag(cfg.bot_id, doc_id, user, reason)
                else:
                    D.defer(cfg.bot_id, doc_id, user)
                republish()
                st.rerun()
            except PermissionError as e:
                st.error(str(e))
        if not changed:
            st.caption("Choose a different option to change the decision.")
if doc["status"] != "archived":
    with st.expander("Remove this document from the chatbot"):
        st.caption("Archiving takes the document out altogether. It isn't deleted and can be restored.")
        if st.button("Archive (can be restored)"):
            D.archive(cfg.bot_id, doc_id, user)
            republish()
            st.rerun()

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
                   e.value:content::STRING AS content, e.value:description::STRING AS description,
                   to_json(e.value:bbox[0]:coord) AS coord
            FROM {p.t('parsed_elements')} pe, LATERAL variant_explode(pe.parsed:document:elements) e
            WHERE pe.doc_id = :d AND pe.parsed IS NOT NULL
            QUALIFY DENSE_RANK() OVER (ORDER BY pe.parsed_at DESC) = 1""", {"d": doc_id})
        for e in els:  # the warehouse returns numbers as text
            e["page"] = int(e["page"]) if e["page"] is not None else None
        pages = sorted({e["page"] for e in els if e["page"] is not None})
        if not pages:
            st.info("Nothing extracted yet.")
        else:
            st.caption("The left side is the original page. The right side is what was read from that page, "
                       "split into blocks. Choose a block to see its full text and where it sits on the page.")
            by_page, general = page_issues(qa.get("messages", []))
            flagged_pages = sorted(pg for pg in by_page if pg in pages)
            page = pages[0] if len(pages) == 1 else st.select_slider(
                "Page", options=pages, format_func=lambda x: f"{x + 1} ⚠" if x in by_page else str(x + 1))
            if flagged_pages:
                st.caption("Pages with possible issues (marked ⚠ on the slider): "
                           + ", ".join(str(pg + 1) for pg in flagged_pages))
            here = by_page.get(page, [])
            with st.container(border=True):  # what was reported for the page on screen, so no scrolling
                if here:
                    st.markdown(f"**Possible issues on page {page + 1}**")
                    for n in here:
                        (st.error if n["kind"] == "problem" else st.warning if n["kind"] == "check" else st.info)(
                            f"{md_text(n['text'])}\n\n*What to do:* {n['next']}")
                else:
                    st.markdown(f"**No issues were reported for page {page + 1}.**")
                if general:
                    with st.expander(f"{len(general)} note(s) about the whole document, with no page given"):
                        for n in general:
                            st.markdown(f"- {md_text(n['text'])}")
                        st.caption("These don't point to a page. Notes written before page numbers were "
                                   "recorded get them after the document is re-read.")
            page_els = [e for e in els if e["page"] == page]

            def body(e: dict) -> str:
                return (e["content"] or e.get("description") or "").strip()

            left, right = st.columns([3, 2])
            with right:
                st.markdown(f"**Page {page + 1}: {len(page_els)} block(s) read**")
                full_text = st.container(border=True)   # filled in below, once a block is chosen
                with st.container(height=320):
                    pick = st.radio(
                        "Blocks on this page", range(len(page_els)), key=f"block_{doc_id}_{page}",
                        format_func=lambda i: f"{i + 1}. {page_els[i]['type']}: "
                                              f"{body(page_els[i])[:60] or '(no text)'}"
                                              + ("…" if len(body(page_els[i])) > 60 else ""))
                with full_text:
                    chosen = page_els[pick]
                    st.caption(f"Block {pick + 1} of {len(page_els)} · {chosen['type']} · full text")
                    if chosen["content"]:
                        st.markdown(md_text(chosen["content"]), unsafe_allow_html=False)
                    elif chosen.get("description"):
                        st.markdown(f"*Description written for this {chosen['type']}:* {md_text(chosen['description'])}",
                                    unsafe_allow_html=False)
                    else:
                        st.markdown("_(nothing was read from this block)_")
            try:
                from PIL import Image, ImageDraw
                # The reader records which image belongs to which page; the folder listing can't be
                # trusted for that (it also holds images from earlier reads).
                images = page_images(sql().query(page_images_sql(p), {"d": doc_id, "v": int(doc["doc_version"])}))
                if page not in images:
                    folder = f"{p.images}/{doc_id}/v{doc['doc_version']}"
                    images = dict(enumerate(sorted((f.path for f in app_client().files.list_directory_contents(folder)
                                                    if not f.is_directory), key=page_order)))
                img = Image.open(io.BytesIO(app_client().files.download(images[page]).contents.read())).convert("RGBA")
                coord = [int(c) for c in json.loads(chosen["coord"] or "[]")]
                marked = len(coord) == 4
                if marked:
                    x1, x2 = sorted((coord[0], coord[2]))
                    y1, y2 = sorted((coord[1], coord[3]))
                    shade = Image.new("RGBA", img.size, (0, 0, 0, 0))
                    ImageDraw.Draw(shade).rectangle([x1, y1, x2, y2], fill=(255, 221, 0, 70), outline=(220, 0, 0, 255),
                                                    width=max(4, img.width // 200))
                    img = Image.alpha_composite(img, shade)
                left.image(img, use_container_width=True)
                left.caption(f"Original page {page + 1}. " + (f"The shaded red box is block {pick + 1}."
                                                              if marked else "This block has no position on the page."))
            except Exception as e:  # noqa: BLE001
                left.info(f"Page image unavailable ({type(e).__name__}).")

            # Hand-typed text, for when the reading can't be trusted: screenshots with arrows, flow
            # charts, forms. It replaces the reading for the whole document.
            mine = D.current_override(cfg.bot_id, doc_id)
            if mine:
                st.success("**The chatbot is using text you typed for this document**, not the blocks read from the "
                           f"page above (saved {str(mine['saved_at'])[:16]}).")
            with st.expander("The text is wrong or out of order? Type it yourself", expanded=bool(mine)):
                st.markdown(
                    "Use this when the reading lost the meaning, for example a screenshot with arrows or a flow "
                    "chart. **What you type replaces everything read from this document**, on every page, and is "
                    "what the chatbot will search and quote.\n\n"
                    "- Write it the way you would explain it to a colleague: a short title, then numbered steps in "
                    "the order they are done.\n"
                    "- Name the buttons, menus and fields exactly as they appear on screen.\n"
                    "- Include every page's content, not only the part that was wrong.\n"
                    "- Answers that use this text will name the document as their source, without a page number.")
                read = "\n\n".join(
                    (f"Page {pg + 1}\n" if len(pages) > 1 else "")
                    + "\n".join(body(e) for e in els if e["page"] == pg and body(e)) for pg in pages)
                typed = st.text_area(
                    "The text for this document", value=(mine["text"] if mine else read), height=320,
                    key=f"override_{doc_id}",
                    help="Starts with what was read, so you can fix it instead of starting from nothing.")
                o1, o2 = st.columns(2)
                if o1.button("Save my text", type="primary", disabled=not typed.strip()):
                    D.save_override(cfg.bot_id, doc_id, typed.strip(), user)
                    run_job("ingest", bot_id=cfg.bot_id, doc_ids=doc_id, rechunk_only="true", generate_golden="false")
                    st.success("Saved. The chatbot's sections for this document are being rebuilt from your text; "
                               "check the **Sections** tab in a few minutes, then approve the document.")
                if mine and o2.button("Go back to the automatic reading"):
                    D.remove_override(cfg.bot_id, doc_id, user)
                    run_job("ingest", bot_id=cfg.bot_id, doc_ids=doc_id, rechunk_only="true", generate_golden="false")
                    st.rerun()

with tab_chunks:
    st.caption("Sections are the pieces the chatbot searches and quotes. Fix one section here without redoing "
               "the document: correct its text, have the app read it again from the page image, or flag it so "
               "it isn't used. Re-reading the whole document later rebuilds its sections and drops these fixes.")
    chunks = sql().query(f"""SELECT chunk_id, chunk_position, section, page_ids, chunk_to_retrieve, flagged
                             FROM {p.t('chunked')} WHERE doc_id = :d ORDER BY chunk_position""",
                         {"d": doc_id})
    edits = D.edited_chunks(cfg.bot_id, doc_id)
    for c in chunks:
        cid = c["chunk_id"]
        page_ids = [int(x) for x in (json.loads(c["page_ids"] or "[]") if isinstance(c["page_ids"], str)
                                     else (c["page_ids"] or []))]
        flagged = str(c["flagged"]).lower() == "true"
        editing = st.session_state.get("editing_section") == cid
        with st.container(border=True):
            st.caption(f"Section {int(c['chunk_position']) + 1} · page(s) {', '.join(str(x + 1) for x in page_ids)} · "
                       f"{c['section'] or ''} · {len(c['chunk_to_retrieve'] or '')} characters"
                       + (" · **flagged, not used**" if flagged else "")
                       + (f" · **edited by {edits[cid]['edited_by']}**" if cid in edits else ""))
            if not editing:
                st.markdown(md_text(c["chunk_to_retrieve"]))
                b1, b2, b3, b4 = st.columns(4)
                if b1.button("Edit the text", key=f"edit_{cid}"):
                    st.session_state["editing_section"] = cid
                    st.session_state.pop(f"proposal_{cid}", None)
                    st.rerun()
                if b2.button("Read it again from the page", key=f"reread_{cid}",
                             help="The app looks at the page image and proposes a corrected version of this "
                                  "section. You check it before anything is saved."):
                    try:
                        images = page_images(sql().query(page_images_sql(p), {"d": doc_id, "v": int(doc["doc_version"])}))
                        pics = [app_client().files.download(images[x]).contents.read() for x in page_ids if x in images]
                        with st.spinner("Reading the page again…"):
                            st.session_state[f"proposal_{cid}"] = propose_section_text(
                                openai_client(app_client()), s.get("models.generation_endpoint"), pics,
                                c["chunk_to_retrieve"] or "")
                        st.session_state["editing_section"] = cid
                        st.rerun()
                    except Exception as e:  # noqa: BLE001 - say so and leave the section as it is
                        st.error(f"The section couldn't be read again ({type(e).__name__}). Nothing was changed. "
                                 "You can still edit the text by hand. If this keeps happening, ask MLOps to check "
                                 "that the app may call the reading model.")
                if b3.button("Use this section again" if flagged else "Don't use this section", key=cid):
                    D.flag_chunk(cfg.bot_id, cid, user, not flagged)
                    republish()
                    st.rerun()
                if cid in edits and b4.button("Undo my edit", key=f"undo_{cid}"):
                    D.restore_chunk(cfg.bot_id, cid, user)
                    republish()
                    st.rerun()
            else:
                proposal = st.session_state.get(f"proposal_{cid}")
                if proposal:
                    (st.info if proposal["changed"] else st.warning)(
                        ("**This is the app's new reading of the section, not saved yet.** "
                         + (md_text(proposal["note"]) + " " if proposal["note"] else "")
                         + "Compare it with the page, change anything that is wrong, then save.")
                        if proposal["changed"] else
                        "The app read the page again and found nothing to change. You can still edit it yourself.")
                new_text = st.text_area("Text of this section", value=(proposal or {}).get("text") or c["chunk_to_retrieve"] or "",
                                        height=260, key=f"text_{cid}")
                with st.expander("Text before your change"):
                    st.markdown(md_text(c["chunk_to_retrieve"]))
                s1, s2 = st.columns(2)
                if s1.button("Save this section", type="primary", key=f"save_{cid}",
                             disabled=not new_text.strip() or new_text.strip() == (c["chunk_to_retrieve"] or "").strip()):
                    D.edit_chunk(cfg.bot_id, cid, new_text, user)
                    republish()
                    st.session_state.pop("editing_section", None)
                    st.session_state.pop(f"proposal_{cid}", None)
                    st.rerun()
                if s2.button("Cancel", key=f"cancel_{cid}"):
                    st.session_state.pop("editing_section", None)
                    st.session_state.pop(f"proposal_{cid}", None)
                    st.rerun()

with tab_fix:
    st.caption("Three ways to fix a document that wasn't read well. Pick the one that fits.")
    st.markdown("**Re-read with some pages skipped**")
    st.caption("For a document with pages that shouldn't be used, such as covers or blank pages.")
    pr = st.text_input("Pages to keep (e.g. 1-3,5,7-40)", doc["page_range"] or "")
    if st.button("Re-read document"):
        try:
            D.set_page_range(cfg.bot_id, doc_id, pr or None, user)
            run_job("ingest", bot_id=cfg.bot_id, doc_ids=doc_id, reparse="true", generate_golden="false")
            st.success("Re-reading in the background.")
        except ValueError as e:
            st.error(str(e))
    st.markdown("**Upload a cleaner copy** (becomes a new version)")
    st.caption("Best when you have the original: export it from Word or PowerPoint instead of using a scan or photo.")
    newf = st.file_uploader("Replacement file", key="replace")
    if newf and st.button("Replace"):
        res = D.register(cfg.bot_id, doc["doc_name"], newf.getvalue(), user)
        if res.ok:
            if not settings().get("ingestion.auto_trigger", True):
                run_job("ingest", bot_id=cfg.bot_id)
            st.success("New version uploaded.")
        else:
            st.error(" ".join(res.reasons))
    st.markdown("**Type the text yourself**")
    st.caption("If the reading lost the meaning (a screenshot with arrows, a flow chart), open "
               "**What the chatbot sees** and use **The text is wrong or out of order? Type it yourself**, "
               "under the page.")

if is_admin():
    with st.expander("MLOps: permanently delete (legal/retention only)"):
        why = st.text_input("Legal reason (required)")
        if why and st.button("Delete permanently", type="secondary"):
            D.hard_delete(cfg.bot_id, doc_id, user, True, why)
            republish()
            st.rerun()
