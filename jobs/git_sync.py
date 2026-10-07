"""Scheduled sync of every live-repo bot (ING-6). --bot_id limits it to one bot."""
from _bootstrap import args, context, uc_secret, vector_client

from factory.documents import Documents
from factory.gitsync import ACTOR, sync_repo
from factory.pipeline import Pipeline

a = args("catalog", "bot_id")
spark, sql, settings, cp, w = context(a.catalog)
token = uc_secret(settings, settings.get("git.secret", ""))
docs = Documents(sql, settings, cp, w)
pipe = Pipeline(sql, settings, cp, w, vector_client())

bots = [cp.get_bot(a.bot_id)] if a.bot_id else cp.list_bots()
for bot in bots:
    if bot["state"] in ("draft", "deleted", "archived", "purged"):
        continue
    cfg = cp.get_config(bot["bot_id"])
    if cfg.source_type != "git":
        continue
    head, changed = sync_repo(cfg, settings, sql, docs, token)
    if changed and not settings.get("ingestion.auto_trigger", True):
        pipe.run(cfg, doc_ids=changed)  # otherwise the bot's file-arrival trigger ingests them
    elif not changed:
        pipe.publish(cfg)  # applies any archives from deleted files
    cp.audit(ACTOR, cfg.bot_id, "git_synced", head, {"changed": len(changed)})
    print(cfg.bot_id, head, f"{len(changed)} changed")
