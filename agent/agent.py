"""The single shared agent behind every chatbot (ARC-1, ARC-2).

Per request:
 1. Load the bot's runtime config from the runtime volume (no SQL warehouse; ARC-3).
 2. Resolve the end user: on-behalf-of caller, or an identity asserted by a trusted
    middleware service principal (IDN-1). Check access with Unity Catalog (GOV-3/4).
 3. Pick the channel: live, or candidate for the owner/Reviewer/testers (REL-1).
 4. Retrieve with the bot's chosen strategy, filtered to its channel and to documents
    inside their effective/expiry window (DCL-1); rerank (RET-1). Strip prompt-injection
    text from retrieved chunks (ANS-5).
 5. Ask Haiku (guardrailed AI Gateway endpoint, ARC-8) for a JSON answer: answered with
    citations and exact excerpts, no_source, or out_of_scope (ANS-1, ANS-2, ANS-4).
 6. Verify citations, excerpts, quotes and links; regenerate once on failure; redact
    restricted numbers (GRD-7).
 7. Return a structured response with release and trace metadata (ANS-3) and write a
    full log record (request, guardrail events, debug payload) to the restricted logs
    volume (PRV-1..3). The monitor job loads it into tables.
"""
from __future__ import annotations

import contextvars
import io
import json
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Generator

import mlflow
from mlflow.pyfunc import ResponsesAgent
from mlflow.types.responses import (ResponsesAgentRequest, ResponsesAgentResponse,
                                    ResponsesAgentStreamEvent)

_HERE = Path(__file__).resolve()
for candidate in (_HERE.parents[1] / "src", _HERE.parent / "code"):
    if candidate.exists():
        sys.path.insert(0, str(candidate))

from factory import PLATFORM_VERSION  # noqa: E402
from factory import gateway as gw  # noqa: E402
from factory import guardrails as g  # noqa: E402
from factory import identity as idn  # noqa: E402
from factory import sensitive  # noqa: E402
from factory import tracing as tr  # noqa: E402
from factory.answering import (SYSTEM_PROMPT, bounded_history, fill_system,  # noqa: E402
                               format_sources, render, search_filters, search_query)
from factory.config import DEFAULT_RETRIEVAL, EVERYONE, BotConfig, PlatformSettings  # noqa: E402
from factory.memory import ManagedSessions, merge_history  # noqa: E402
from factory.provisioning import index_name_for  # noqa: E402
from factory.runtime import logs_dir, runtime_dir  # noqa: E402
from factory.sql import WarehouseRunner, ident  # noqa: E402

CATALOG = os.environ.get("FACTORY_CATALOG", "chatbots")
WAREHOUSE_ID = os.environ.get("FACTORY_WAREHOUSE_ID", "")
ENVIRONMENT = os.environ.get("FACTORY_ENV", "prod")
HASH_KEY = os.environ.get("FACTORY_IDENTITY_HASH_KEY", "").encode()        # secret env var
ENC_KEY = os.environ.get("FACTORY_IDENTITY_ENCRYPTION_KEY", "").encode()   # secret env var
PROMPT_NAME = os.environ.get("FACTORY_PROMPT_NAME", "")  # MLflow Prompt Registry name (PRM-1)
MASK_TRACES = os.environ.get("FACTORY_MASK_TRACES", "true") == "true"

# Native model-call spans with token usage for every OpenAI-client call (OBS-7).
mlflow.openai.autolog()


MSG = {
    "refused": "Sorry, that's outside what I can help with. For other questions, please contact {owner}.",
    "no_source": ("I couldn't find this in my documents, so I don't want to guess. "
                  "Please contact {owner} for help."),
    "denied": "You don't have access to this chatbot. Ask its owner, {owner}, for access.",
    "blocked": "I can't help with that request.",
    "unavailable": "This chatbot isn't available right now. Please try again later.",
    "budget": "This chatbot has reached its monthly budget and is paused until the 1st. Contact {owner}.",
    "unverified": ("I found relevant documents but couldn't produce an answer I could verify word "
                   "for word. Here are the most relevant sources:"),
    "error": "Sorry, something went wrong. Please try again.",
}


class _TTLCache(dict):
    """Tiny TTL cache; dict reads/writes are atomic, so no lock is needed."""

    def get_or_load(self, key, ttl, loader):
        hit = self.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        value = loader()
        self[key] = (time.time(), value)
        return value


class ChatbotAgent(ResponsesAgent):
    def __init__(self):
        self._cache = _TTLCache()
        self._w = None
        self._vsc = None
        self._llm = None
        self._pool = ThreadPoolExecutor(max_workers=8)
        self._stream_ok = os.environ.get("FACTORY_STREAM_LLM", "true") == "true"
        self._session_store = None

    # Clients --------------------------------------------------------------
    @property
    def w(self):
        if self._w is None:
            from databricks.sdk import WorkspaceClient
            self._w = WorkspaceClient()
        return self._w

    @property
    def llm(self):
        if self._llm is None:
            from factory.llm import gateway_client
            self._llm = gateway_client(self.w, self.settings().get("unity_gateway.base_path", "/ai-gateway/mlflow/v1"))
        return self._llm

    @property
    def vsc(self):
        if self._vsc is None:
            from databricks.vector_search.client import VectorSearchClient
            self._vsc = VectorSearchClient(disable_notice=True)
        return self._vsc

    def _read_json(self, path: str) -> dict:
        return json.loads(self.w.files.download(path).contents.read())

    # Runtime config (no warehouse) --------------------------------------------
    def settings(self) -> PlatformSettings:
        def load():
            base = PlatformSettings.load({"catalog": CATALOG})
            try:
                overrides = self._read_json(f"{runtime_dir(base)}/settings.json")
            except Exception:  # noqa: BLE001 - defaults until MLOps saves settings
                overrides = {}
            overrides["catalog"] = CATALOG
            return PlatformSettings.load(overrides)
        return self._cache.get_or_load("settings", 60, load)

    def runtime(self, bot_id: str, s: PlatformSettings) -> dict:
        ident(bot_id)  # validates
        return self._cache.get_or_load(("bot", bot_id), s.get("agent.config_cache_ttl_seconds", 60),
                               lambda: self._read_json(f"{runtime_dir(s)}/bots/{bot_id}.json"))

    # Identity and access (IDN-1, GOV-3/4) -----------------------------------------
    def caller(self):
        """Returns (caller name, OBO workspace client)."""
        from databricks.sdk import WorkspaceClient
        try:
            from databricks_ai_bridge import ModelServingUserCredentials
            obo = WorkspaceClient(credentials_strategy=ModelServingUserCredentials())
        except Exception:  # local testing
            obo = WorkspaceClient()
        me = obo.current_user.me()
        name = me.user_name or (me.emails[0].value if me.emails else "") or me.display_name or ""
        return name, obo

    def _probe(self, obo, s: PlatformSettings, bot_id: str, table: str, who: str) -> bool:
        def run():
            try:
                WarehouseRunner(obo, WAREHOUSE_ID, tags={"source": "agent_access", "bot_id": bot_id}).query(
                    f"SELECT 1 FROM {ident(s.catalog, bot_id, table)} LIMIT 1")
                return True
            except PermissionError:
                return False
            except RuntimeError as e:
                if any(k in str(e).upper() for k in ("PERMISSION", "INSUFFICIENT", "NOT AUTHORIZED")):
                    return False
                raise
        return self._cache.get_or_load(("probe", bot_id, table, who),
                               s.get("agent.access_cache_ttl_seconds", 900), run)

    def _groups(self, user: str, s: PlatformSettings) -> set[str]:
        def load():
            try:
                users = list(self.w.users.list(filter=f'userName eq "{user}"', attributes="groups"))
                return {gr.display for u in users for gr in (u.groups or []) if gr.display}
            except Exception:  # noqa: BLE001 - default deny
                return set()
        return self._cache.get_or_load(("groups", user), s.get("agent.access_cache_ttl_seconds", 900), load)

    def _profile(self, user: str, s: PlatformSettings) -> dict | None:
        """user_access lookup (IDN-3), only needed for pseudonymous logging today."""
        def load():
            try:
                rows = WarehouseRunner(self.w, WAREHOUSE_ID, tags={"source": "agent_profile"}).query(
                    f"SELECT * FROM {s.fq('user_access')} WHERE lower(user_id) = :u", {"u": user})
            except Exception:  # noqa: BLE001
                rows = []
            return idn.active_profile(rows, date.today())
        return self._cache.get_or_load(("profile", user), s.get("agent.access_cache_ttl_seconds", 900), load)

    def authorize(self, cfg: BotConfig, who: idn.Identity, obo, channel: str,
                  s: PlatformSettings) -> bool:
        if who.source == "obo":
            table = "tester_probe" if channel == "candidate" else "access_probe"
            return self._probe(obo, s, cfg.bot_id, table, who.user)
        # Middleware-asserted user: they may not have a Databricks identity.
        if channel == "candidate":
            names = {cfg.owner_user, cfg.reviewer, *cfg.testers}
            return who.user in {n.lower() for n in names if n} or bool(
                {cfg.owner_group, cfg.reviewer} & self._groups(who.user, s))
        if cfg.access_mode == "middleware":
            return True  # the trusted middleware grants binary access (GOV-9)
        if EVERYONE in cfg.allowed_principals:  # everyone at the company who can sign in (WIZ-15)
            return True
        allowed = {p.lower() for p in cfg.allowed_principals}
        return who.user in allowed or bool(set(cfg.allowed_principals) & self._groups(who.user, s))

    # Retrieval (RET-1, DCL-1) ------------------------------------------------------
    def _search(self, cfg, s, query, channel, strategy, rerank: bool) -> list[dict]:
        name, kind = ("search.rerank", "RERANKER") if rerank else ("search.vector", "RETRIEVER")
        with mlflow.start_span(name=name, span_type=kind) as span:
            span.set_inputs({"query": query, "query_type": strategy.get("query_type", "hybrid"),
                             "num_results": int(strategy.get("num_results", 8)), "channel": channel})
            idx = self.vsc.get_index(s.get("ai_search.endpoint"), index_name_for(s, cfg))
            cols = ["chunk_id", "doc_id", "doc_version", "doc_name", "source_uri", "page_ids",
                    "section", "chunk_to_retrieve"]
            kwargs = dict(query_text=query, columns=cols, num_results=int(strategy.get("num_results", 8)),
                          filters=search_filters(cfg.bot_id, channel, int(time.time())),
                          query_type=strategy.get("query_type", "hybrid"))
            if rerank:
                from databricks.vector_search.reranker import DatabricksReranker
                kwargs["reranker"] = DatabricksReranker(columns_to_rerank=["chunk_to_retrieve", "doc_name"])
            res = idx.similarity_search(**kwargs)
            names = [c["name"] for c in res["manifest"]["columns"]]
            rows = [dict(zip(names, r)) for r in res.get("result", {}).get("data_array", [])]
            for i, r in enumerate(rows, 1):
                r["rank"] = i
            span.set_outputs(tr.documents(rows))
            span.set_attributes({"results": len(rows), "top_score": rows[0].get("score") if rows else None})
            return rows

    def retrieve(self, cfg: BotConfig, s: PlatformSettings, query: str, channel: str,
                 log: dict) -> list[dict]:
        strategy = {**DEFAULT_RETRIEVAL, **(cfg.retrieval or {})}
        rerank = bool(strategy.get("rerank", True))
        with mlflow.start_span(name="retrieve", span_type="RETRIEVER") as span:
            span.set_inputs({"query": query, "strategy": strategy.get("name"), "channel": channel})
            # AI Search bills per endpoint-hour, not per query: the pre-rerank search (for the
            # audit trail and rerank-movement metrics) runs in parallel inside the same trace.
            pre = self._pool.submit(contextvars.copy_context().run, self._search, cfg, s, query, channel,
                                    strategy, False) if rerank else None
            try:
                hits = self._search(cfg, s, query, channel, strategy, rerank)
            except (ImportError, TypeError) as e:  # reranker unavailable in this client version
                log["fired"].append("reranker_unavailable")
                log["debug"]["reranker_error"] = repr(e)
                hits = self._search(cfg, s, query, channel, strategy, False)
                rerank = False
            pre_rows = []
            if pre is not None:
                try:
                    pre_rows = pre.result(timeout=10)
                except Exception as e:  # noqa: BLE001 - logging only
                    log["debug"]["pre_rerank_error"] = repr(e)
            pre_rank = {r["chunk_id"]: r["rank"] for r in pre_rows}
            for h in hits:
                h["pre_rerank_rank"] = pre_rank.get(h["chunk_id"])
            hits = hits[: int(strategy.get("context_results", 6))]
            movement = tr.rerank_movement(hits) if rerank else {}
            log["top_score"] = hits[0].get("score") if hits else None
            log["rerank"] = movement
            log["debug"]["strategy"] = {**strategy, "rerank_applied": rerank}
            log["debug"]["pre_rerank"] = [_slim(r) for r in pre_rows]
            log["debug"]["post_rerank"] = [_slim(r) for r in hits]
            span.set_outputs(tr.documents(hits))
            span.set_attributes({"rerank_applied": rerank, "results": len(hits),
                                 "top_score": log["top_score"], **{f"rerank_{k}": v for k, v in movement.items()}})
            return hits

    # Model calls (OBS-7): streamed so time-to-first-token is measured; the OpenAI autolog
    # child span carries MLflow's native token usage, this span adds TTFT, cost and attempt.
    def _chat(self, name: str, endpoint: str, messages: list[dict], log: dict, s: PlatformSettings,
              **kw) -> str:
        with mlflow.start_span(name=name, span_type="CHAIN") as span:
            span.set_inputs({"model": endpoint, "messages": messages, **kw})
            started, ttft, parts, usage = time.time(), None, [], None
            kw["extra_headers"] = gw.request_tags(log)  # per-bot usage in system.ai_gateway.usage
            try:
                if not self._stream_ok:
                    raise _NoStream()
                stream = self.llm.chat.completions.create(
                    model=endpoint, messages=messages, stream=True,
                    stream_options={"include_usage": True}, **kw)
                for chunk in stream:
                    reason = gw.blocked_reason(chunk)
                    if reason:
                        raise _GatewayBlocked(reason)
                    delta = chunk.choices[0].delta.content if chunk.choices else None
                    if delta:
                        ttft = ttft if ttft is not None else int((time.time() - started) * 1000)
                        parts.append(delta)
                    usage = getattr(chunk, "usage", None) or usage
                text = "".join(parts)
            except Exception as e:  # noqa: BLE001
                if _blocked(e):
                    raise
                if not isinstance(e, _NoStream):
                    log["debug"]["stream_error"] = repr(e)[:500]
                    msg = str(e).lower()
                    if "stream" in msg and ("not supported" in msg or "unsupported" in msg):
                        self._stream_ok = False  # this endpoint can't stream: stop trying
                parts, ttft = [], None  # retry once without streaming (transient errors too)
                resp = self.llm.chat.completions.create(model=endpoint, messages=messages, **kw)
                reason = gw.blocked_reason(resp)
                if reason:
                    raise _GatewayBlocked(reason)
                text, usage = resp.choices[0].message.content or "", resp.usage
            tin, tout = getattr(usage, "prompt_tokens", 0) or 0, getattr(usage, "completion_tokens", 0) or 0
            cost = tr.llm_cost(tin, tout, s.get("pricing", {}))
            log["tokens"][0] += tin
            log["tokens"][1] += tout
            log["llm_calls"] += 1
            if log.get("ttft_ms") is None and ttft is not None and name.startswith("answer"):
                log["ttft_ms"] = ttft
            span.set_outputs({"content": text})
            span.set_attributes({"llm.model": endpoint, "llm.gateway": "unity_gateway", "llm.input_tokens": tin, "llm.output_tokens": tout,
                                 "llm.total_tokens": tin + tout, "llm.cost_usd": cost,
                                 "llm.ttft_ms": ttft, "llm.streamed": ttft is not None,
                                 "llm.latency_ms": int((time.time() - started) * 1000)})
            return text

    def _prompt(self) -> tuple[str, str]:
        """Answer prompt at the Prompt Registry's @production alias (MLflow caches the lookup and
        links the prompt version to the trace); the code default if the registry is unreachable."""
        if PROMPT_NAME:
            try:
                p = mlflow.genai.load_prompt(f"prompts:/{PROMPT_NAME}@production")
                return p.template, f"prompts:/{PROMPT_NAME}/{p.version}"
            except Exception:  # noqa: BLE001
                pass
        return SYSTEM_PROMPT, "code:default"

    def _sessions(self, s):
        """Managed sessions store (Beta), or None when disabled or unavailable."""
        if self._session_store is None and s.get("history.store") == "managed_sessions":
            try:
                self._session_store = ManagedSessions(self.w, s.get("history.store_name", "chatbot_factory"))
            except Exception:  # noqa: BLE001
                self._session_store = False
        return self._session_store or None

    def rewrite(self, history: list[dict], question: str, endpoint: str, log: dict, s) -> str:
        """RET-2: optional LLM rewrite of follow-ups; off unless the bot enables it."""
        with mlflow.start_span(name="query.rewrite", span_type="CHAIN") as span:
            span.set_inputs({"question": question})
            convo = "\n".join(f"{m['role']}: {m['content'][:800]}" for m in history[-6:-1])
            out = self._chat("llm.rewrite", endpoint, [{"role": "user", "content":
                             "Rewrite the last question as a standalone search query using the "
                             f"conversation for context. Return only the query.\n\n{convo}\nuser: {question}"}],
                             log, s, max_tokens=120, temperature=0).strip() or question
            span.set_outputs({"query": out})
            return out

    # Main -----------------------------------------------------------------
    @mlflow.trace(name="chatbot.answer", span_type="AGENT")
    def predict(self, request: ResponsesAgentRequest) -> ResponsesAgentResponse:
        started = time.time()
        ci = dict(request.custom_inputs or {})
        request_id = ci.get("request_id") or str(uuid.uuid4())
        s = self.settings()
        log = _new_log(request_id, ci)
        log["trace_id"] = _trace_id()
        msgs = _messages(request)
        log["question"] = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
        result: dict = {"text": MSG["error"], "citations": [], "conflicts": [], "retrieval": []}
        try:
            result = self._answer(msgs, ci, s, log)
        except Exception as e:  # noqa: BLE001
            log["outcome"], log["error"] = "error", repr(e)[:1000]
        log["answer"] = result["text"]
        log["latency_ms"] = int((time.time() - started) * 1000)
        log["cost_usd"] = tr.llm_cost(log["tokens"][0], log["tokens"][1], s.get("pricing", {}))
        self._tag_trace(log)
        self._save_turn(s, log)
        self._write_log(s, log)
        return ResponsesAgentResponse(
            output=[self.create_text_output_item(text=result["text"], id=request_id)],
            custom_outputs={
                "request_id": request_id, "trace_id": log["trace_id"], "outcome": log["outcome"],
                "answer": result.get("answer", result["text"]), "citations": result["citations"],
                "conflicts": result["conflicts"], "retrieval": result["retrieval"],
                "retrieved_chunk_ids": [r["chunk_id"] for r in result["retrieval"]],
                "release_id": log["release_id"], "config_version": log["config_version"],
                "config_hash": log["config_hash"], "manifest_hash": log["manifest_hash"],
                "model": log["model"], "platform_version": PLATFORM_VERSION,
                "app_version": log["app_version"], "environment": ENVIRONMENT,
                "channel": log["channel"], "guardrails_fired": sorted(set(log["fired"])),
                "identity_mode": log.get("identity_mode"),
                "usage": {"input_tokens": log["tokens"][0], "output_tokens": log["tokens"][1],
                          "cost_usd": log["cost_usd"], "ttft_ms": log["ttft_ms"],
                          "latency_ms": log["latency_ms"]},
            })

    def _save_turn(self, s, log: dict) -> None:
        """Append this turn to the managed session (async, best effort; CNV-2)."""
        sessions = self._sessions(s)
        if not (sessions and log.get("conversation_id") and log.get("history_mode") not in (None, "off")):
            return
        actor = log.get("user_hash") or log.get("user_id")
        if not actor:  # no stable per-user key: never share a session between users
            return
        turns = [{"role": "user", "content": log["question"]}, {"role": "assistant", "content": log["answer"]}]
        self._pool.submit(lambda: _quiet(sessions.append, log["conversation_id"], actor, turns))

    def _tag_trace(self, log: dict) -> None:
        """Trace-level tags (searchable/filterable in MLflow) plus the native user and session
        fields so the MLflow UI groups a conversation's questions (OBS-8)."""
        try:
            mlflow.update_current_trace(
                tags=tr.trace_tags(log),
                metadata={"mlflow.trace.session": log.get("conversation_id") or log["request_id"],
                          "mlflow.trace.user": log.get("user_id") or log.get("user_hash") or "unknown",
                          # links the trace to the release's LoggedModel (MLflow Versions tab, OBS-13)
                          **({"mlflow.modelId": log["model_id"]} if log.get("model_id") else {})})
        except Exception:  # noqa: BLE001 - tracing never breaks an answer
            pass

    def predict_stream(self, request: ResponsesAgentRequest) -> Generator[ResponsesAgentStreamEvent, None, None]:
        # Answers are verified before release, so we emit the final verified text.
        resp = self.predict(request)
        for item in resp.output:
            yield ResponsesAgentStreamEvent(type="response.output_item.done", item=item)

    def _answer(self, msgs: list[dict], ci: dict, s: PlatformSettings, log: dict) -> dict:
        empty = {"citations": [], "conflicts": [], "retrieval": []}
        bot_id = ci.get("bot_id", "")
        log["bot_id"] = bot_id
        with mlflow.start_span(name="config.load", span_type="TOOL") as span:
            rt = self.runtime(bot_id, s)
            cfg = BotConfig.from_dict(rt["config"])
            channel = "candidate" if ci.get("channel") == "candidate" else "live"
            release = rt.get("candidate_release" if channel == "candidate" else "live_release") or {}
            log.update(channel=channel, release_id=release.get("release_id"),
                       config_version=release.get("config_version") or rt.get("config_version"),
                       config_hash=release.get("config_hash"), manifest_hash=release.get("manifest_hash"),
                       identity_mode=cfg.identity_mode, history_mode=cfg.history_mode, critical=cfg.critical,
                       model_id=release.get("model_id") if cfg.obs_on("link_versions") else None,
                       judges={n: cfg.judge_on(n) for n in tr.PRODUCTION_JUDGES})
            span.set_outputs({"bot_id": bot_id, "channel": channel, "release_id": log["release_id"],
                              "state": rt.get("state"), "strategy": (cfg.retrieval or {}).get("name")})
        owner = cfg.owner_user

        with mlflow.start_span(name="identity.resolve", span_type="GUARDRAIL") as span:
            caller, obo = self.caller()
            who = idn.resolve(caller, ci, s.get("access.trusted_callers", []))
            synthetic = bool(ci.get("synthetic")) and caller in set(
                s.get("access.trusted_callers", []) + s.get("access.synthetic_callers", []))
            log["synthetic"] = synthetic
            profile = self._profile(who.user, s) if cfg.identity_mode == "pseudonymous" else None
            log.update(idn.log_fields(who, cfg.identity_mode, HASH_KEY, ENC_KEY, profile))
            # Pseudonymous bots never put the identity in the trace, only the keyed hash.
            span.set_outputs({"source": who.source, "identity_mode": cfg.identity_mode,
                              "user": log.get("user_id") or log.get("user_hash"), "synthetic": synthetic})

        with mlflow.start_span(name="access.check", span_type="GUARDRAIL") as span:
            state = rt.get("state")
            if rt.get("deleted") or not release or (channel == "live" and state != "live"):
                log["outcome"] = "unavailable"
                span.set_outputs({"allowed": False, "reason": state or "no release"})
                return {"text": MSG["budget" if state == "budget_paused" else "unavailable"].format(owner=owner),
                        **empty}
            allowed = synthetic or self.authorize(cfg, who, obo, channel, s)
            span.set_outputs({"allowed": allowed, "channel": channel})
            if not allowed:
                log["outcome"] = "denied"
                _event(log, "access", "deny", f"{who.source} user not authorized for {channel}")
                return {"text": MSG["denied"].format(owner=owner), **empty}

        endpoint = s.answer_service(cfg.answer_model)
        log["model"] = endpoint
        limit_quotes = cfg.limit_quotes or cfg.access_mode == "middleware"  # GRD-13

        with mlflow.start_span(name="history.select", span_type="MEMORY") as span:
            sessions, actor = self._sessions(s), log.get("user_hash") or log.get("user_id")
            conv_id = log.get("conversation_id")
            stored = []
            if sessions and actor and conv_id and cfg.history_mode != "off" and len(msgs) <= 1:
                try:
                    stored = sessions.history(conv_id, actor, 2 * int(s.get("agent.history_turns", 10)))
                except Exception as e:  # noqa: BLE001
                    log["debug"]["session_error"] = repr(e)[:300]
            msgs = merge_history(stored, msgs)
            history = bounded_history(msgs, cfg.history_mode, int(s.get("agent.history_turns", 10)),
                                      int(s.get("agent.history_max_chars", 12000)))
            question = history[-1]["content"] if history else ""
            span.set_outputs({"mode": cfg.history_mode, "turns_kept": len(history), "turns_received": len(msgs),
                              "turns_from_session_store": len(stored)})

        user_turns = [m["content"] for m in history if m["role"] == "user"]
        if cfg.query_rewrite and len(user_turns) > 1:
            query = self.rewrite(history, question, endpoint, log, s)
        else:
            query = search_query(history, cfg.history_mode)
        log["debug"]["search_query"] = query

        hits = self.retrieve(cfg, s, query, channel, log)
        retrieval = [_slim(h) for h in hits]
        if not hits:
            log["outcome"] = "no_source"
            return {"text": MSG["no_source"].format(owner=owner), **empty, "retrieval": retrieval}

        with mlflow.start_span(name="guardrail.retrieved_injection", span_type="GUARDRAIL") as span:
            sources_text, stripped = [], []
            for h in hits:
                clean, found = g.strip_injection(h["chunk_to_retrieve"] or "")
                if found:
                    stripped.append(h["chunk_id"])
                    _event(log, "retrieved_injection", "strip", f"chunk {h['chunk_id']}", "; ".join(found))
                sources_text.append(clean)
            span.set_outputs({"chunks_checked": len(hits), "chunks_stripped": stripped})

        with mlflow.start_span(name="prompt.build", span_type="PARSER") as span:
            template, prompt_uri = self._prompt()
            log["prompt_uri"] = prompt_uri
            system = fill_system(template, cfg, g.platform_rules(s.get("guardrails.platform_rules", {}),
                                                                 cfg.disabled_rules))
            context = format_sources(hits, sources_text, _pages)
            messages = [{"role": "system", "content": f"{system}\n\nSOURCES:\n{context}"}]
            messages += [{"role": m["role"], "content": m["content"]} for m in history]
            log["debug"]["prompt"] = messages
            span.set_attributes({"prompt_uri": prompt_uri, "sources": len(hits),
                                 "prompt_chars": sum(len(m["content"]) for m in messages)})

        parsed = check = None
        for attempt in range(1, 3):
            name = "answer.attempt_1" if attempt == 1 else f"answer.retake_{attempt}"
            try:
                raw = self._chat(name, endpoint, messages, log, s, temperature=s.get("agent.temperature", 0.0),
                                 max_tokens=int(s.get("agent.max_output_tokens", 1200)))
            except Exception as e:  # noqa: BLE001
                if _blocked(e):
                    with mlflow.start_span(name="guardrail.gateway", span_type="GUARDRAIL") as span:
                        span.set_outputs({"blocked": True, "reason": str(e)[:500]})
                    log["outcome"] = "blocked"
                    _event(log, "gateway_guardrail", "block", str(e)[:500], question)
                    return {"text": MSG["blocked"], **empty, "retrieval": retrieval}
                raise
            log["debug"].setdefault("model_outputs", []).append(raw)
            with mlflow.start_span(name="guardrail.output_check", span_type="GUARDRAIL") as span:
                span.set_inputs({"attempt": attempt})
                parsed, check = g.parse_structured(raw), None
                if parsed is None:
                    problems, ok = ["Reply with valid JSON in one of the three forms."], False
                elif parsed["status"] in ("out_of_scope", "no_source"):
                    problems, ok = [], True
                else:
                    check = g.check_answer(parsed.get("answer", ""), sources_text, system, limit_quotes,
                                           s.get("agent.max_quote_words", 60),
                                           citations=parsed.get("citations") or [])
                    ok = check.ok and g.has_citation(parsed.get("answer", ""))
                    if not check.ok:
                        for f in check.fired:
                            _event(log, f, "regenerate", "; ".join(check.problems))
                    problems = check.problems or ([] if ok else
                                                  ["Cite sources with [n] and give an exact excerpt for each."])
                span.set_outputs({"passed": ok, "status": (parsed or {}).get("status", "invalid_json"),
                                  "fired": sorted(set(check.fired)) if check else [], "problems": problems})
            if ok:
                break
            log["retakes"] += 1
            messages = messages + [{"role": "assistant", "content": raw},
                                   {"role": "user", "content": "Revise your reply. " + " ".join(problems)}]
        else:
            log["outcome"] = "unverified"
            _event(log, "unverified", "fallback", "answer could not be verified after retry")
            listing = "\n".join(f"- {h['doc_name']}, page(s) {_pages(h)}" for h in hits[:3])
            return {"text": f"{MSG['unverified']}\n{listing}", **empty, "retrieval": retrieval}

        if parsed["status"] == "out_of_scope":
            log["outcome"] = "refused"
            _event(log, "topic_rules", "refuse", "model judged question out of scope", question)
            return {"text": MSG["refused"].format(owner=owner), **empty, "retrieval": retrieval}
        if parsed["status"] == "no_source":
            log["outcome"] = "no_source"
            return {"text": MSG["no_source"].format(owner=owner), **empty, "retrieval": retrieval}

        with mlflow.start_span(name="guardrail.redact", span_type="GUARDRAIL") as span:
            answer, found = sensitive.redact(check.text)
            if found:
                _event(log, "sensitive_output", "redact", ", ".join(sorted(set(found))))
            span.set_outputs({"redacted": sorted(set(found)), "quote_limit": limit_quotes})

        with mlflow.start_span(name="response.render", span_type="PARSER") as span:
            citations = []
            for c in parsed.get("citations") or []:
                h = hits[c["source"] - 1]
                excerpt, _ = sensitive.redact(c.get("excerpt", ""))
                if limit_quotes:
                    excerpt = g.cap_quotes(f'"{excerpt}"', s.get("agent.max_quote_words", 60))[0].strip('"')
                citations.append({"n": c["source"], "excerpt": excerpt, "chunk_id": h["chunk_id"],
                                  "doc_id": h["doc_id"], "doc_name": h["doc_name"],
                                  "doc_version": h.get("doc_version"), "pages": _pages(h),
                                  "section": h.get("section"), "source_uri": h.get("source_uri")})
            conflicts = [c for c in (parsed.get("conflicts") or []) if c.get("sources")]
            if conflicts:
                _event(log, "conflict_disclosed", "annotate", json.dumps(conflicts)[:1000])
            span.set_outputs({"citations": len(citations), "conflicts": len(conflicts)})

        log["outcome"] = "answered"
        log["cited_chunk_ids"] = [c["chunk_id"] for c in citations]
        log["conflicts"] = conflicts
        return {"text": render(answer, citations, conflicts), "answer": answer,
                "citations": citations, "conflicts": conflicts, "retrieval": retrieval}

    # Logging (PRV-1..3) -------------------------------------------------------------
    def _write_log(self, s: PlatformSettings, log: dict) -> None:
        """One JSON file per request in the restricted logs volume; best effort, async."""
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = f"{logs_dir(s)}/requests/date={day}/{log['request_id']}.json"
        record = dict(log)
        record["guardrails_fired"] = sorted(set(log["fired"]))

        def put():
            try:
                self.w.files.upload(path, io.BytesIO(json.dumps(record, default=str).encode()),
                                    overwrite=True)
            except Exception:  # noqa: BLE001 - never fail the user's request
                pass
        self._pool.submit(put)


def _new_log(request_id: str, ci: dict) -> dict:
    return {"request_id": request_id, "ts": datetime.now(timezone.utc).isoformat(),
            "bot_id": ci.get("bot_id", ""), "channel": "live", "synthetic": False,
            "conversation_id": ci.get("conversation_id"), "outcome": "error",
            "release_id": None, "config_version": None, "config_hash": None, "manifest_hash": None,
            "model": None, "platform_version": PLATFORM_VERSION,
            "app_version": ci.get("app_version") or os.environ.get("FACTORY_APP_VERSION", ""),
            "environment": ENVIRONMENT, "cited_chunk_ids": [], "conflicts": [],
            "tokens": [0, 0], "llm_calls": 0, "retakes": 0, "ttft_ms": None, "top_score": None,
            "rerank": {}, "prompt_uri": None, "fired": [], "events": [], "error": None, "debug": {}}


def _event(log: dict, rule: str, action: str, reason: str, content: str = "") -> None:
    log["fired"].append(rule)
    log["events"].append({"event_id": str(uuid.uuid4()), "rule": rule, "action": action,
                          "reason": reason[:2000], "content": (content or "")[:4000],
                          "ts": datetime.now(timezone.utc).isoformat()})


def _quiet(fn, *args) -> None:
    try:
        fn(*args)
    except Exception:  # noqa: BLE001 - background persistence never affects the answer
        pass


class _NoStream(Exception):
    pass


class _GatewayBlocked(Exception):
    """Unity Gateway service policy DENY (returned as HTTP 200 with databricks_service_policy)."""


def _blocked(e: Exception) -> bool:
    if isinstance(e, _GatewayBlocked):
        return True
    msg = str(e).lower()  # older endpoint guardrails and rate limits surface as errors
    return "guardrail" in msg or "blocked by" in msg


def _trace_id() -> str | None:
    try:
        return mlflow.get_active_trace_id()
    except Exception:  # noqa: BLE001
        return None


def _messages(request: ResponsesAgentRequest) -> list[dict]:
    out = []
    for item in request.input:
        d = item.model_dump() if hasattr(item, "model_dump") else dict(item)
        role, content = d.get("role"), d.get("content")
        if role not in ("user", "assistant"):
            continue
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        out.append({"role": role, "content": content or ""})
    return out


_pages = tr.pages


def _slim(h: dict) -> dict:
    return {"rank": h.get("rank"), "pre_rerank_rank": h.get("pre_rerank_rank"),
            "chunk_id": h.get("chunk_id"), "doc_id": h.get("doc_id"), "doc_name": h.get("doc_name"),
            "doc_version": h.get("doc_version"), "pages": _pages(h), "score": h.get("score")}


def _mask_span(span) -> None:
    """Trace PII masking (OBS-14): restricted numbers (SSN, card, IBAN...) never reach trace storage."""
    if span.inputs is not None:
        span.set_inputs(tr.mask(span.inputs))
    if span.outputs is not None:
        span.set_outputs(tr.mask(span.outputs))


if MASK_TRACES:
    mlflow.tracing.configure(span_processors=[_mask_span])

AGENT = ChatbotAgent()
mlflow.models.set_model(AGENT)
