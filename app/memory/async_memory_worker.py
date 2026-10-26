"""Non-blocking learner-memory consolidation.

After each conversation turn, the root agent's ``after_agent_callback``
enqueues a lightweight *memory job*. A background ``asyncio`` worker then:

1. Scrubs PII from the exchange (:mod:`app.observability.pii_redactor`).
2. Extracts structured insights (preferred explanation style, interests,
   concept practised and outcome) using a fast Gemini model with a strict
   ``response_schema`` - falling back to deterministic heuristics offline.
3. Merges them into the persistent :class:`LearnerProfile`.

The kid never waits for any of this - the reply is already on screen.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config.settings import get_settings
from app.memory.session_store import get_or_create_profile, get_store
from app.observability.logger import get_logger
from app.observability.pii_redactor import get_pii_scrubber
from app.observability.tracing import traced_span
from app.schemas.memory_schemas import (
    ExplanationStyle,
    LearnerProfile,
    MasteryLevel,
    MemoryInsight,
    SkillProgress,
)

logger = get_logger(__name__)

_STYLE_HINTS = {
    ExplanationStyle.VISUAL: ("draw", "picture", "show me", "whiteboard", "see it"),
    ExplanationStyle.STORY: ("story", "pretend", "imagine"),
    ExplanationStyle.STEP_BY_STEP: ("step", "slowly", "one at a time", "again"),
    ExplanationStyle.HANDS_ON: ("blocks", "count with", "use my fingers", "act it out"),
}
_INTEREST_WORDS = (
    "bunny", "bunnies", "unicorn", "unicorns", "rainbow", "rainbows", "drawing", "dance", "dancing",
    "cats", "kittens", "puppies", "butterflies", "princess", "mermaid", "baking", "cupcakes", "horses",
)
_EXTRACTION_PROMPT = """You maintain a learning profile for an 8-year-old student.
From this single tutoring exchange, extract ONLY what is clearly evidenced:
- preferred_style: visual | story | step_by_step | hands_on (null if unclear)
- new_interests: hobbies/favourite things the kid mentioned (no names, places or personal data)
- interaction_note: one short tip on how to talk with her (null if nothing new)
- concept, subject, outcome (solved_independently | solved_with_hints | still_struggling) if she practised a concept.

KID: {kid}
BUDDY: {buddy}"""


@dataclass(frozen=True)
class MemoryJob:
    kid_id: str
    kid_text: str
    buddy_text: str


def heuristic_insight(kid_text: str, buddy_text: str) -> MemoryInsight:
    """Deterministic offline extraction (used in CI and as a fallback)."""
    lower = kid_text.lower()
    style = next((s for s, hints in _STYLE_HINTS.items() if any(h in lower for h in hints)), None)
    interests = sorted({w for w in _INTEREST_WORDS if re.search(rf"\b{w}\b", lower)})
    outcome = None
    if any(p in lower for p in ("i got it", "i did it", "easy", "i know", "is it ")):
        outcome = "solved_independently" if "hint" not in lower else "solved_with_hints"
    elif any(p in lower for p in ("i don't get", "i dont get", "confused", "too hard", "help")):
        outcome = "still_struggling"
    concept = None
    for key, name, subject in (
        ("fraction", "fractions", "math"),
        ("quarter", "fractions", "math"),
        ("half", "fractions", "math"),
        ("carry", "adding with regrouping", "math"),
        ("plus", "addition", "math"),
        ("butterfly", "butterfly life cycle", "science"),
        ("plant", "what plants need", "science"),
        ("spell", "spelling ai/ay words", "english"),
        ("adjective", "describing words", "english"),
    ):
        if key in lower or key in buddy_text.lower():
            concept, subj = name, subject
            break
    return MemoryInsight(
        preferred_style=style,
        new_interests=interests,
        concept=concept,
        subject=subj if concept else None,
        outcome=outcome if concept else None,
    )


async def llm_insight(kid_text: str, buddy_text: str) -> MemoryInsight:
    """Extract insights with a fast Gemini model using a strict response schema."""
    from google import genai
    from google.genai import types

    settings = get_settings()
    client = genai.Client(vertexai=True, project=settings.project_id, location=settings.location)
    response = await client.aio.models.generate_content(
        model=settings.fast_model,
        contents=_EXTRACTION_PROMPT.format(kid=kid_text[:1500], buddy=buddy_text[:1500]),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=MemoryInsight,
            temperature=0.0,
        ),
    )
    return MemoryInsight.model_validate_json(response.text)


def merge_insight(profile: LearnerProfile, insight: MemoryInsight) -> LearnerProfile:
    """Pure function: fold an insight into the learner profile."""
    data = profile.model_copy(deep=True)
    if insight.preferred_style and insight.preferred_style != ExplanationStyle.UNKNOWN:
        data.preferred_style = insight.preferred_style
    for interest in insight.new_interests:
        clean = interest.strip().lower()[:30]
        if clean and clean not in data.interests:
            data.interests = (data.interests + [clean])[-10:]
    if insight.interaction_note:
        data.interaction_notes = (data.interaction_notes + [insight.interaction_note[:160]])[-20:]
    if insight.concept and insight.outcome:
        key = insight.concept.lower()
        skill = data.skills.get(key) or SkillProgress(concept=insight.concept, subject=insight.subject or "any")
        skill.attempts += 1
        if insight.outcome == "solved_independently":
            skill.successes += 1
        elif insight.outcome == "solved_with_hints":
            skill.successes += 0.5  # partial credit when hints were needed
        ratio = skill.successes / skill.attempts
        skill.mastery = (
            MasteryLevel.MASTERED if skill.attempts >= 5 and ratio >= 0.9
            else MasteryLevel.CONFIDENT if skill.attempts >= 3 and ratio >= 0.7
            else MasteryLevel.PRACTICING if skill.attempts >= 1
            else MasteryLevel.NEW
        )
        skill.last_practiced = datetime.now(timezone.utc).isoformat()
        data.skills[key] = skill
    data.updated_at = datetime.now(timezone.utc).isoformat()
    return data


class AsyncLearnerMemoryManager:
    """Background queue + worker that consolidates memory off the hot path."""

    def __init__(self, use_llm: bool | None = None) -> None:
        self._queue: asyncio.Queue[MemoryJob] | None = None
        self._worker: asyncio.Task | None = None
        self._use_llm = get_settings().is_cloud_enabled if use_llm is None else use_llm
        self._scrubber = get_pii_scrubber()

    def _ensure_worker(self) -> None:
        loop = asyncio.get_running_loop()
        if self._queue is None or self._worker is None or self._worker.done() or self._worker.get_loop() is not loop:
            self._queue = asyncio.Queue(maxsize=100)
            self._worker = loop.create_task(self._run(), name="learner-memory-worker")

    def enqueue(self, kid_id: str, kid_text: str, buddy_text: str) -> bool:
        """Schedule consolidation without awaiting it. Returns False if dropped."""
        if not kid_text.strip():
            return False
        try:
            self._ensure_worker()
            assert self._queue is not None
            self._queue.put_nowait(
                MemoryJob(kid_id, self._scrubber.redact_text(kid_text), self._scrubber.redact_text(buddy_text))
            )
            logger.info("memory_job_enqueued", extra={"kid_id": kid_id, "queue_size": self._queue.qsize()})
            return True
        except (asyncio.QueueFull, RuntimeError):
            logger.warning("memory_job_dropped", extra={"kid_id": kid_id})
            return False

    async def drain(self) -> None:
        """Wait until all queued jobs are processed (tests / graceful shutdown)."""
        if self._queue is not None:
            await self._queue.join()

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            job = await self._queue.get()
            try:
                await self.process(job)
            except Exception:  # noqa: BLE001 - never crash the worker
                logger.exception("memory_job_failed")
            finally:
                self._queue.task_done()

    async def process(self, job: MemoryJob) -> LearnerProfile:
        with traced_span("memory.consolidate", kid_id=job.kid_id, use_llm=self._use_llm):
            insight = heuristic_insight(job.kid_text, job.buddy_text)
            if self._use_llm:
                try:
                    llm = await llm_insight(job.kid_text, job.buddy_text)
                    insight = MemoryInsight(
                        preferred_style=llm.preferred_style or insight.preferred_style,
                        new_interests=sorted(set(llm.new_interests) | set(insight.new_interests)),
                        interaction_note=llm.interaction_note,
                        concept=llm.concept or insight.concept,
                        subject=llm.subject or insight.subject,
                        outcome=llm.outcome or insight.outcome,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("memory_llm_extraction_failed", extra={"error_type": type(exc).__name__})

            # Blocking DB I/O runs in a thread so the event loop stays free.
            profile = await asyncio.to_thread(get_or_create_profile, job.kid_id)
            updated = merge_insight(profile, insight)
            await asyncio.to_thread(get_store().save_profile, updated)
            logger.info(
                "memory_consolidated",
                extra={"kid_id": job.kid_id, "insight": insight.model_dump(mode="json", exclude_none=True)},
            )
            return updated


memory_manager = AsyncLearnerMemoryManager()
