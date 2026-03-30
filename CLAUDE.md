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

## Metrics (Business Impact)
- **Mean Time to Interview (MTTI):** Hours from client request to interview (lower is better)
- **Interview Fulfillment Rate (IFR):** % of requests resulting in completed interviews (higher is better)
