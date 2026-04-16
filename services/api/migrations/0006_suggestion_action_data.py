"""Add action_data JSONB column to agent_suggestions.

Holds action-specific payload from the classifier — typed per action type
in Python (Pydantic discriminated unions), unstructured in Postgres. This
replaces per-action-type columns with a single extensible JSONB field.

Examples:
  DRAFT_EMAIL:  {"directive": "Share Claire's availability with client", "recipient_type": "client"}
  Future actions will add their own shapes without migrations.
"""

from yoyo import step

step(
    """
    ALTER TABLE agent_suggestions
    ADD COLUMN action_data JSONB NOT NULL DEFAULT '{}';
    """,
    """
    ALTER TABLE agent_suggestions
    DROP COLUMN IF EXISTS action_data;
    """,
)
