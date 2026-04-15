"""Create or update the scheduling-classifier-v2 chat prompt in LangFuse.

Usage:
    LANGFUSE_PUBLIC_KEY=... LANGFUSE_SECRET_KEY=... python scripts/create_classifier_prompts.py

Requires LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY env vars.
Optionally set LANGFUSE_HOST for self-hosted instances.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from langfuse import Langfuse

from api.classifier.formatters import format_stage_states, format_transitions
from api.classifier.models import ClassificationResult
from api.classifier.prompts import SYSTEM_PROMPT, USER_PROMPT


def main():
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")

    if not public_key or not secret_key:
        print("ERROR: LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY must be set")
        sys.exit(1)

    kwargs = {"public_key": public_key, "secret_key": secret_key}
    host = os.environ.get("LANGFUSE_HOST")
    if host:
        kwargs["host"] = host

    client = Langfuse(**kwargs)

    # Build the system prompt with static content injected
    schema = json.dumps(ClassificationResult.model_json_schema(), indent=2)
    system_content = (
        SYSTEM_PROMPT.replace("{{stage_states}}", format_stage_states())
        .replace("{{transitions}}", format_transitions())
        .replace("{{classification_schema}}", schema)
    )

    # Create the chat prompt with system + user messages
    # The user message keeps its template variables for runtime compilation
    client.create_prompt(
        name="scheduling-classifier-v2",
        type="chat",
        prompt=[
            {"role": "system", "content": system_content},
            {"role": "user", "content": USER_PROMPT},
        ],
        config={
            "model": "claude-haiku-4-5-20251001",
            "temperature": 0.0,
            "max_tokens": 2048,
        },
        labels=["production"],
    )

    print("Created prompt: scheduling-classifier-v2 (chat, labeled 'production')")
    print(f"  System message: {len(system_content)} chars")
    print("  User message template variables: email, thread_history, loop_state,")
    print("    active_loops_summary, events, direction")

    client.flush()
    print("\nDone. Prompt is live in LangFuse.")


if __name__ == "__main__":
    main()
