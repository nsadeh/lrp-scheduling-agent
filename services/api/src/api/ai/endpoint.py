"""Typed LLM endpoint factory.

Combines a LangFuse prompt reference, input/output Pydantic models,
and the LLM service into a single async callable. This is the
developer-facing API for defining use-case-specific LLM functions.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langfuse import observe
from pydantic import BaseModel, ValidationError

from api.ai.errors import LLMParseError
from api.ai.langfuse_client import fetch_prompt

if TYPE_CHECKING:
    from langfuse import Langfuse

    from api.ai.llm_service import LLMService

logger = logging.getLogger(__name__)


class TypedEndpoint[OutputT: BaseModel]:
    """A typed LLM endpoint that fetches prompts from LangFuse and parses structured output."""

    def __init__(
        self,
        *,
        name: str,
        system_prompt_name: str,
        user_prompt_name: str,
        output_type: type[OutputT],
        label: str = "production",
    ):
        self.name = name
        self.system_prompt_name = system_prompt_name
        self.user_prompt_name = user_prompt_name
        self.output_type = output_type
        self.label = label

    @observe(name="typed_endpoint")
    async def __call__(
        self,
        *,
        llm: LLMService,
        langfuse: Langfuse,
        variables: dict[str, str],
        model_override: str | None = None,
    ) -> OutputT:
        """Execute the endpoint: fetch prompts, call LLM, parse output.

        Args:
            llm: The LLM service for provider routing.
            langfuse: LangFuse client for prompt fetching.
            variables: Template variables for prompt compilation.
            model_override: Override the model from prompt config.

        Returns:
            Parsed output matching output_type.

        Raises:
            PromptNotFoundError: Prompt not found in LangFuse.
            LLMParseError: Response doesn't match output schema.
            LLMUnavailableError: All providers failed.
        """
        # Fetch and compile prompts
        system_prompt = fetch_prompt(langfuse, self.system_prompt_name, label=self.label)
        user_prompt = fetch_prompt(langfuse, self.user_prompt_name, label=self.label)

        system_text = system_prompt.compile(**variables)
        user_text = user_prompt.compile(**variables)

        # Read model config from system prompt
        config = system_prompt.config or {}
        model = model_override or config.get("model")
        temperature = config.get("temperature", 0.2)
        max_tokens = config.get("max_tokens", 2048)

        # Build messages
        messages = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ]

        # Call LLM and parse JSON
        raw = await llm.complete_json(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        # Validate against output type
        try:
            return self.output_type.model_validate(raw)
        except ValidationError as exc:
            raise LLMParseError(
                f"LLM response does not match {self.output_type.__name__} schema: {exc}"
            ) from exc


def llm_endpoint[T: BaseModel](
    *,
    name: str,
    system_prompt_name: str,
    user_prompt_name: str,
    output_type: type[T],
    label: str = "production",
) -> TypedEndpoint[T]:
    """Factory for creating typed LLM endpoints.

    Usage:
        classify_email = llm_endpoint(
            name="classify_email",
            system_prompt_name="scheduling-classifier-v2",
            user_prompt_name="scheduling-classifier-user-v2",
            output_type=ClassificationResult,
        )

        result = await classify_email(
            llm=llm_service,
            langfuse=langfuse_client,
            variables={"email": "...", "thread_history": "..."},
        )
    """
    return TypedEndpoint(
        name=name,
        system_prompt_name=system_prompt_name,
        user_prompt_name=user_prompt_name,
        output_type=output_type,
        label=label,
    )
