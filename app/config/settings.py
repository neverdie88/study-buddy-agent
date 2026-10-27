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

"""Typed, environment-driven settings for the Study Buddy agent.

All configuration is read from environment variables (never hardcoded secrets).
Sensitive values (e.g. Google Calendar OAuth credentials) are resolved at
runtime through :mod:`app.config.secrets` using Google Cloud Secret Manager.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings resolved from the environment."""

    project_id: str = field(default_factory=lambda: os.getenv("GOOGLE_CLOUD_PROJECT", ""))
    location: str = field(default_factory=lambda: os.getenv("GOOGLE_CLOUD_LOCATION", "global"))

    # --- Strategic model routing -------------------------------------------
    fast_model: str = field(default_factory=lambda: os.getenv("BUDDY_FAST_MODEL", "gemini-3.8-flash"))
    reasoning_model: str = field(default_factory=lambda: os.getenv("BUDDY_REASONING_MODEL", "gemini-3.1-pro-preview"))

    # --- Kid profile defaults ----------------------------------------------
    kid_id: str = field(default_factory=lambda: os.getenv("BUDDY_KID_ID", "kid-001"))
    kid_display_name: str = field(default_factory=lambda: os.getenv("BUDDY_KID_NAME", "Star"))
    kid_grade_level: int = field(default_factory=lambda: int(os.getenv("BUDDY_KID_GRADE", "3")))
    kid_timezone: str = field(default_factory=lambda: os.getenv("BUDDY_KID_TIMEZONE", "Australia/Sydney"))

    # --- Storage backends -------------------------------------------------
    # "sqlite" (local / CI default) or "firestore" (production on Google Cloud).
    storage_backend: str = field(default_factory=lambda: os.getenv("BUDDY_STORAGE_BACKEND", "sqlite"))
    sqlite_path: str = field(
        default_factory=lambda: os.getenv("BUDDY_SQLITE_PATH", str(PROJECT_ROOT / ".data" / "study_buddy.db"))
    )
    session_db_url: str = field(
        default_factory=lambda: os.getenv(
            "BUDDY_SESSION_DB_URL", f"sqlite+aiosqlite:///{PROJECT_ROOT / '.data' / 'adk_sessions.db'}"
        )
    )

    # --- Calendar backend --------------------------------------------------
    # "local" (persistent SQLite calendar) or "google" (Google Calendar API).
    calendar_backend: str = field(default_factory=lambda: os.getenv("BUDDY_CALENDAR_BACKEND", "local"))
    google_calendar_id: str = field(default_factory=lambda: os.getenv("BUDDY_GOOGLE_CALENDAR_ID", "primary"))
    calendar_secret_name: str = field(
        default_factory=lambda: os.getenv("BUDDY_CALENDAR_SECRET_NAME", "study-buddy-calendar-credentials")
    )

    # --- School materials retrieval ---------------------------------------
    materials_dir: str = field(
        default_factory=lambda: os.getenv(
            "BUDDY_MATERIALS_DIR", str(PROJECT_ROOT / "data" / "sample_school_materials")
        )
    )
    vertex_search_datastore: str = field(default_factory=lambda: os.getenv("BUDDY_VERTEX_SEARCH_DATASTORE", ""))

    # --- Context & memory --------------------------------------------------
    compaction_interval: int = field(default_factory=lambda: int(os.getenv("BUDDY_COMPACTION_INTERVAL", "6")))
    compaction_overlap: int = field(default_factory=lambda: int(os.getenv("BUDDY_COMPACTION_OVERLAP", "2")))

    # --- Observability / safety -------------------------------------------
    enable_cloud_dlp: bool = field(default_factory=lambda: _env_bool("BUDDY_ENABLE_CLOUD_DLP", False))
    enable_cloud_trace: bool = field(default_factory=lambda: _env_bool("BUDDY_ENABLE_CLOUD_TRACE", False))
    enable_cloud_logging: bool = field(default_factory=lambda: _env_bool("BUDDY_ENABLE_CLOUD_LOGGING", False))
    log_level: str = field(default_factory=lambda: os.getenv("BUDDY_LOG_LEVEL", "INFO"))

    @property
    def is_cloud_enabled(self) -> bool:
        return bool(self.project_id)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide cached settings instance."""
    return Settings()
