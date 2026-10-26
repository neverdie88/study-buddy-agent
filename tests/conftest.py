"""Shared pytest fixtures: isolated, offline, deterministic environment."""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Every test gets its own SQLite files and no cloud dependencies."""
    monkeypatch.setenv("BUDDY_SQLITE_PATH", str(tmp_path / "buddy.db"))
    monkeypatch.setenv("BUDDY_SESSION_DB_URL", f"sqlite+aiosqlite:///{tmp_path / 'sessions.db'}")
    monkeypatch.setenv("BUDDY_ENABLE_CLOUD_DLP", "false")
    monkeypatch.setenv("BUDDY_ENABLE_CLOUD_TRACE", "false")
    monkeypatch.setenv("BUDDY_ENABLE_CLOUD_LOGGING", "false")
    if os.getenv("BUDDY_RUN_LIVE_EVAL") != "1":
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    from app.config import settings as settings_mod
    from app.memory import session_store
    from app.observability import pii_redactor
    from app.tools import school_material_tools

    for cached in (
        settings_mod.get_settings,
        session_store.get_store,
        pii_redactor.get_pii_scrubber,
        school_material_tools.get_material_index,
    ):
        cached.cache_clear()
    yield
    for cached in (settings_mod.get_settings, session_store.get_store, school_material_tools.get_material_index):
        cached.cache_clear()


class FakeToolContext:
    """Minimal stand-in for ADK ToolContext used by unit tests."""

    def __init__(self, confirmed: bool | None = None):
        self.state: dict = {}
        self.requested: list[dict] = []
        self.function_call_id = "call-test-1"
        if confirmed is None:
            self.tool_confirmation = None
        else:
            from google.adk.tools.tool_confirmation import ToolConfirmation

            self.tool_confirmation = ToolConfirmation(confirmed=confirmed)

    def request_confirmation(self, *, hint=None, payload=None):
        self.requested.append({"hint": hint, "payload": payload})


@pytest.fixture
def tool_context():
    return FakeToolContext()


@pytest.fixture
def fake_tool_context_cls():
    return FakeToolContext
