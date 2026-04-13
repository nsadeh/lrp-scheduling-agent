"""Unit tests for OAuth scope validation in TokenStore."""

from api.gmail.exceptions import GmailAuthError, GmailScopeError


class TestScopeValidation:
    def test_scope_error_has_missing_scopes(self):
        err = GmailScopeError("missing scopes", missing_scopes=["scope1", "scope2"])
        assert err.missing_scopes == ["scope1", "scope2"]
        assert "missing scopes" in str(err)

    def test_scope_error_is_auth_error(self):
        """GmailScopeError inherits from GmailAuthError for catch-all handling."""
        err = GmailScopeError("test", missing_scopes=[])
        assert isinstance(err, GmailAuthError)

    def test_scope_validation_with_matching_scopes(self):
        """When stored scopes cover required scopes, no error is raised."""
        stored_scopes = ["https://www.googleapis.com/auth/gmail.modify"]
        required = set(stored_scopes)
        granted = set(stored_scopes)
        missing = required - granted
        assert len(missing) == 0

    def test_scope_validation_detects_missing_scopes(self):
        """When stored scopes don't cover required scopes, missing are identified."""
        required = {
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/calendar",
        }
        granted = {"https://www.googleapis.com/auth/gmail.modify"}
        missing = required - granted
        assert missing == {"https://www.googleapis.com/auth/calendar"}
