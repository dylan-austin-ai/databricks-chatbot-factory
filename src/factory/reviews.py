"""MLflow Review Queues (REV-1): one queue per chatbot, assigned to its owner and Reviewer.
Used by production judging (failed answers) and the app (document-gap questions, OBS-25)."""
from __future__ import annotations


def queue_traces(cfg, trace_ids: list[str]) -> int:
    """Add traces to the chatbot's review queue (created on first use). Needs the traces
    experiment set as the active MLflow experiment. Returns how many were queued."""
    from mlflow.genai.label_schemas import InputPassFail, create_label_schema
    from mlflow.genai.review_queues import add_items_to_review_queue, create_review_queue, list_review_queues
    ids = sorted({t for t in trace_ids if t})
    if not ids:
        return 0
    name = f"review_{cfg.bot_id}"
    queue = next((q for q in list_review_queues() if q.name == name), None)
    if queue is None:
        schema = create_label_schema(
            name="answer_correct", type="feedback", enable_comment=True, overwrite=True,
            input=InputPassFail(positive_label="Correct and sourced", negative_label="Wrong or unsupported"),
            instruction="Check the answer against the cited excerpts and the question.")
        queue = create_review_queue(
            name=name, queue_type="custom", schema_ids=[getattr(schema, "schema_id", None) or schema.name],
            users=sorted({u for u in (cfg.owner_user, cfg.reviewer) if "@" in (u or "")}))
    add_items_to_review_queue(queue.queue_id, item_ids=ids)
    return len(ids)
