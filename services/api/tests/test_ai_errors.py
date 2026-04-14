"""Tests for the AI error hierarchy."""

import pytest

from api.ai.errors import (
    AIError,
    LangFuseUnavailableError,
    LLMBudgetExceededError,
    LLMParseError,
    LLMUnavailableError,
    PromptNotFoundError,
)


def test_all_errors_inherit_from_ai_error():
    for cls in [
        LangFuseUnavailableError,
        PromptNotFoundError,
        LLMUnavailableError,
        LLMParseError,
        LLMBudgetExceededError,
    ]:
        assert issubclass(cls, AIError)


def test_llm_parse_error_carries_raw_response():
    err = LLMParseError("bad json", raw_response='{"broken')
    assert err.raw_response == '{"broken'
    assert "bad json" in str(err)


def test_llm_parse_error_raw_response_defaults_to_none():
    err = LLMParseError("bad json")
    assert err.raw_response is None


def test_errors_can_be_caught_by_base_class():
    with pytest.raises(AIError):
        raise LLMUnavailableError("all providers down")
