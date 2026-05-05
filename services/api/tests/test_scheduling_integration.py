"""Integration tests for scheduling loops against a real Postgres database.

These tests hit the actual database to verify the full stack:
aiosql queries → LoopService → real SQL → real Postgres.

Requires: docker compose up -d postgres
"""

import os

import pytest
from psycopg_pool import AsyncConnectionPool

from api.scheduling.models import StageState
from api.scheduling.service import InvalidTransitionError, LoopService

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://dev:dev@localhost:5432/lrp_dev")

EMAIL = "testcoord@longridgepartners.com"


@pytest.fixture
async def pool():
    p = AsyncConnectionPool(conninfo=DATABASE_URL)
    await p.open()
    yield p
    await p.close()


@pytest.fixture
async def svc(pool):
    return LoopService(db_pool=pool, gmail=None)


@pytest.fixture(autouse=True)
async def cleanup(pool):
    """Clean up test data after each test."""
    yield
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM email_drafts")
        await conn.execute("DELETE FROM agent_suggestions")
        await conn.execute("DELETE FROM time_slots")
        await conn.execute("DELETE FROM loop_email_threads")
        await conn.execute("DELETE FROM loop_events")
        await conn.execute("DELETE FROM stages")
        await conn.execute("DELETE FROM loops")
        await conn.execute("DELETE FROM candidates")
        await conn.execute("DELETE FROM contacts")
        await conn.execute("DELETE FROM client_contacts")
        await conn.execute("DELETE FROM coordinators WHERE email = %s", (EMAIL,))


async def _create_test_loop(svc: LoopService) -> dict:
    """Helper: create contacts and a loop, return IDs."""
    client = await svc.find_or_create_client_contact(
        name="Jane Doe", email="jane@acme.com", company="Acme Capital"
    )
    recruiter = await svc.find_or_create_contact(
        name="Bob Lee", email="bob@recruit.com", role="recruiter"
    )
    cm = await svc.find_or_create_contact(
        name="Sarah Kim", email="sarah@lrp.com", role="client_manager"
    )
    loop = await svc.create_loop(
        coordinator_email=EMAIL,
        coordinator_name="Test Coordinator",
        candidate_name="John Smith",
        client_contact_id=client.id,
        recruiter_id=recruiter.id,
        client_manager_id=cm.id,
        title="Smith, Acme Capital",
        first_stage_name="Round 1",
    )
    return {
        "loop": loop,
        "client": client,
        "recruiter": recruiter,
        "cm": cm,
    }


class TestCoordinators:
    async def test_get_or_create(self, svc: LoopService):
        c1 = await svc.get_or_create_coordinator("Alice", EMAIL)
        assert c1.email == EMAIL
        assert c1.id.startswith("crd_")

        # Upsert returns same ID
        c2 = await svc.get_or_create_coordinator("Alice Updated", EMAIL)
        assert c2.id == c1.id

    async def test_get_by_email(self, svc: LoopService):
        await svc.get_or_create_coordinator("Alice", EMAIL)
        found = await svc.get_coordinator_by_email(EMAIL)
        assert found is not None
        assert found.email == EMAIL

    async def test_get_by_email_not_found(self, svc: LoopService):
        found = await svc.get_coordinator_by_email("nobody@example.com")
        assert found is None


class TestContacts:
    async def test_create_contact(self, svc: LoopService):
        c = await svc.find_or_create_contact(name="Bob", email="bob@test.com", role="recruiter")
        assert c.id.startswith("con_")
        assert c.role == "recruiter"

    async def test_create_client_contact(self, svc: LoopService):
        c = await svc.find_or_create_client_contact(
            name="Jane", email="jane@acme.com", company="Acme"
        )
        assert c.id.startswith("cli_")
        assert c.company == "Acme"

    async def test_create_candidate(self, svc: LoopService):
        c = await svc.find_or_create_candidate("John Smith")
        assert c.id.startswith("can_")
        assert c.name == "John Smith"

    async def test_search_contacts(self, svc: LoopService):
        await svc.find_or_create_contact(name="Bob Lee", email="bob@test.com", role="recruiter")
        await svc.find_or_create_contact(
            name="Bobby Jones", email="bobby@test.com", role="recruiter"
        )
        await svc.find_or_create_contact(
            name="Alice", email="alice@test.com", role="client_manager"
        )
        results = await svc.search_contacts("Bob")
        assert len(results) == 2

        results = await svc.search_contacts("Bob", role="recruiter")
        assert len(results) == 2

        results = await svc.search_contacts("Ali")
        assert len(results) == 1

    async def test_search_client_contacts(self, svc: LoopService):
        await svc.find_or_create_client_contact(
            name="Jane Doe", email="jane@acme.com", company="Acme"
        )
        results = await svc.search_client_contacts("Jan")
        assert len(results) == 1
        assert results[0].name == "Jane Doe"

    async def test_find_or_create_contact_reuses_existing_by_email_and_role(
        self, svc: LoopService, pool
    ):
        first = await svc.find_or_create_contact(
            name="Alice Adams", email="alice@lrp.com", role="recruiter"
        )
        # Second call with same (email, role) but a different typed name —
        # must reuse the existing row and must NOT overwrite the name.
        second = await svc.find_or_create_contact(
            name="Alice A.", email="alice@lrp.com", role="recruiter"
        )
        assert second.id == first.id
        assert second.name == "Alice Adams"

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT count(*), max(name) FROM contacts WHERE email = %s",
                    ("alice@lrp.com",),
                )
            ).fetchone()
        assert row[0] == 1
        assert row[1] == "Alice Adams"

    async def test_find_or_create_contact_same_email_different_role_creates_new(
        self, svc: LoopService
    ):
        recruiter = await svc.find_or_create_contact(
            name="Alex", email="alex@lrp.com", role="recruiter"
        )
        cm = await svc.find_or_create_contact(
            name="Alex", email="alex@lrp.com", role="client_manager"
        )
        # Same email but different role = different logical contact.
        assert recruiter.id != cm.id

    async def test_find_or_create_contact_creates_new_when_email_differs(self, svc: LoopService):
        a = await svc.find_or_create_contact(name="Alice", email="alice@lrp.com", role="recruiter")
        b = await svc.find_or_create_contact(name="Bob", email="bob@lrp.com", role="recruiter")
        assert a.id != b.id

    async def test_find_or_create_client_contact_reuses_existing_by_email(
        self, svc: LoopService, pool
    ):
        first = await svc.find_or_create_client_contact(
            name="Jane Doe", email="jane@acme.com", company="Acme"
        )
        second = await svc.find_or_create_client_contact(
            name="Jane D.", email="jane@acme.com", company="Different Co"
        )
        assert second.id == first.id
        assert second.name == "Jane Doe"
        assert second.company == "Acme"

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT count(*) FROM client_contacts WHERE email = %s",
                    ("jane@acme.com",),
                )
            ).fetchone()
        assert row[0] == 1

    async def test_get_contact_by_email(self, svc: LoopService):
        created = await svc.find_or_create_contact(
            name="Alice", email="alice@lrp.com", role="recruiter"
        )
        found = await svc.get_contact_by_email("alice@lrp.com", role="recruiter")
        assert found is not None
        assert found.id == created.id

        # Wrong role — no match.
        assert await svc.get_contact_by_email("alice@lrp.com", role="client_manager") is None
        # Unknown email — no match.
        assert await svc.get_contact_by_email("nobody@lrp.com", role="recruiter") is None

    async def test_get_client_contact_by_email(self, svc: LoopService):
        created = await svc.find_or_create_client_contact(
            name="Jane", email="jane@acme.com", company="Acme"
        )
        found = await svc.get_client_contact_by_email("jane@acme.com")
        assert found is not None
        assert found.id == created.id
        assert await svc.get_client_contact_by_email("nobody@acme.com") is None


class TestCreateLoop:
    async def test_creates_loop_with_stage_and_events(self, svc: LoopService):
        data = await _create_test_loop(svc)
        loop = data["loop"]

        assert loop.id.startswith("lop_")
        assert loop.title == "Smith, Acme Capital"
        assert loop.candidate is not None
        assert loop.candidate.name == "John Smith"
        assert loop.client_contact is not None
        assert loop.recruiter is not None
        assert loop.client_manager is not None

        # Should have one stage in 'new' state
        assert len(loop.stages) == 1
        assert loop.stages[0].name == "Round 1"
        assert loop.stages[0].state == StageState.NEW

        # Should have events: loop_created + stage_created
        events = await svc.get_events(loop.id)
        event_types = [e.event_type for e in events]
        assert "loop_created" in event_types
        assert "stage_created" in event_types

    async def test_creates_loop_with_email_thread(self, svc: LoopService):
        client = await svc.find_or_create_client_contact(
            name="Jane", email="jane@acme.com", company="Acme"
        )
        recruiter = await svc.find_or_create_contact(
            name="Bob", email="bob@test.com", role="recruiter"
        )
        loop = await svc.create_loop(
            coordinator_email=EMAIL,
            coordinator_name="Test",
            candidate_name="Candidate",
            client_contact_id=client.id,
            recruiter_id=recruiter.id,
            title="Test Loop",
            gmail_thread_id="thread_abc123",
            gmail_subject="Re: Interview",
        )
        assert len(loop.email_threads) == 1
        assert loop.email_threads[0].gmail_thread_id == "thread_abc123"
        assert loop.email_threads[0].subject == "Re: Interview"

        events = await svc.get_events(loop.id)
        assert "thread_linked" in [e.event_type for e in events]


class TestGetLoop:
    async def test_get_loop_populates_all_relations(self, svc: LoopService):
        data = await _create_test_loop(svc)
        loop = await svc.get_loop(data["loop"].id)

        assert loop.coordinator is not None
        assert loop.coordinator.email == EMAIL
        assert loop.client_contact is not None
        assert loop.client_contact.company == "Acme Capital"
        assert loop.recruiter is not None
        assert loop.recruiter.name == "Bob Lee"
        assert loop.client_manager is not None
        assert loop.candidate is not None

    async def test_get_loop_not_found(self, svc: LoopService):
        with pytest.raises(ValueError, match="Loop not found"):
            await svc.get_loop("lop_nonexistent")


class TestStageStateMachine:
    async def test_advance_new_to_awaiting_candidate(self, svc: LoopService):
        data = await _create_test_loop(svc)
        stage = data["loop"].stages[0]

        result = await svc.advance_stage(stage.id, StageState.AWAITING_CANDIDATE, EMAIL)
        assert result.state == StageState.AWAITING_CANDIDATE

        events = await svc.get_events(data["loop"].id, stage_id=stage.id)
        advanced = [e for e in events if e.event_type == "stage_advanced"]
        assert len(advanced) == 1
        assert advanced[0].data["from_state"] == "new"
        assert advanced[0].data["to_state"] == "awaiting_candidate"

    async def test_full_happy_path(self, svc: LoopService):
        data = await _create_test_loop(svc)
        stage_id = data["loop"].stages[0].id

        await svc.advance_stage(stage_id, StageState.AWAITING_CANDIDATE, EMAIL)
        await svc.advance_stage(stage_id, StageState.AWAITING_CLIENT, EMAIL)
        await svc.advance_stage(stage_id, StageState.SCHEDULED, EMAIL)
        result = await svc.advance_stage(stage_id, StageState.COMPLETE, EMAIL)
        assert result.state == StageState.COMPLETE

    async def test_awaiting_client_back_to_awaiting_candidate(self, svc: LoopService):
        data = await _create_test_loop(svc)
        stage_id = data["loop"].stages[0].id

        await svc.advance_stage(stage_id, StageState.AWAITING_CANDIDATE, EMAIL)
        await svc.advance_stage(stage_id, StageState.AWAITING_CLIENT, EMAIL)
        result = await svc.advance_stage(stage_id, StageState.AWAITING_CANDIDATE, EMAIL)
        assert result.state == StageState.AWAITING_CANDIDATE


class TestMarkCold:
    async def test_mark_cold_from_new(self, svc: LoopService):
        data = await _create_test_loop(svc)
        stage_id = data["loop"].stages[0].id

        result = await svc.mark_cold(stage_id, EMAIL, reason="candidate withdrew")
        assert result.state == StageState.COLD

        events = await svc.get_events(data["loop"].id, stage_id=stage_id)
        cold_events = [e for e in events if e.event_type == "stage_marked_cold"]
        assert len(cold_events) == 1
        assert cold_events[0].data["reason"] == "candidate withdrew"

    async def test_mark_cold_from_awaiting_client(self, svc: LoopService):
        data = await _create_test_loop(svc)
        stage_id = data["loop"].stages[0].id

        await svc.advance_stage(stage_id, StageState.AWAITING_CANDIDATE, EMAIL)
        await svc.advance_stage(stage_id, StageState.AWAITING_CLIENT, EMAIL)
        result = await svc.mark_cold(stage_id, EMAIL)
        assert result.state == StageState.COLD


class TestReviveStage:
    async def test_revive_to_new(self, svc: LoopService):
        data = await _create_test_loop(svc)
        stage_id = data["loop"].stages[0].id

        await svc.mark_cold(stage_id, EMAIL)
        result = await svc.revive_stage(stage_id, StageState.NEW, EMAIL)
        assert result.state == StageState.NEW

    async def test_revive_to_awaiting_candidate(self, svc: LoopService):
        data = await _create_test_loop(svc)
        stage_id = data["loop"].stages[0].id

        await svc.mark_cold(stage_id, EMAIL)
        result = await svc.revive_stage(stage_id, StageState.AWAITING_CANDIDATE, EMAIL)
        assert result.state == StageState.AWAITING_CANDIDATE

    async def test_revive_to_awaiting_client(self, svc: LoopService):
        data = await _create_test_loop(svc)
        stage_id = data["loop"].stages[0].id

        await svc.mark_cold(stage_id, EMAIL)
        result = await svc.revive_stage(stage_id, StageState.AWAITING_CLIENT, EMAIL)
        assert result.state == StageState.AWAITING_CLIENT

    async def test_cannot_revive_non_cold_stage(self, svc: LoopService):
        data = await _create_test_loop(svc)
        stage_id = data["loop"].stages[0].id

        with pytest.raises(InvalidTransitionError):
            await svc.revive_stage(stage_id, StageState.NEW, EMAIL)

    async def test_revive_records_event(self, svc: LoopService):
        data = await _create_test_loop(svc)
        stage_id = data["loop"].stages[0].id

        await svc.mark_cold(stage_id, EMAIL)
        await svc.revive_stage(stage_id, StageState.NEW, EMAIL)

        events = await svc.get_events(data["loop"].id, stage_id=stage_id)
        revived = [e for e in events if e.event_type == "stage_revived"]
        assert len(revived) == 1
        assert revived[0].data["to_state"] == "new"


class TestAddStage:
    async def test_add_stage(self, svc: LoopService):
        data = await _create_test_loop(svc)
        loop_id = data["loop"].id

        stage2 = await svc.add_stage(loop_id, "Round 2", EMAIL)
        assert stage2.name == "Round 2"
        assert stage2.state == StageState.NEW
        assert stage2.ordinal == 1

        loop = await svc.get_loop(loop_id)
        assert len(loop.stages) == 2

    async def test_add_multiple_stages_ordinals(self, svc: LoopService):
        data = await _create_test_loop(svc)
        loop_id = data["loop"].id

        await svc.add_stage(loop_id, "Round 2", EMAIL)
        stage3 = await svc.add_stage(loop_id, "Final", EMAIL)
        assert stage3.ordinal == 2

        loop = await svc.get_loop(loop_id)
        assert len(loop.stages) == 3
        assert [s.name for s in loop.stages] == ["Round 1", "Round 2", "Final"]


class TestEmailThreads:
    async def test_link_thread(self, svc: LoopService):
        data = await _create_test_loop(svc)
        loop_id = data["loop"].id

        result = await svc.link_thread(loop_id, "thread_123", "Subject Line", EMAIL)
        assert result is not None
        assert result.gmail_thread_id == "thread_123"

        loop = await svc.get_loop(loop_id)
        assert len(loop.email_threads) == 1

    async def test_link_duplicate_thread_ignored(self, svc: LoopService):
        data = await _create_test_loop(svc)
        loop_id = data["loop"].id

        await svc.link_thread(loop_id, "thread_123", "Subject", EMAIL)
        result = await svc.link_thread(loop_id, "thread_123", "Subject", EMAIL)
        assert result is None  # duplicate ignored

        loop = await svc.get_loop(loop_id)
        assert len(loop.email_threads) == 1

    async def test_find_loop_by_thread(self, svc: LoopService):
        data = await _create_test_loop(svc)
        loop_id = data["loop"].id

        await svc.link_thread(loop_id, "thread_xyz", "Subject", EMAIL)

        found = await svc.find_loop_by_thread("thread_xyz")
        assert found is not None
        assert found.id == loop_id

    async def test_find_loop_by_thread_not_found(self, svc: LoopService):
        found = await svc.find_loop_by_thread("nonexistent_thread")
        assert found is None


class TestTimeSlots:
    async def test_add_time_slot(self, svc: LoopService):
        from datetime import UTC, datetime

        data = await _create_test_loop(svc)
        stage_id = data["loop"].stages[0].id

        ts = await svc.add_time_slot(
            stage_id=stage_id,
            start_time=datetime(2026, 4, 10, 14, 0, tzinfo=UTC),
            duration_minutes=60,
            timezone="America/New_York",
            coordinator_email=EMAIL,
            zoom_link="https://zoom.us/j/123",
        )
        assert ts.id.startswith("tms_")
        assert ts.duration_minutes == 60
        assert ts.zoom_link == "https://zoom.us/j/123"

        loop = await svc.get_loop(data["loop"].id)
        assert len(loop.stages[0].time_slots) == 1

    async def test_time_slot_event_recorded(self, svc: LoopService):
        from datetime import UTC, datetime

        data = await _create_test_loop(svc)
        stage_id = data["loop"].stages[0].id

        await svc.add_time_slot(
            stage_id=stage_id,
            start_time=datetime(2026, 4, 10, 14, 0, tzinfo=UTC),
            duration_minutes=45,
            timezone="America/New_York",
            coordinator_email=EMAIL,
        )
        events = await svc.get_events(data["loop"].id, stage_id=stage_id)
        ts_events = [e for e in events if e.event_type == "time_slot_added"]
        assert len(ts_events) == 1
        assert ts_events[0].data["duration_minutes"] == 45


class TestStatusBoard:
    async def test_empty_board_for_new_coordinator(self, svc: LoopService):
        board = await svc.get_status_board(EMAIL)
        assert board.action_needed == []
        assert board.waiting == []
        assert board.scheduled == []

    async def test_new_loop_appears_in_action_needed(self, svc: LoopService):
        await _create_test_loop(svc)
        board = await svc.get_status_board(EMAIL)
        assert len(board.action_needed) == 1
        assert board.action_needed[0].title == "Smith, Acme Capital"

    async def test_awaiting_stage_appears_in_waiting(self, svc: LoopService):
        data = await _create_test_loop(svc)
        stage_id = data["loop"].stages[0].id
        await svc.advance_stage(stage_id, StageState.AWAITING_CANDIDATE, EMAIL)

        board = await svc.get_status_board(EMAIL)
        assert len(board.waiting) == 1

    async def test_scheduled_stage_appears_in_scheduled(self, svc: LoopService):
        data = await _create_test_loop(svc)
        stage_id = data["loop"].stages[0].id
        await svc.advance_stage(stage_id, StageState.AWAITING_CANDIDATE, EMAIL)
        await svc.advance_stage(stage_id, StageState.AWAITING_CLIENT, EMAIL)
        await svc.advance_stage(stage_id, StageState.SCHEDULED, EMAIL)

        board = await svc.get_status_board(EMAIL)
        assert len(board.scheduled) == 1

    async def test_complete_loop_appears_in_complete(self, svc: LoopService):
        data = await _create_test_loop(svc)
        stage_id = data["loop"].stages[0].id
        await svc.advance_stage(stage_id, StageState.AWAITING_CANDIDATE, EMAIL)
        await svc.advance_stage(stage_id, StageState.AWAITING_CLIENT, EMAIL)
        await svc.advance_stage(stage_id, StageState.SCHEDULED, EMAIL)
        await svc.advance_stage(stage_id, StageState.COMPLETE, EMAIL)

        board = await svc.get_status_board(EMAIL)
        assert len(board.complete) == 1

    async def test_cold_loop_appears_in_cold(self, svc: LoopService):
        data = await _create_test_loop(svc)
        stage_id = data["loop"].stages[0].id
        await svc.mark_cold(stage_id, EMAIL)

        board = await svc.get_status_board(EMAIL)
        assert len(board.cold) == 1


class TestEvents:
    async def test_events_ordered_chronologically(self, svc: LoopService):
        data = await _create_test_loop(svc)
        stage_id = data["loop"].stages[0].id

        await svc.advance_stage(stage_id, StageState.AWAITING_CANDIDATE, EMAIL)
        await svc.advance_stage(stage_id, StageState.AWAITING_CLIENT, EMAIL)

        events = await svc.get_events(data["loop"].id)
        timestamps = [e.occurred_at for e in events]
        assert timestamps == sorted(timestamps)

    async def test_filter_events_by_stage(self, svc: LoopService):
        data = await _create_test_loop(svc)
        loop_id = data["loop"].id
        stage1_id = data["loop"].stages[0].id

        stage2 = await svc.add_stage(loop_id, "Round 2", EMAIL)

        await svc.advance_stage(stage1_id, StageState.AWAITING_CANDIDATE, EMAIL)

        # Events for stage 1 should include stage_created + stage_advanced
        s1_events = await svc.get_events(loop_id, stage_id=stage1_id)
        s1_types = [e.event_type for e in s1_events]
        assert "stage_created" in s1_types
        assert "stage_advanced" in s1_types

        # Events for stage 2 should only include stage_created
        s2_events = await svc.get_events(loop_id, stage_id=stage2.id)
        s2_types = [e.event_type for e in s2_events]
        assert s2_types == ["stage_created"]


class TestRouteIntegration:
    """Test the full route → service → DB → card response cycle."""

    @pytest.fixture
    async def client(self, pool):
        """HTTP client with real DB."""
        from httpx import ASGITransport, AsyncClient

        from api.main import app
        from api.overview.service import OverviewService

        app.state.db = pool
        app.state.scheduling = LoopService(db_pool=pool, gmail=None)
        app.state.overview_service = OverviewService(db_pool=pool)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    _TEST_EMAIL = "test-coordinator@longridgepartners.com"

    @staticmethod
    def _event(**extra):
        """Build a test add-on event with a proper userIdToken."""
        import base64
        import json

        payload = base64.urlsafe_b64encode(
            json.dumps({"email": TestRouteIntegration._TEST_EMAIL}).encode()
        ).decode()
        token = f"header.{payload}.signature"
        base = {
            "commonEventObject": {"hostApp": "GMAIL"},
            "authorizationEventObject": {"userIdToken": token},
        }
        base.update(extra)
        return base

    async def test_homepage_returns_overview(self, client):
        resp = await client.post("/addon/homepage", json=self._event())
        assert resp.status_code == 200
        nav = resp.json()["action"]["navigations"][0]
        card = nav.get("pushCard") or nav.get("updateCard")
        # Homepage now shows overview with Suggestions tab
        text = str(card)
        assert "Suggestions" in text or "caught up" in text.lower()

    async def test_on_message_unlinked_thread(self, client):
        resp = await client.post(
            "/addon/on-message",
            json=self._event(gmail={"threadId": "thread_new", "messageId": "msg_1"}),
        )
        assert resp.status_code == 200
        body = str(resp.json())
        assert "not linked" in body.lower() or "Create" in body

    async def test_create_loop_and_view(self, client, svc):
        await svc.find_or_create_client_contact(name="Jane", email="jane@acme.com", company="Acme")
        await svc.find_or_create_contact(name="Bob", email="bob@r.com", role="recruiter")

        import base64
        import json

        payload = base64.urlsafe_b64encode(
            json.dumps({"email": self._TEST_EMAIL}).encode()
        ).decode()
        token = f"header.{payload}.signature"

        resp = await client.post(
            "/addon/action",
            json={
                "commonEventObject": {
                    "hostApp": "GMAIL",
                    "invokedFunction": "create_loop",
                    "parameters": {"action_name": "create_loop"},
                    "formInputs": {
                        "candidate_name": {"stringInputs": {"value": ["Test Candidate"]}},
                        "client_name": {"stringInputs": {"value": ["Jane"]}},
                        "client_email": {"stringInputs": {"value": ["jane@acme.com"]}},
                        "client_company": {"stringInputs": {"value": ["Acme"]}},
                        "recruiter_name": {"stringInputs": {"value": ["Bob"]}},
                        "recruiter_email": {"stringInputs": {"value": ["bob@r.com"]}},
                        "first_stage_name": {"stringInputs": {"value": ["Round 1"]}},
                    },
                },
                "authorizationEventObject": {"userIdToken": token},
            },
        )
        assert resp.status_code == 200
        # After creating a loop, we return to the overview (not the old loop detail)
        body = str(resp.json())
        assert "caught up" in body.lower() or "navigations" in body

    async def test_homepage_shows_empty_state_when_no_suggestions(self, client, svc):
        """Homepage shows 'All caught up' when there are no pending suggestions."""
        resp = await client.post("/addon/homepage", json=self._event())
        assert resp.status_code == 200
        body = str(resp.json())
        assert "caught up" in body.lower()
