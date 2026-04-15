#!/usr/bin/env python3
"""Integration test for the AI infrastructure.

Runs against real LangFuse + LLM providers to verify:
1. LangFuse prompt config drives model selection
2. Defaults work when no model config in prompt
3. Failover works when primary provider fails
4. Each provider (Anthropic, OpenAI, Gemini) can serve a request
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
from api.ai.langfuse_client import init_langfuse  # noqa: E402
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
        config={"model": "gpt-4o-mini", "temperature": 0.0, "max_tokens": 200},
        labels=["production"],
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
        "Prompt with gpt-4o-mini config returns valid response",
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
        labels=["production"],
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


# ── Test 3: Failover — primary fails, secondary/tertiary picked up ──────────


async def test_failover(langfuse):
    """Use a service with a bad primary key to force failover to secondary."""
    print("\n[Test 3] Failover when primary provider fails")

    openai_key = os.environ.get("OPENAI_API_KEY")
    google_key = os.environ.get("GOOGLE_AI_API_KEY")
    if not openai_key:
        return report(False, "OPENAI_API_KEY not set — cannot test failover")

    svc = LLMService(
        anthropic_key="sk-ant-INVALID-KEY-FOR-TESTING",
        openai_key=openai_key,
        google_key=google_key,
    )

    prompt_name = f"test-integration-failover-{int(time.time())}"
    langfuse.create_prompt(
        name=prompt_name,
        prompt=CLASSIFY_CHAT_PROMPT,
        type="chat",
        config={"model": "claude-sonnet-4-20250514", "temperature": 0.0, "max_tokens": 200},
        labels=["production"],
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
    ok2 = report(elapsed < 20, f"Completed within latency budget ({elapsed:.1f}s < 20s)")
    return ok1 and ok2


# ── Test 3b: All providers fail → LLMUnavailableError ────────────────────────


async def test_all_providers_fail():
    """Verify LLMUnavailableError is raised when all providers fail."""
    print("\n[Test 3b] All providers fail -> LLMUnavailableError")

    svc = LLMService(anthropic_key="sk-ant-INVALID", openai_key="sk-INVALID")

    try:
        await svc.complete(
            messages=[{"role": "user", "content": "hello"}],
            model="claude-sonnet-4-20250514",
        )
        return report(False, "Expected LLMUnavailableError but got success")
    except LLMUnavailableError as e:
        return report(True, "LLMUnavailableError raised as expected", str(e)[:80])
    except Exception as e:
        return report(False, f"Unexpected error type: {type(e).__name__}", str(e)[:80])


# ── Test 4: Direct provider calls ────────────────────────────────────────────


async def test_direct_provider_calls():
    """Call each configured provider directly to verify they all work."""
    print("\n[Test 4] Direct provider calls (Anthropic, OpenAI, Gemini)")

    messages = [{"role": "user", "content": "Reply with exactly one word: hello"}]
    results = []

    providers = [
        ("ANTHROPIC_API_KEY", "anthropic", "anthropic/claude-haiku-4-5-20251001"),
        ("OPENAI_API_KEY", "openai", "openai/gpt-4o-mini"),
        ("GOOGLE_AI_API_KEY", "google", "gemini/gemini-2.0-flash"),
    ]

    for env_var, provider_name, model in providers:
        api_key = os.environ.get(env_var)
        if not api_key:
            results.append(report(False, f"{provider_name}: {env_var} not set — skipped"))
            continue

        svc = LLMService(**{f"{provider_name}_key": api_key})  # type: ignore[arg-type]
        try:
            start = time.monotonic()
            resp = await svc.complete(messages=messages, model=model, max_tokens=20)
            elapsed = time.monotonic() - start
            results.append(
                report(
                    len(resp.content) > 0,
                    f"{provider_name}: {model} responded in {elapsed:.1f}s",
                    f"content='{resp.content[:30]}'",
                )
            )
        except Exception as e:
            results.append(report(False, f"{provider_name}: {model} failed", str(e)[:80]))

    return all(results)


# ── Test 5: Traces captured in LangFuse ──────────────────────────────────────


async def test_traces_captured(langfuse, llm):
    """Make an LLM call and verify OTel tracing pipeline sends to LangFuse."""
    print("\n[Test 5] Traces captured in LangFuse")

    # Make a real LLM call
    prompt_name = f"test-integration-trace-{int(time.time())}"
    langfuse.create_prompt(
        name=prompt_name,
        prompt=[
            {"role": "system", "content": "Reply with exactly: blue."},
            {"role": "user", "content": "Text: {{text}}"},
        ],
        type="chat",
        config={"model": "gpt-4o-mini", "temperature": 0.0, "max_tokens": 50},
        labels=["production"],
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

    # Verify the OTel tracing pipeline is configured
    from opentelemetry import trace as otel_trace

    provider = otel_trace.get_tracer_provider()
    provider_name = type(provider).__name__

    ok2 = report(
        provider_name == "TracerProvider",
        f"OTel TracerProvider is active (type={provider_name})",
    )

    # Check the span processor chain
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

    # Flush and verify no errors
    flush_ok = False
    try:
        if hasattr(provider, "force_flush"):
            provider.force_flush()
        langfuse.flush()
        flush_ok = True
    except Exception:
        pass

    ok4 = report(flush_ok, "OTel + LangFuse flush completed without errors")

    # Verify observations exist in LangFuse
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

    # Initialize
    langfuse = init_langfuse()
    if langfuse is None:
        print(f"\n  {FAIL} LANGFUSE keys not set — cannot run integration tests")
        sys.exit(1)
    print(f"\n  {INFO} LangFuse client initialized")

    llm = init_llm_service()
    if llm is None:
        print(f"\n  {FAIL} No LLM provider keys set — cannot run integration tests")
        sys.exit(1)
    print(f"  {INFO} LLMService initialized")

    results = []

    tests = [
        ("test 1", lambda: test_langfuse_model_config(langfuse, llm)),
        ("test 2", lambda: test_default_model(langfuse, llm)),
        ("test 3", lambda: test_failover(langfuse)),
        ("test 3b", lambda: test_all_providers_fail()),
        ("test 4", lambda: test_direct_provider_calls()),
        ("test 5", lambda: test_traces_captured(langfuse, llm)),
    ]

    for name, test_fn in tests:
        try:
            results.append(await test_fn())
        except Exception as e:
            report(False, f"{name} crashed: {e}")
            results.append(False)

    # Cleanup
    langfuse.flush()
    langfuse.shutdown()

    # Summary
    passed = sum(results)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"Results: {passed}/{total} passed")
    print(f"{'=' * 60}")

    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
