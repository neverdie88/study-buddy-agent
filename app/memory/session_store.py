"""Persistent storage for learner profiles and the kid calendar.

Two interchangeable backends share one interface:

* :class:`SqliteBuddyStore` - a durable on-disk SQLite database (default for
  local development, CI and single-instance Cloud Run).
* :class:`FirestoreBuddyStore` - Google Cloud Firestore (production), selected
  with ``BUDDY_STORAGE_BACKEND=firestore``.

Conversation history itself is persisted by ADK's session service - see
:func:`build_session_service`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path

from app.config.settings import get_settings
from app.observability.logger import get_logger
from app.schemas.calendar_schemas import CalendarEvent
from app.schemas.memory_schemas import LearnerProfile

logger = get_logger(__name__)


class BuddyStore(ABC):
    """Abstract persistence interface for profiles and calendar events."""

    # ---- learner profile ----
    @abstractmethod
    def load_profile(self, kid_id: str) -> LearnerProfile | None: ...

    @abstractmethod
    def save_profile(self, profile: LearnerProfile) -> None: ...

    # ---- calendar ----
    @abstractmethod
    def list_events(self, kid_id: str, start_iso: str, end_iso: str) -> list[CalendarEvent]: ...

    @abstractmethod
    def get_event(self, kid_id: str, event_id: str) -> CalendarEvent | None: ...

    @abstractmethod
    def add_event(self, kid_id: str, event: CalendarEvent) -> CalendarEvent: ...

    @abstractmethod
    def delete_event(self, kid_id: str, event_id: str) -> bool: ...

    @staticmethod
    def new_event_id() -> str:
        return f"evt_{uuid.uuid4().hex[:12]}"


class SqliteBuddyStore(BuddyStore):
    """Thread-safe SQLite implementation."""

    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS learner_profiles (kid_id TEXT PRIMARY KEY, profile_json TEXT NOT NULL)"
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS calendar_events (
                       event_id TEXT PRIMARY KEY, kid_id TEXT NOT NULL, start_time TEXT NOT NULL,
                       event_json TEXT NOT NULL)"""
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_events_kid_start ON calendar_events(kid_id, start_time)")

    def load_profile(self, kid_id: str) -> LearnerProfile | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT profile_json FROM learner_profiles WHERE kid_id = ?", (kid_id,)
            ).fetchone()
        return LearnerProfile.model_validate_json(row[0]) if row else None

    def save_profile(self, profile: LearnerProfile) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO learner_profiles(kid_id, profile_json) VALUES(?, ?) "
                "ON CONFLICT(kid_id) DO UPDATE SET profile_json = excluded.profile_json",
                (profile.kid_id, profile.model_dump_json()),
            )

    def list_events(self, kid_id: str, start_iso: str, end_iso: str) -> list[CalendarEvent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_json FROM calendar_events WHERE kid_id = ? AND start_time >= ? AND start_time <= ? "
                "ORDER BY start_time",
                (kid_id, start_iso, end_iso),
            ).fetchall()
        return [CalendarEvent.model_validate_json(r[0]) for r in rows]

    def get_event(self, kid_id: str, event_id: str) -> CalendarEvent | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT event_json FROM calendar_events WHERE kid_id = ? AND event_id = ?", (kid_id, event_id)
            ).fetchone()
        return CalendarEvent.model_validate_json(row[0]) if row else None

    def add_event(self, kid_id: str, event: CalendarEvent) -> CalendarEvent:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO calendar_events(event_id, kid_id, start_time, event_json) VALUES(?,?,?,?)",
                (event.event_id, kid_id, event.start_time, event.model_dump_json()),
            )
        return event

    def delete_event(self, kid_id: str, event_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM calendar_events WHERE kid_id = ? AND event_id = ?", (kid_id, event_id)
            )
        return cur.rowcount > 0


class FirestoreBuddyStore(BuddyStore):
    """Google Cloud Firestore implementation (``kids/{kid_id}/...``)."""

    def __init__(self, project_id: str) -> None:
        from google.cloud import firestore

        self._db = firestore.Client(project=project_id)

    def _kid(self, kid_id: str):
        return self._db.collection("kids").document(kid_id)

    def load_profile(self, kid_id: str) -> LearnerProfile | None:
        snap = self._kid(kid_id).collection("memory").document("learner_profile").get()
        return LearnerProfile.model_validate(snap.to_dict()) if snap.exists else None

    def save_profile(self, profile: LearnerProfile) -> None:
        self._kid(profile.kid_id).collection("memory").document("learner_profile").set(
            json.loads(profile.model_dump_json())
        )

    def list_events(self, kid_id: str, start_iso: str, end_iso: str) -> list[CalendarEvent]:
        query = (
            self._kid(kid_id)
            .collection("calendar_events")
            .where("start_time", ">=", start_iso)
            .where("start_time", "<=", end_iso)
            .order_by("start_time")
        )
        return [CalendarEvent.model_validate(d.to_dict()) for d in query.stream()]

    def get_event(self, kid_id: str, event_id: str) -> CalendarEvent | None:
        snap = self._kid(kid_id).collection("calendar_events").document(event_id).get()
        return CalendarEvent.model_validate(snap.to_dict()) if snap.exists else None

    def add_event(self, kid_id: str, event: CalendarEvent) -> CalendarEvent:
        self._kid(kid_id).collection("calendar_events").document(event.event_id).set(
            json.loads(event.model_dump_json())
        )
        return event

    def delete_event(self, kid_id: str, event_id: str) -> bool:
        ref = self._kid(kid_id).collection("calendar_events").document(event_id)
        if not ref.get().exists:
            return False
        ref.delete()
        return True


@lru_cache(maxsize=1)
def get_store() -> BuddyStore:
    """Return the configured persistent store (cached per process)."""
    settings = get_settings()
    if settings.storage_backend == "firestore" and settings.project_id:
        logger.info("store_initialized", extra={"backend": "firestore"})
        return FirestoreBuddyStore(settings.project_id)
    logger.info("store_initialized", extra={"backend": "sqlite"})
    return SqliteBuddyStore(settings.sqlite_path)


def get_or_create_profile(kid_id: str | None = None) -> LearnerProfile:
    """Load the learner profile, creating a default one on first use."""
    settings = get_settings()
    kid_id = kid_id or settings.kid_id
    store = get_store()
    profile = store.load_profile(kid_id)
    if profile is None:
        profile = LearnerProfile(
            kid_id=kid_id,
            display_name=settings.kid_display_name,
            grade_level=settings.kid_grade_level,
            interests=["bunnies", "unicorns", "rainbows", "drawing"],
        )
        store.save_profile(profile)
    return profile


def build_session_service():
    """Build a persistent ADK session service for multi-turn conversation history.

    * On Vertex AI Agent Runtime the platform injects a managed
      ``VertexAiSessionService`` automatically.
    * Locally / on Cloud Run we use ADK's ``DatabaseSessionService`` backed by
      SQLite (or Cloud SQL via ``BUDDY_SESSION_DB_URL``) so history survives
      restarts.
    """
    from google.adk.sessions import DatabaseSessionService

    settings = get_settings()
    if settings.session_db_url.startswith("sqlite"):
        db_file = settings.session_db_url.split("///", 1)[-1]
        Path(db_file).parent.mkdir(parents=True, exist_ok=True)
    return DatabaseSessionService(db_url=settings.session_db_url)
