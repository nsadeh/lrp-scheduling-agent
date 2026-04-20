# People API enablement — per-environment ops step

The recruiter directory autocomplete in the create-loop form calls Google's
**People API** (specifically `people.googleapis.com/v1/people:searchDirectoryPeople`)
once per coordinator keystroke (after Google's debounce) and once at
loop-creation time to capture the recruiter's avatar URL.

The People API must be **explicitly enabled** in the GCP project that owns
the OAuth client before Google will accept any request — *even with a valid,
correctly-scoped user token*. A disabled API returns `403 Forbidden` with a
message like:

> People API has not been used in project `<NNN>` before or it is disabled.
> Enable it by visiting `https://console.developers.google.com/apis/api/people.googleapis.com/overview?project=<NNN>`

This is a one-time step per GCP project. OAuth consent, scope grants, and
Railway env vars do not substitute for it.

## When to do it

Before the first coordinator in an environment clicks "Authorize Gmail
Access" after the directory-autocomplete feature ships. If you forget, the
symptom is: coordinator types in the recruiter field, nothing populates in
the dropdown. The API server logs show
`directory/search: People API call failed for <coordinator>` with a 403.

## Who to do it as

Anyone with the **Service Usage Admin** role (or Owner / Editor) on the
target GCP project.

## Environments and status

| Environment | GCP project | Status |
| ----------- | ----------- | ------ |
| **Staging** | `ai-agents-dev-492713` (numeric: `571926031175`) | ✅ Enabled 2026-04-20 |
| **Production** | TBD — confirm with `gcloud workspace-add-ons deployments describe lrp-scheduling-prod` | ⬜ Pending — do this before the prod rollout |

Update the table when each environment is enabled so the next person doesn't
have to guess.

## How to enable — two options

### Option A: CLI (preferred)

```sh
gcloud services enable people.googleapis.com --project=<PROJECT_ID>
```

For staging (already done):

```sh
gcloud services enable people.googleapis.com --project=ai-agents-dev-492713
```

For prod, substitute the prod project ID. You can find it by running:

```sh
# Shows the OAuth scopes AND which GCP project backs the deployment
gcloud workspace-add-ons deployments describe lrp-scheduling-prod
```

### Option B: Console UI

1. Sign in to https://console.cloud.google.com with an account that has
   Service Usage Admin on the target project.
2. Switch to the correct project in the top-of-page project picker.
3. Navigate to **APIs & Services → Library**.
4. Search for **People API**. Click the result.
5. Click **Enable**. Propagation is usually seconds; Google's message says
   to allow up to a few minutes.

## Verify it worked

After enabling, have a coordinator (or yourself in staging) re-open the
create-loop form and type two characters in the Recruiter Name field. A
dropdown should populate with Workspace members matching the query.

If it still 403s, check the API server logs. Two specific log lines tell
you where the problem is:

- `directory/search: scope error for <email> (missing=[...])` — the
  coordinator's stored token is missing `directory.readonly`. Resolution:
  have them click "Authorize Gmail Access" to re-consent (the scope-check
  pre-check in `_handle_show_create_form` should surface this automatically,
  but if it doesn't, clearing their row from `gmail_tokens` forces the flow).
- `directory/search: People API call failed for <email>` with a traceback
  containing `403 Forbidden` — People API still isn't enabled for this
  project, or the caller's token is for a different project than you
  enabled it in. Double-check `<PROJECT_ID>` in the 403 message against the
  project you enabled.

## Why this isn't automatable from the app

Enabling a Google API is an administrative action on the GCP project
itself — it's not something an OAuth-scoped user token can do at runtime.
That's deliberate on Google's side (you don't want a compromised token to
silently turn on billable APIs). The cost is this one-time operational
step per environment; the benefit is the project-level API surface is
explicitly controlled by humans with GCP admin access.

## Cost

People API `searchDirectoryPeople` is free at our volume. Google's documented
per-user quota for the default tier is 90 requests/second; our worst-case
projection (5 coordinators × 10 loops/day × ~3 debounced keystrokes =
~150 requests/day total) is below the noise floor.
