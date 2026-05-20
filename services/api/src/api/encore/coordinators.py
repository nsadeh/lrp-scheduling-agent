"""Hardcoded LRP coordinator emails.

Single source of truth for filtering coordinators out of Encore Genie results
and gating special-case branches (Adam = skip the loop entirely; Ana =
trigger initials-parsing fallback when she is the loop coordinator).
"""

ADAM_EMAIL = "adam@longridgepartners.com"
ANA_EMAIL = "acooke@longridgepartners.com"
FIONA_EMAIL = "fcampbell@longridgepartners.com"
SARA_EMAIL = "scampoli@longridgepartners.com"
MARISSA_EMAIL = "mbradley@longridgepartners.com"

COORDINATOR_EMAILS: frozenset[str] = frozenset(
    {ADAM_EMAIL, ANA_EMAIL, FIONA_EMAIL, SARA_EMAIL, MARISSA_EMAIL}
)
