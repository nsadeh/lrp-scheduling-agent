"""Allow loops to exist with incomplete contact info.

Drop NOT NULL on loops.recruiter_id, loops.client_contact_id, and
client_contacts.company so the auto-resolver can create a loop with
whatever the classifier extracted. Missing pieces are collected
just-in-time by the sidebar widget that needs them (e.g. forward-to-
recruiter draft asks for the recruiter inline before sending).
"""

from yoyo import step

step(
    """
    ALTER TABLE loops ALTER COLUMN recruiter_id DROP NOT NULL;
    ALTER TABLE loops ALTER COLUMN client_contact_id DROP NOT NULL;
    ALTER TABLE client_contacts ALTER COLUMN company DROP NOT NULL;
    """,
    """
    ALTER TABLE client_contacts ALTER COLUMN company SET NOT NULL;
    ALTER TABLE loops ALTER COLUMN client_contact_id SET NOT NULL;
    ALTER TABLE loops ALTER COLUMN recruiter_id SET NOT NULL;
    """,
)
