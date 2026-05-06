-- name: store_token(user_email, refresh_token_encrypted, scopes)!
-- Upsert an encrypted refresh token for a user, clearing stale flag on re-auth.
INSERT INTO gmail_tokens (user_email, refresh_token_encrypted, scopes, updated_at)
VALUES (:user_email, :refresh_token_encrypted, :scopes, now())
ON CONFLICT (user_email) DO UPDATE SET
    refresh_token_encrypted = EXCLUDED.refresh_token_encrypted,
    scopes = EXCLUDED.scopes,
    is_stale = false,
    updated_at = now();

-- name: load_token(user_email)^
-- Load the encrypted refresh token, scopes, and stale flag for a user.
SELECT refresh_token_encrypted, scopes, is_stale
FROM gmail_tokens
WHERE user_email = :user_email;

-- name: delete_token(user_email)!
-- Delete a user's stored token.
DELETE FROM gmail_tokens
WHERE user_email = :user_email;

-- name: has_token(user_email)$
-- Check if a user has a stored token. Returns True/False.
SELECT EXISTS(
    SELECT 1 FROM gmail_tokens WHERE user_email = :user_email
) AS has_token;

-- name: mark_stale(user_email)!
-- Flag a token as stale after a RefreshError.
UPDATE gmail_tokens
SET is_stale = true, updated_at = now()
WHERE user_email = :user_email;

-- name: is_token_stale(user_email)$
-- Check if a user's token is marked stale.
SELECT is_stale FROM gmail_tokens WHERE user_email = :user_email;

-- name: get_all_watched_emails
-- List coordinator emails with valid (non-stale) stored tokens.
SELECT user_email FROM gmail_tokens WHERE NOT is_stale;
