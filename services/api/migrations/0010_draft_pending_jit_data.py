"""Add pending_jit_data to email_drafts for in-flight contact picks.

Stores the coordinator's recruiter / client / CM picks on the draft until
Send fires. Without this column, picking a recruiter from the autocomplete
either has to commit to the loop immediately (current behavior — easy to
misclick), or be lost on the next card refresh. Persisting per-draft makes
the picks recoverable across re-renders, supports a small "x" clear
button, and only commits to the loop at Send time.

Shape:
{
  "recruiter":      {"name": "...", "email": "..."},
  "client_contact": {"name": "...", "email": "...", "company": "..."},
  "client_manager": {"name": "...", "email": "..."}
}
"""

from yoyo import step

step(
    """
    ALTER TABLE email_drafts
    ADD COLUMN pending_jit_data JSONB NOT NULL DEFAULT '{}'::jsonb;
    """,
    """
    ALTER TABLE email_drafts
    DROP COLUMN IF EXISTS pending_jit_data;
    """,
)
