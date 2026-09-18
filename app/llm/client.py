import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime

from groq import APIConnectionError, Groq, RateLimitError

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


# Hosted models whose budget is gone for the day. Waiting does not help and every
# later call to that model would fail the same way, so it leaves the rotation for
# the rest of the process rather than being re-learned per call.
_exhausted_models: set[str] = set()

# A per-minute limit recovers on its own, so the model is not retired -- it is
# stood down until its own reset time. Without this, every later call repeated
# the same multi-minute wait before falling back: one observed proposal took 338
# seconds, and a hundred of them would have taken hours. The first call pays the
# wait, discovers the limit, and the rest go straight to the next model until the
# window reopens.
_cooldown_until: dict[str, float] = {}


def groq_chain() -> list[str]:
    """Hosted models to try, in order, skipping any already out of budget."""
    if not settings.groq_env_key:
        return []
    ordered = [settings.groq_model] + [
        m.strip() for m in settings.groq_fallback_models.split(",") if m.strip()
    ]
    now = time.monotonic()
    seen, chain = set(), []
    for model in ordered:
        if not model or model in seen or model in _exhausted_models:
            continue
        if _cooldown_until.get(model, 0.0) > now:
            continue
        seen.add(model)
        chain.append(model)
    return chain


def groq_available() -> bool:
    return bool(groq_chain())


# Long enough to stop thrashing, short enough that a model is not lost for a run.
_MAX_COOLDOWN_SECONDS = 300.0

# Above this, switching models beats waiting.
_MAX_INLINE_WAIT_SECONDS = 10.0


def _is_daily_quota(error: Exception) -> bool:
    """A per-day cap, as opposed to a per-minute one. The per-minute limits are
    worth waiting out; the daily one is not -- it is gone until tomorrow."""
    message = str(error).lower()
    return "per day" in message or "tpd" in message or "rpd" in message


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
    cache-collided under the Groq model's name. It follows the same fallback the
    calls do, so a result cached after a fallback is keyed to what produced it."""
    chain = groq_chain()
    if chain:
        return chain[0]
    if settings.local_fallback_model:
        return f"local:{settings.local_fallback_model}"
    return "none"


def _complete_json_groq(system_prompt: str, user_prompt: str, max_tokens: int,
                        model: str | None = None) -> tuple[dict, int]:
    """Bounded retry on 429 only -- a rate limit is transient and expected under
    load (this project's own mapping stage can fire dozens of calls in quick
    succession); any other error is a real failure and must not be retried away."""
    client = Groq(api_key=settings.groq_env_key, timeout=60.0)
    started = datetime.now(UTC)
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=model or settings.groq_model,
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
            delay = _rate_limit_delay(e, attempt)
            # Waiting minutes here is the wrong trade when the caller has two
            # more hosted models and a local one available. A short wait is
            # cheaper than a switch; a long one is not, so hand the call back and
            # let complete_json move down the chain and stand this model down.
            # Measured before this: a single proposal took 338 seconds, and a
            # hundred of them would have taken hours.
            if delay > _MAX_INLINE_WAIT_SECONDS:
                raise
            time.sleep(delay)
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
    """Hosted model first, local model if the hosted one cannot serve the call.

    The local model is named `local_fallback_model` but was never actually a
    fallback: with a key set, a hosted outage or an exhausted daily quota failed
    the whole run while a working model sat idle on localhost. It now falls back
    for the two failures a fallback is for -- the provider being unreachable, and
    the account being out of budget for the day. A per-minute rate limit is still
    waited out rather than escaped, because that one resolves by itself, and a
    bad request still fails loudly rather than being retried elsewhere.
    """
    budget = max_tokens or settings.llm_max_output_tokens
    chain = groq_chain()

    for position, model in enumerate(chain):
        try:
            return _complete_json_groq(system_prompt, user_prompt, budget, model=model)
        except (RateLimitError, APIConnectionError) as exc:
            # A model out of budget for the day leaves the rotation for the rest
            # of the process; an unreachable provider takes all of them out,
            # since the next one is the same endpoint. "Request too large" is a
            # property of one request, so that model stays in rotation and only
            # this call moves on.
            if isinstance(exc, APIConnectionError):
                _exhausted_models.update(chain)
            elif _is_daily_quota(exc):
                _exhausted_models.add(model)
            else:
                # A per-minute limit. Stand this model down until its own reset
                # time rather than rediscovering the limit on every call.
                wait = min(_rate_limit_delay(exc, 0), _MAX_COOLDOWN_SECONDS)
                _cooldown_until[model] = time.monotonic() + wait
                print(f"{model} rate limited; standing it down for {wait:.0f}s",
                      file=sys.stderr)

            remaining = chain[position + 1:] if not isinstance(exc, APIConnectionError) else []
            next_model = (remaining[0] if remaining
                          else settings.local_fallback_model or None)
            if next_model is None:
                raise
            print(f"{model} unavailable ({type(exc).__name__}); trying {next_model}",
                  file=sys.stderr)

    if settings.local_fallback_model:
        return _complete_json_local(system_prompt, user_prompt, budget)
    raise LLMNotConfigured("no GROQ_ENV_KEY and no LOCAL_FALLBACK_MODEL configured")


def complete_json_reported(system_prompt: str, user_prompt: str, max_tokens: int | None = None,
                           report=None) -> tuple[dict, int, str]:
    """complete_json, plus a note when the chain moves and when a call fails.

    The routing decision is made down here but it is only interesting up there,
    where a run_id exists and frames can be written. Rather than thread a run_id
    through every model call, the caller passes a reporter and this tells it the
    two things worth seeing: that a model dropped out and which one took over, or
    that the call failed outright. Returns the model that actually answered.
    """
    before = active_model()
    try:
        result, latency_ms = complete_json(system_prompt, user_prompt, max_tokens)
    except Exception as exc:
        if report:
            report("llm_failed", f"{before} could not answer: {type(exc).__name__}: {exc}"[:300])
        raise

    after = active_model()
    if report and after != before:
        report("llm_switch", f"{before} is out of budget — now using {after}")
    return result, latency_ms, after


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
