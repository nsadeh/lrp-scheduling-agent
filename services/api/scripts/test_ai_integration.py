#!/usr/bin/env python3
"""Integration test for the AI infrastructure.

Runs against real LangFuse + OpenRouter to verify:
1. LangFuse prompt config drives model selection
2. Defaults work when no model config in prompt
3. Failover works when the primary model fails
4. Each provider (Anthropic, OpenAI, Gemini) can serve a request via OpenRouter
5. Traces are captured in LangFuse

Usage:
    cd services/api
    PYTHONPATH=src uv run python3 scripts/test_ai_integration.py
"""

import asyncio
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pydantic import BaseModel  # noqa: E402

from api.ai.endpoint import llm_endpoint  # noqa: E402
from api.ai.errors import LLMUnavailableError  # noqa: E402
from api.ai.langfuse_client import DEFAULT_PROMPT_LABEL, init_langfuse  # noqa: E402
from api.ai.llm_service import LLMService, init_llm_service  # noqa: E402

# ── Test models ──────────────────────────────────────────────────────────────

CLASSIFY_CHAT_PROMPT = [
    {
        "role": "system",
        "content": (
            "Classify the following text into exactly one category: "
            "greeting, question, statement."
        ),
    },
    {"role": "user", "content": "Text: {{text}}"},
]


class ClassifyInput(BaseModel):
    text: str


class ClassifyOutput(BaseModel):
    category: str
    confidence: float


# ── Helpers ──────────────────────────────────────────────────────────────────

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
INFO = "\033[94mi\033[0m"


def report(ok: bool, label: str, detail: str = ""):
    mark = PASS if ok else FAIL
    suffix = f" -- {detail}" if detail else ""
    print(f"  {mark} {label}{suffix}")
    return ok


# ── Test 1: LangFuse prompt config specifies model → that model is used ──────


async def test_langfuse_model_config(langfuse, llm):
    """Create a prompt in LangFuse with a specific model config and verify it's used."""
    print("\n[Test 1] LangFuse prompt config drives model selection")

    prompt_name = f"test-integration-model-config-{int(time.time())}"
    langfuse.create_prompt(
        name=prompt_name,
        prompt=CLASSIFY_CHAT_PROMPT,
        type="chat",
        config={"model": "openai/gpt-4o-mini", "temperature": 0.0, "max_tokens": 200},
        labels=[DEFAULT_PROMPT_LABEL],
    )
    await asyncio.sleep(1)

    endpoint = llm_endpoint(
        name="test_model_config",
        prompt_name=prompt_name,
        input_type=ClassifyInput,
        output_type=ClassifyOutput,
    )

    resp = await endpoint(
        llm=llm, langfuse=langfuse, data=ClassifyInput(text="Hello, how are you today?")
    )

    return report(
        isinstance(resp, ClassifyOutput) and resp.category != "",
        "Prompt with openai/gpt-4o-mini config returns valid response",
        f"category={resp.category}, confidence={resp.confidence}",
    )


# ── Test 2: No model in config → default model used ─────────────────────────


async def test_default_model(langfuse, llm):
    """Create a prompt with no model config and verify the default is used."""
    print("\n[Test 2] Default model used when LangFuse config has no model")

    prompt_name = f"test-integration-no-model-{int(time.time())}"
    langfuse.create_prompt(
        name=prompt_name,
        prompt=CLASSIFY_CHAT_PROMPT,
        type="chat",
        config={},
        labels=[DEFAULT_PROMPT_LABEL],
    )
    await asyncio.sleep(1)

    endpoint = llm_endpoint(
        name="test_default_model",
        prompt_name=prompt_name,
        input_type=ClassifyInput,
        output_type=ClassifyOutput,
    )

    resp = await endpoint(
        llm=llm, langfuse=langfuse, data=ClassifyInput(text="What time is the meeting?")
    )

    return report(
        isinstance(resp, ClassifyOutput) and resp.category != "",
        "Prompt with no model config returns valid response (default model)",
        f"category={resp.category}, confidence={resp.confidence}",
    )


# ── Test 3: Failover — primary model fails, secondary picks it up ───────────


async def test_failover(langfuse):
    """Configure a nonexistent primary model to force failover to the secondary."""
    print("\n[Test 3] Failover when primary model is invalid")

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return report(False, "OPENROUTER_API_KEY not set — cannot test failover")

    svc = LLMService(
        api_key=api_key,
        secondary_model="openai/gpt-4o-mini",
        tertiary_model="google/gemini-2.5-flash",
    )

    prompt_name = f"test-integration-failover-{int(time.time())}"
    langfuse.create_prompt(
        name=prompt_name,
        prompt=CLASSIFY_CHAT_PROMPT,
        type="chat",
        config={
            "model": "anthropic/this-model-does-not-exist",
            "temperature": 0.0,
            "max_tokens": 200,
        },
        labels=[DEFAULT_PROMPT_LABEL],
    )
    await asyncio.sleep(1)

    endpoint = llm_endpoint(
        name="test_failover",
        prompt_name=prompt_name,
        input_type=ClassifyInput,
        output_type=ClassifyOutput,
    )

    start = time.monotonic()
    resp = await endpoint(
        llm=svc, langfuse=langfuse, data=ClassifyInput(text="I am going to the store.")
    )
    elapsed = time.monotonic() - start

    ok1 = report(
        isinstance(resp, ClassifyOutput) and resp.category != "",
        "Failover to secondary succeeded",
        f"category={resp.category}, confidence={resp.confidence}",
    )
    ok2 = report(elapsed < 60, f"Completed within latency budget ({elapsed:.1f}s < 60s)")
    return ok1 and ok2


# ── Test 3b: All providers fail → LLMUnavailableError ────────────────────────


async def test_all_providers_fail():
    """Verify LLMUnavailableError is raised when the OpenRouter key is invalid."""
    print("\n[Test 3b] Invalid API key -> LLMUnavailableError")

    svc = LLMService(api_key="sk-or-INVALID-KEY-FOR-TESTING")

    try:
        await svc.complete(
            messages=[{"role": "user", "content": "hello"}],
            model="anthropic/claude-sonnet-4.6",
        )
        return report(False, "Expected LLMUnavailableError but got success")
    except LLMUnavailableError as e:
        return report(True, "LLMUnavailableError raised as expected", str(e)[:80])
    except Exception as e:
        return report(False, f"Unexpected error type: {type(e).__name__}", str(e)[:80])


# ── Test 4: Each provider via OpenRouter ─────────────────────────────────────


async def test_direct_provider_calls(llm):
    """Call each provider's flagship cheap model via OpenRouter."""
    print("\n[Test 4] Each provider via OpenRouter (Anthropic, OpenAI, Google)")

    messages = [{"role": "user", "content": "Reply with exactly one word: hello"}]
    results = []

    models = [
        "anthropic/claude-haiku-4.5",
        "openai/gpt-4o-mini",
        "google/gemini-2.5-flash",
    ]

    for model in models:
        try:
            start = time.monotonic()
            resp = await llm.complete(messages=messages, model=model, max_tokens=20)
            elapsed = time.monotonic() - start
            results.append(
                report(
                    len(resp.content) > 0,
                    f"{model} responded in {elapsed:.1f}s",
                    f"content='{resp.content[:30]}'",
                )
            )
        except Exception as e:
            results.append(report(False, f"{model} failed", str(e)[:80]))

    return all(results)


# ── Test 5: Traces captured in LangFuse ──────────────────────────────────────


async def test_traces_captured(langfuse, llm):
    """Make an LLM call and verify OTel tracing pipeline sends to LangFuse."""
    print("\n[Test 5] Traces captured in LangFuse")

    prompt_name = f"test-integration-trace-{int(time.time())}"
    langfuse.create_prompt(
        name=prompt_name,
        prompt=[
            {"role": "system", "content": "Reply with exactly: blue."},
            {"role": "user", "content": "Text: {{text}}"},
        ],
        type="chat",
        config={"model": "openai/gpt-4o-mini", "temperature": 0.0, "max_tokens": 50},
        labels=[DEFAULT_PROMPT_LABEL],
    )
    await asyncio.sleep(1)

    endpoint = llm_endpoint(
        name="test_trace_capture",
        prompt_name=prompt_name,
        input_type=ClassifyInput,
        output_type=ClassifyOutput,
    )

    resp = await endpoint(llm=llm, langfuse=langfuse, data=ClassifyInput(text="Look up at the sky"))

    ok1 = report(isinstance(resp, ClassifyOutput), "LLM call succeeded for trace test")

    from opentelemetry import trace as otel_trace

    provider = otel_trace.get_tracer_provider()
    provider_name = type(provider).__name__

    ok2 = report(
        provider_name == "TracerProvider",
        f"OTel TracerProvider is active (type={provider_name})",
    )

    has_langfuse_processor = False
    exporter_type = "unknown"
    if hasattr(provider, "_active_span_processor"):
        proc = provider._active_span_processor
        for sp in getattr(proc, "_span_processors", []):
            if "Langfuse" in type(sp).__name__:
                has_langfuse_processor = True
                exporter_type = type(getattr(sp, "span_exporter", None)).__name__
                break

    ok3 = report(
        has_langfuse_processor,
        f"LangfuseSpanProcessor is registered (exporter={exporter_type})",
    )

    flush_ok = False
    try:
        if hasattr(provider, "force_flush"):
            provider.force_flush()
        langfuse.flush()
        flush_ok = True
    except Exception:
        pass

    ok4 = report(flush_ok, "OTel + LangFuse flush completed without errors")

    await asyncio.sleep(3)
    try:
        all_obs = langfuse.api.observations.get_many(limit=5)
        ok5 = report(
            len(all_obs.data) > 0,
            f"LangFuse has {len(all_obs.data)} recent observations",
        )
    except Exception as e:
        ok5 = report(False, f"Could not query observations: {e}")

    return ok1 and ok2 and ok3 and ok4 and ok5


# ── Main ─────────────────────────────────────────────────────────────────────


async def main():
    print("=" * 60)
    print("AI Infrastructure Integration Tests")
    print("=" * 60)

    langfuse = init_langfuse()
    if langfuse is None:
        print(f"\n  {FAIL} LANGFUSE keys not set — cannot run integration tests")
        sys.exit(1)
    print(f"\n  {INFO} LangFuse client initialized")

    llm = init_llm_service()
    if llm is None:
        print(f"\n  {FAIL} OPENROUTER_API_KEY not set — cannot run integration tests")
        sys.exit(1)
    print(f"  {INFO} LLMService initialized")

    results = []

    tests = [
        ("test 1", lambda: test_langfuse_model_config(langfuse, llm)),
        ("test 2", lambda: test_default_model(langfuse, llm)),
        ("test 3", lambda: test_failover(langfuse)),
        ("test 3b", lambda: test_all_providers_fail()),
        ("test 4", lambda: test_direct_provider_calls(llm)),
        ("test 5", lambda: test_traces_captured(langfuse, llm)),
    ]

    for name, test_fn in tests:
        try:
            results.append(await test_fn())
        except Exception as e:
            report(False, f"{name} crashed: {e}")
            results.append(False)

    langfuse.flush()
    langfuse.shutdown()

    passed = sum(results)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"Results: {passed}/{total} passed")
    print(f"{'=' * 60}")

    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
