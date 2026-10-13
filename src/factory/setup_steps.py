"""The setup steps that are finished outside the app, and how far along each one is (ADM-9).

Some of the platform's setup happens in Databricks screens. The app can check a few of those
steps for itself (a table name has been recorded and the table exists); the rest it cannot see at
all. The two must never look the same: an empty box next to something the app simply can't check
reads as "you haven't done this". So each step is one of three things: done, to do (with what to
do), or for a person to confirm.
"""
from __future__ import annotations

from dataclasses import dataclass

DONE, TODO, CONFIRM = "done", "todo", "confirm"


@dataclass(frozen=True)
class SetupStep:
    key: str
    title: str
    state: str            # done | todo | confirm (set in Databricks where the app can't look)
    detail: str           # done: what was found. todo: what to do. confirm: where it is set
    why: str              # what depends on it
    guide: str            # the section of docs/SETUP.md with the clicks
    by_hand: bool = False  # True when only a person can say it is done
    optional: bool = False


_RECORD = ("Copy the table's full name (catalog.schema.table), paste it in the **Platform** section here under "
           "**{field}**, press **Save settings**, then run the observability task once (SETUP C5, step 3).")


def _table_step(settings, key: str, title: str, field: str, turn_on: str, why: str, guide: str,
                existing: set[str] | None, must_be_home: bool) -> SetupStep:
    """A step that is done when a table's name is recorded in the settings and the table is there."""
    name = (settings.get(f"unity_gateway.{key}") or "").strip()
    home = f"{settings.catalog}.{settings.platform_schema}."
    if not name:
        return SetupStep(key, title, TODO, f"{turn_on} {_RECORD.format(field=field)}", why, guide)
    if must_be_home and not name.lower().startswith(home.lower()):
        return SetupStep(key, title, TODO, f"The name recorded, `{name}`, is not in this environment's `{home}`. "
                         f"Record the table that belongs to this environment under **Platform** › **{field}**.",
                         why, guide)
    if existing is not None and name.lower().startswith(home.lower()) and name.lower() not in existing:
        return SetupStep(key, title, TODO, f"The app can't find a table called `{name}`. Check the spelling against "
                         f"the name Databricks shows, and correct it under **Platform** › **{field}**. {turn_on}",
                         why, guide)
    return SetupStep(key, title, DONE, f"Recorded as `{name}`.", why, guide)


def setup_steps(settings, existing: set[str] | None = None, usage_policy: bool | None = None,
                confirmed: dict | None = None) -> list[SetupStep]:
    """Every outside-the-app setup step with its state. `existing`: lower-case full names of the
    recorded tables that exist in this environment's platform schema (None when that couldn't be
    checked). `usage_policy`: whether the factory's jobs carry a usage policy (None when that
    couldn't be read). `confirmed`: what people have ticked, {key: {"by": ..., "on": ...}}."""
    confirmed = confirmed or {}
    # Name the catalog and schema outright: "this environment's catalog" leaves people asking which.
    where = f"catalog `{settings.catalog}`, schema `{settings.platform_schema}`"
    steps = [
        _table_step(settings, "inference_table", "The answer service logs its calls", "Answer service inference table",
                    "In Databricks, open **AI Gateway**, open the answer model service and turn on its inference "
                    f"table in {where}.",
                    "Without it the Guardrails charts and the cost comparison can't show what the gateway saw.",
                    "C2 and C5", existing, must_be_home=True),
        _table_step(settings, "evaluator_inference_table", "The guardrail evaluator logs its verdicts",
                    "Evaluator inference table",
                    "In Databricks, open **AI Gateway**, open the guardrail evaluator service and turn on its "
                    f"inference table in {where}.",
                    "Without it the hallucination check's verdicts aren't recorded anywhere you can see.",
                    "C3 and C5", existing, must_be_home=True),
        _table_step(settings, "trace_table", "Unity Gateway traces are stored", "Unified trace table",
                    "In this workspace, a metastore admin opens **AI Gateway** › **Govern** › **Traces** › **Set up "
                    f"tracing** and chooses {where}. If tracing is already set up there, use the table it shows. "
                    "Databricks creates the table only when the gateway handles its first call after that, so if "
                    "no table is shown yet, you have done your part: come back after a chatbot has answered a "
                    "question, and the table will be listed there and in Catalog Explorer under that schema.",
                    "Without it MLOps and Security can't read the gateway's own traces. Nothing in the app depends "
                    "on it, so it can wait.", "C4 and C5", existing,
                    must_be_home=False),
        SetupStep("genie", "The Genie space over the monitoring tables exists",
                  DONE if settings.get("observability.genie_space_id") else TODO,
                  "Created." if settings.get("observability.genie_space_id") else
                  "Run the observability task (SETUP C5, step 3); it creates the space. Then share it (SETUP C7).",
                  "It answers plain-language questions about usage and quality.", "B3 and C7"),
    ]
    serving = (settings.get("access.serving_identity") or "").strip()
    steps.append(SetupStep(
        "serving_identity", "The agent's endpoint signs in as the agent service principal", DONE if serving else TODO,
        f"Signs in as `{serving}`, recorded by the deploy, which also grants it the trace tables." if serving
        else "Run the `deploy_agent` job. It makes the endpoint sign in as the dedicated agent service principal "
             "(the one created in SETUP A6), records that here and grants it the trace tables. Nothing has to be "
             "looked up or pasted.",
        "Without it the endpoint runs as an identity nobody chose and nothing was granted to, so the chatbot "
        "can't read its settings or store its traces.", "A6 and B3"))
    policy_why = "Without it the factory's cost can't be told apart from other spending, or QA from prod."
    if usage_policy is None:   # couldn't read the job: a person has to say
        steps.append(SetupStep("usage_policy", "The jobs carry a serverless usage policy", CONFIRM,
                               "Set as the `usage_policy_id` bundle variable when deploying.", policy_why, "A9 and A11",
                               by_hand=True))
    else:
        steps.append(SetupStep("usage_policy", "The jobs carry a serverless usage policy",
                               DONE if usage_policy else TODO,
                               "The ingest job has one." if usage_policy else
                               "Create the policy (SETUP A9), put its ID in this environment's "
                               "`variable-overrides.json` as `usage_policy_id` (SETUP A11) and deploy the bundle again.",
                               policy_why, "A9 and A11"))
    steps += [
        SetupStep("user_authorisation", "The agent is given the asking user's credentials", CONFIRM,
                  "Your user name › **Settings** › **Previews** › **Agent Framework: On-Behalf-Of-User "
                  "Authorization**, switched on by a workspace admin. Then run the `deploy_agent` job once more.",
                  "Without it the chatbot can't tell who is asking and answers nobody: people see \"I can't confirm "
                  "who is asking\" and quality checks stop with \"nothing to score\".", "A2", by_hand=True),
        SetupStep("review_queues", "The MLflow Review Queues preview is on", CONFIRM,
                  "Your user name › **Settings** › **Previews**, by a workspace admin.",
                  "Without it failed and thumbs-down answers aren't queued for a person to review.", "A2", by_hand=True),
        SetupStep("embed_dashboards", "Dashboards may be embedded in *.databricksapps.com", CONFIRM,
                  "Your user name › **Settings** › **Security** › **External access** › **Embed dashboards**, by a "
                  "workspace admin.", "Without it the drift dashboard on the Monitoring page shows as a blank box.",
                  "C6", by_hand=True),
        SetupStep("account_budget", "The account budget for the factory exists", CONFIRM,
                  "Account console › **Usage** › **Budgets**, filtered on the tag `component` = `chatbot-factory`, by "
                  "an account admin.", "It emails MLOps at 70%, 90% and 100% of the monthly cap.", "A10", by_hand=True),
        SetupStep("app_scaling", "The app runs on more than one instance", CONFIRM,
                  "**Compute** › **Apps** › the app › **Edit** › **Configure**.",
                  "Only needed for prod, or if the app feels slow with many people using it.", "C8", by_hand=True,
                  optional=True),
    ]
    out = []
    for step in steps:
        tick = confirmed.get(step.key) if step.by_hand else None
        if tick:
            step = SetupStep(step.key, step.title, DONE, f"Confirmed by {tick.get('by', 'someone')} on "
                             f"{tick.get('on', 'an earlier date')}. Set in: {step.detail}", step.why, step.guide,
                             by_hand=True, optional=step.optional)
        out.append(step)
    return out


def summary(steps: list[SetupStep]) -> str:
    """One line saying where setup stands, leaving optional steps out of the count."""
    needed = [s for s in steps if not s.optional]
    done, todo = sum(s.state == DONE for s in needed), sum(s.state == TODO for s in needed)
    waiting = sum(s.state == CONFIRM for s in needed)
    parts = [f"{done} of {len(needed)} done"]
    if todo:
        parts.append(f"{todo} still to do")
    if waiting:
        parts.append(f"{waiting} for you to confirm")
    return ", ".join(parts) + "."
