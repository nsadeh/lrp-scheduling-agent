# Long Ridge Partners Scheduling Agent — Approved Proposal

Source: LRP Scheduling Proposal.pdf (approved March 2026)

## Goals

1. **Draft emails for coordinators.** The agent composes scheduling emails including availability requests, confirmations, and Zoom link forwards.
2. **Stay on top of every thread.** The agent keeps track of every in-progress scheduling request and makes sure nothing gets dropped. If a recruiter hasn't responded, a client hasn't confirmed, or a Zoom link hasn't been forwarded, the agent drafts a follow-up for the coordinator.
3. **Update systems automatically.** Upon coordinator approval, the agent creates the shared LRP interview calendar entry, creates the Encore interview activity, and drafts the prep call invite for CM, recruiter, and candidate.

## Non-Goals

1. **No autonomous sending.** Every email requires coordinator approval. The agent will not have the capability to send emails autonomously.
2. **No visibility beyond coordinators.** Recruiters, CMs, and clients will not interact with or be aware of the agent.
3. **No candidate-facing communication.** The recruiter owns the candidate relationship. The agent drafts to recruiters, never to candidates.
4. **No judgment calls.** When a situation requires human judgment, the agent asks the coordinator.

## Metrics

- **Mean Time to Interview (MTTI):** Time (hours) from client interview request to interview taking place (less is better).
- **Interview Fulfillment Rate (IFR):** Percentage of client interview requests that result in a completed interview (more is better).
- Baseline: computed from Encore and Gmail data, Jan–March 2026.

## User Interface

The agent lives inside Gmail as a **Google Workspace add-on**. Two tabs:

### Drafts Tab
- Sidebar panel appears when coordinator opens a scheduling email with the agent's recommended action.
- Three options on every draft: send as-is, edit by hand, or ask for corrections.
- "Agent queue" label in Gmail's left sidebar collecting all active threads with status indicators.

### Status Board Tab
- Shows every active scheduling thread grouped by who's blocking it (waiting on recruiter, waiting on client, confirmed but pending systems updates).
- Each thread shows client, candidate, current step, and how long it's been waiting. Overdue items flagged in red.
- Summary stats: total active threads, overdue count, average time to interview.

### Gmail Requirement
The add-on requires the Gmail web interface (not Outlook). LRP runs on Google Workspace; one coordinator may need to switch from Outlook.

## Learning

- Learns client-specific workflows on the fly by asking coordinators and saving responses to internal client profiles.
- No model fine-tuning or retraining. Learning is stored as structured preferences and rules.

## System Architecture

Backend service hosted in the cloud connecting to three systems:

- **Gmail** (via Google Workspace APIs): Reads emails, creates drafts, sends approved emails, manages Agent queue label. Uses domain-wide delegation via service account.
- **Google Calendar** (via Google Workspace APIs): Creates/updates shared LRP interview calendar entries and prep call invites.
- **Encore** (via Cluein Data Connector): Reads candidate/recruiter records. Writes interview activities. Read/write connection scoped to interview-related data only.

The Gmail sidebar add-on calls the backend service when a coordinator opens a thread.

## Data Model

- **Scheduling threads:** One record per active interview request. Tracks state, parties involved, timestamps, next expected action.
- **Client preferences:** Learned per-client settings (Zoom link behavior, who sends invites, availability-first party). Updated via coordinator answers/corrections.
- **Coordinator profiles:** Which coordinator owns which clients, draft corrections informing future drafts.

No candidate resumes, compensation data, or sensitive recruiting info stored. Candidate identification uses email addresses as unique keys.

## Request Flow

1. Scheduling email arrives in coordinator's Gmail inbox.
2. Backend detects, classifies (new request, availability response, confirmation, reschedule, cancellation), identifies client/candidate/recruiter from thread and Encore.
3. Backend determines next action and drafts a response.
4. Coordinator opens email; sidebar displays draft and action buttons.
5. Coordinator approves/edits/corrects. Backend sends email, updates thread state, triggers system updates (calendar, Encore, prep call).

If coordinator doesn't open the email, item sits in Agent queue with escalating age indicators.

## Key Risks

- **Wrong candidate:** Identifies by email; asks coordinator when ambiguous.
- **Wrong recruiter:** Surfaces discrepancy if thread recruiter doesn't match Encore owner.
- **Overlapping schedules:** Tracks availability across coordinator accounts, flags conflicts.
- **Misdirected email:** Every email requires explicit coordinator approval.
- **System sync failures:** Queues update and surfaces failure in status board.
- **Agent downtime:** Agent is an overlay; coordinators schedule manually if it goes down.

## Data Security

- All data encrypted in transit (TLS) and at rest.
- Gmail access scoped to coordinator accounts only.
- Encore access scoped to interview-related records only.
- No sensitive candidate info beyond names/emails.
- No data shared with third parties. Anthropic does not retain/train on processed data.
- LRP Workspace admin retains full control over agent access.
