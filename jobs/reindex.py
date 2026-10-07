"""Platform parser/chunker change: re-index only the affected docs (CAS-7).

--parser ai_parse_document | text_reader | all   --extensions md,txt (optional)
Runs immediately across all bots, but touches only docs the change affects.
"""
from _bootstrap import args, context, vector_client

from factory.ingestion import BotPaths
from factory.pipeline import Pipeline

a = args("catalog", "parser", "extensions")
spark, sql, settings, cp, w = context(a.catalog)
pipe = Pipeline(sql, settings, cp, w, vector_client())
exts = {e.strip().lower() for e in a.extensions.split(",") if e.strip()}

for bot in cp.list_bots():
    if bot["state"] in ("draft", "deleted"):
        continue
    cfg = cp.get_config(bot["bot_id"])
    p = BotPaths(settings, cfg.bot_id)
    rows = sql.query(f"SELECT doc_id, parser, file_ext FROM {p.t('manifest')} "
                     "WHERE status NOT IN ('archived', 'superseded')")
    affected = [r["doc_id"] for r in rows
                if (a.parser in ("", "all") or r["parser"] == a.parser)
                and (not exts or (r["file_ext"] or "").lower() in exts)]
    if affected:
        print(f"{cfg.bot_id}: re-indexing {len(affected)} doc(s)")
        pipe.run(cfg, doc_ids=affected, reparse=True, generate_golden=False)
        cp.audit("system:reindex", cfg.bot_id, "reindexed", a.parser or "all",
                 {"docs": len(affected)})
