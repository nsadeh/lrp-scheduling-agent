"""Shared test configuration.

Sets required environment variables with dummy values so the app can
start without real AI infrastructure. Individual tests that verify
missing-key behavior (e.g., test_ai_langfuse.py) clear these as needed.
"""

import os

# Set dummy AI keys BEFORE any app code imports — the lifespan reads these
# at startup. Tests never make real LLM/LangFuse calls.
os.environ.setdefault("LANGFUSE_PUBLIC_KEY", "pk-test-dummy")
os.environ.setdefault("LANGFUSE_SECRET_KEY", "sk-test-dummy")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")
