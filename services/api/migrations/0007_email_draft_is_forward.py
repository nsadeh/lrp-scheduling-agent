"""Add is_forward boolean to email_drafts.

Distinguishes forward drafts (new recipients, optional note) from reply
drafts (same thread, required message body). Populated by the drafter at
generation time; read by the sidebar to choose Send vs Forward UI.

Backfills existing rows as false — the prior behavior treated everything
as a reply, so false preserves that semantic for pre-existing drafts.
"""

from yoyo import step

step(
    """
    ALTER TABLE email_drafts
    ADD COLUMN is_forward BOOLEAN NOT NULL DEFAULT false;
    """,
    """
    ALTER TABLE email_drafts
    DROP COLUMN IF EXISTS is_forward;
    """,
)
