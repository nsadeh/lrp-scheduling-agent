"""Create scheduling loop tables.

coordinators, contacts, candidates, loops, stages, events, threads, time slots.
"""

from yoyo import step

step(
    """
    CREATE TABLE coordinators (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        email       TEXT NOT NULL UNIQUE,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE client_contacts (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        email       TEXT NOT NULL,
        company     TEXT NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE contacts (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        email       TEXT NOT NULL,
        role        TEXT NOT NULL,
        company     TEXT,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE candidates (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        notes       TEXT,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE loops (
        id                  TEXT PRIMARY KEY,
        coordinator_id      TEXT NOT NULL REFERENCES coordinators(id),
        client_contact_id   TEXT NOT NULL REFERENCES client_contacts(id),
        recruiter_id        TEXT NOT NULL REFERENCES contacts(id),
        client_manager_id   TEXT REFERENCES contacts(id),
        candidate_id        TEXT NOT NULL REFERENCES candidates(id),
        title               TEXT NOT NULL,
        notes               TEXT,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE stages (
        id          TEXT PRIMARY KEY,
        loop_id     TEXT NOT NULL REFERENCES loops(id),
        name        TEXT NOT NULL,
        state       TEXT NOT NULL DEFAULT 'new',
        ordinal     INT NOT NULL DEFAULT 0,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE loop_events (
        id              TEXT PRIMARY KEY,
        loop_id         TEXT NOT NULL REFERENCES loops(id),
        stage_id        TEXT REFERENCES stages(id),
        event_type      TEXT NOT NULL,
        data            JSONB NOT NULL DEFAULT '{}',
        actor_email     TEXT NOT NULL,
        occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX idx_loop_events_loop ON loop_events(loop_id, occurred_at);
    CREATE INDEX idx_loop_events_stage ON loop_events(stage_id, occurred_at);

    CREATE TABLE loop_email_threads (
        id              TEXT PRIMARY KEY,
        loop_id         TEXT NOT NULL REFERENCES loops(id),
        gmail_thread_id TEXT NOT NULL,
        subject         TEXT,
        linked_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(loop_id, gmail_thread_id)
    );

    CREATE TABLE time_slots (
        id                  TEXT PRIMARY KEY,
        stage_id            TEXT NOT NULL REFERENCES stages(id),
        start_time          TIMESTAMPTZ NOT NULL,
        duration_minutes    INT NOT NULL DEFAULT 60,
        timezone            TEXT NOT NULL,
        zoom_link           TEXT,
        notes               TEXT,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    """
    DROP TABLE IF EXISTS time_slots;
    DROP TABLE IF EXISTS loop_email_threads;
    DROP TABLE IF EXISTS loop_events;
    DROP TABLE IF EXISTS stages;
    DROP TABLE IF EXISTS loops;
    DROP TABLE IF EXISTS candidates;
    DROP TABLE IF EXISTS contacts;
    DROP TABLE IF EXISTS client_contacts;
    DROP TABLE IF EXISTS coordinators;
    """,
)
