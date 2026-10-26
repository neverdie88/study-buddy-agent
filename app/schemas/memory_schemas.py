"""Schemas for the adaptive learner memory (style, preferences, progress)."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ExplanationStyle(str, Enum):
    VISUAL = "visual"
    STORY = "story"
    STEP_BY_STEP = "step_by_step"
    HANDS_ON = "hands_on"
    UNKNOWN = "unknown"


class MasteryLevel(str, Enum):
    NEW = "new"
    PRACTICING = "practicing"
    CONFIDENT = "confident"
    MASTERED = "mastered"


class SkillProgress(StrictModel):
    concept: str
    subject: str
    mastery: MasteryLevel = MasteryLevel.NEW
    attempts: int = 0
    successes: float = 0.0
    last_practiced: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class LearnerProfile(StrictModel):
    """Long-term, PII-scrubbed memory about how this kid learns best."""

    kid_id: str
    display_name: str
    grade_level: int = Field(..., ge=1, le=12)
    preferred_style: ExplanationStyle = ExplanationStyle.UNKNOWN
    interests: list[str] = Field(default_factory=list, max_length=10)
    encouragement_style: str = "warm and upbeat"
    interaction_notes: list[str] = Field(default_factory=list, max_length=20)
    skills: dict[str, SkillProgress] = Field(default_factory=dict)
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def focus_areas(self, limit: int = 3) -> list[str]:
        weak = [s for s in self.skills.values() if s.mastery in (MasteryLevel.NEW, MasteryLevel.PRACTICING)]
        weak.sort(key=lambda s: (s.successes / s.attempts) if s.attempts else 0.0)
        return [s.concept for s in weak[:limit]]

    def strengths(self, limit: int = 3) -> list[str]:
        strong = [s for s in self.skills.values() if s.mastery in (MasteryLevel.CONFIDENT, MasteryLevel.MASTERED)]
        return [s.concept for s in strong[:limit]]


class RecordLearningMilestoneInput(StrictModel):
    concept: str = Field(..., min_length=2, max_length=80, description="Concept practised, e.g. 'adding fractions'.")
    subject: str = Field(..., min_length=2, max_length=30)
    outcome: Literal["solved_independently", "solved_with_hints", "still_struggling"]


class MemoryInsight(StrictModel):
    """Insight extracted asynchronously from a finished conversation turn."""

    preferred_style: ExplanationStyle | None = None
    new_interests: list[str] = Field(default_factory=list)
    interaction_note: str | None = None
    concept: str | None = None
    subject: str | None = None
    outcome: Literal["solved_independently", "solved_with_hints", "still_struggling"] | None = None
