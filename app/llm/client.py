import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime

from groq import Groq, RateLimitError

from app.db import connect, new_id
from app.settings import settings

MAX_RATE_LIMIT_RETRIES = 3
_GROQ_DURATION_RE = re.compile(r"^(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+(?:\.\d+)?)s)?$")


def _parse_groq_duration(text: str) -> float | None:
    """Groq's x-ratelimit-reset-* headers use "7.66s" or "2m59.56s", not plain
    seconds like retry-after."""
    m = _GROQ_DURATION_RE.match(text.strip())
    if not m or not (m.group("minutes") or m.group("seconds")):
        return None
    minutes = float(m.group("minutes") or 0)
    seconds = float(m.group("seconds") or 0)
    return minutes * 60 + seconds


def _is_retryable_rate_limit(error: RateLimitError) -> bool:
    """Not every 429 is transient. When the provider says a single request's
    expected output is larger than the account's per-minute ceiling, waiting
    changes nothing -- the same request will be rejected again. Retrying that is
    just three wasted sleeps before the same failure, so it fails fast instead."""
    message = str(error).lower()
    return not ("request too large" in message or "reduce max_tokens" in message.lower())


def _rate_limit_delay(error: RateLimitError, attempt: int) -> float:
    """retry-after is the direct answer when Groq sends it. Falling back to the
    token/request reset headers (see Groq's rate-limit docs) is more informed
    than a blind exponential backoff, since it reflects the account's actual
    window instead of guessing."""
    headers = error.response.headers
    retry_after = headers.get("retry-after")
    if retry_after is not None:
        try:
            return float(retry_after)
        except ValueError:
            pass
    for header_name in ("x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        raw = headers.get(header_name)
        if raw:
            parsed = _parse_groq_duration(raw)
            if parsed is not None:
                return parsed
    return float(2 ** attempt)


class LLMNotConfigured(Exception):
    """No LLM backend is configured. Callers may catch this specifically to fall
    back to a deterministic path. Any other exception from a call means the LLM
    *was* configured and failed -- that must surface, not be swallowed."""


def active_model() -> str:
    """Which model would actually serve the next call -- used as the cache key
    and audit `model` field, so a local-model result never gets mislabeled or
    cache-collided under the Groq model's name."""
    if settings.groq_env_key:
        return settings.groq_model
    if settings.local_fallback_model:
        return f"local:{settings.local_fallback_model}"
    return "none"


def _complete_json_groq(system_prompt: str, user_prompt: str, max_tokens: int) -> tuple[dict, int]:
    """Bounded retry on 429 only -- a rate limit is transient and expected under
    load (this project's own mapping stage can fire dozens of calls in quick
    succession); any other error is a real failure and must not be retried away."""
    client = Groq(api_key=settings.groq_env_key, timeout=60.0)
    started = datetime.now(UTC)
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=settings.groq_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.2,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
            break
        except RateLimitError as e:
            if attempt == MAX_RATE_LIMIT_RETRIES or not _is_retryable_rate_limit(e):
                raise
            time.sleep(_rate_limit_delay(e, attempt))
    latency_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    return json.loads(response.choices[0].message.content), latency_ms


def _complete_json_local(system_prompt: str, user_prompt: str, max_tokens: int) -> tuple[dict, int]:
    """llama.cpp's llama-server, OpenAI-compatible /v1/chat/completions. A local
    quantized model is slower than a hosted one (thinking models add hidden
    reasoning tokens before the JSON answer), hence the generous timeout."""
    body_out = {
        "model": settings.local_fallback_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": max(max_tokens, settings.local_max_output_tokens),
        "response_format": {"type": "json_object"},
    }
    if settings.local_disable_thinking:
        # Ignored by templates that don't understand it, decisive for those that
        # do -- see settings.local_disable_thinking for why this isn't optional
        # in practice.
        body_out["chat_template_kwargs"] = {"enable_thinking": False}
    payload = json.dumps(body_out).encode()
    req = urllib.request.Request(
        f"{settings.local_fallback_url}/chat/completions",
        data=payload, headers={"Content-Type": "application/json"}, method="POST",
    )
    started = datetime.now(UTC)
    try:
        with urllib.request.urlopen(req, timeout=120.0) as resp:
            body = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError(f"local LLM at {settings.local_fallback_url} unreachable: {e}") from e
    latency_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)

    choice = body["choices"][0]
    content = choice["message"].get("content") or ""
    if not content.strip():
        # A reasoning model that ran out of budget mid-thought returns an empty
        # answer, and json.loads would report only "Expecting value: line 1
        # column 1" -- true, useless, and nothing to act on.
        reasoning = len(choice["message"].get("reasoning_content") or "")
        raise RuntimeError(
            f"local LLM returned no answer (finish_reason={choice.get('finish_reason')!r}, "
            f"{reasoning} characters of reasoning). It spent the whole output budget "
            "thinking. Set LOCAL_DISABLE_THINKING=true or raise LOCAL_MAX_OUTPUT_TOKENS."
        )
    return json.loads(content), latency_ms


def complete_json(system_prompt: str, user_prompt: str, max_tokens: int | None = None) -> tuple[dict, int]:
    budget = max_tokens or settings.llm_max_output_tokens
    if settings.groq_env_key:
        return _complete_json_groq(system_prompt, user_prompt, budget)
    if settings.local_fallback_model:
        return _complete_json_local(system_prompt, user_prompt, budget)
    raise LLMNotConfigured("no GROQ_ENV_KEY and no LOCAL_FALLBACK_MODEL configured")


def _prompt_hash(model: str, system_prompt: str, user_prompt: str) -> str:
    return hashlib.sha256(f"{model}\n{system_prompt}\n{user_prompt}".encode()).hexdigest()


def cached_complete_json(system_prompt: str, user_prompt: str) -> tuple[dict, int, bool]:
    """Returns (result, latency_ms, cache_hit). Cached by (model, prompt_hash) per
    the project invariant -- a cache hit costs nothing and reports latency_ms=0."""
    model = active_model()
    prompt_hash = _prompt_hash(model, system_prompt, user_prompt)

    with connect() as conn:
        row = conn.execute(
            "SELECT response FROM llm_cache WHERE model = ? AND prompt_hash = ?",
            (model, prompt_hash),
        ).fetchone()
    if row:
        return json.loads(row["response"]), 0, True

    result, latency_ms = complete_json(system_prompt, user_prompt)

    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO llm_cache (id, model, prompt_hash, response, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (new_id(), model, prompt_hash, json.dumps(result), datetime.now(UTC).isoformat()),
        )
    return result, latency_ms, False
