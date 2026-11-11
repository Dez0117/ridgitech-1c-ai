import os

from langchain_openrouter import ChatOpenRouter
from openrouter import OpenRouter


def create_qwen_model(*, model=None, temperature=0.2, max_tokens=4096, reasoning="none", seed=42):
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise ValueError("Задайте OPENROUTER_API_KEY в .env или переменных окружения.")
    return ChatOpenRouter(
        # The SDK otherwise supplies its own retries even when max_retries=0.
        client=OpenRouter(api_key=key, timeout_ms=60_000, retry_config=None),
        model=model or os.environ.get("QWEN_MODEL", "qwen/qwen3.8-27b"),
        temperature=temperature, max_tokens=max_tokens, seed=seed,
        reasoning={"effort": reasoning},
        openrouter_provider={"require_parameters": True},
        max_retries=0, timeout=60_000,
    )
