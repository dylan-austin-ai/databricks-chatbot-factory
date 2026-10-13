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


def brief(error: BaseException, limit: int = 200) -> str:
    """An error as one short line, for an alert or a log: its type and the first line of its
    message. A database error can run to pages (the whole statement, a stack trace), and none of
    that belongs in an alert that is emailed and shown on a page."""
    first = next((line.strip() for line in str(error).splitlines() if line.strip()), "")
    text = f"{type(error).__name__}: {first}" if first else type(error).__name__
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def one_line(text: str | None, limit: int = 240) -> str:
    """Stored text squeezed onto one line and cut to a readable length."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


ALERT_LABEL: dict[str, str] = {
    "availability": "Not reachable", "latency": "Slow answers", "error_rate": "Failing questions",
    "harmful_output": "Answer flagged", "traffic_low": "Quieter than usual", "traffic_high": "Busier than usual",
    "idle": "Not being used", "budget": "Budget", "expiry": "Document expiring", "question_drift": "New topics",
    "drift": "Changed outside the app", "quality_drop": "Quality dropped", "guardrail_spike": "Safety checks busy",
    "eval_regression": "Quality check failed", "provision_failed": "Setup stopped",
    "smoke_failed": "New version not published", "alert_setup_failed": "Email alerts not set up",
    "integrity": "Search index not updated", "runtime_publish_failed": "Settings not delivered",
}

# Kinds whose stored message is an error written by a machine. People are shown this sentence
# instead; the stored message stays on the Admin page for whoever fixes it.
ALERT_PLAIN: dict[str, str] = {
    "provision_failed": "Setting up this chatbot stopped part-way.",
    "alert_setup_failed": "Email alerts for this chatbot couldn't be set up automatically.",
    "runtime_publish_failed": "This chatbot's latest settings couldn't be delivered to the answering service, "
                              "so it is still using the previous ones.",
    "integrity": "A check on this chatbot's sections failed, so its search index was not updated.",
    "availability": "The chatbot couldn't be reached, or it failed a routine test question.",
}

# Kinds the chatbot's owner can do something about. Everything else is for MLOps.
OWNER_ACTS = frozenset({"harmful_output", "traffic_low", "idle", "expiry", "question_drift", "quality_drop",
                        "guardrail_spike", "eval_regression", "smoke_failed", "provision_failed"})

# What MLOps should do about the kinds that are theirs.
ALERT_MLOPS: dict[str, str] = {
    "availability": "Check the agent's serving endpoint (state and recent events). An endpoint waking from "
                    "zero clears on its own; a failed test question has its reason in the recorded message.",
    "latency": "Check the load on the serving endpoint and the rate limits on the answer model service.",
    "error_rate": "Open **All chatbots**, filter to this chatbot and open the failing questions' traces.",
    "traffic_high": "Check that it is expected (a launch, a campaign) and keep an eye on cost.",
    "budget": "Raise the budget under **Chatbots** if the spending is expected. At 100% the chatbot pauses if "
              "it is set to.",
    "drift": "Someone changed this chatbot's tables outside the app. The recorded message names the table and "
             "who: revoke the grant or drop the table.",
    "provision_failed": "Open **Launch** for this chatbot: the failed step is marked there, and **Try again** "
                        "re-runs it. The recorded message says why it failed.",
    "alert_setup_failed": "The email alerts for this chatbot don't exist yet. Press **Try setting them up "
                          "again**. If it fails again the recorded message says why (usually the jobs' "
                          "principal may not create SQL alerts).",
    "integrity": "Duplicate or empty sections were found. Re-read the affected documents (Documents, **Fix a "
                 "problem**) and the next update of the test version will try again.",
    "runtime_publish_failed": "Check that the app's service principal can write to the runtime volume, then "
                              "save any setting for this chatbot again.",
}


def alert_text(kind: str, message: str | None) -> str:
    """What to show for an alert: a plain sentence where the stored message is machine text,
    otherwise the stored message on one line."""
    return ALERT_PLAIN.get(kind) or one_line(message)


def mlops_advice(kind: str) -> str:
    return ALERT_MLOPS.get(kind) or alert_advice(kind)


def group_alerts(alerts: list[dict]) -> list[dict]:
    """Alerts (newest first) with repeats folded together: one entry per chatbot and kind, with
    how many times it was raised and everything a page needs to show it."""
    groups: dict[tuple, dict] = {}
    for a in alerts:
        key = (a.get("bot_id"), a.get("kind"))
        if key not in groups:
            kind = a.get("kind") or ""
            groups[key] = {"bot_id": a.get("bot_id"), "kind": kind, "count": 0, "latest": a,
                           "severity": a.get("severity"),
                           "label": ALERT_LABEL.get(kind, kind.replace("_", " ").capitalize()),
                           "text": alert_text(kind, a.get("message")), "for_owner": kind in OWNER_ACTS,
                           "advice": alert_advice(kind), "mlops_advice": mlops_advice(kind)}
        groups[key]["count"] += 1
    return list(groups.values())


SETUP, TEMPORARY, UNCLEAR, STOPPED_BY_HAND = "setup", "temporary", "unclear", "stopped"
_JOB_SETUP = (r"did not answer|environment variable|is required|not set\b|not configured|No module named|ModuleNotFoundError|"
              r"ImportError|PERMISSION_DENIED|does not have .*(privilege|permission)|not authorized|Forbidden|"
              r"RESOURCE_DOES_NOT_EXIST|NOT_FOUND|does not exist|cannot be found|AttributeError|TypeError|NameError|"
              r"KeyError|unexpected keyword|SyntaxError|INVALID_PARAMETER_VALUE")
_JOB_TEMPORARY = (r"timed out|TimeoutError|TIMEDOUT|TEMPORARILY_UNAVAILABLE|RESOURCE_EXHAUSTED|\b429\b|\b503\b|"
                  r"CLOUD_FAILURE|Concurrent|skipped this run")


def job_stopped(message: str | None) -> tuple[str, str]:
    """Why a background job stopped, as far as its error tells, and what to do: a fault in how
    the job is set up (starting it again changes nothing), something temporary, or unclear."""
    text = message or ""
    if re.search(r"CANCEL", text, re.I):
        return STOPPED_BY_HAND, "Someone stopped it. Start it again with the button here when you are ready."
    if re.search(_JOB_SETUP, text, re.I):
        return SETUP, ("This is a fault in how the job is set up, not something you did, and starting it again "
                       "won't help. Send MLOps the message below.")
    if re.search(_JOB_TEMPORARY, text, re.I):
        return TEMPORARY, "This looks temporary. Start it again with the button here."
    return UNCLEAR, ("Start it again with the button here. If it stops a second time, send MLOps the message "
                     "below.")
