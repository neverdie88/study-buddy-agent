"""Strict schemas for school-material retrieval and tutoring outputs."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Subject(str, Enum):
    MATH = "math"
    SCIENCE = "science"
    ENGLISH = "english"
    HISTORY = "history"
    ANY = "any"


class SearchSchoolMaterialsInput(StrictModel):
    query: str = Field(..., min_length=2, max_length=300, description="What the kid is studying or stuck on.")
    subject: Subject = Field(Subject.ANY, description="Optional subject filter.")
    max_results: int = Field(3, ge=1, le=5, description="Number of passages to return (1-5).")


class MaterialPassage(StrictModel):
    source_title: str
    subject: Subject
    excerpt: str
    relevance_score: float = Field(..., ge=0.0, le=1.0)


class SearchSchoolMaterialsOutput(StrictModel):
    status: Literal["success"] = "success"
    passages: list[MaterialPassage]
    retrieval_backend: str


class StudyGuideSummary(StrictModel):
    title: str
    subject: Subject
    topics: list[str]


class ListStudyGuidesOutput(StrictModel):
    status: Literal["success"] = "success"
    guides: list[StudyGuideSummary]


class CurriculumContext(StrictModel):
    """Structured output of the ``curriculum_researcher_agent``."""

    subject: Subject
    concept: str = Field(..., description="The core concept the homework is testing.")
    teacher_method: str = Field(..., description="How the class materials teach this concept.")
    key_facts: list[str] = Field(default_factory=list, max_length=6)
    sources: list[str] = Field(default_factory=list)


class PedagogyVerdict(str, Enum):
    APPROVED = "approved"
    REVISED = "revised"


class PedagogyReview(StrictModel):
    """Structured output of the ``pedagogy_critic_agent`` (self-evaluation)."""

    verdict: PedagogyVerdict
    gives_away_final_answer: bool
    age_appropriate: bool
    final_reply_to_kid: str = Field(..., min_length=1, description="The safe, Socratic reply the kid will see.")
    detected_concept: str = Field(..., description="Concept practised in this turn, for progress tracking.")
    kid_seemed_confident: bool
