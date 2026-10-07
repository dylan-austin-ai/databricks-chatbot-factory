"""Auto-generated documentation per bot (CON-2, CON-5)."""
from __future__ import annotations

from .config import BotConfig, PlatformSettings
from .guardrails import platform_rules


def model_card(cfg: BotConfig, settings: PlatformSettings, state: str, docs: list[dict],
               eval_run: dict | None, version: int, releases: list[dict] | None = None) -> str:
    metrics = (eval_run or {}).get("metrics", {})
    metrics = {k: v for k, v in metrics.items() if k in settings.thresholds()}
    active = [d for d in docs if d["status"] == "approved"]
    lines = [
        f"# {cfg.display_name}",
        "",
        f"**Purpose:** {cfg.purpose}",
        "",
        "| | |", "|---|---|",
        f"| Status | {state} |",
        f"| Config version | {version} |",
        f"| Owner | {cfg.owner_user} ({cfg.owner_group}) |",
        f"| Reviewer | {cfg.reviewer} |",
        f"| Access | {'Through trusted apps (middleware)' if cfg.access_mode == 'middleware' else 'Restricted: ' + ', '.join(cfg.allowed_principals)} |",
        f"| Conversation history | {cfg.history_mode} |",
        f"| Identity in logs | {cfg.identity_mode} |",
        f"| Retrieval | {cfg.retrieval.get('query_type')}, rerank {'on' if cfg.retrieval.get('rerank') else 'off'} |",
        f"| Monthly budget | ${cfg.budget_usd:,.0f} ({'pause' if cfg.budget_action == 'pause' else 'alert only'}) |",
        f"| Document sensitivity | {cfg.sensitivity.title()} |",
        f"| Answer model | {cfg.answer_model} via guardrailed Unity Gateway model service `{settings.answer_service(cfg.answer_model)}` |",
        f"| Channels | {', '.join(cfg.channels)} |",
        f"| Technical name | `{settings.catalog}.{cfg.bot_id}` |",
        "",
        "## Platform rules (set by MLOps)",
        *[f"- {r}" for r in platform_rules(settings.get("guardrails.platform_rules", {}), cfg.disabled_rules)],
        *([f"- Turned off by an admin for this chatbot: {', '.join(cfg.disabled_rules)}"] if cfg.disabled_rules else []),
        "",
        "## Extra topics the owner added",
        ", ".join(cfg.refuse_topics) or "None.",
        "",
        "## Guardrails",
        "Always on: safety filter, PII masking, jailbreak and prompt-injection blocking (gateway and "
        "in retrieved documents), rate limits, exact-excerpt verification, conflict disclosure, "
        "abstention when nothing relevant is found, no invented links, instruction-leak prevention.",
        "",
        f"Quote length limit: {'on' if cfg.limit_quotes or cfg.access_mode == 'middleware' else 'off'}",
        "",
        f"## Documents ({len(active)} approved)",
        *[f"- {d['doc_name']} (v{d['doc_version']}, {d['readability'] or 'n/a'})" for d in active],
        "",
        "## Latest evaluation",
    ]
    if metrics:
        lines += ["| Metric | Score | Required |", "|---|---|---|"]
        for k, v in sorted(metrics.items()):
            t = settings.thresholds().get(k)
            lines.append(f"| {k.replace('_', ' ')} | {v:.0%} | {f'{t:.0%}' if t else '-'} |")
    else:
        lines.append("Not evaluated yet.")
    if releases:  # REL-8
        lines += ["", "## Release history", "| Release | Status | Approved by | Published | Eval | "
                  "Config hash | Manifest hash | Commit |", "|---|---|---|---|---|---|---|---|"]
        for r in releases:
            lines.append(f"| {r['release_id']} | {r['status']} | {r.get('approved_by') or ''} | "
                         f"{r.get('promoted_at') or ''} | {r.get('eval_passed')} | "
                         f"{(r.get('config_hash') or '')[:12]} | {(r.get('manifest_hash') or '')[:12]} | "
                         f"{(r.get('git_commit') or '')[:10]} |")
    lines += ["", "## How answers work",
              "Every answer cites the source document and page, with exact quotes. If the documents "
              f"don't cover a question, the chatbot says so and points to {cfg.owner_user}."]
    return "\n".join(lines)
