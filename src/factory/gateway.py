"""Unity Gateway (formerly AI Gateway) for answer models (ARC-8, GW-1..6).

Answer models are Unity Gateway *model services*: Unity Catalog securables
(<catalog>.<platform_schema>.<service>). They are created here through the REST API with
their routing and fallbacks; an existing service is never modified. The agent calls them through the OpenAI-compatible unified API at
<host>/ai-gateway/mlflow/v1 with model="<catalog.schema.name>".

Rate limits, the inference table and guardrail *service policies* (with Enforce/Log mode) are
configured in the Unity Gateway UI, so `ui_checklist` lists the exact values MLOps enters; the
Admin page and docs/SETUP.md show it. A policy DENY returns HTTP 200 with a top-level
`databricks_service_policy` object, which `blocked_reason` detects.
"""
from __future__ import annotations

import json

from databricks.sdk.errors import NotFound

API = "/api/2.1/unity-catalog/model-services"


def _services(settings) -> dict[str, dict]:
    """Answer services by label, plus the guardrail evaluator: Log-mode policies run on it, so their
    verdicts (which never reach the caller) land in its inference table (GW-7). Enforce-mode blocks
    are seen in the response itself, so those policies keep Databricks' default evaluator."""
    ev = settings.get("unity_gateway.evaluator")
    return {**settings.get("models.answer_services", {}), **({"evaluator": ev} if ev else {})}


def labels(settings) -> list[str]:
    return list(_services(settings))


def service_name(settings, label: str) -> str:
    services = _services(settings)
    if label not in services:
        raise ValueError(f"Model '{label}' has no Unity Gateway model service. Allowed: {sorted(services)}")
    return f"{settings.catalog}.{settings.platform_schema}.{services[label]['service']}"


def _destination(name: str, model: str, pct: int | None = None) -> dict:
    d = {"name": name, "destination_type": "DESTINATION_TYPE_PAY_PER_TOKEN_FOUNDATION_MODEL",
         "pay_per_token_config": {"model": f"models/{model}"}}
    if pct is not None:
        d["traffic_percentage"] = pct
    return d


def service_config(settings, label: str) -> dict:
    """Model service body: primary route and ordered fallbacks (GW-3)."""
    spec = _services(settings)[label]
    return {
        "comment": f"Chatbot Factory model service '{label}'",
        "config": {
            "routing": {"destinations": [_destination("primary", spec["model"], 100)],
                        "fallback": {"destinations": [_destination(f"fallback_{i + 1}", m)
                                                      for i, m in enumerate(spec.get("fallbacks", []))]}},
        },
    }


def _routed_models(body: dict) -> list[str]:
    """Models a service body routes to, primary first then fallbacks, wherever they are nested."""
    found: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            model = (node.get("pay_per_token_config") or {}).get("model")
            if model:
                found.append(model)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(body)
    return found


def ensure_model_service(w, settings, label: str) -> str:
    """Create the model service if it doesn't exist; returns its full name.

    An existing service is left untouched: the API can't update routing yet (UpdateModelService
    rejects config.routing), and rate limits, the inference table and policies are set in the
    UI. If its routing differs from the settings, that is printed so it can be changed there.
    """
    full = service_name(settings, label)
    body = service_config(settings, label)
    try:
        existing = w.api_client.do("GET", f"{API}/{full}")
    except NotFound:  # the SDK's 404, including ResourceDoesNotExist -> create
        catalog, schema, sid = full.split(".")
        w.api_client.do("POST", API, query={"parent": f"schemas/{catalog}.{schema}", "model_service_id": sid},
                        body=body)
        return full
    wanted, current = _routed_models(body), _routed_models(existing or {})
    if current and current != wanted:
        print(f"WARNING: model service {full} routes to {current} but the settings say {wanted}. "
              "The API can't change routing on an existing service: edit it in the Unity Gateway UI, "
              "or delete the service and rerun this job (its UI settings must then be re-entered).")
    return full


def ui_checklist(settings) -> list[dict]:
    """What MLOps sets in the Unity Gateway UI (no API today): per answer service the EXECUTE grant,
    rate limits, inference table and service policies (phase, Enforce/Log, evaluator); and the
    evaluator's inference table. docs/SETUP.md step 7 walks through it."""
    gw, out = settings.get("unity_gateway", {}), []
    ev = service_name(settings, "evaluator") if gw.get("evaluator") else None
    for label, spec in _services(settings).items():
        svc = service_name(settings, label)
        out.append({"service": svc, "setting": "Inference table",
                    "value": f"{settings.catalog}.{settings.platform_schema}, prefix gw_{spec['service']}"})
        if label == "evaluator":
            continue
        out.append({"service": svc, "setting": "Permission", "value": "EXECUTE for the agent service principal"})
        for r in gw.get("rate_limits", []):
            out.append({"service": svc, "setting": f"Rate limit ({r['key'].replace('_', ' ')})",
                        "value": f"{r['requests']} requests per {r['renewal_period']}"})
        for p in gw.get("policies", []):
            out.append({"service": svc, "setting": f"Policy {p['policy']}",
                        "value": f"{', '.join(p.get('phases', ['input', 'output']))} · "
                                 f"{p.get('mode', 'enforce').title()} · {p.get('why', '')}"
                                 + (f" · evaluator {ev}" if ev and p.get("mode") == "log" else "")})
    return out


def request_tags(log: dict) -> dict[str, str]:
    """Per-request tags -> system.ai_gateway.usage.request_tags, for per-bot usage and cost (GW-4)."""
    tags = {"bot_id": log.get("bot_id"), "release_id": log.get("release_id"), "channel": log.get("channel"),
            "request_id": log.get("request_id"), "conversation_id": log.get("conversation_id"),
            "synthetic": str(bool(log.get("synthetic"))).lower()}
    return {"Databricks-Ai-Gateway-Request-Tags": json.dumps({k: str(v) for k, v in tags.items() if v})}


def blocked_reason(obj) -> str | None:
    """A service-policy DENY comes back as HTTP 200 with a top-level databricks_service_policy."""
    extra = getattr(obj, "model_extra", None) or (obj if isinstance(obj, dict) else {}) or {}
    policy = extra.get("databricks_service_policy")
    if not policy:
        return None
    if isinstance(policy, dict):
        decision = str(policy.get("result") or policy.get("decision") or "DENY").upper()
        if decision not in {"DENY", "BLOCK"}:
            return None
        return str(policy.get("reason") or policy.get("policy") or "blocked by a service policy")
    return str(policy)
