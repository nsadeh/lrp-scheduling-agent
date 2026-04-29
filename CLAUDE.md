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
- **Railway changes must update the deployment guide.** Whenever a PR introduces a new Railway service, a new required env var, a provisioning step, or any other action that a human has to take in the Railway dashboard or CLI to ship the change, update [references/railway-deployment.md](references/railway-deployment.md) in the same PR. The guide is the single source of truth for what prod/staging need to look like — if it's not in there, it's not going to get done.
- **Env var additions must update the env var reference.** When adding or removing an `os.environ.get(...)` call, update [references/env-vars.md](references/env-vars.md) to match. Keep entries grounded in `file:line` citations so the doc ages gracefully.
- **Never hardcode model names in eval scripts.** Eval scripts must read the model from the LangFuse prompt config (`prompt.config.get("model")`). Only override the model when explicitly asked — do not silently flip a Gemini prompt to Anthropic or vice versa.

## Environment Variables

See [references/env-vars.md](references/env-vars.md) for the full inventory — every var read by the codebase, with file:line citations, defaults, and current dev status.

## Metrics (Business Impact)

- **Mean Time to Interview (MTTI):** Hours from client request to interview (lower is better)
- **Interview Fulfillment Rate (IFR):** % of requests resulting in completed interviews (higher is better)
