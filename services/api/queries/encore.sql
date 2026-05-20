-- Encore (Cluein) MS SQL read-only queries.
-- Loaded via aiosql with the pymssql driver (named-param style :name is
-- rewritten to pyformat %(name)s by the adapter). All queries are read-only;
-- a CI grep enforces no INSERT/UPDATE/DELETE in this file.

-- name: get_recent_candidate_activity
-- Recent non-microsoft-domain Genie rows for a candidate within the cutoff
-- window. Returns one row per Genie entry (a single recruiter may appear
-- multiple times — the resolver groups by UserEnteredGUID).
SELECT TOP 50
    g.UserEnteredGUID,
    g.TimeEntered,
    g.GenieNotes,
    gt.GenieType,
    e.EmailAddress,
    e.EmailDisplayName,
    p.FirstName,
    p.LastName
FROM tGenie g
LEFT JOIN tGenieLink gl ON gl.GenieGUID = g.GUID
LEFT JOIN tPeople    p  ON p.GUID = gl.CPSUGUID
LEFT JOIN tGenieType gt ON gt.GUID = g.GenieTypeGUID
LEFT JOIN tUserEmail e  ON e.UserGUID = g.UserEnteredGUID
WHERE p.LastName = :last_name
  AND p.FirstName LIKE :first_name_like
  AND gt.GenieType IN ('General Information', 'Submit to Client')
  AND g.TimeEntered >= :cutoff
  AND e.EmailAddress NOT LIKE '%microsoft%'
  AND e.EmailAddress != 'lrpinterviews@longridgepartners.com'
ORDER BY g.TimeEntered DESC;

-- name: get_email_for_login
-- Map a user's Login (typically initials, e.g. 'DC') to their non-microsoft
-- email. Used by the Ana-Cooke initials fallback.
SELECT DISTINCT
    e.EmailAddress,
    e.EmailDisplayName
FROM tUserEmail e
INNER JOIN tUser u ON u.GUID = e.UserGUID
WHERE u.Login = :initials
  AND e.EmailAddress NOT LIKE '%microsoft%';
