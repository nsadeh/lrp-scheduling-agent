"""Publish next-action-agent v42.

v42 = v41 verbatim + targeted edits:
  A. PRECOGNITION: append checklist item 13 (assumptions self-check).
  B. Rewrite Example 1: recruiter is now known (Rachel Kim); standardize
     coordinator name + loop id; refresh precog points 1/5/10; add point 13.
  C. Add precog point 13 to Examples 2 and 3.
  D. New Example 4 — ask_coordinator (unidentified third party "John Doe").
  E. New Example 5 — update_actor (scheduling request, recruiter unknown).
  F. Resolve the draft-email guidance contradiction so it agrees with
     the update_actor teaching.

The system message is built by fetching the immutable v41 prompt from
LangFuse and applying exact string substitutions — this guarantees the
unchanged ~32KB of prompt text is preserved byte-for-byte. The user
message and the model config are carried over from v41 verbatim.

Run from services/api:
    cd services/api && uv run python scripts/update_next_action_agent_v42.py
"""

# Embedded LangFuse example blocks are intentionally single long string
# literals (literal "\n\n" separators inside example prose) — wrapping them
# would alter prompt content. Same convention as the sibling update_*.py
# prompt-publish scripts.
# ruff: noqa: E501

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from langfuse import Langfuse

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

PROMPT_NAME = "next-action-agent"
BASE_VERSION = 41
TARGET_LABEL = "development"


def _replace_once(text: str, old: str, new: str, *, tag: str) -> str:
    """Substitute `old`→`new`, asserting `old` occurs exactly once."""
    count = text.count(old)
    if count != 1:
        raise SystemExit(
            f"[edit {tag}] expected exactly 1 occurrence of anchor, found {count}.\n"
            f"Anchor (first 120 chars): {old[:120]!r}"
        )
    return text.replace(old, new, 1)


# ---------------------------------------------------------------------------
# Edit A — PRECOGNITION: append item 13
# ---------------------------------------------------------------------------

PRECOG_OLD = (
    "12. How sure are you that you should do what you think you should do? "
    "If not completely sure, what are some alternatives?\n\n"
    "Summarize your thinking based on this checklist"
)
PRECOG_NEW = (
    "12. How sure are you that you should do what you think you should do? "
    "If not completely sure, what are some alternatives?\n"
    "13. Are you making any assumptions or presumptions? If so, are they "
    "strongly supported by the evidence? If not, do you need to clarify "
    "anything with the coordinator?\n\n"
    "Summarize your thinking based on this checklist"
)


# ---------------------------------------------------------------------------
# Edit F — resolve the draft-email guidance contradiction
# ---------------------------------------------------------------------------

DRAFT_GUIDANCE_OLD = (
    "So even if you do not know the actor's information (name/email), you "
    "can still make this suggestion. This is better than asking the user "
    "for their information."
)
DRAFT_GUIDANCE_NEW = (
    "If the actor you need to email (for example, the recruiter) is unknown "
    "on the loop, do NOT guess who they are — suggest `update_actor` first "
    "so the coordinator can set them. Once the actor is known, draft the "
    "email."
)


# ---------------------------------------------------------------------------
# Edit B — Example 1 rewrite (known recruiter Rachel Kim)
# ---------------------------------------------------------------------------

EX1_LOOP_ID_OLD = "<loop id='lop_1234'>"
EX1_LOOP_ID_NEW = "<loop id='lop_ABC001'>"

EX1_COORD_OLD = "<coordinator>Lisa Chen</coordinator>"
EX1_COORD_NEW = "<coordinator>Lisa Park</coordinator>"

EX1_RECRUITER_OLD = "<recruiter>Unknown for this loop</recruiter>"
EX1_RECRUITER_NEW = "<recruiter>Rachel Kim rachel@longridgepartners.com</recruiter>"

EX1_POINT1_OLD = (
    "David Chen (Apex Capital) is the client contact. Recruiter is unknown "
    "for this loop. Candidate is Jordan Martinez."
)
EX1_POINT1_NEW = (
    "David Chen (Apex Capital) is the client contact. Rachel Kim is the "
    "recruiter, a known loop actor. Candidate is Jordan Martinez."
)

EX1_POINT5_OLD = (
    "to collect Jordan's availability. The recruiter is unknown on this "
    "loop, but Lisa likely knows who it is. She can't contact the candidate "
    "directly"
)
EX1_POINT5_NEW = (
    "to collect Jordan's availability. Rachel Kim is a known recruiter on "
    "this loop, so we can draft straight to her. She can't contact the "
    "candidate directly"
)

EX1_POINT10_OLD = (
    "10. **Email check:** Sending to the recruiter. The recruiter is NOT on "
    "this thread and has no visibility into the client's request. This adds "
    "new information."
)
EX1_POINT10_NEW = (
    "10. **Email check:** Sending to Rachel Kim (the recruiter). She is NOT "
    "on this thread and has no visibility into the client's request. This "
    "adds new information."
)

# Add point 13 to Example 1 (anchor on its unique point-12 + Summary).
EX1_P13_OLD = (
    "12. **Confidence:** 0.95. Straightforward new interview request, clear "
    "next step.\\n\\n**Summary:** New interview request from the client"
)
EX1_P13_NEW = (
    "12. **Confidence:** 0.95. Straightforward new interview request, clear "
    "next step.\\n\\n13. **Assumptions:** No unsupported assumptions — Rachel "
    "Kim is a known loop actor and the client's interview request is "
    "explicit. Nothing to clarify with the coordinator.\\n\\n**Summary:** New "
    "interview request from the client"
)


# ---------------------------------------------------------------------------
# Edit C — add point 13 to Examples 2 and 3
# ---------------------------------------------------------------------------

EX2_P13_OLD = (
    "12. **Confidence:** 0.95. Clear availability response, standard next "
    "step.\\n\\n**Summary:** Rachel provided Jordan's avails."
)
EX2_P13_NEW = (
    "12. **Confidence:** 0.95. Clear availability response, standard next "
    "step.\\n\\n13. **Assumptions:** None unsupported — Rachel stated the "
    "three avail windows explicitly, and forwarding avails to the client is "
    "the standard next step. Nothing to clarify.\\n\\n**Summary:** Rachel "
    "provided Jordan's avails."
)

EX3_P13_OLD = (
    "12. **Confidence:** 0.95. Outbound confirmation to recruiter is the "
    "final step before `scheduled`.\\n\\n**Summary:** Outbound email"
)
EX3_P13_NEW = (
    "12. **Confidence:** 0.95. Outbound confirmation to recruiter is the "
    "final step before `scheduled`.\\n\\n13. **Assumptions:** None — the "
    "thread explicitly shows David picked Wednesday May 14 at 2pm and Lisa "
    "relayed exactly that to Rachel. The conclusion rests on stated facts, "
    "not inference.\\n\\n**Summary:** Outbound email"
)


# ---------------------------------------------------------------------------
# Edits D & E — new Examples 4 (ask_coordinator) and 5 (update_actor)
# ---------------------------------------------------------------------------

EXAMPLE_4 = r"""In the following example, the client's request is clear, but the email also instructs that an unidentified third party ("John Doe") be looped in. We do not know who John Doe is or which side he is on, so we ask the coordinator rather than guess:

```xml
<example>
  <input>
    <email-thread>
     no prior messages
    </email-thread>
    <email direction='inbound'>
      <timestamp>2026-05-06T10:05:00-04:00</timestamp>
      <from>Sarah Lin slin@brevanhoward.com</from>
      <to>Marissa Bradley mbradley@longridgepartners.com</to>
      <cc>Haley Park hpark@longridgepartners.com</cc>
      <subject>First round - Tom Becker</subject>
      <body>Hi Marissa,\n\nWe'd like to set up a first-round interview with Tom Becker for the Quant Researcher role. Also - please make sure John Doe sees this before we lock anything in.\n\nThanks,\nSarah Lin\nBrevan Howard</body>
    </email>
    <loops>
    <loop id='lop_DEF777'>
      <stage>NEW</stage>
      <actors>
        <coordinator>Marissa Bradley</coordinator>
        <client-contact>Sarah Lin, Brevan Howard</client-contact>
        <client-manager>Haley Park</client-manager>
        <recruiter>Rachel Kim rachel@longridgepartners.com</recruiter>
        <candidate>Tom Becker</candidate>
      </actors>
      <pending-suggestions>No current suggestions</pending-suggestions>
    </loop>
    </loops>
  </input>

  <output>

  1. **Actors:** Marissa Bradley is coordinator. Haley Park is CM (CC'd). Sarah Lin (Brevan Howard) is the client contact. Rachel Kim is the recruiter. Tom Becker is the candidate. Marissa is acting only as the coordinator.\n\n2. **Loop state:** NEW. No prior messages, no pending suggestions. First communication for this loop.\n\n3. **Who emailed:** Sarah Lin, the client contact, inbound. She wants a first round AND asks that "John Doe" see this first.\n\n4. **What they want:** A first-round interview for Tom Becker — but explicitly gated on an unidentified "John Doe" seeing it before anything is locked in.\n\n5. **Next step:** Normally we'd forward to Rachel for avails. But the client put an explicit precondition on an unknown person. We do not know who John Doe is or which side he is on, and acting before he is looped in could violate the client's instruction.\n\n6. **Risks/issues:** Yes — proceeding could break the client's stated condition. Guessing John Doe's identity (a colleague? someone at Brevan Howard? the candidate's reference?) risks routing confidential info to the wrong person.\n\n7. **Email needed:** Not yet — the right move is to clarify with the coordinator before drafting anything.\n\n8. **IRL tasks:** None for now.\n\n9. **Info needed via email:** Eventually avails from Rachel, but blocked until the John Doe instruction is understood.\n\n10. **Email check:** No outbound email yet.\n\n11. **Stage check:** No stage change — nothing has progressed.\n\n12. **Confidence:** 0.6 on any scheduling action, because the client's precondition is unresolved. High confidence that asking the coordinator is the correct move.\n\n13. **Assumptions:** The only way to proceed without asking is to assume who John Doe is and what "see this" means — that is NOT supported by any evidence on the thread. Per the rule against assuming, we must clarify with the coordinator.\n\n**Summary:** Clear interview request, but the client gated it on an unidentified "John Doe." We must not guess his identity or role. Ask the coordinator who he is and how to handle his involvement before drafting anything.

  <suggestions>
  [
    {
      "action": "ask_coordinator",
      "summary": "Clarify who 'John Doe' is before progressing Tom Becker's first round",
      "reasoning": "The client made the interview contingent on an unidentified 'John Doe' seeing it first. We have no evidence of who he is or which side he is on, and must not assume. The coordinator likely has the context.",
      "confidence": 0.92,
      "action_data": {
        "question": "Sarah at Brevan Howard asked that 'John Doe' see this before we lock in Tom Becker's first round, but he isn't on the thread and I don't know who he is or which side he's on. Who is John Doe, and how should I handle his involvement before I move this forward?"
      },
      "classification": "follow_up_needed",
      "target_loop_id": "lop_DEF777"
    }
  ]
  </suggestions>
  </output>
</example>
```"""

EXAMPLE_5 = r"""In the following example, it is a clear scheduling request, but the recruiter we would need to email is unknown on the loop. We must not guess who the recruiter is — instead we prompt the coordinator to set the recruiter via update_actor:

```xml
<example>
  <input>
    <email-thread>
     no prior messages
    </email-thread>
    <email direction='inbound'>
      <timestamp>2026-05-07T13:40:00-04:00</timestamp>
      <from>David Chen dchen@apexcap.com</from>
      <to>Lisa Park lisa@longridgepartners.com</to>
      <cc>Adam L'esperance adam@longridgepartners.com</cc>
      <subject>Interview with Apex Capital - Priya Raman</subject>
      <body>Hi Lisa,\n\nWe'd like to schedule a first-round interview with Priya Raman for the Senior Analyst position. Please send over some times that work.\n\nBest,\nDavid Chen\nApex Capital</body>
    </email>
    <loops>
    <loop id='lop_GHI888'>
      <stage>NEW</stage>
      <actors>
        <coordinator>Lisa Park</coordinator>
        <client-contact>David Chen, Apex Capital</client-contact>
        <client-manager>Adam L'esperance</client-manager>
        <recruiter>Unknown for this loop</recruiter>
        <candidate>Priya Raman</candidate>
      </actors>
      <pending-suggestions>No current suggestions</pending-suggestions>
    </loop>
    </loops>
  </input>

  <output>

  1. **Actors:** Lisa Park is coordinator. Adam L'esperance is CM (CC'd). David Chen (Apex Capital) is the client contact. The recruiter is Unknown on this loop. Priya Raman is the candidate. Lisa is acting only as the coordinator — no indication she is also the recruiter.\n\n2. **Loop state:** NEW. No prior messages, no pending suggestions. First communication for this loop.\n\n3. **Who emailed:** David Chen, the client contact, inbound. He's requesting a first-round interview with Priya Raman.\n\n4. **What they want:** Schedule a first round for Priya Raman; they want available times.\n\n5. **Next step:** The next step is to get Priya's avails, which come from the recruiter. But the recruiter is Unknown on this loop, and the candidate must be reached through the recruiter — we cannot proceed without knowing who the recruiter is.\n\n6. **Risks/issues:** Guessing the recruiter risks sending a candidate request to the wrong colleague. We must not assume their identity.\n\n7. **Email needed:** Yes, eventually — to the recruiter for avails — but we cannot draft it until the recruiter is set on the loop.\n\n8. **IRL tasks:** None — no indication Lisa is also the recruiter.\n\n9. **Info needed via email:** Priya's avails, via the recruiter — blocked on the missing actor.\n\n10. **Email check:** No outbound email yet.\n\n11. **Stage check:** No stage change — we have not contacted anyone yet.\n\n12. **Confidence:** 0.95 that the recruiter must be set before we can act. The action that requires the recruiter (collect avails) is the immediate next step, so this is a "just in time" actor need, not idle unknown-filling.\n\n13. **Assumptions:** The only way to draft now is to assume who the recruiter is — unsupported by the thread. Per the rule against assuming, and because we need this actor for the immediate next action, prompt the coordinator to set the recruiter.\n\n**Summary:** Clear new interview request, but the recruiter is unknown and is required for the immediate next step (collecting avails). Do not guess — prompt the coordinator to set the recruiter via update_actor.

  <suggestions>
  [
    {
      "action": "update_actor",
      "summary": "Set the recruiter on Priya Raman's loop so we can request avails",
      "reasoning": "Clear interview request whose immediate next step is collecting candidate avails via the recruiter, but the recruiter is unknown on the loop. We must not guess; the coordinator should set the recruiter.",
      "confidence": 0.95,
      "action_data": {
        "role": "recruiter"
      },
      "classification": "new_interview_request",
      "target_loop_id": "lop_GHI888"
    }
  ]
  </suggestions>
  </output>
</example>
```"""

# Examples 4 & 5 are inserted right after Example 3's closing fence,
# immediately before the "# TASK DESCRIPTION" heading.
NEW_EXAMPLES_OLD = "```\n\n# TASK DESCRIPTION"
NEW_EXAMPLES_NEW = "```\n\n" + EXAMPLE_4 + "\n\n" + EXAMPLE_5 + "\n\n# TASK DESCRIPTION"


def build_system_message(v41_system: str) -> str:
    text = v41_system
    text = _replace_once(text, PRECOG_OLD, PRECOG_NEW, tag="A:precog-13")
    text = _replace_once(text, DRAFT_GUIDANCE_OLD, DRAFT_GUIDANCE_NEW, tag="F:draft-guidance")
    text = _replace_once(text, EX1_LOOP_ID_OLD, EX1_LOOP_ID_NEW, tag="B:ex1-loopid")
    text = _replace_once(text, EX1_COORD_OLD, EX1_COORD_NEW, tag="B:ex1-coord")
    text = _replace_once(text, EX1_RECRUITER_OLD, EX1_RECRUITER_NEW, tag="B:ex1-recruiter")
    text = _replace_once(text, EX1_POINT1_OLD, EX1_POINT1_NEW, tag="B:ex1-p1")
    text = _replace_once(text, EX1_POINT5_OLD, EX1_POINT5_NEW, tag="B:ex1-p5")
    text = _replace_once(text, EX1_POINT10_OLD, EX1_POINT10_NEW, tag="B:ex1-p10")
    text = _replace_once(text, EX1_P13_OLD, EX1_P13_NEW, tag="B:ex1-p13")
    text = _replace_once(text, EX2_P13_OLD, EX2_P13_NEW, tag="C:ex2-p13")
    text = _replace_once(text, EX3_P13_OLD, EX3_P13_NEW, tag="C:ex3-p13")
    text = _replace_once(text, NEW_EXAMPLES_OLD, NEW_EXAMPLES_NEW, tag="DE:new-examples")
    return text


def main() -> None:
    lf = Langfuse()

    base = lf.get_prompt(PROMPT_NAME, version=BASE_VERSION, type="chat")
    print(f"[base] fetched {PROMPT_NAME} v{base.version} labels={base.labels}")

    messages = base.prompt
    if len(messages) != 2 or messages[0]["role"] != "system":
        raise SystemExit(f"unexpected v41 message shape: {[m['role'] for m in messages]}")

    v41_system = messages[0]["content"]
    user_message = messages[1]["content"]  # carried over verbatim

    new_system = build_system_message(v41_system)
    delta = len(new_system) - len(v41_system)
    print(f"[build] system message: {len(v41_system)} -> {len(new_system)} chars (+{delta})")

    new_prompt = lf.create_prompt(
        name=PROMPT_NAME,
        type="chat",
        prompt=[
            {"role": "system", "content": new_system},
            {"role": messages[1]["role"], "content": user_message},
        ],
        config=base.config,  # verbatim from v41 (model unchanged)
        labels=[TARGET_LABEL],
    )
    lf.flush()
    print(
        f"[publish] {PROMPT_NAME} version {new_prompt.version} "
        f"labels={new_prompt.labels} config={new_prompt.config}"
    )


if __name__ == "__main__":
    main()
