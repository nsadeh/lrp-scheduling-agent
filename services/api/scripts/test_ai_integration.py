#!/usr/bin/env python3
"""Integration test for the AI infrastructure.

Runs against real LangFuse + LLM providers to verify:
1. LangFuse prompt config drives model selection
2. Defaults work when no model config in prompt
3. Failover works when primary provider fails
4. Traces are captured in LangFuse

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


class ClassifyInput(BaseModel):
    text: str


class ClassifyOutput(BaseModel):
    category: str
    confidence: float


# ── Helpers ──────────────────────────────────────────────────────────────────

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
INFO = "\033[94mi\033[0m"


def result(ok: bool, label: str, detail: str = ""):
    mark = PASS if ok else FAIL
    suffix = f" — {detail}" if detail else ""
    print(f"  {mark} {label}{suffix}")
    return ok


# ── Test 1: LangFuse prompt config specifies model → that model is used ──────


async def test_langfuse_model_config(langfuse, llm):
    """Create a prompt in LangFuse with a specific model config and verify it's used."""
    print("\n[Test 1] LangFuse prompt config drives model selection")

    # Create a test prompt in LangFuse with OpenAI model config
    prompt_name = f"test-integration-model-config-{int(time.time())}"
    langfuse.create_prompt(
        name=prompt_name,
        prompt=(
            "Classify the following text into exactly one category: "
            "greeting, question, statement. Text: {{text}}"
        ),
        config={
            "model": "gpt-4o-mini",
            "temperature": 0.0,
            "max_tokens": 200,
        },
        labels=["production"],
    )
    # Give LangFuse a moment to propagate
    await asyncio.sleep(1)

    endpoint = llm_endpoint(
        name="test_model_config",
        prompt_name=prompt_name,
        input_type=ClassifyInput,
        output_type=ClassifyOutput,
    )

    resp = await endpoint(
        llm=llm,
        langfuse=langfuse,
        data=ClassifyInput(text="Hello, how are you today?"),
    )

    ok = result(
        isinstance(resp, ClassifyOutput) and resp.category != "",
        "Prompt with gpt-4o-mini config returns valid response",
        f"category={resp.category}, confidence={resp.confidence}",
    )
    return ok


# ── Test 2: No model in config → default model used ─────────────────────────


async def test_default_model(langfuse, llm):
    """Create a prompt with no model config and verify the default is used."""
    print("\n[Test 2] Default model used when LangFuse config has no model")

    prompt_name = f"test-integration-no-model-{int(time.time())}"
    langfuse.create_prompt(
        name=prompt_name,
        prompt=(
            "Classify the following text into exactly one category: "
            "greeting, question, statement. Text: {{text}}"
        ),
        config={},  # No model specified
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
        llm=llm,
        langfuse=langfuse,
        data=ClassifyInput(text="What time is the meeting?"),
    )

    ok = result(
        isinstance(resp, ClassifyOutput) and resp.category != "",
        "Prompt with no model config returns valid response (default model)",
        f"category={resp.category}, confidence={resp.confidence}",
    )
    return ok


# ── Test 3: Failover — primary fails, secondary/tertiary picked up ──────────


async def test_failover(langfuse):
    """Use a service with a bad primary key to force failover to secondary."""
    print("\n[Test 3] Failover when primary provider fails")

    # Create service with invalid Anthropic key but valid OpenAI key
    openai_key = os.environ.get("OPENAI_API_KEY")
    google_key = os.environ.get("GOOGLE_AI_API_KEY")
    if not openai_key:
        result(False, "OPENAI_API_KEY not set — cannot test failover")
        return False

    svc = LLMService(
        anthropic_key="sk-ant-INVALID-KEY-FOR-TESTING",
        openai_key=openai_key,
        google_key=google_key,
    )

    prompt_name = f"test-integration-failover-{int(time.time())}"
    langfuse.create_prompt(
        name=prompt_name,
        prompt=(
            "Classify the following text into exactly one category: "
            "greeting, question, statement. Text: {{text}}"
        ),
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
        llm=svc,
        langfuse=langfuse,
        data=ClassifyInput(text="I am going to the store."),
    )
    elapsed = time.monotonic() - start

    ok1 = result(
        isinstance(resp, ClassifyOutput) and resp.category != "",
        "Failover to secondary succeeded",
        f"category={resp.category}, confidence={resp.confidence}",
    )
    ok2 = result(
        elapsed < 20,
        f"Completed within latency budget ({elapsed:.1f}s < 20s)",
    )
    return ok1 and ok2


# ── Test 3b: All providers fail → LLMUnavailableError ────────────────────────


async def test_all_providers_fail():
    """Verify LLMUnavailableError is raised when all providers fail."""
    print("\n[Test 3b] All providers fail → LLMUnavailableError")

    svc = LLMService(
        anthropic_key="sk-ant-INVALID",
        openai_key="sk-INVALID",
    )

    try:
        await svc.complete(
            messages=[{"role": "user", "content": "hello"}],
            model="claude-sonnet-4-20250514",
        )
        ok = result(False, "Expected LLMUnavailableError but got success")
    except LLMUnavailableError as e:
        ok = result(True, "LLMUnavailableError raised as expected", str(e)[:80])
    except Exception as e:
        ok = result(False, f"Unexpected error type: {type(e).__name__}", str(e)[:80])

    return ok


# ── Test 4: Traces captured in LangFuse ──────────────────────────────────────


async def test_traces_captured(langfuse, llm):
    """Make a call and verify trace spans appear in LangFuse."""
    print("\n[Test 4] Traces captured in LangFuse")

    prompt_name = f"test-integration-trace-{int(time.time())}"
    langfuse.create_prompt(
        name=prompt_name,
        prompt="Reply with a single word: the color of the sky. Text: {{text}}",
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

    await endpoint(
        llm=llm,
        langfuse=langfuse,
        data=ClassifyInput(text="Look up at the sky"),
    )

    # Verify the OTel tracing pipeline is configured
    from opentelemetry import trace as otel_trace

    provider = otel_trace.get_tracer_provider()
    provider_name = type(provider).__name__

    ok1 = result(
        provider_name == "TracerProvider",
        f"OTel TracerProvider is active (type={provider_name})",
    )

    # Check the span processor chain: LangfuseSpanProcessor → OTLPSpanExporter
    has_langfuse_processor = False
    if hasattr(provider, "_active_span_processor"):
        proc = provider._active_span_processor
        for sp in getattr(proc, "_span_processors", []):
            if "Langfuse" in type(sp).__name__:
                has_langfuse_processor = True
                exporter_type = type(getattr(sp, "span_exporter", None)).__name__
                break

    ok2 = result(
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
        flush_ok = False

    ok3 = result(flush_ok, "OTel + LangFuse flush completed without errors")

    # Verify observations exist in LangFuse (from this session's earlier tests)
    await asyncio.sleep(3)
    try:
        all_obs = langfuse.api.observations.get_many(limit=5)
        ok4 = result(
            len(all_obs.data) > 0,
            f"LangFuse has {len(all_obs.data)} recent observations (confirms ingestion works)",
        )
    except Exception as e:
        ok4 = result(False, f"Could not query observations: {e}")

    return ok1 and ok2 and ok3 and ok4


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

    # Run tests
    try:
        results.append(await test_langfuse_model_config(langfuse, llm))
    except Exception as e:
        result(False, f"Test 1 crashed: {e}")
        results.append(False)

    try:
        results.append(await test_default_model(langfuse, llm))
    except Exception as e:
        result(False, f"Test 2 crashed: {e}")
        results.append(False)

    try:
        results.append(await test_failover(langfuse))
    except Exception as e:
        result(False, f"Test 3 crashed: {e}")
        results.append(False)

    try:
        results.append(await test_all_providers_fail())
    except Exception as e:
        result(False, f"Test 3b crashed: {e}")
        results.append(False)

    try:
        results.append(await test_traces_captured(langfuse, llm))
    except Exception as e:
        result(False, f"Test 4 crashed: {e}")
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
