"""Add photo_url to contacts for Workspace directory avatar URLs.

Populated when a recruiter is selected via the directory autocomplete in the
create-loop form. NULL for pre-existing rows and for recruiters typed in
manually — UI degrades gracefully (no avatar) when missing.
"""

from yoyo import step

step(
    """
    ALTER TABLE contacts
    ADD COLUMN photo_url TEXT;
    """,
    """
    ALTER TABLE contacts
    DROP COLUMN IF EXISTS photo_url;
    """,
)
