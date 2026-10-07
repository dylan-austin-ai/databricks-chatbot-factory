"""Triggered by the app when the creator hits Go (PRV-1..7).

Provision (idempotent) -> ingest -> run first eval -> move bot to 'testing'.
Progress is written to provisioning_steps, which the app shows in plain English.
"""
from _bootstrap import args, context, uc_secret, vector_client

from factory.lifecycle import State
from factory.pipeline import Pipeline
from factory.provisioning import Provisioner

a = args("catalog", "bot_id", "actor", "agent_principal", "ingest_job_id")
spark, sql, settings, cp, w = context(a.catalog)
cfg = cp.get_config(a.bot_id)
vsc = vector_client()

cp.set_state(cfg.bot_id, State.PROVISIONING.value, a.actor or "system:job")
try:
    Provisioner(sql, settings, cp, w, vsc, agent_principal=a.agent_principal or None,
                ingest_job_id=a.ingest_job_id or None).run(
        cfg, a.actor or "system:job", progress=print)
    pipe = Pipeline(sql, settings, cp, w, vsc)
    if cfg.source_type == "git":
        from factory.documents import Documents
        from factory.gitsync import sync_repo
        token = uc_secret(settings, settings.get("git.secret", ""))
        sync_repo(cfg, settings, sql, Documents(sql, settings, cp, w), token)
    pipe.run(cfg)
    cp.set_state(cfg.bot_id, State.TESTING.value, a.actor or "system:job")
except Exception as e:
    cp.set_state(cfg.bot_id, State.PROVISION_FAILED.value, a.actor or "system:job", {"error": repr(e)})
    cp.alert(cfg.bot_id, "provision_failed", "high", f"Provisioning failed: {e}")
    raise
