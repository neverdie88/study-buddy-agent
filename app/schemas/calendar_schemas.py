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

"""Strict Pydantic JSON schemas for the kid calendar tools.

All models use ``extra="forbid"`` so the LLM cannot smuggle unexpected fields,
and field validators constrain values to safe, well-formed ranges.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EventCategory(str, Enum):
    SCHOOL_CLASS = "school_class"
    HOMEWORK_DEADLINE = "homework_deadline"
    EXAM = "exam"
    STUDY_SESSION = "study_session"
    SPORTS = "sports"
    HOBBY = "hobby"
    FAMILY = "family"
    OTHER = "other"


#: Categories a parent considers high-stakes. Removing them always requires
#: an explicit human confirmation (HITL).
PROTECTED_CATEGORIES: frozenset[EventCategory] = frozenset(
    {EventCategory.EXAM, EventCategory.HOMEWORK_DEADLINE, EventCategory.SCHOOL_CLASS}
)


class ListCalendarEventsInput(StrictModel):
    """Input for ``list_kid_calendar_events``."""

    start_date: str = Field(..., description="Inclusive start date, ISO format YYYY-MM-DD.")
    end_date: str = Field(..., description="Inclusive end date, ISO format YYYY-MM-DD.")

    @field_validator("start_date", "end_date")
    @classmethod
    def _iso_date(cls, v: str) -> str:
        datetime.strptime(v, "%Y-%m-%d")
        return v

    @model_validator(mode="after")
    def _range(self) -> "ListCalendarEventsInput":
        start = datetime.strptime(self.start_date, "%Y-%m-%d")
        end = datetime.strptime(self.end_date, "%Y-%m-%d")
        if end < start:
            raise ValueError("end_date must be on or after start_date")
        if (end - start).days > 31:
            raise ValueError("date range must be 31 days or fewer")
        return self


class ScheduleCalendarEventInput(StrictModel):
    """Input for ``schedule_kid_calendar_event``."""

    title: str = Field(..., min_length=2, max_length=80, description="Short, kid-friendly event title.")
    start_time: str = Field(..., description="Local start time, ISO format YYYY-MM-DDTHH:MM.")
    duration_minutes: int = Field(..., ge=10, le=240, description="Duration between 10 and 240 minutes.")
    category: EventCategory = Field(..., description="Event category.")
    notes: str = Field("", max_length=280, description="Optional notes.")

    @field_validator("start_time")
    @classmethod
    def _iso_datetime(cls, v: str) -> str:
        datetime.strptime(v, "%Y-%m-%dT%H:%M")
        return v

    @field_validator("start_time")
    @classmethod
    def _reasonable_hours(cls, v: str) -> str:
        hour = datetime.strptime(v, "%Y-%m-%dT%H:%M").hour
        if hour < 6 or hour >= 20:
            raise ValueError("kid events must start between 06:00 and 19:59 (bedtime rule)")
        return v


class RemoveCalendarEventInput(StrictModel):
    """Input for ``remove_kid_calendar_event``."""

    event_id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{4,64}$", description="Event id from list_kid_calendar_events.")
    reason: str = Field(..., min_length=3, max_length=200, description="Why the kid wants it removed.")


class CalendarEvent(StrictModel):
    event_id: str
    title: str
    start_time: str
    end_time: str
    category: EventCategory
    notes: str = ""
    parent_locked: bool = False


class ToolStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    PENDING_CONFIRMATION = "pending_confirmation"
    REJECTED = "rejected"


class ToolErrorResult(StrictModel):
    """Uniform guided-error payload returned to the LLM instead of raising."""

    status: Literal[ToolStatus.ERROR] = ToolStatus.ERROR
    error_code: str
    message: str
    recovery_instructions: str = Field(..., description="Concrete next steps the agent should take to recover.")


class ListCalendarEventsOutput(StrictModel):
    status: Literal[ToolStatus.SUCCESS] = ToolStatus.SUCCESS
    events: list[CalendarEvent]
    count: int


class ScheduleCalendarEventOutput(StrictModel):
    status: Literal[ToolStatus.SUCCESS] = ToolStatus.SUCCESS
    event: CalendarEvent
    conflicts: list[CalendarEvent] = []


class RemoveCalendarEventOutput(StrictModel):
    status: ToolStatus
    removed_event_id: str | None = None
    message: str
