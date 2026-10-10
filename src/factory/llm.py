"""Thin wrappers over Databricks model endpoints: pay-per-token serving endpoints (generation)
and Unity Gateway model services (answers). Both are OpenAI-compatible."""
from __future__ import annotations

import base64
import json
import re
from typing import Any


def gateway_client(workspace_client=None, base_path: str = "/ai-gateway/mlflow/v1"):
    """OpenAI client for Unity Gateway model services (GW-1). Auth is re-read from the
    workspace client on every request, so OAuth tokens refresh without restarts."""
    import httpx
    from openai import OpenAI
    if workspace_client is None:
        from databricks.sdk import WorkspaceClient
        workspace_client = WorkspaceClient()

    class _Auth(httpx.Auth):
        def auth_flow(self, request):
            request.headers.update(workspace_client.config.authenticate())
            yield request

    return OpenAI(base_url=workspace_client.config.host.rstrip("/") + base_path, api_key="databricks",
                  http_client=httpx.Client(auth=_Auth(), timeout=120))


def openai_client(workspace_client=None):
    if workspace_client is None:
        from databricks.sdk import WorkspaceClient
        workspace_client = WorkspaceClient()
    return workspace_client.serving_endpoints.get_open_ai_client()


def extract_json(text: str) -> Any:
    """Parse the first JSON object in a model response (tolerates code fences)."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise ValueError("No JSON in model response")
    return json.JSONDecoder().raw_decode(text[start:])[0]


def chat(client, endpoint: str, messages: list[dict], max_tokens: int = 1500,
         temperature: float = 0.0) -> tuple[str, dict]:
    resp = client.chat.completions.create(
        model=endpoint, messages=messages, max_tokens=max_tokens, temperature=temperature,
    )
    usage = getattr(resp, "usage", None)
    return resp.choices[0].message.content or "", {
        "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
        "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
    }


def image_type(data: bytes) -> str:
    """Media type from the image's own bytes. Page images from the document reader are usually
    JPEG, and the model rejects an image whose declared type doesn't match its content."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def chat_json(client, endpoint: str, prompt: str, max_tokens: int = 3000,
              images: list[bytes] | None = None, retries: int = 1) -> Any:
    content: Any = prompt
    if images:
        content = [{"type": "text", "text": prompt}] + [
            {"type": "image_url",
             "image_url": {"url": f"data:{image_type(img)};base64," + base64.b64encode(img).decode()}}
            for img in images
        ]
    messages = [{"role": "user", "content": content}]
    last_err = None
    for _ in range(retries + 1):
        text, _usage = chat(client, endpoint, messages, max_tokens=max_tokens)
        try:
            return extract_json(text)
        except ValueError as e:
            last_err = e
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": "Return valid JSON only."}]
    raise last_err  # type: ignore[misc]
