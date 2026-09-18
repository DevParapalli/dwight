from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    db_path: Path = Path("data/dwight.db")
    schema_path: Path = Path("schemas/employee.v1.yaml")
    policy_path: Path = Path("policy/escalation.yaml")

    # Testing-only. /nuke empties every table in one call, which is exactly what
    # you want between test runs and never what you want otherwise, so it is off
    # unless asked for and the route does not exist when it is off.
    enable_nuke: bool = False

    groq_env_key: str | None = None
    groq_model: str = "qwen/qwen3.8-27b"
    # Groq rejects a request outright when its *expected* output exceeds the
    # account's output-tokens-per-minute ceiling, so the cap has to be explicit
    # and the value-normalization batch small enough to fit under it. Both are
    # settings rather than constants because the right numbers depend entirely
    # on the provider tier in use.
    # Groq's ceiling is not a preference, it is the account tier: a request whose
    # *expected* output exceeds the org's output-tokens-per-minute limit is
    # rejected outright with "request too large", which no retry can fix. On a
    # 1000 OTPM tier these are close to the maximum that works. Raising them
    # needs a tier upgrade, not a bigger number here.
    llm_max_output_tokens: int = 900
    llm_value_batch_size: int = 15

    # The target system runs as its own process (tools/mock_target_api.py), so
    # the agent talks to it over real HTTP rather than calling into itself.
    target_api_url: str = "http://127.0.0.1:8900/v1"
    # llama.cpp's llama-server, OpenAI-compatible endpoint, confirmed live at M5.
    local_fallback_model: str = ""
    local_fallback_url: str = "http://localhost:8080/v1"
    # Reasoning models spend the output budget thinking before they answer. On a
    # batched JSON classification qwen3-8b burned 4096 tokens on 16k characters
    # of reasoning and still emitted nothing; with thinking off the same call
    # answers in 336 tokens and 7.6s. Raising the budget does not fix it.
    local_disable_thinking: bool = True
    # Local inference is not metered, so these are set generously. max_tokens is
    # a ceiling, not a reservation -- a model that stops early costs nothing for
    # headroom it didn't use -- and the bigger batch means far fewer round trips
    # than the Groq path can afford. This restores PLAN.md's "up to fifty per
    # call" for the backend that can actually do it.
    local_max_output_tokens: int = 8192
    local_value_batch_size: int = 50


settings = Settings()
