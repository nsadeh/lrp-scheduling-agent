# LRP Scheduling Agent

## Overview

A human-in-the-loop scheduling agent for Long Ridge Partners, an executive search firm specializing in hedge funds and private equity. The agent helps coordinators efficiently schedule interviews between clients (hirers) and candidates by drafting emails, tracking scheduling threads, and updating systems — all requiring coordinator approval before any action is taken.

## UX Philosophy

The user experience goal for this application is "user opens sidebar, click type click, everything is done." That is, all the next todos are displayed cleanly on the Google Add-on sidebar. The AI does a great job of classifying and preparing the actions. All the user has to do is open the sidebar, approve every suggestion, potentially make light edits, and all their work is done. They do not have to open more windows, write separate emails, etc.

In a call, I showed them the Cursor "tab" button that is a gag gift for developers - it's a keyboard composed of just a large tab key, which is the approve autocomplete hotkey for most in-IDE coding copilots. Coding with them often felt like just pressing the tab key all day when they were doing a good job. This should feel like that - they're just clicking "yes."

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

## Environment Variables

- `DATABASE_URL` — Postgres connection string
- `REDIS_URL` — Redis connection string
- `GOOGLE_SERVICE_ACCOUNT_KEY` — Service account JSON for Gmail/Calendar domain-wide delegation
- `ENCORE_API_URL` — Cluein data connector endpoint for Encore
- `ENCORE_API_KEY` — Cluein API key
- `ANTHROPIC_API_KEY` — Claude API key for agent reasoning
- `PUBSUB_TOPIC` — Google Cloud Pub/Sub topic for Gmail push notifications (e.g., `projects/my-project/topics/gmail-push`)
- `PUBSUB_WEBHOOK_AUDIENCE` — Expected OIDC audience for verifying Pub/Sub push tokens (must be set in production)
- `PUBSUB_SERVICE_ACCOUNT` — Email of the service account that signs Pub/Sub push messages (default: `gmail-api-push@system.gserviceaccount.com`)
- `GMAIL_TOKEN_ENCRYPTION_KEY` — Fernet key for encrypting stored Gmail OAuth refresh tokens
- `REQUIRED_SCOPES` — Comma-separated list of Gmail OAuth scopes required for coordinator tokens

## Metrics (Business Impact)

- **Mean Time to Interview (MTTI):** Hours from client request to interview (lower is better)
- **Interview Fulfillment Rate (IFR):** % of requests resulting in completed interviews (higher is better)
