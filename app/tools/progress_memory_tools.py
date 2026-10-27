# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Memory tools the agents can call explicitly."""

from __future__ import annotations

from pydantic import ValidationError

from app.config.settings import get_settings
from app.memory.async_memory_worker import merge_insight
from app.memory.session_store import get_or_create_profile, get_store
from app.observability.tracing import traced_span
from app.schemas.memory_schemas import MemoryInsight, RecordLearningMilestoneInput
from app.tools.errors import guided_error, validation_error


def retrieve_learner_profile() -> dict:
    """Get what Buddy remembers about the kid: learning style, interests, strengths and focus areas.

    Call this when the kid asks "How am I doing?", when planning practice, or
    when you need to personalise an explanation (e.g. use her favourite things
    in examples).

    Returns:
        dict: ``{"status":"success","display_name","grade_level","preferred_style",
        "interests":[...],"encouragement_style","interaction_notes":[...],
        "strengths":[...],"focus_areas":[...],"skills":{concept:{mastery,attempts,successes}}}``
        or a guided error.
    """
    with traced_span("memory.retrieve_profile"):
        try:
            profile = get_or_create_profile(get_settings().kid_id)
        except Exception as exc:  # noqa: BLE001
            return guided_error(
                "MEMORY_UNAVAILABLE",
                f"Learner profile could not be loaded ({type(exc).__name__}).",
                "Continue without personalisation and ask the kid what she likes and how she likes to learn.",
            )
    return {
        "status": "success",
        "display_name": profile.display_name,
        "grade_level": profile.grade_level,
        "preferred_style": profile.preferred_style.value,
        "interests": profile.interests,
        "encouragement_style": profile.encouragement_style,
        "interaction_notes": profile.interaction_notes[-5:],
        "strengths": profile.strengths(),
        "focus_areas": profile.focus_areas(),
        "skills": {
            k: {"mastery": s.mastery.value, "attempts": s.attempts, "successes": s.successes}
            for k, s in profile.skills.items()
        },
    }


def record_learning_milestone(concept: str, subject: str, outcome: str) -> dict:
    """Save a learning milestone after the kid finishes practising a concept.

    Args:
        concept: The concept practised, e.g. "quarters of a shape" or "adding with regrouping".
        subject: The subject, e.g. "math", "science", "english".
        outcome: One of "solved_independently", "solved_with_hints", "still_struggling".

    Returns:
        dict: ``{"status":"success","concept","mastery","attempts"}`` or a guided error.
    """
    try:
        args = RecordLearningMilestoneInput(concept=concept, subject=subject, outcome=outcome)
    except ValidationError as exc:
        return validation_error(exc, "record_learning_milestone")

    with traced_span("memory.record_milestone", concept=args.concept, outcome=args.outcome):
        try:
            profile = get_or_create_profile(get_settings().kid_id)
            updated = merge_insight(
                profile, MemoryInsight(concept=args.concept, subject=args.subject, outcome=args.outcome)
            )
            get_store().save_profile(updated)
        except Exception as exc:  # noqa: BLE001
            return guided_error(
                "MEMORY_WRITE_FAILED",
                f"Milestone could not be saved ({type(exc).__name__}).",
                "Do not retry more than once; just celebrate the effort with the kid.",
            )
    skill = updated.skills[args.concept.lower()]
    return {"status": "success", "concept": skill.concept, "mastery": skill.mastery.value, "attempts": skill.attempts}
