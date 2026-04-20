# RFC: Recruiter Directory Autocomplete

| Field         | Value                                 |
| ------------- | ------------------------------------- |
| **Author(s)** | Kinematic Labs                        |
| **Status**    | Draft                                 |
| **Created**   | 2026-04-20                            |
| **Updated**   | 2026-04-20                            |
| **Reviewers** | LRP Engineering, LRP Coordinator team |
| **Decider**   | Nim Sadeh                             |
| **Issue**     | [#27](https://github.com/nsadeh/lrp-scheduling-agent/issues/27) |

## Context and Scope

When a coordinator creates a scheduling loop from the add-on sidebar, `build_create_loop_form` in `services/api/src/api/scheduling/cards.py:201` renders the recruiter's name and email as two free-text `TextInput` widgets. Coordinators either type from memory or copy-paste from an email thread. Both are error-prone — typos silently corrupt the `contacts` table (no email validation), and coordinators frequently report they "don't remember the exact address." The customer raised this explicitly during a demo; they asked for a dropdown that filters to LRP Workspace members.

This RFC proposes replacing the free-text inputs with an autocomplete backed by Google Workspace's directory. The change is narrow by design — it only touches the recruiter fields in the create-loop form, not the client contact or client manager fields, and it does not change the scheduling loop data model beyond adding a photo URL column. It also fixes a pre-existing dedup bug in `find_or_create_contact` discovered while designing this feature, bundled here because the code paths overlap.

## Goals

- **G1: Directory-filtered typeahead.** A coordinator typing a name or email into the recruiter field sees a suggestion dropdown drawn from the live LRP Workspace directory, filtered by the current query, updating within ~500ms p95 of each keystroke (after debounce).
- **G2: Atomic suggestion acceptance.** Selecting a suggestion populates both the name and email fields in the form. The coordinator does not have to re-type or reconcile the two fields.
- **G3: Avatar on selection.** Once a loop is created with a directory-sourced recruiter, the recruiter's Google Workspace avatar renders in the Recruiter section of the loop detail card.
- **G4: No regression on existing autocomplete.** Client-contact and client-manager autocomplete (backed by their respective tables) continues to work unchanged.
- **G5: Contacts dedup fix.** The existing bug where `find_or_create_contact` always inserts (creating N duplicate rows for the same recruiter over time) is fixed. After this RFC ships, the invariant `COUNT(*) FROM contacts GROUP BY email, role HAVING COUNT(*) > 1` returns zero rows.

## Non-Goals

- **Inferring role from the directory.** We do not attempt to distinguish recruiters from CMs from leadership from ops using OU membership, group membership, or title fields. _Rationale:_ the LRP directory has no structured role metadata our code could rely on, and every heuristic we considered breaks on edge cases. The coordinator's selection on a specific loop is the authoritative labeling event — we already store it as `loops.recruiter_id` — and we don't need pre-labeling for anything else in this RFC.
- **Native chip-picker UX with avatars in the dropdown itself.** The ideal — `SelectionInput MULTI_SELECT` with `platformDataSource.commonDataSource=USER` — is documented as "Only available for Google Chat apps. Not available for Google Workspace add-ons." _Rationale:_ host-level SDK restriction we cannot work around. Avatars render post-selection only.
- **Offline or pre-fetched directory.** No local cache, no nightly sync, no `org_members` table. _Rationale:_ the directory is small (~50 members) and Google's People API is fast. Introducing a sync job brings staleness, a sync-principal identity problem, and `is_active` bookkeeping for zero runtime benefit at this scale.
- **External / non-`longridgepartners.com` recruiters.** Customer confirmed they use zero external contractors as recruiters. _Rationale:_ handling them would force a "typed fallback" that undermines the whole point of the picker; if this ever changes, we add one checkbox.
- **Replacing client-contact or client-manager pickers.** _Rationale:_ client contacts live in a different table (`client_contacts`), have different attributes (company is required), and are often NOT Workspace members. Directory autocomplete doesn't apply. Out of scope.

## Background

### How OAuth scopes work in this codebase

Per-coordinator OAuth refresh tokens are stored encrypted (Fernet) in `gmail_tokens` and loaded via `TokenStore.load_credentials` in `services/api/src/api/gmail/auth.py`. On each call, `load_credentials` validates that the token's granted scopes cover every entry in `REQUIRED_SCOPES` (env var, comma-separated). If any scope is missing it raises `GmailScopeError`, which propagates up and triggers the coordinator's re-consent via `/addon/oauth/start` (see `routes.py:769`). That flow uses `prompt=consent` and `access_type=offline`, so the coordinator is walked through Google's consent screen and a fresh refresh token replaces the old one.

Today `REQUIRED_SCOPES` defaults to `gmail.modify`. To read the directory we extend it to include `https://www.googleapis.com/auth/directory.readonly` (Google People API). This is the same scope Gmail's native "To:" autocomplete uses, and it is the least-privileged scope that supports `people:searchDirectoryPeople`. We do not add `admin.directory.user.readonly` — that is a restricted admin scope we don't need.

### Why user-scope OAuth and not DWD

Domain-wide delegation would require: provision a new service account in GCP, generate and store a key, ask a Workspace Super Admin to grant `admin.directory.user.readonly` to that SA's numeric client_id via Admin Console → Security → API Controls, implement user impersonation in a new client, and pick an impersonation identity (e.g., `admin@longridgepartners.com`). That's additional infrastructure, a higher-privilege scope (restricted vs sensitive), a fragile dependency on a specific admin mailbox, and a longer review if we ever apply for OAuth verification. User-scope OAuth achieves the same outcome — every coordinator can already list their own Workspace directory — with one checkbox on the existing consent screen and zero new infrastructure.

### People API response shape

`GET https://people.googleapis.com/v1/people:searchDirectoryPeople?query=<q>&readMask=names,emailAddresses,photos&sources=DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE` returns a `SearchDirectoryPeopleResponse`. Each `person` has `resourceName` (stable opaque ID of form `people/<id>`), `names[].displayName`, `emailAddresses[].value`, and `photos[].url` (CDN URL at `lh3.googleusercontent.com`, publicly fetchable, may expire but can be refreshed by re-querying). The photo URL is suitable for direct embedding in card `DecoratedText.startIcon.iconUrl`.

### Card framework support for autocomplete

Google's Card v2 framework supports dynamic autocomplete on `TextInput` via the `autoCompleteAction` field (`TextInput.setAutoCompleteAction` in the Apps Script reference, equivalent to `auto_complete_action` in the HTTP runtime JSON). The backend callback returns a `SuggestionsResponse` containing a flat list of `Suggestions.items[].text` entries. Google renders this as a plain-text dropdown under the input; there is no framework support for per-item avatars, descriptions, or rich formatting.

The richer `SelectionInput MULTI_SELECT` widget with `platformDataSource.commonDataSource=USER` is the native Google people-chip picker used across Chat and other Workspace surfaces, but it is unavailable in Gmail add-ons (host restriction). See [PlatformDataSource docs](https://developers.google.com/apps-script/reference/card-service/platform-data-source).

### Scale context

| Measure | Value |
| ------- | ----- |
| LRP Workspace members (directory size) | ~50 |
| Active coordinators (today) | 1 |
| Projected active coordinators | ~5 |
| Loops created per coordinator per day | ~10 |
| Autocomplete calls per loop creation (avg keystrokes after debounce) | ~3 |
| People API p50 / p99 latency (observed) | <200ms / <500ms |

At these volumes, live-per-keystroke directory queries are trivial. Aggregate load is O(150) People API calls per day across all coordinators, far below Google's per-user quota limits.

## Proposed Design

### Overview

Extend `REQUIRED_SCOPES` by one value. Add one backend endpoint that proxies directory search via the calling coordinator's own OAuth token. Wire the two recruiter `TextInput` widgets in the create-loop form to that endpoint via Google's `autoCompleteAction`. Persist the selected recruiter's avatar URL on the loop and render it on the detail card. Fix the pre-existing contacts dedup bug as part of the same migration.

No new services, no new queues, no scheduled jobs, no new identities.

### System Context Diagram

```mermaid
sequenceDiagram
    participant U as Coordinator (Gmail)
    participant G as Google Add-on Framework
    participant B as FastAPI Backend
    participant P as Google People API

    U->>G: Types "sa" in recruiter name field
    G->>B: POST action: autocomplete_recruiter<br/>(query="sa", coordinator ID token)
    B->>B: Verify ID token → coordinator email
    B->>B: Load coordinator's OAuth creds<br/>(gmail_tokens, scope-check directory.readonly)
    B->>P: GET people:searchDirectoryPeople<br/>?query=sa&readMask=names,emailAddresses,photos
    P-->>B: [Sarah Chen, Sam Ray, Salvador Ortiz, ...]
    B-->>G: SuggestionsResponse<br/>[{text: "Sarah Chen <sarah@..>"}, ...]
    G->>U: Renders plain-text dropdown

    U->>G: Clicks "Sarah Chen <sarah@..>"
    G->>B: POST action: recruiter_selected<br/>(selected text, form state)
    B->>B: Parse "Name <email>"; stash photoUrl<br/>keyed by email in form hidden field
    B-->>G: UpdateCard with recruiter_name and recruiter_email pre-filled
    G->>U: Re-renders form with both fields populated
```

### Detailed Design

#### 1. Scope change

**File:** `.env`, `services/api/railway.toml`, `services/api/deployment.staging.json`, `services/api/deployment.prod.json`

Add `https://www.googleapis.com/auth/directory.readonly` to `REQUIRED_SCOPES`. The add-on manifest's OAuth scope list also needs the new scope so Google knows to display it on the consent screen — update both `deployment.staging.json` and `deployment.prod.json`.

On the next addon request from any coordinator, `TokenStore.load_credentials` raises `GmailScopeError(missing_scopes=["…directory.readonly"])`. The existing error handling in `addon/routes.py` intercepts this and redirects to `/addon/oauth/start`, which now requests the expanded scope set. Coordinator approves the one new line, flow returns them to the original action. This re-consent path already exists and is exercised today for other scope changes — no new code.

#### 2. New endpoint: `/addon/directory/search`

**File:** `services/api/src/api/addon/routes.py`

```python
@addon_router.post("/directory/search")
async def directory_search(request: AddonRequest) -> AutocompleteSuggestionsResponse:
    """Proxy People API searchDirectoryPeople using the calling coordinator's token."""
    email = _verified_coordinator_email(request)
    query = _get_param(request, "query") or ""
    if len(query) < 1:
        return AutocompleteSuggestionsResponse(suggestions=Suggestions(items=[]))
    creds = await request.app.state.token_store.load_credentials(email)
    people = await _people_search(creds, query, page_size=10)
    return AutocompleteSuggestionsResponse(
        suggestions=Suggestions(items=[
            SuggestionItem(text=_format_suggestion(p)) for p in people
        ])
    )
```

`_people_search` is a thin wrapper over `httpx.AsyncClient` hitting `people.googleapis.com/v1/people:searchDirectoryPeople` with `readMask=names,emailAddresses,photos` and `sources=DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE`. Response parsing keeps `resourceName`, first `displayName`, first `value` email, first `photos[].url`.

The suggestion `text` field encodes both name and email as `"Display Name <email@longridgepartners.com>"` — a format we control and can unambiguously parse on selection. The photo URL is NOT surfaced in the suggestion text (the widget doesn't render it); it's re-fetched at selection time.

#### 3. Card form changes

**File:** `services/api/src/api/scheduling/cards.py`

The two `TextInputWidget`s for `recruiter_name` and `recruiter_email` each get an `autoCompleteAction` pointing at `autocomplete_recruiter`. That action handler is a one-line wrapper that calls `/addon/directory/search` with the current input value as the query.

When a coordinator picks a suggestion, Google re-renders the form with the selected string in whichever field they were typing in. We need the OTHER field populated too. There are two paths:

1. **`onChangeAction` on the name field** parses `"Name <email>"`, splits, and returns an `UpdateCard` that rewrites the form with both fields set. Preferred. Uses a standard action callback we already have infrastructure for.
2. **Collapse to a single "Recruiter" field** that stores `"Name <email>"` as one string. Parsed on submit. Less preferred — changes the data model cosmetically and loses validation ergonomics.

We go with path 1 in the default implementation and fall back to path 2 only if `onChangeAction` turns out not to re-render peer fields as expected. This is the "implementation-detail spike on day 1" open question below.

On form submit, the recruiter's `photoUrl` needs to flow through. We stash it as a hidden form field populated at suggestion-select time via the same `onChangeAction`. If the coordinator bypasses the picker and types manually (they still can — the `TextInput` is not locked), `photoUrl` stays empty and we degrade gracefully.

#### 4. Avatar persistence

**File:** `services/api/migrations/0007_recruiter_photo_url.py` (new)

```sql
ALTER TABLE contacts ADD COLUMN photo_url TEXT;
```

Store the photo URL on `contacts` rather than on `loops` — the avatar is a property of the person, not the loop, and it stays stable across multiple loops involving the same recruiter. `photo_url` is populated on upsert during loop creation (see next section).

Loop detail card renders it via `DecoratedText.startIcon.iconUrl = contact.photo_url` in the Recruiter section. If null, fall back to no icon or a generic person glyph.

We cache the photo URL as-provided by Google, with no refresh logic in this RFC. If URLs start 404-ing in the wild (observed lifetime is weeks-to-months), we add a refresh path in a follow-up. For now the degradation is "no avatar" which is no worse than today.

#### 5. Contacts dedup fix (bundled bug)

**File:** `services/api/migrations/0007_recruiter_photo_url.py` (same migration)

```sql
-- Deduplicate existing rows before adding the constraint
WITH keepers AS (
    SELECT DISTINCT ON (email, role) id FROM contacts ORDER BY email, role, created_at
)
DELETE FROM contacts WHERE id NOT IN (SELECT id FROM keepers);

ALTER TABLE contacts ADD CONSTRAINT contacts_email_role_unique UNIQUE (email, role);
```

**File:** `services/api/queries/scheduling.sql`

```sql
-- name: upsert_contact^
INSERT INTO contacts (id, name, email, role, company, photo_url)
VALUES (:id, :name, :email, :role, :company, :photo_url)
ON CONFLICT (email, role) DO UPDATE
    SET name = EXCLUDED.name,
        photo_url = COALESCE(EXCLUDED.photo_url, contacts.photo_url)
RETURNING id, name, email, role, company, photo_url, created_at;
```

`LoopService.find_or_create_contact` calls the new `upsert_contact` query. The old `create_contact` query is removed (no other callers after this change). Note the `COALESCE` — we never overwrite a stored photo URL with NULL, so manual edits don't regress a directory-sourced avatar.

There is a subtle foreign-key concern in the dedup step: `loops.recruiter_id` and `loops.client_manager_id` both reference `contacts(id)`. If we delete a duplicate row that some loop still references, we'd violate the FK. The migration must therefore re-point those references before deleting:

```sql
-- Re-point loop references to the canonical (earliest) contact row
UPDATE loops l
SET recruiter_id = keeper.id
FROM (
    SELECT DISTINCT ON (email, role) email, role, id FROM contacts ORDER BY email, role, created_at
) keeper,
    contacts dup
WHERE l.recruiter_id = dup.id
  AND dup.email = keeper.email AND dup.role = keeper.role
  AND dup.id <> keeper.id;
-- Same for client_manager_id
-- Then delete orphans
```

This is slightly ugly SQL but runs in <1s at current data volumes (one loop in production) and is written so it's idempotent if re-run.

### Key Trade-offs

1. **Per-coordinator scope grant vs admin-level DWD.** We trade a one-time re-consent friction on each coordinator for dramatically simpler auth infrastructure (no new SA, no admin action in Workspace Console, no impersonation). Acceptable because the re-consent flow is already built and exercised for other scope changes.

2. **Live query vs local cache/sync.** We trade ~200ms of added per-keystroke latency for zero staleness, zero sync-principal bookkeeping, zero `is_active` logic, zero cold-start problem, and one fewer table. At 50-member org scale the latency is imperceptible after debounce.

3. **Plain-text dropdown vs native chip picker.** We trade avatars-in-dropdown UX (only available in Chat apps) for the Gmail add-on host we actually target. Avatars still render post-selection in the loop detail card — the Workspace face-recognition goal is met, just at selection time instead of hover time.

4. **Trust the coordinator's picker choice vs ongoing role inference.** We trade a system that might someday auto-label Sarah as a "recruiter" vs "CM" for a system where the coordinator's selection is ground truth. Acceptable because the directory has no reliable role signal, and selection is already how `loops.recruiter_id` and `loops.client_manager_id` get populated today.

## Alternatives Considered

### Alternative 1: Service account + DWD + nightly directory sync

Provision a new GCP service account, generate a JSON key, store it in Railway as `GOOGLE_DIRECTORY_SA_KEY`. Ask an LRP Workspace Super Admin to grant `admin.directory.user.readonly` to the SA's numeric client_id via Admin Console → Security → API Controls → Domain-wide delegation. Implement a nightly arq job that impersonates `admin@longridgepartners.com` and writes the directory into a new `org_members` table (with `is_active`). Autocomplete reads from `org_members` instead of proxying live.

**Trade-offs:** Queries become local-DB-fast (no ~200ms Google hop per keystroke). But we take on: a restricted (not sensitive) OAuth scope that's higher privilege than `directory.readonly`; a new GCP identity with its own rotation/secrets-management surface; a dependency on a specific admin mailbox as the impersonation principal; explicit `is_active` bookkeeping for leavers; a sync job that can fail silently and leave us stale; and coordinator-invisible admin console work that couples project timeline to Workspace IT.

**Why not:** The performance gain is indistinguishable to the user after debounce (200ms is below the threshold coordinators perceive as sluggish). Every other cost is real. We considered this path in this issue's discussion and the customer explicitly pushed back: *"I can't believe we need this level of hackiness to work with google workspace."* They were right — there is a simpler path, and it's the one we propose.

### Alternative 2: Server-side autocomplete against only the `contacts` table

Wire `search_contacts` (`services/api/src/api/scheduling/service.py:136`) to the create-loop form via `autoCompleteAction`. No new scope, no new endpoint beyond what the service already has, no People API dependency.

**Trade-offs:** Zero infrastructure cost, zero coordinator friction. But `contacts` is a cache of *people who have been contacted*, not a roster. The first time any coordinator books a loop with Sarah, `contacts` contains zero Sarahs — so autocomplete surfaces nothing useful for the case the customer actually complained about ("I don't remember their email").

**Why not:** It fails the core requirement. We would still need Directory to cover first-time recruiters, and once we have Directory the `contacts`-only path is dead code.

### Alternative 3: Native `SelectionInput MULTI_SELECT` with `platformDataSource.commonDataSource=USER`

Use Google's native people-chip picker — typeahead, avatars in the dropdown, org-member filtering all rendered by Google. This would be the cleanest possible UX and removes our need to call the People API ourselves.

**Trade-offs:** Best possible UX; zero backend work for directory search.

**Why not:** Unavailable in Gmail add-ons. Google's docs are explicit: *"Only available for Google Chat apps. Not available for Google Workspace add-ons."* (See [PlatformDataSource](https://developers.google.com/apps-script/reference/card-service/platform-data-source).) The restriction is host-level and not something we can toggle. If Google expands support to Gmail at some future date, we swap — the data model in this RFC doesn't change.

### Alternative 4: Defer until Encore integration auto-fills the recruiter

Skip this RFC entirely. When the Encore/Cluein ATS integration lands, the recruiter will be auto-disambiguated from the candidate's Encore record in most cases, and manual entry becomes the exception path.

**Trade-offs:** Saves 2–3 days of eng now. But Encore is real-world data — disambiguation will sometimes fail (missing record, stale record, multiple candidates with the same name). In every failure case the coordinator falls back to manual entry, which brings us back to the free-text bug we're trying to fix. And the customer has asked for this specifically and recently.

**Why not:** Manual entry is the irreducible fallback no matter how good the ATS integration gets. Fixing the manual path is useful independent of Encore, and the effort is small. Deferring would leave the bug live for the (unknown) duration of the Encore project.

### Do Nothing / Status Quo

Leave the form as free-text. Continue tolerating typos, continue tolerating "I don't remember their email" friction, continue creating duplicate `contacts` rows on every loop submission.

**What happens:** Customer raised this in a demo. The UX hit is small per-loop but accumulates. Typo'd email addresses do two things over time: they corrupt the `contacts` cache (making it even less useful for downstream autocomplete), and they get embedded into real drafted emails that the coordinator then sends to an address that doesn't exist. Today coordinators catch these visually before sending, but the classifier and downstream agent infrastructure treat these typos as real contacts and may surface them as suggestions in future features.

**Why not:** The customer pain is real, and the fix is cheap. "Do nothing" is a viable short-term choice if the eng team is underwater — but it's not underwater, and this slots neatly into 2–3 days.

## Success and Failure Criteria

### Definition of Success

| Criterion | Metric | Target | Measurement Method |
| --------- | ------ | ------ | ------------------ |
| **G1: Responsive typeahead** | p95 latency from autocomplete call to suggestions rendered | < 500ms | Request duration log on `/addon/directory/search`, aggregated weekly |
| **G1: Usable result quality** | % of autocomplete calls returning ≥1 suggestion when query length ≥ 2 | > 90% | Count of empty-result responses / total, aggregated weekly |
| **G2: Atomic selection** | % of created loops where `recruiter_name` and `recruiter_email` come from a single autocomplete selection (detected by sentinel in form state) | > 80% over 2 weeks post-launch | Instrumentation on loop-create handler |
| **G3: Avatar coverage** | % of new loops with non-null `contacts.photo_url` for recruiter | > 80% over 2 weeks post-launch | SQL query on `loops` joined with `contacts` |
| **G4: No regression** | Existing client/CM autocomplete success rate | Unchanged from pre-launch baseline (within ±5%) | Same instrumentation on those paths |
| **G5: Dedup holds** | Count of `(email, role)` groups in `contacts` with >1 row | 0 | Daily assertion query |

### Definition of Failure

- **Re-consent adoption stalls.** After 2 weeks post-launch, fewer than 80% of active coordinators have re-consented. Diagnosis: either the consent screen language is confusing, or `GmailScopeError` is not reliably triggering the re-consent redirect in some addon contexts.
- **Autocomplete latency exceeds 1s p95 after 1 week of tuning.** The widget feels broken; coordinators stop trusting it and revert to manual typing.
- **Selection atomicity fails.** More than 15% of loops land with mismatched `recruiter_name` / `recruiter_email` domain (e.g., name from one person, email from another). Indicates `onChangeAction` peer-field update path doesn't work as designed and we need to fall back to the single combined field (Alternative 1 in §3).
- **Dedup migration fails in production.** Foreign key violations during the re-point step. Migration is reversible; we roll back and re-plan the dedup.

### Evaluation Timeline

- **T+1 week:** Re-consent adoption across all active coordinators. Confirm G1 and G2 metrics are tracking to target.
- **T+2 weeks:** Full success check against all G1–G5 metrics. Decision on whether any follow-up work is needed (photo URL refresh, role-aware ranking, etc.).
- **T+6 weeks:** Retrospective — does the picker still feel right, or has Encore integration changed the calculus?

## Observability and Monitoring Plan

### Metrics

| Metric | Source | Dashboard / Alert | Threshold |
| ------ | ------ | ----------------- | --------- |
| `/addon/directory/search` request duration | FastAPI middleware log + PostHog | Addon Performance dashboard (new) | Alert if p95 > 1s for 10 min |
| `/addon/directory/search` error rate | Sentry | Error Budget dashboard | Alert if > 5% for 10 min |
| `/addon/directory/search` empty-result rate | FastAPI log | Addon Performance dashboard | No alert; track weekly |
| Re-consent flow completions | Log event on successful `/addon/oauth/callback` with the new scope | PostHog funnel | Manual review at T+1w |
| **Atomic-selection rate (G2)** | Log sentinel `recruiter_source=directory\|manual` written to a hidden form field at suggestion-select time; emitted on loop create | Addon Performance dashboard | Alert if < 70% for 2 consecutive weeks |
| Loops created with `recruiter_photo_url` populated (G3) | DB query on `loops` ⨝ `contacts` | Weekly metrics report | No alert |
| **Client / CM autocomplete regression (G4)** | Existing `search_contacts` / `search_client_contacts` call log | Addon Performance dashboard | Alert if call count drops > 50% week-over-week |
| Contacts dedup invariant | Daily SQL assertion | Existing Sentry cron-hook | Alert on any non-zero count |

### Logging

- Each `/addon/directory/search` call logs: coordinator email, query length (not content), result count, duration, and outcome (ok / scope_error / people_api_error).
- Each re-consent completion logs the set of granted scopes, coordinator email.
- No logging of query content or suggestion content — the directory is low-sensitivity but there's no reason to log it.

### Alerting

- Error rate and latency alerts above go to the existing on-call Sentry channel.
- Dedup invariant alert is the only hard-fail cron alert. It should never fire; if it does we have a data integrity regression and the upsert query is broken.

### Dashboards

- New PostHog dashboard: "Addon Performance" with the three `/directory/search` metrics and the re-consent funnel. Audience: Nim + on-call during rollout week.
- Existing "Addon Health" dashboard continues to cover overall addon error rate; the new endpoint inherits it.

## Cross-Cutting Concerns

### Security

Adding `directory.readonly` to the coordinator's OAuth token expands the token's blast radius: a compromised token can now enumerate the full LRP Workspace directory. This is an information-disclosure risk (read-only) against data that the coordinator can already see via Gmail's native "To:" picker anyway. No new write capability is added.

Token storage is unchanged — Fernet-encrypted refresh tokens in `gmail_tokens`, keys held in the Railway environment. The new endpoint inherits the existing ID-token verification in `_verified_coordinator_email`, so unauthenticated requests cannot trigger directory lookups against any coordinator's token.

### Privacy

We read the LRP Workspace directory and surface names, email addresses, and photo URLs to the coordinator. All three are already surfaced to the coordinator by native Google products (Gmail, Contacts, Chat). We do not store directory results in our database beyond the `contacts.photo_url` column for recruiters actually selected on loops. No PII enters logs.

### Scalability

Live directory queries scale with coordinator keystrokes. At projected 5 coordinators × 10 loops/day × ~3 debounced keystrokes per autocomplete = ~150 People API calls/day. Google's People API quota is 90 req/sec per user under the default quota; we are nowhere near that. No scaling work required.

### Rollout and Rollback

Staged rollout:
1. Merge RFC, implement, ship to staging.
2. Add `directory.readonly` to staging's `REQUIRED_SCOPES`. Nim re-consents in the staging addon, exercises a full loop-creation end-to-end. Verify all G1–G5 instrumentation fires correctly.
3. Deploy to prod (code and `REQUIRED_SCOPES` update in the same release). First prod interaction from any coordinator triggers `GmailScopeError`, bounces to `/addon/oauth/start`, coordinator approves the one new scope, back to normal.

Rollback: revert `REQUIRED_SCOPES` to the prior value, revert the code. Coordinators' existing tokens (which now have the wider scope) still work for the narrower scope check — no forced re-consent on rollback.

The contacts dedup migration is the riskier piece. The migration is written idempotently and has a straightforward reverse (`DROP CONSTRAINT`; the duplicate rows cannot be restored but no production data is lost because the keeper row is the one that downstream loops reference after the re-point). If the migration fails mid-flight, partial state is recoverable via the standard yoyo rollback path.

## Open Questions

- **Peer-field update via `onChangeAction`.** Does `TextInput.autoCompleteAction`'s selection event allow an action handler to return `UpdateCard` that atomically sets another input's value, or does the framework only allow updating the current field? Needs a small spike on day 1 of implementation. Fallback is the single combined "Recruiter: Name <email>" field (Alternative 1 in §Detailed Design §3). _Owner:_ implementation engineer.
- **Verification status of `directory.readonly`.** Is LRP's OAuth app currently Google-verified, and does `directory.readonly` (sensitive scope) trigger any additional friction on the consent screen for internal-only distribution? Likely fine for Workspace-internal add-ons, but worth confirming before staging so a scary "unverified app" warning doesn't surprise Nim. _Owner:_ Nim or Kinematic.
- **Photo URL lifetime.** Google `lh3.googleusercontent.com` URLs are generally stable but can expire on user photo change or account deletion. Initial implementation stores and uses them as-is. If we start seeing broken images in the wild (tracked via a lightweight server-side HEAD check on loop detail render, or via coordinator reports), we add a refresh-on-miss path. _Owner:_ post-launch observation; not blocking.

## Milestones and Timeline

| Phase | Description | Estimated Duration |
| ----- | ----------- | ------------------ |
| Phase 1 | Scope change + consent flow verified in staging | 0.5 day |
| Phase 2 | `/addon/directory/search` endpoint + People API wrapper | 0.5 day |
| Phase 3 | Card form autocomplete wiring + `onChangeAction` spike | 1 day |
| Phase 4 | Avatar persistence, rendering in loop detail card | 0.5 day |
| Phase 5 | Contacts dedup migration + upsert SQL | 0.5 day |
| Phase 6 | End-to-end testing in staging; Nim walkthrough | 0.5 day |

Total: **~3 days** of focused eng time. Deploy to prod at the end of the third day; success evaluation at T+1 week.
