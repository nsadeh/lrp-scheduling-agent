# LRP Scheduling Agent

## Overview
A human-in-the-loop scheduling agent for Long Ridge Partners, an executive search firm specializing in hedge funds and private equity. The agent helps coordinators efficiently schedule interviews between clients (hirers) and candidates by drafting emails, tracking scheduling threads, and updating systems — all requiring coordinator approval before any action is taken.

## Architecture
- **services/api**: Python FastAPI backend — the agent brain. Connects to Gmail (domain-wide delegation), Google Calendar, and Encore (via Cluein). Owns the database for scheduling threads, client preferences, and coordinator profiles.
- **services/addon**: Google Workspace Add-on (Apps Script) — Gmail sidebar UI with drafts tab and status board tab. Calls the backend API when coordinators interact with scheduling threads.

## Key Domain Concepts
- **Coordinator**: LRP employee who manages scheduling. Primary user of the agent.
- **Recruiter**: LRP employee who owns the candidate relationship. Agent drafts emails TO recruiters, never to candidates directly.
- **Client/Hirer**: The company requesting interviews.
- **Candidate**: Person being interviewed. Identified by email address.
- **Encore**: LRP's ATS/CRM system (accessed via Cluein Data Connector). Source of truth for candidate/recruiter records.
- **Scheduling thread**: One Gmail thread per interview request, tracked through states: new request → awaiting availability → awaiting confirmation → confirmed → systems updated.

## Iron Law: No Dev-Only Hacks

**Dev exists to prove production works.** Never write code that only works in development — no hardcoded fallback emails, no `if ENVIRONMENT == "development"` shortcuts, no silent defaults that mask broken auth flows. If something doesn't work in dev, it must be fixed the same way it would be fixed in production. A dev hack that makes the sidebar "work" by returning fake data is worse than a visible error, because it hides a real problem that will surface in production. Every code path exercised in dev must be a code path that runs in production.

## Critical Constraints
- **No autonomous sending.** Every outbound email requires explicit coordinator approval.
- **No candidate-facing communication.** The recruiter owns the candidate relationship.
- **No visibility beyond coordinators.** Recruiters, CMs, and clients are unaware of the agent.
- **Learning = structured rules, not fine-tuning.** Client preferences and corrections stored as data, not model changes.

## Development
- Local infra: `docker compose up -d` (Postgres, Redis)
- API dev: `./scripts/dev-api.sh`
- All services: `./scripts/dev-all.sh`

## Conventions
- See the postgres, python-microservices skills for stack conventions
- RFCs go in rfcs/ — use the rfc-writer skill
- Reference docs go in references/
- Database IDs use prefixed NanoIDs (e.g., `thr_` for threads, `cli_` for clients, `crd_` for coordinators)

## Code Style: Prefer Functional Patterns

Minimize mutable shared state. This codebase has been bitten by Python's mutable-state-across-modules pitfalls (e.g., importing a module-level string by value instead of by reference). Follow these principles:

1. **Pure functions over mutable globals.** If a value needs to be shared across modules, use a getter function (`get_action_url()`) rather than a module-level variable (`_action_url`). Functions always return the current value; imported variables capture a snapshot.

2. **Immutable data by default.** Use `frozen=True` on dataclasses, `Literal` types over mutable enums where practical, and `tuple` over `list` for fixed collections.

3. **Dependency injection over global singletons.** Pass services, configs, and clients as function/constructor arguments — not via module-level globals or `import`-time side effects. The arq worker `ctx` dict and FastAPI `app.state` are the composition roots.

4. **No import-time side effects.** Module imports should not trigger network calls, database connections, or state mutations. Initialization belongs in explicit `startup()` / `lifespan()` functions.

5. **Context managers for resources.** Database connections, locks, and spans should use `with`/`async with` — never manual acquire/release spread across functions.

## Environment Variables
- `DATABASE_URL` — Postgres connection string
- `REDIS_URL` — Redis connection string
- `GOOGLE_SERVICE_ACCOUNT_KEY` — Service account JSON for Gmail/Calendar domain-wide delegation
- `ENCORE_API_URL` — Cluein data connector endpoint for Encore
- `ENCORE_API_KEY` — Cluein API key
- `ANTHROPIC_API_KEY` — Claude API key for agent reasoning

## Metrics (Business Impact)
- **Mean Time to Interview (MTTI):** Hours from client request to interview (lower is better)
- **Interview Fulfillment Rate (IFR):** % of requests resulting in completed interviews (higher is better)
