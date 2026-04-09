"""Domain-specific exceptions for Gmail API operations."""


class GmailApiError(Exception):
    """Base exception for Gmail API errors."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class GmailAuthError(GmailApiError):
    """Refresh token invalid, revoked, or encryption key mismatch."""


class GmailUserNotAuthorizedError(GmailApiError):
    """No stored token for this user — they haven't completed the OAuth flow."""


class GmailNotFoundError(GmailApiError):
    """Message, thread, or draft ID doesn't exist."""


class GmailRateLimitError(GmailApiError):
    """Gmail API quota exceeded."""


class GmailValidationError(GmailApiError):
    """Invalid input (e.g. empty recipients) caught before making an API call."""
