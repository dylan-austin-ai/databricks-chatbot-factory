"""Incremental ingestion for one bot (DOC-12): new uploads, re-parses (QA-11),
and re-publishing after approvals or archives (DOC-8)."""
from _bootstrap import args, context, vector_client

from factory.pipeline import Pipeline

a = args("catalog", "bot_id", "doc_ids", "reparse", "publish_only", "generate_golden", "rechunk_only",
         "golden_only")
spark, sql, settings, cp, w = context(a.catalog)
cfg = cp.get_config(a.bot_id)
pipe = Pipeline(sql, settings, cp, w, vector_client())

if a.publish_only == "true":
    pipe.publish(cfg)
elif a.golden_only == "true":
    from factory.ingestion import BotPaths
    p = BotPaths(settings, cfg.bot_id)
    ids = [r["doc_id"] for r in sql.query(
        f"SELECT doc_id FROM {p.t('manifest')} WHERE status = 'approved' AND is_active")]
    if ids:
        pipe.generate_golden(cfg, p, ids)
else:
    doc_ids = [d for d in a.doc_ids.split(",") if d] or None
    gen = {"true": True, "false": False}.get(a.generate_golden)
    print(pipe.run(cfg, doc_ids=doc_ids, reparse=a.reparse == "true" or a.rechunk_only == "true",
                   generate_golden=gen, rechunk_only=a.rechunk_only == "true"))
