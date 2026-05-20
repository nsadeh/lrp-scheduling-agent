"""Eval scenarios: NextActionAgent must NOT emit a duplicate
UPDATE_ACTOR(role=recruiter) when one is already pending on the loop.

The resolver-synthesized UPDATE_ACTOR (Phase 3) lands on the loop before
the next-action agent runs. The agent's `_suggestion_fingerprint` dedup
guard at next_action_agent.py:285 should suppress any re-emission.

These are unit-level scenarios driving the same dedup function the agent
uses in `_validate_batch` — equivalent to an eval-set assertion without
needing the full agent harness.
"""

from __future__ import annotations

from api.classifier.models import SuggestedAction
from api.classifier.next_action_agent import _suggestion_fingerprint


class TestRecruiterUpdateActorDedup:
    def test_resolver_emit_dedupes_against_agent_reemit(self):
        """Resolver-emitted and agent-emitted UPDATE_ACTOR(recruiter) with
        identical (loop_id, action, action_data) must share a fingerprint."""
        resolver_fp = _suggestion_fingerprint(
            loop_id="lop_99",
            action=SuggestedAction.UPDATE_ACTOR,
            action_data={"role": "recruiter"},
        )
        agent_fp = _suggestion_fingerprint(
            loop_id="lop_99",
            action=SuggestedAction.UPDATE_ACTOR,
            action_data={"role": "recruiter"},
        )
        assert resolver_fp == agent_fp

    def test_dedup_distinguishes_different_loops(self):
        """An UPDATE_ACTOR(recruiter) on loop A must NOT dedup an
        UPDATE_ACTOR(recruiter) on loop B — multi-loop classifier passes."""
        fp_a = _suggestion_fingerprint(
            loop_id="lop_a",
            action=SuggestedAction.UPDATE_ACTOR,
            action_data={"role": "recruiter"},
        )
        fp_b = _suggestion_fingerprint(
            loop_id="lop_b",
            action=SuggestedAction.UPDATE_ACTOR,
            action_data={"role": "recruiter"},
        )
        assert fp_a != fp_b

    def test_dedup_distinguishes_recruiter_from_client_manager(self):
        """An UPDATE_ACTOR(recruiter) must NOT dedup an UPDATE_ACTOR(client_manager)
        on the same loop — the resolver only emits for recruiter, the agent may
        still emit for the other actors."""
        recruiter_fp = _suggestion_fingerprint(
            loop_id="lop_99",
            action=SuggestedAction.UPDATE_ACTOR,
            action_data={"role": "recruiter"},
        )
        cm_fp = _suggestion_fingerprint(
            loop_id="lop_99",
            action=SuggestedAction.UPDATE_ACTOR,
            action_data={"role": "client_manager"},
        )
        assert recruiter_fp != cm_fp

    def test_simulated_dedup_against_pending_set(self):
        """Mimic the agent's `seen_fingerprints` check: an incoming UPDATE_ACTOR
        whose fingerprint matches the resolver-emitted PENDING one is dropped."""
        seen: set[str] = {
            _suggestion_fingerprint(
                loop_id="lop_99",
                action=SuggestedAction.UPDATE_ACTOR,
                action_data={"role": "recruiter"},
            )
        }
        incoming_fp = _suggestion_fingerprint(
            loop_id="lop_99",
            action=SuggestedAction.UPDATE_ACTOR,
            action_data={"role": "recruiter"},
        )
        assert incoming_fp in seen, "Agent must drop duplicate UPDATE_ACTOR(recruiter)"
