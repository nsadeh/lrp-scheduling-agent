"""Deterministic eval: run scheduling-new-loop-classifier (latest version)
against every item in the 'Loop creation (XML)' dataset and compare against
the cleaned expected outputs.

Phase 1 — cleanup
  Strip suggestions whose action is not in the loop classifier's allowed set
  ({create_loop, link_thread, no_action}) from each item's expected_output.
  The legacy expecteds carried draft_email suggestions emitted by an older
  classifier that handled drafting too — v23 cannot emit those.

Phase 2 — eval
  For each item, run classify_new_thread with the prompt's default config
  (temperature stays at 0.0 — see api.ai.llm_service.complete). Compare
  actual vs cleaned-expected:
    1. Same number of suggestions.
    2. Order-independent multiset match on (action, target_loop_id, action_data).
       action_data is compared as a subset — every key/value in expected must
       appear in actual; extra keys in actual don't fail the match. This
       accommodates expecteds that were authored with a subset of the
       create_loop schema (e.g. only cm + candidate, no client name).

Run from repo root:
    cd services/api && uv run python scripts/eval_loop_classifier_xml.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from langfuse import Langfuse  # noqa: E402
from langfuse.model import ChatPromptClient  # noqa: E402

from api.ai.langfuse_client import fetch_prompt  # noqa: E402
from api.ai.llm_service import DEFAULT_MODEL, init_llm_service  # noqa: E402
from api.classifier.agent_runtime import parse_suggestions_envelope  # noqa: E402
from api.classifier.schemas import LoopClassifierInput  # noqa: E402

DATASET = "Loop creation (XML)"
ALLOWED_ACTIONS = {"create_loop", "link_thread", "no_action"}


# ---------------------------------------------------------------------------
# Phase 1 — clean expected_output
# ---------------------------------------------------------------------------


def clean_expected(expected: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Return (cleaned, n_stripped). Keep only loop-classifier-allowed actions."""
    if not expected or "suggestions" not in expected:
        return expected, 0
    original = expected["suggestions"]
    kept = [s for s in original if s.get("action") in ALLOWED_ACTIONS]
    return {**expected, "suggestions": kept}, len(original) - len(kept)


def cleanup_dataset(lf: Langfuse) -> None:
    ds = lf.get_dataset(DATASET)
    total_stripped = 0
    items_changed = 0
    for item in ds.items:
        cleaned, stripped = clean_expected(item.expected_output or {})
        if stripped == 0:
            continue
        lf.create_dataset_item(
            dataset_name=DATASET,
            id=item.id,
            input=item.input,
            expected_output=cleaned,
            metadata=item.metadata,
        )
        total_stripped += stripped
        items_changed += 1
        print(f"  [cleanup] item {item.id}: stripped {stripped} non-classifier suggestion(s)")
    print(f"[cleanup] {items_changed} items modified, {total_stripped} suggestions removed")


# ---------------------------------------------------------------------------
# Phase 2 — eval
# ---------------------------------------------------------------------------


def canonicalize(sug: dict[str, Any]) -> tuple[str, str | None, str]:
    """Reduce a suggestion to a comparable key — action, target_loop_id, sorted action_data JSON."""
    return (
        sug.get("action", ""),
        sug.get("target_loop_id"),
        json.dumps(sug.get("action_data", {}) or {}, sort_keys=True),
    )


def action_data_is_subset(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    """Every key/value in `expected` must appear in `actual`. Extras in actual are OK."""
    for k, v in (expected or {}).items():
        if k not in actual:
            return False
        if actual[k] != v:
            return False
    return True


def match_suggestions(
    expected_sugs: list[dict[str, Any]],
    actual_sugs: list[dict[str, Any]],
) -> tuple[bool, list[str]]:
    """Order-independent match. Returns (ok, list of mismatch reasons)."""
    reasons: list[str] = []
    if len(expected_sugs) != len(actual_sugs):
        reasons.append(f"count mismatch: expected={len(expected_sugs)} actual={len(actual_sugs)}")
        return False, reasons

    unmatched_actual = list(range(len(actual_sugs)))
    for exp in expected_sugs:
        match_idx: int | None = None
        for j in unmatched_actual:
            act = actual_sugs[j]
            if exp.get("action") != act.get("action"):
                continue
            if exp.get("target_loop_id") != act.get("target_loop_id"):
                continue
            if not action_data_is_subset(
                exp.get("action_data") or {}, act.get("action_data") or {}
            ):
                continue
            match_idx = j
            break
        if match_idx is None:
            reasons.append(
                f"no actual match for expected suggestion: "
                f"action={exp.get('action')!r} target_loop_id={exp.get('target_loop_id')!r} "
                f"action_data={exp.get('action_data')!r}"
            )
            return False, reasons
        unmatched_actual.remove(match_idx)
    return True, reasons


async def run_eval(lf: Langfuse) -> int:
    llm = init_llm_service()
    ds = lf.get_dataset(DATASET)
    prompt = fetch_prompt(lf, "scheduling-new-loop-classifier")
    print(
        f"[eval] running prompt v{prompt.version} (labels={prompt.labels}) "
        f"on {len(ds.items)} items"
    )

    config: dict = prompt.config or {}
    model = config.get("model", DEFAULT_MODEL)
    temperature = config.get("temperature", 0.0)
    max_tokens = config.get("max_tokens", 4096)

    passed = 0
    failed = 0
    errors = 0
    failures: list[tuple[str, list[str], list[dict], list[dict]]] = []

    for i, item in enumerate(ds.items):
        try:
            data = LoopClassifierInput(**item.input)
        except Exception as e:
            print(f"  [item {i}] INPUT INVALID: {e}")
            errors += 1
            continue

        # Compile prompt → messages, drive llm.complete directly, parse the
        # <suggestions> envelope. Mirrors the new LoopClassifier runtime
        # path (no JSON-schema injection, no llm_endpoint indirection).
        input_dict = data.model_dump()
        if isinstance(prompt, ChatPromptClient):
            compiled = prompt.compile(**input_dict)
            messages = [dict(m) for m in compiled]
        else:
            messages = [{"role": "system", "content": prompt.compile(**input_dict)}]

        try:
            response = await llm.complete(
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            actual = parse_suggestions_envelope(response.content)
        except Exception as e:
            print(f"  [item {i}] LLM ERROR: {e}")
            errors += 1
            continue

        actual_sugs = [s.model_dump() for s in actual.suggestions]
        expected_sugs = (item.expected_output or {}).get("suggestions", [])

        ok, reasons = match_suggestions(expected_sugs, actual_sugs)
        if ok:
            passed += 1
            print(f"  [item {i}] PASS ({len(actual_sugs)} suggestion(s))")
        else:
            failed += 1
            failures.append((item.id, reasons, expected_sugs, actual_sugs))
            print(f"  [item {i}] FAIL — {reasons[0]}")

    print()
    print("=" * 70)
    print(f"RESULT: passed={passed}, failed={failed}, errors={errors}, total={len(ds.items)}")
    print("=" * 70)

    if failures:
        print()
        print("Failure details:")
        for item_id, reasons, exp, act in failures:
            print(f"\n--- item {item_id} ---")
            for r in reasons:
                print(f"  reason: {r}")
            print("  expected:")
            for s in exp:
                print(
                    f"    action={s.get('action')} "
                    f"target_loop_id={s.get('target_loop_id')} "
                    f"action_data={s.get('action_data')}"
                )
            print("  actual:")
            for s in act:
                print(
                    f"    action={s.get('action')} "
                    f"target_loop_id={s.get('target_loop_id')} "
                    f"action_data={s.get('action_data')}"
                )

    return 0 if failed == 0 and errors == 0 else 1


async def main() -> int:
    lf = Langfuse()
    print("[phase 1] cleaning expected_outputs…")
    cleanup_dataset(lf)
    print()
    print("[phase 2] running deterministic eval…")
    return await run_eval(lf)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
