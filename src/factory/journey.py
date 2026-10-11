"""What a chatbot's owner should do next (UX-1).

Getting a chatbot live takes the same steps every time. This turns what the app already knows
about a chatbot (its state, its documents, its test questions and its test version) into a
checklist with exactly one current step, written for someone who has never done it before:
what the situation is, what to press, and on which page.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from .ingestion import BotPaths
from .sql import union_by_key

DONE, NOW, WAITING, PROBLEM, LATER = "done", "now", "waiting", "problem", "later"
STATUS_LABEL = {DONE: "Done", NOW: "Your turn", WAITING: "Nothing to do yet", PROBLEM: "Needs fixing",
                LATER: "Later"}


@dataclass(frozen=True)
class Step:
    title: str
    status: str          # done | now | waiting | problem | later
    detail: str = ""     # the situation, and what to do about it
    page: str = ""       # where to do it: create | documents | test_questions | launch | ""


def _n(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else many or one + 's'}"


def _is(value) -> bool:
    return str(value).lower() == "true"


def steps(state: str, docs: dict | None, questions: dict | None, needed: int, candidate: dict | None,
          has_live: bool, reviewer: str = "", is_reviewer: bool = False) -> list[Step]:
    """The path to launch, in order. `docs` counts documents: reading, waiting, flagged, approved.
    `questions` counts test questions: total, approved. `candidate` is the test version's release
    row, or None. Only the first step that isn't done is live; later ones show as "later"."""
    d = {k: int((docs or {}).get(k) or 0) for k in ("reading", "waiting", "flagged", "approved")}
    q = {k: int((questions or {}).get(k) or 0) for k in ("total", "approved")}
    out: list[Step] = []

    if state == "draft":
        out.append(Step("Finish setup", NOW, "Setup was started but not finished. Open **Create chatbot**, find it "
                        "under **Unfinished chatbots** and press **Continue setup**.", "create"))
    elif state == "provisioning":
        out.append(Step("Finish setup", WAITING, "The chatbot is being set up in the background. This usually "
                        "takes about 10 minutes. Press **Refresh** to see progress."))
    elif state == "provision_failed":
        out.append(Step("Finish setup", PROBLEM, "Setup stopped part-way. Open **Launch** and press **Try again**. "
                        "If it stops a second time, send the failed step shown there to MLOps.", "launch"))
    else:
        out.append(Step("Finish setup", DONE, "The chatbot is set up."))

    total = sum(d.values())
    out.append(Step("Add documents", DONE, f"{_n(total, 'document')} added.") if total else
               Step("Add documents", NOW, "Open **Documents**, choose **Add documents** and upload the files the "
                    "chatbot should answer from.", "documents"))

    if d["flagged"]:
        review = Step("Review your documents", PROBLEM, f"{_n(d['flagged'], 'document')} "
                      f"{'has' if d['flagged'] == 1 else 'have'} a problem. Open **Documents**, choose **Review a "
                      "document** and follow the steps shown for each one. A document you don't need can be "
                      "archived instead.", "documents")
    elif d["waiting"]:
        review = Step("Review your documents", NOW, f"{_n(d['waiting'], 'document')} "
                      f"{'is' if d['waiting'] == 1 else 'are'} waiting for your decision. Open **Documents**, choose "
                      "**Review a document**, check each one reads correctly and press **Approve**.", "documents")
    elif d["reading"] and not d["approved"]:
        review = Step("Review your documents", WAITING, f"{_n(d['reading'], 'document')} "
                      f"{'is' if d['reading'] == 1 else 'are'} still being read. This takes a few minutes per "
                      "document. Press **Refresh** to check.")
    elif d["approved"]:
        review = Step("Review your documents", DONE, f"{_n(d['approved'], 'document')} approved."
                      + (f" {_n(d['reading'], 'more')} still being read." if d["reading"] else ""))
    else:
        review = Step("Review your documents", LATER, "Approve each document once it has been read.", "documents")
    out.append(review)

    if q["approved"] >= needed:
        out.append(Step("Approve test questions", DONE, f"{q['approved']} test questions approved."))
    elif not q["total"]:
        out.append(Step("Approve test questions", NOW, "There are no test questions yet. Open **Test questions** and "
                        "press **Suggest questions from my approved documents**, or type your own.", "test_questions"))
    else:
        out.append(Step("Approve test questions", NOW, f"{q['approved']} of {needed} approved, so "
                        f"{needed - q['approved']} more to go. Open **Test questions**, tick **Approved** on the ones "
                        "that are right, fix or remove the rest, then press **Save changes**.", "test_questions"))

    passed = bool(candidate) and _is(candidate.get("eval_passed"))
    if not candidate:
        check = Step("Pass the quality check", DONE, "The live version passed.") if has_live else \
            Step("Pass the quality check", WAITING, "The test version is built automatically after you approve "
                 "documents. It appears on **Launch** a few minutes later.", "launch")
    elif passed:
        check = Step("Pass the quality check", DONE, "The test version passed the quality check.")
    elif candidate.get("eval_run_id"):
        check = Step("Pass the quality check", PROBLEM, "The last quality check didn't pass. Open **Launch** to see "
                     "which results are below target and what to change, then run the check again.", "launch")
    else:
        check = Step("Pass the quality check", NOW, "Open **Launch** and press **Run the quality check**. It takes "
                     "about 15 minutes; you can leave the page while it runs.", "launch")
    out.append(check)

    requested = bool(candidate) and candidate.get("review_requested_at") is not None
    if not candidate:
        approval = Step("Get approval and publish", DONE, "Published.") if has_live else \
            Step("Get approval and publish", LATER, f"{reviewer or 'The Reviewer'} approves, then it goes live.")
    elif requested and is_reviewer:
        approval = Step("Get approval and publish", NOW, "This version is waiting for **your** approval. Open "
                        "**Launch**, look at the results and press **Approve and publish** or **Send back for "
                        "changes**.", "launch")
    elif requested:
        approval = Step("Get approval and publish", WAITING, f"Waiting for **{reviewer or 'the Reviewer'}** to "
                        "approve. They see it on their Home page. There is nothing for you to do until they decide.")
    else:
        approval = Step("Get approval and publish", NOW, "Open **Launch** and press **Send for approval**. "
                        f"{reviewer or 'The Reviewer'} then approves it and it goes live.", "launch")
    out.append(approval)

    seen_open = False
    for i, step in enumerate(out):   # one current step; everything after it waits its turn
        if step.status == DONE:
            continue
        if seen_open:
            out[i] = replace(step, status=LATER)
        seen_open = True
    return out


def next_step(state: str, path: list[Step], has_candidate: bool = False) -> Step:
    """The one thing to tell the owner now. Chatbots that are live, paused or archived have their
    own message unless a new version is on its way."""
    current = next((step for step in path if step.status != DONE), None)
    working_on_a_version = has_candidate and current is not None
    if state == "live" and not working_on_a_version:
        return Step("Live", DONE, "Live and answering questions. Nothing to do. Changes you make to documents "
                    "go into a new test version first.")
    if state == "paused":
        return Step("Paused", NOW, "Paused, so nobody can ask it questions. Open **Launch** and press **Resume** "
                    "to turn it back on.", "launch")
    if state == "budget_paused":
        return Step("Paused", WAITING, "Paused because this month's budget was reached. It turns back on by "
                    "itself on the 1st. To turn it on sooner, ask MLOps to raise the budget.")
    if state == "archived":
        return Step("Archived", WAITING, "Archived. It no longer answers questions; its documents and history are "
                    "kept. Ask MLOps if it should be restored.")
    return current or Step("Live", DONE, "Everything is done.")


def progress_select(p: BotPaths) -> str:
    """Document and test-question counts for one chatbot, as one row."""
    return f"""SELECT d.reading, d.waiting, d.flagged, d.approved, q.questions, q.questions_ok
        FROM (SELECT count_if(status = 'pending_review' AND readability IS NULL) AS reading,
                     count_if(status = 'pending_review' AND readability IS NOT NULL) AS waiting,
                     count_if(status = 'flagged') AS flagged, count_if(status = 'approved') AS approved
              FROM {p.t('manifest')}) d
        CROSS JOIN (SELECT count(*) AS questions, count_if(approved) AS questions_ok
                    FROM {p.t('golden_set')} WHERE active) q"""


def progress_sql(paths: list[BotPaths]) -> tuple[str, dict[str, str]]:
    """One statement that counts documents and test questions for every chatbot in `paths`
    (those whose setup has finished). One row per chatbot: bot_id, reading, waiting, flagged,
    approved, questions, questions_ok."""
    return union_by_key({p.bot_id: progress_select(p) for p in paths})


def counts(row: dict | None) -> tuple[dict, dict]:
    """A progress_sql row as the (docs, questions) pair that steps() takes."""
    row = row or {}
    return ({k: int(row.get(k) or 0) for k in ("reading", "waiting", "flagged", "approved")},
            {"total": int(row.get("questions") or 0), "approved": int(row.get("questions_ok") or 0)})
