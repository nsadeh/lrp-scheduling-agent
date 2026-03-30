-- name: store_token(user_email, refresh_token_encrypted, scopes)!
-- Upsert an encrypted refresh token for a user.
INSERT INTO gmail_tokens (user_email, refresh_token_encrypted, scopes, updated_at)
VALUES (:user_email, :refresh_token_encrypted, :scopes, now())
ON CONFLICT (user_email) DO UPDATE SET
    refresh_token_encrypted = EXCLUDED.refresh_token_encrypted,
    scopes = EXCLUDED.scopes,
    updated_at = now();

-- name: load_token(user_email)^
-- Load the encrypted refresh token for a user. Returns None if not found.
SELECT user_email, refresh_token_encrypted, scopes, created_at, updated_at
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
