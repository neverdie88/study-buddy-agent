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

"""Kid calendar tools with Human-in-the-Loop (HITL) protection.

* ``list_kid_calendar_events`` - read the schedule.
* ``schedule_kid_calendar_event`` - add an event; double-booking pauses for
  a grown-up's confirmation.
* ``remove_kid_calendar_event`` - ALWAYS pauses for a grown-up's
  confirmation via ADK ``tool_context.request_confirmation`` before deleting.

Backends: the persistent :mod:`app.memory.session_store` calendar (default) or
Google Calendar (``BUDDY_CALENDAR_BACKEND=google``) with credentials pulled
from Secret Manager.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.adk.tools import ToolContext
from pydantic import ValidationError

from app.config.secrets import SecretNotAvailableError, get_secret_resolver
from app.config.settings import get_settings
from app.memory.session_store import BuddyStore, get_store
from app.observability.logger import get_logger
from app.observability.tracing import traced_span
from app.schemas.calendar_schemas import (
    PROTECTED_CATEGORIES,
    CalendarEvent,
    EventCategory,
    ListCalendarEventsInput,
    ListCalendarEventsOutput,
    RemoveCalendarEventInput,
    RemoveCalendarEventOutput,
    ScheduleCalendarEventInput,
    ScheduleCalendarEventOutput,
    ToolStatus,
)
from app.tools.errors import guided_error, validation_error

logger = get_logger(__name__)
_FMT = "%Y-%m-%dT%H:%M"


# --------------------------------------------------------------------------- #
# Google Calendar backend (optional)
# --------------------------------------------------------------------------- #
class GoogleCalendarBackend:
    """Thin adapter over the Google Calendar v3 API.

    Credentials (authorized-user JSON with a refresh token, scoped to the
    kid's calendar) are loaded from Secret Manager - never from the repo.
    """

    def __init__(self) -> None:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        settings = get_settings()
        info = json.loads(get_secret_resolver().get_secret(settings.calendar_secret_name))
        creds = Credentials.from_authorized_user_info(info, ["https://www.googleapis.com/auth/calendar.events"])
        self._svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self._cal_id = settings.google_calendar_id
        self._tz = settings.kid_timezone

    @staticmethod
    def _to_event(item: dict) -> CalendarEvent:
        private = item.get("extendedProperties", {}).get("private", {})
        start = item["start"].get("dateTime", item["start"].get("date", ""))[:16]
        end = item["end"].get("dateTime", item["end"].get("date", ""))[:16]
        category = private.get("buddy_category", "other")
        return CalendarEvent(
            event_id=item["id"],
            title=item.get("summary", "(no title)"),
            start_time=start,
            end_time=end,
            category=EventCategory(category) if category in EventCategory._value2member_map_ else EventCategory.OTHER,
            notes=item.get("description", "")[:280],
            parent_locked=private.get("parent_locked") == "true",
        )

    def list_events(self, start_iso: str, end_iso: str) -> list[CalendarEvent]:
        tz = ZoneInfo(self._tz)
        resp = (
            self._svc.events()
            .list(
                calendarId=self._cal_id,
                timeMin=datetime.strptime(start_iso, _FMT).replace(tzinfo=tz).isoformat(),
                timeMax=datetime.strptime(end_iso, _FMT).replace(tzinfo=tz).isoformat(),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        return [self._to_event(i) for i in resp.get("items", [])]

    def get_event(self, event_id: str) -> CalendarEvent | None:
        try:
            return self._to_event(self._svc.events().get(calendarId=self._cal_id, eventId=event_id).execute())
        except Exception:  # noqa: BLE001
            return None

    def add_event(self, event: CalendarEvent) -> CalendarEvent:
        body = {
            "summary": event.title,
            "description": event.notes,
            "start": {"dateTime": f"{event.start_time}:00", "timeZone": self._tz},
            "end": {"dateTime": f"{event.end_time}:00", "timeZone": self._tz},
            "extendedProperties": {"private": {"buddy_category": event.category.value}},
        }
        created = self._svc.events().insert(calendarId=self._cal_id, body=body).execute()
        return self._to_event(created)

    def delete_event(self, event_id: str) -> bool:
        self._svc.events().delete(calendarId=self._cal_id, eventId=event_id).execute()
        return True


class LocalCalendarBackend:
    """Calendar persisted in the Study Buddy store (SQLite / Firestore)."""

    def __init__(self, store: BuddyStore, kid_id: str) -> None:
        self._store, self._kid = store, kid_id

    def list_events(self, start_iso: str, end_iso: str) -> list[CalendarEvent]:
        return self._store.list_events(self._kid, start_iso, end_iso)

    def get_event(self, event_id: str) -> CalendarEvent | None:
        return self._store.get_event(self._kid, event_id)

    def add_event(self, event: CalendarEvent) -> CalendarEvent:
        return self._store.add_event(self._kid, event)

    def delete_event(self, event_id: str) -> bool:
        return self._store.delete_event(self._kid, event_id)


def get_calendar_backend():
    settings = get_settings()
    if settings.calendar_backend == "google":
        return GoogleCalendarBackend()
    backend = LocalCalendarBackend(get_store(), settings.kid_id)
    seed_demo_week_if_empty(backend)
    return backend


def seed_demo_week_if_empty(backend: LocalCalendarBackend) -> None:
    """Seed a realistic school week (from the class timetable) on first run."""
    tz = ZoneInfo(get_settings().kid_timezone)
    today = datetime.now(tz).replace(minute=0, second=0, microsecond=0, tzinfo=None)
    monday = today - timedelta(days=today.weekday())
    if backend.list_events((monday - timedelta(days=7)).strftime(_FMT), (monday + timedelta(days=14)).strftime(_FMT)):
        return
    plan = [
        (0, 16, 45, "Sport practice 🏃‍♀️", EventCategory.SPORTS, False),
        (1, 8, 30, "Library day - return books 📚", EventCategory.SCHOOL_CLASS, True),
        (2, 17, 0, "Art club 🎨", EventCategory.HOBBY, False),
        (3, 9, 0, "Maths sheet due ✏️", EventCategory.HOMEWORK_DEADLINE, True),
        (3, 16, 30, "Dance class 💃", EventCategory.HOBBY, False),
        (4, 9, 0, "Spelling quiz (ai / ay words) 📝", EventCategory.EXAM, True),
        (5, 10, 0, "Grandma visit 👵", EventCategory.FAMILY, False),
        (11, 9, 0, "Butterfly poster due 🦋", EventCategory.HOMEWORK_DEADLINE, True),
    ]
    for day, hour, mins, title, cat, locked in plan:
        start = monday + timedelta(days=day, hours=hour - monday.hour, minutes=mins)
        backend.add_event(
            CalendarEvent(
                event_id=BuddyStore.new_event_id(),
                title=title,
                start_time=start.strftime(_FMT),
                end_time=(start + timedelta(minutes=45)).strftime(_FMT),
                category=cat,
                parent_locked=locked,
            )
        )


# --------------------------------------------------------------------------- #
# ADK tools
# --------------------------------------------------------------------------- #
def list_kid_calendar_events(start_date: str, end_date: str) -> dict:
    """List the kid's calendar events between two dates (inclusive).

    Use it to answer "What do I have tomorrow?", to find free time for study,
    and ALWAYS before removing an event so you have the correct ``event_id``.

    Args:
        start_date: First day to include, ISO format YYYY-MM-DD.
        end_date: Last day to include, ISO format YYYY-MM-DD (max 31 days after start_date).

    Returns:
        dict: ``{"status":"success","events":[{"event_id","title","start_time",
        "end_time","category","notes","parent_locked"}],"count":int}`` or a
        guided error with ``recovery_instructions``.
    """
    try:
        args = ListCalendarEventsInput(start_date=start_date, end_date=end_date)
    except ValidationError as exc:
        return validation_error(exc, "list_kid_calendar_events")

    with traced_span("calendar.list", start_date=args.start_date, end_date=args.end_date):
        try:
            events = get_calendar_backend().list_events(f"{args.start_date}T00:00", f"{args.end_date}T23:59")
        except SecretNotAvailableError:
            return guided_error(
                "CALENDAR_NOT_CONNECTED",
                "The Google Calendar connection is not set up.",
                "Tell the kid a grown-up needs to connect the calendar; do not retry.",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("calendar_list_failed")
            return guided_error(
                "CALENDAR_UNAVAILABLE",
                f"Calendar could not be read ({type(exc).__name__}).",
                "Retry once. If it fails again, apologise kindly and suggest asking a grown-up.",
            )
    return ListCalendarEventsOutput(events=events, count=len(events)).model_dump(mode="json")


def schedule_kid_calendar_event(
    title: str,
    start_time: str,
    duration_minutes: int,
    category: str,
    tool_context: ToolContext,
    notes: str = "",
) -> dict:
    """Add a new event to the kid's calendar (study time, activity, reminder...).

    If the new event overlaps an existing one, a grown-up must confirm the
    double-booking before it is saved (Human-in-the-Loop).

    Args:
        title: Short kid-friendly title, 2-80 characters (e.g. "Practise spelling words").
        start_time: Local start time in ISO format YYYY-MM-DDTHH:MM, between 06:00 and 19:59.
        duration_minutes: Length of the event, 10-240 minutes.
        category: One of school_class, homework_deadline, exam, study_session,
            sports, hobby, family, other.
        tool_context: Injected by ADK; used for the HITL confirmation.
        notes: Optional extra details (max 280 characters).

    Returns:
        dict: ``{"status":"success","event":{...},"conflicts":[...]}``, a
        ``pending_confirmation`` / ``rejected`` status, or a guided error.
    """
    try:
        args = ScheduleCalendarEventInput(
            title=title, start_time=start_time, duration_minutes=duration_minutes, category=category, notes=notes
        )
    except ValidationError as exc:
        return validation_error(exc, "schedule_kid_calendar_event")

    start = datetime.strptime(args.start_time, _FMT)
    end = start + timedelta(minutes=args.duration_minutes)
    with traced_span("calendar.schedule", category=args.category.value, duration=args.duration_minutes):
        try:
            backend = get_calendar_backend()
            same_day = backend.list_events(start.strftime("%Y-%m-%dT00:00"), start.strftime("%Y-%m-%dT23:59"))
        except Exception as exc:  # noqa: BLE001
            return guided_error(
                "CALENDAR_UNAVAILABLE",
                f"Calendar could not be read ({type(exc).__name__}).",
                "Retry once. If it fails again, apologise and suggest asking a grown-up.",
            )

        conflicts = [
            e for e in same_day
            if datetime.strptime(e.start_time, _FMT) < end and datetime.strptime(e.end_time, _FMT) > start
        ]

        if conflicts:
            confirmation = tool_context.tool_confirmation
            if confirmation is None:
                tool_context.request_confirmation(
                    hint=(
                        f"'{args.title}' at {args.start_time} overlaps with "
                        + ", ".join(f"'{c.title}' ({c.start_time[11:]})" for c in conflicts)
                        + ". Grown-up: allow the double-booking?"
                    ),
                    payload={"title": args.title, "start_time": args.start_time},
                )
                return RemoveCalendarEventOutput(
                    status=ToolStatus.PENDING_CONFIRMATION,
                    message="Waiting for a grown-up to approve the overlapping event.",
                ).model_dump(mode="json")
            if not confirmation.confirmed:
                return RemoveCalendarEventOutput(
                    status=ToolStatus.REJECTED,
                    message="A grown-up said no to the double-booking. Suggest a different free time instead.",
                ).model_dump(mode="json")

        event = backend.add_event(
            CalendarEvent(
                event_id=BuddyStore.new_event_id(),
                title=args.title,
                start_time=args.start_time,
                end_time=end.strftime(_FMT),
                category=args.category,
                notes=args.notes,
            )
        )
    return ScheduleCalendarEventOutput(event=event, conflicts=conflicts).model_dump(mode="json")


def remove_kid_calendar_event(event_id: str, reason: str, tool_context: ToolContext) -> dict:
    """Remove an event from the kid's calendar - ALWAYS requires a grown-up's OK.

    Execution pauses with an explicit Human-in-the-Loop confirmation request.
    The event is deleted only after a parent approves. Call
    ``list_kid_calendar_events`` first to get the exact ``event_id``.

    Args:
        event_id: The ``event_id`` returned by ``list_kid_calendar_events``.
        reason: Why the kid wants to remove it (3-200 characters).
        tool_context: Injected by ADK; used for the HITL confirmation.

    Returns:
        dict: ``{"status": "success"|"pending_confirmation"|"rejected",
        "removed_event_id": str|None, "message": str}`` or a guided error.
    """
    try:
        args = RemoveCalendarEventInput(event_id=event_id, reason=reason)
    except ValidationError as exc:
        return validation_error(exc, "remove_kid_calendar_event")

    with traced_span("calendar.remove", event_id=args.event_id):
        try:
            backend = get_calendar_backend()
            event = backend.get_event(args.event_id)
        except Exception as exc:  # noqa: BLE001
            return guided_error(
                "CALENDAR_UNAVAILABLE",
                f"Calendar could not be read ({type(exc).__name__}).",
                "Retry once. If it fails again, apologise and suggest asking a grown-up.",
            )
        if event is None:
            return guided_error(
                "EVENT_NOT_FOUND",
                f"No event with id '{args.event_id}'.",
                "Call list_kid_calendar_events for the relevant dates, find the matching event_id by its "
                "title and time, then call remove_kid_calendar_event again. Ask the kid if several match.",
            )

        confirmation = tool_context.tool_confirmation
        if confirmation is None:
            protected = event.parent_locked or event.category in PROTECTED_CATEGORIES
            tool_context.request_confirmation(
                hint=(
                    f"{'⚠️ IMPORTANT school event! ' if protected else ''}"
                    f"Remove '{event.title}' on {event.start_time.replace('T', ' at ')}? Reason: {args.reason}"
                ),
                payload={"event_id": event.event_id, "title": event.title, "protected": protected},
            )
            return RemoveCalendarEventOutput(
                status=ToolStatus.PENDING_CONFIRMATION,
                message="Waiting for a grown-up to approve removing this event.",
            ).model_dump(mode="json")

        if not confirmation.confirmed:
            return RemoveCalendarEventOutput(
                status=ToolStatus.REJECTED,
                message="A grown-up said the event should stay. Tell the kid kindly and offer another idea.",
            ).model_dump(mode="json")

        backend.delete_event(event.event_id)
    return RemoveCalendarEventOutput(
        status=ToolStatus.SUCCESS, removed_event_id=event.event_id, message=f"Removed '{event.title}'."
    ).model_dump(mode="json")
