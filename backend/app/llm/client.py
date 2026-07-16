"""LLM client wrapper — Hugging Face Inference Providers backend.

All Claude usage was replaced with HF hosted models. The two public helpers keep
their original signatures so the rest of the app is unchanged:
- parse_structured(...): one structured call -> validated Pydantic instance.
- batch_score(...): score many items (concurrent per-item calls; HF has no batch API).

HF Inference Providers has no batch API and no prompt caching, so:
- `batch_score` fans out concurrent calls instead of a server-side batch.
- `cache_system` / `thinking` / poll args are accepted but ignored (kept for
  call-site compatibility).
Structured output is provider-agnostic: the JSON schema is embedded in the prompt
and the response is parsed with one retry.
"""
from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.config import get_settings

T = TypeVar("T", bound=BaseModel)
log = logging.getLogger("llm")

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class LLMError(RuntimeError):
    pass


def _chat(
    messages: list[dict], tier: str, max_tokens: int,
    model_override: str | None = None, json_mode: bool = False,
) -> str:
    """Chat completion for a tier ("fast" | "quality"). Returns the assistant text."""
    settings = get_settings()
    kind, model, base_url, api_key = settings.resolve_tier(tier)
    if model_override:
        model = model_override
    log.info("LLM call: tier=%s kind=%s model=%s max_tokens=%s", tier, kind, model, max_tokens)

    if kind == "openai":
        # OpenAI-compatible HTTP API (Groq, Ollama, Gemini, OpenRouter, ...).
        # Plain httpx — no extra SDK; this is just a POST to /chat/completions.
        import httpx

        if not base_url:
            raise LLMError(f"No base_url configured for the {tier} tier's OpenAI-compatible backend.")
        url = base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        payload = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.0}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        last = ""
        for attempt in range(4):
            try:
                resp = httpx.post(url, json=payload, headers=headers, timeout=60)
            except httpx.HTTPError as exc:
                last = str(exc)
                log.warning("LLM %s network error (attempt %d/4): %s", model, attempt + 1, exc)
                time.sleep(min(2 ** attempt, 8))
                continue
            # Retry rate limits / transient server errors with bounded backoff.
            if resp.status_code == 429 or resp.status_code >= 500:
                last = f"{resp.status_code}"
                log.warning("LLM %s transient %s (attempt %d/4): %s",
                            model, resp.status_code, attempt + 1, resp.text[:160])
                retry_after = resp.headers.get("retry-after")
                wait = float(retry_after) if retry_after else 2 ** attempt
                # Honor the provider's Retry-After (Groq free windows exceed 8s), but
                # cap so one rate-limited call can't stall the run indefinitely.
                time.sleep(min(wait, 90))
                continue
            if resp.status_code >= 400:
                log.warning("LLM %s failed %s: %s", model, resp.status_code, resp.text[:200])
                raise LLMError(f"LLM request failed {resp.status_code}: {resp.text[:200]}")
            content = resp.json()["choices"][0]["message"]["content"] or ""
            log.info("LLM %s ok (%d chars): %r", model, len(content), content[:160])
            return content
        log.error("LLM %s failed after retries (last=%s)", model, last)
        raise LLMError(f"LLM request failed after retries (last={last}).")

    # Hugging Face Inference Providers.
    from huggingface_hub import InferenceClient

    if not settings.hf_token:
        raise LLMError("HF_TOKEN is not set; cannot call Hugging Face Inference Providers.")
    try:
        client = InferenceClient(token=settings.hf_token, provider=settings.hf_provider or "auto")
        resp = client.chat_completion(messages=messages, model=model, max_tokens=max_tokens, temperature=0.0)
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM (HF) %s failed: %s", model, exc)
        raise LLMError(f"HF request failed: {exc}") from exc
    return resp.choices[0].message.content or ""


# --- JSON schema helpers (also used to instruct the model) ----------------

def _harden_schema(node: dict) -> None:
    """In place: make a JSON schema strict (additionalProperties=false, all keys required)."""
    if not isinstance(node, dict):
        return
    if node.get("type") == "object" or "properties" in node:
        node["additionalProperties"] = False
        props = node.get("properties", {})
        node["required"] = list(props.keys())
        for sub in props.values():
            _harden_schema(sub)
    for key in ("items", "$defs", "definitions"):
        child = node.get(key)
        if isinstance(child, dict):
            if key in ("$defs", "definitions"):
                for sub in child.values():
                    _harden_schema(sub)
            else:
                _harden_schema(child)


def json_schema_format(schema: type[BaseModel]) -> dict:
    """Build an output-format json_schema dict from a Pydantic model."""
    js = schema.model_json_schema()
    _harden_schema(js)
    return {"type": "json_schema", "schema": js}


def _extract_json(text: str) -> str:
    """Pull a JSON object out of a model response (handles ``` fences / prose)."""
    text = (text or "").strip()
    fenced = _JSON_FENCE.search(text)
    if fenced:
        return fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


def _complete_structured(
    *, system: str, user: str, schema: type[T], tier: str, max_tokens: int, model_override: str | None = None
) -> T:
    schema_dict = json_schema_format(schema)["schema"]
    keys = ", ".join(schema_dict.get("properties", {}).keys())
    base_system = (
        f"{system}\n\nReturn ONLY one JSON object — no prose, no markdown, no code fences. "
        f"The object must have exactly these top-level keys: {keys}. "
        f"Fill the VALUES from the input; do NOT return the schema definition itself. "
        f"Value types must match this JSON schema:\n{json.dumps(schema_dict)}"
    )
    messages = [{"role": "system", "content": base_system}, {"role": "user", "content": user}]

    last_err: Exception | None = None
    for attempt in range(2):
        text = _chat(messages, tier, max_tokens, model_override=model_override, json_mode=True)
        try:
            return schema.model_validate(json.loads(_extract_json(text)))
        except (json.JSONDecodeError, ValidationError) as exc:
            last_err = exc
            log.warning(
                "Structured parse for %s attempt %d failed (%s); raw=%r",
                schema.__name__, attempt + 1, type(exc).__name__, (text or "")[:300],
            )
            # Firmer instruction on retry.
            messages[0]["content"] = base_system + "\n\nYour previous reply was not valid JSON. Return ONLY the JSON object."
    log.error("Structured parse for %s gave up after retries: %s", schema.__name__, last_err)
    raise LLMError(f"Model did not return schema-valid JSON after retries: {last_err}")


# --- public API (stable signatures) --------------------------------------

def parse_structured(
    *,
    system: str,
    user: str,
    schema: type[T],
    model: str | None = None,
    max_tokens: int = 8000,
    tier: str = "quality",
    cache_system: bool = True,  # accepted for compatibility; no HF equivalent
    thinking: bool = True,      # accepted for compatibility; ignored
) -> T:
    return _complete_structured(
        system=system, user=user, schema=schema, tier=tier, max_tokens=max_tokens, model_override=model
    )


def batch_score(
    *,
    system: str,
    items: list[tuple[str, str]],
    schema: type[T],
    model: str | None = None,
    max_tokens: int = 1024,
    max_workers: int = 4,       # modest fan-out to respect free-tier rate limits
    tier: str = "fast",
    poll_seconds: int = 0,      # accepted for compatibility; ignored
    timeout_seconds: int = 0,   # accepted for compatibility; ignored
) -> dict[str, T]:
    """Score many (custom_id, text) items concurrently. Returns successes only."""
    if not items:
        return {}

    def _one(item: tuple[str, str]) -> tuple[str, T | None]:
        cid, text = item
        try:
            return cid, _complete_structured(
                system=system, user=text, schema=schema, tier=tier, max_tokens=max_tokens, model_override=model
            )
        except LLMError:
            return cid, None

    out: dict[str, T] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for cid, parsed in pool.map(_one, items):
            if parsed is not None:
                out[cid] = parsed
    return out
