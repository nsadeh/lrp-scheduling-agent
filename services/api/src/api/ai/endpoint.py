"""Typed LLM endpoint factory.

Creates use-case-specific async callables that combine:
- A LangFuse prompt reference (fetched and cached by the SDK)
- Typed Pydantic input/output models
- The LLMService for provider routing
- Automatic JSON schema instruction, parsing, and one retry on parse failure

Usage:
    classify_email = llm_endpoint(
        name="classify_email",
        prompt_name="scheduling-classify-email",
        input_type=ClassifyEmailInput,
        output_type=ClassifyEmailOutput,
    )

    result = await classify_email(llm=llm_service, langfuse=langfuse, input=input_data)
"""

import json
import logging
from typing import Any

from langfuse import Langfuse, observe
from pydantic import BaseModel

from api.ai.errors import LLMParseError
from api.ai.langfuse_client import fetch_prompt
from api.ai.llm_service import DEFAULT_MODEL, LLMService

logger = logging.getLogger(__name__)

JSON_INSTRUCTION_TEMPLATE = (
    "You must respond with valid JSON matching this schema:\n"
    "```json\n{schema}\n```\n"
    "Respond ONLY with the JSON object, no other text."
)

FIX_JSON_MESSAGE = (
    "Your previous response was not valid JSON or did not match the required schema. "
    "Error: {error}\n\n"
    "Please respond with ONLY a valid JSON object matching this schema:\n"
    "```json\n{schema}\n```"
)


def llm_endpoint[T_Input: BaseModel, T_Output: BaseModel](
    *,
    name: str,
    prompt_name: str,
    input_type: type[T_Input],
    output_type: type[T_Output],
) -> "LLMEndpoint":
    """Create a typed LLM endpoint.

    Args:
        name: Endpoint name (used for tracing spans and logging).
        prompt_name: LangFuse prompt name to fetch.
        input_type: Pydantic model for the input (fields become template variables).
        output_type: Pydantic model for the output (LLM response is parsed into this).

    Returns:
        An LLMEndpoint callable that takes (llm, langfuse, input) and returns output_type.
    """
    return LLMEndpoint(
        name=name,
        prompt_name=prompt_name,
        input_type=input_type,
        output_type=output_type,
    )


class LLMEndpoint:
    """A typed LLM endpoint that fetches a prompt, calls the LLM, and parses the result."""

    def __init__(
        self,
        *,
        name: str,
        prompt_name: str,
        input_type: type[BaseModel],
        output_type: type[BaseModel],
    ):
        self.name = name
        self.prompt_name = prompt_name
        self.input_type = input_type
        self.output_type = output_type
        self._output_schema = json.dumps(output_type.model_json_schema(), indent=2)

    @observe()
    async def __call__(
        self,
        *,
        llm: LLMService,
        langfuse: Langfuse,
        data: BaseModel,
        **overrides: Any,
    ) -> BaseModel:
        """Execute the endpoint: fetch prompt → compile → call LLM → parse.

        Args:
            llm: The LLMService instance for making LLM calls.
            langfuse: The LangFuse client for prompt fetching.
            data: The typed input data. Fields are used as template variables.
            **overrides: Override model config (model, temperature, max_tokens).

        Returns:
            Parsed output matching output_type.

        Raises:
            LLMParseError: If the LLM response can't be parsed after one retry.
            LLMUnavailableError: If all providers fail.
            LangFuseUnavailableError: If prompt can't be fetched.
            PromptNotFoundError: If prompt doesn't exist.
        """
        # 1. Fetch prompt from LangFuse
        prompt = fetch_prompt(langfuse, self.prompt_name)
        config: dict = prompt.config or {}

        # 2. Read model config (LangFuse config is primary, overrides take precedence)
        model = overrides.get("model", config.get("model", DEFAULT_MODEL))
        temperature = overrides.get("temperature", config.get("temperature", 0.0))
        max_tokens = overrides.get("max_tokens", config.get("max_tokens", 4096))

        # 3. Compile the prompt template with input fields
        input_dict = data.model_dump()
        compiled_prompt = prompt.compile(**input_dict)

        # 4. Build messages with JSON schema instruction
        json_instruction = JSON_INSTRUCTION_TEMPLATE.format(schema=self._output_schema)
        messages = [
            {"role": "system", "content": f"{json_instruction}\n\n{compiled_prompt}"},
            {"role": "user", "content": json.dumps(input_dict)},
        ]

        # 5. Call LLM
        response = await llm.complete(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        # 6. Parse response
        parsed, parse_error = self._try_parse(response.content)
        if parsed is not None:
            logger.info(
                "Endpoint '%s' succeeded (model=%s, provider=%s, latency=%.0fms)",
                self.name,
                response.model,
                response.provider,
                response.latency_ms,
            )
            return parsed

        # 7. Retry once with "fix your JSON" follow-up
        logger.warning("Endpoint '%s': first parse failed, retrying with fix prompt", self.name)
        fix_message = FIX_JSON_MESSAGE.format(
            error=parse_error,
            schema=self._output_schema,
        )
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": fix_message})

        retry_response = await llm.complete(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        parsed, retry_error = self._try_parse(retry_response.content)
        if parsed is not None:
            logger.info(
                "Endpoint '%s' succeeded on retry (model=%s, latency=%.0fms)",
                self.name,
                retry_response.model,
                retry_response.latency_ms,
            )
            return parsed

        raise LLMParseError(
            f"Endpoint '{self.name}': failed to parse LLM response after retry. "
            f"Error: {retry_error}",
            raw_response=retry_response.content,
        )

    def _try_parse(self, content: str) -> tuple[BaseModel | None, str]:
        """Try to parse LLM response content into output_type.

        Returns (parsed_model, error_string). On success, error_string is empty.
        Handles common LLM quirks: markdown code fences, leading/trailing whitespace.
        """
        cleaned = content.strip()

        # Strip markdown code fences if present
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            # Remove first line (```json or ```) and last line (```)
            if len(lines) >= 3 and lines[-1].strip() == "```":
                cleaned = "\n".join(lines[1:-1]).strip()

        try:
            data = json.loads(cleaned)
            return self.output_type.model_validate(data), ""
        except (json.JSONDecodeError, Exception) as exc:
            return None, str(exc)
