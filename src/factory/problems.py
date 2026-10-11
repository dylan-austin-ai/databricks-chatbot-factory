"""Errors in plain words (UX-4).

The people using the app are not engineers. When something fails they need three things: what
happened in a sentence, what to do now, and whether it is theirs to fix. The technical text is
still kept, folded away, for whoever they send it to.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

YOU, WAIT, MLOPS = "you", "wait", "mlops"


@dataclass(frozen=True)
class Problem:
    what: str     # what happened
    do: str       # what to do now
    who: str      # you (try again) | wait (it fixes itself) | mlops (needs someone with access)


_SEND = ("Open **Show technical details** below, copy the text and send it to MLOps with a note of what you "
         "were doing.")

# (pattern over "<ErrorType>: <message>", Problem). First match wins, so the specific ones are first.
_RULES: list[tuple[str, Problem]] = [
    (r"TABLE_OR_VIEW_NOT_FOUND|TABLE_DOES_NOT_EXIST|SCHEMA_NOT_FOUND|NO_SUCH_CATALOG|cannot be found|"
     r"does not exist|RESOURCE_DOES_NOT_EXIST",
     Problem("Part of the data this page needs hasn't been created yet.",
             "If the chatbot was created in the last 15 minutes, its setup is probably still running: wait a few "
             "minutes and press **Try again**. If it still fails, a setup step needs to be run by MLOps. " + _SEND,
             MLOPS)),
    (r"PermissionError|PERMISSION_DENIED|INSUFFICIENT_PERMISSIONS|INSUFFICIENT_PRIVILEGES|does not have .*"
     r"(privilege|permission)|not authorized|Forbidden|\b403\b",
     Problem("The app isn't allowed to read or change this.",
             "This isn't something you did wrong, and trying again won't help. " + _SEND, MLOPS)),
    (r"Concurrent|CONCURRENT|DELTA_CONCURRENT",
     Problem("Someone else, or a background job, changed the same data at the same moment.",
             "Nothing was lost. Press **Try again**.", YOU)),
    (r"TimeoutError|timed out|TIMEOUT|DEADLINE_EXCEEDED",
     Problem("That took too long and was stopped.",
             "Wait a minute and press **Try again**. The first action after a quiet period is slower while the "
             "database starts up.", WAIT)),
    (r"TEMPORARILY_UNAVAILABLE|RESOURCE_EXHAUSTED|REQUEST_LIMIT_EXCEEDED|Too Many Requests|\b429\b|\b503\b|"
     r"warehouse.*(start|stopp)|is starting",
     Problem("Databricks is busy or still starting up.",
             "Wait a minute and press **Try again**. Nothing was lost.", WAIT)),
    (r"ConnectionError|ConnectTimeout|Connection (reset|refused|aborted)|Max retries|Name or service not known",
     Problem("The app couldn't reach Databricks.",
             "Wait a minute and press **Try again**. If other pages fail too, tell MLOps the app is offline.", WAIT)),
    (r"job isn't configured|JOB_ID|INVALID_PARAMETER_VALUE.*job",
     Problem("A background job this button starts isn't connected to the app.",
             "You can't fix this from here. " + _SEND, MLOPS)),
]
_UNKNOWN = Problem("Something unexpected went wrong.",
                   "Press **Try again**. If it happens a second time: " + _SEND[0].lower() + _SEND[1:], YOU)


def chat_apology(error: BaseException) -> str:
    """What to say in the chat when a question couldn't be answered. The person asking may not
    own the chatbot, so this never mentions MLOps or technical details."""
    text = f"{type(error).__name__}: {error}"
    if re.search(_RULES[1][0], text, re.I | re.S):
        return ("Sorry, you don't have access to this chatbot. Ask its owner to add you, then try again.")
    if describe(error).who == WAIT:
        return ("Sorry, the chatbot is busy or still waking up, which can take a few minutes after a quiet "
                "period. Please ask again in a minute or two.")
    return ("Sorry, something went wrong and I couldn't answer. Please ask again. If it keeps happening, tell the "
            "chatbot's owner.")


def describe(error: BaseException) -> Problem:
    """The plain-language version of an error."""
    text = f"{type(error).__name__}: {error}"
    for pattern, problem in _RULES:
        if re.search(pattern, text, re.I | re.S):
            return problem
    return _UNKNOWN


_NOTHING = "MLOps is told automatically. There is nothing for you to do."

# What an owner should do about each kind of alert the monitor raises.
ALERT_ADVICE: dict[str, str] = {
    "availability": "The chatbot couldn't be reached, so people may see \"not available right now\". " + _NOTHING,
    "latency": "Answers are slower than the target. This is usually temporary. If it lasts more than a day, "
               "tell MLOps.",
    "error_rate": "More questions than usual are failing. " + _NOTHING + " **Monitoring** shows when it started.",
    "harmful_output": "An answer was flagged as possibly harmful. Open **Monitoring**, choose **Guardrails** to see "
                      "it, and tell your Reviewer.",
    "traffic_low": "Fewer people are asking questions than usual. Nothing is broken. Remind people the chatbot "
                   "exists, or archive it if it's no longer needed.",
    "idle": "Nobody has asked this chatbot anything for a while. Nothing is broken. Archive it on **Launch** if "
            "it's no longer needed.",
    "traffic_high": "Many more questions than usual. Nothing to fix; this is for your information.",
    "budget": "Spending is close to or over this month's budget. " + _NOTHING,
    "expiry": "A document is about to expire and will stop being used in answers. Open **Documents**, choose "
              "**Add documents** and upload its new version, or change its expiry date under **Review a "
              "document**.",
    "question_drift": "People are asking about different things than at launch. Open **Monitoring**, choose "
                      "**Question drift** to see the new topics, and add documents that cover them.",
    "drift": "The mix of questions or answers changed noticeably. Open **Monitoring** and choose **Retrieval** to "
             "see what moved.",
    "quality_drop": "Fewer answers are passing the quality checks. Open **Monitoring** and choose **Quality** to "
                    "see which check is failing. The usual cause is a document that is out of date or missing.",
    "guardrail_spike": "Safety checks are stepping in more often than usual. Open **Monitoring** and choose "
                       "**Guardrails** to see which rule and which questions.",
    "eval_regression": "The regular quality check of the live version didn't pass. Open **Launch** to see which "
                       "results are below target.",
    "provision_failed": "Setup stopped part-way. Open **Launch** and press **Try again**.",
    "smoke_failed": "The new version failed its final test while publishing, so the previous version stayed "
                    "live. Open **Launch**, run the quality check again and send it for approval again.",
}


def alert_advice(kind: str) -> str:
    """What to do about an alert of this kind; platform faults are MLOps' to fix."""
    return ALERT_ADVICE.get(kind, "This is a platform problem. " + _NOTHING)


_FIND = ("The chatbot isn't finding the right part of your documents often enough. The usual causes: a test "
         "question whose answer isn't in any approved document (fix or remove it on **Test questions**), or a "
         "document that was read badly (check it on **Documents**). Then run the check again.")
# (pattern over one failure line from the quality check, what to do about it). First match wins.
_CHECK_ADVICE: list[tuple[str, str]] = [
    (r"^Only \d+ approved test question", "Open **Test questions**, approve more questions, then run the check "
     "again."),
    (r"wasn't scored|weren't scored", "This is usually a temporary hiccup, not a problem with your chatbot. Press "
     "**Run the quality check again**. If it happens twice, tell MLOps."),
    (r"citation mapping failure", "An answer pointed at a source that doesn't contain what it said. This must "
     "never happen, so the version can't go live. Run the check again; if it repeats, tell MLOps."),
    (r"leakage failure|sensitive output failure", "An answer showed something it must never show. The version "
     "can't go live. Tell your Reviewer and MLOps; don't just run it again."),
    (r"possibly unsafe", "Your Reviewer opens this page, reads the flagged answers under **Review answers "
     "flagged as possibly unsafe** and decides whether they are acceptable."),
    (r"^(hit at \d|mrr) is", _FIND),
    (r"^correctness is", "Answers don't match the expected answers closely enough. Check on **Test questions** "
     "that the expected answers are right, and on **Documents** that the documents containing them are "
     "approved. Then run the check again."),
    (r"^groundedness is", "Some answers say more than the documents do. The usual cause is a missing document: "
     "add the one that covers the topic on **Documents**, approve it and run the check again."),
    (r"^relevance is", "Some answers don't address what was asked. Check the wording of the questions on **Test "
     "questions**: vague questions get vague answers. Then run the check again."),
    (r"^citation accuracy is", "Some answers cite a page that doesn't back them up. " + _FIND),
    (r"^refusal accuracy is", "The chatbot answered questions it should have declined, or declined ones it "
     "should have answered. On **Test questions**, check which questions are marked **Should refuse**. To add "
     "topics it must refuse, ask MLOps."),
]


def check_advice(failure: str) -> str:
    """What the owner should do about one line of a failed quality check."""
    for pattern, advice in _CHECK_ADVICE:
        if re.search(pattern, failure or "", re.I):
            return advice
    return "Run the quality check again. If the same line comes back, send it to MLOps."
