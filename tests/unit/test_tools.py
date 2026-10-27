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

"""Unit tests for tools: strict schemas, guided errors and HITL behaviour."""

from datetime import datetime, timedelta

from app.tools.calendar_tools import (
    list_kid_calendar_events,
    remove_kid_calendar_event,
    schedule_kid_calendar_event,
)
from app.tools.school_material_tools import list_available_study_guides, search_school_materials
from app.tools.whiteboard_tools import WHITEBOARD_STATE_KEY, clear_whiteboard, draw_on_whiteboard

TOMORROW = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")


# ----------------------------------------------------------------- calendar
def test_list_events_rejects_bad_dates_with_recovery_instructions():
    result = list_kid_calendar_events("next friday", TOMORROW)
    assert result["status"] == "error"
    assert result["error_code"] == "INVALID_ARGUMENTS"
    assert "recovery_instructions" in result and result["recovery_instructions"]


def test_list_events_rejects_ranges_over_31_days():
    result = list_kid_calendar_events("2026-01-01", "2026-03-01")
    assert result["error_code"] == "INVALID_ARGUMENTS"


def test_schedule_event_enforces_bedtime_rule(tool_context):
    result = schedule_kid_calendar_event("Movie night", f"{TOMORROW}T21:00", 60, "family", tool_context)
    assert result["status"] == "error"
    assert "19:59" in result["message"]


def test_schedule_event_rejects_unknown_category(tool_context):
    result = schedule_kid_calendar_event("Practice", f"{TOMORROW}T16:00", 30, "party_time", tool_context)
    assert result["error_code"] == "INVALID_ARGUMENTS"


def test_schedule_then_list_roundtrip(tool_context):
    created = schedule_kid_calendar_event("Practise spelling", f"{TOMORROW}T18:30", 20, "study_session", tool_context)
    assert created["status"] == "success", created
    listed = list_kid_calendar_events(TOMORROW, TOMORROW)
    assert any(e["title"] == "Practise spelling" for e in listed["events"])


def test_double_booking_requires_grown_up_confirmation(fake_tool_context_cls):
    first = schedule_kid_calendar_event("Piano", f"{TOMORROW}T18:00", 60, "hobby", fake_tool_context_cls())
    assert first["status"] == "success"
    ctx = fake_tool_context_cls()
    second = schedule_kid_calendar_event("Drawing", f"{TOMORROW}T18:30", 30, "hobby", ctx)
    assert second["status"] == "pending_confirmation"
    assert ctx.requested and "overlaps" in ctx.requested[0]["hint"]


def test_remove_event_always_pauses_for_hitl(fake_tool_context_cls):
    created = schedule_kid_calendar_event("Dance", f"{TOMORROW}T19:00", 45, "hobby", fake_tool_context_cls())
    event_id = created["event"]["event_id"]

    ctx = fake_tool_context_cls()
    pending = remove_kid_calendar_event(event_id, "I'm tired", ctx)
    assert pending["status"] == "pending_confirmation"
    assert ctx.requested[0]["payload"]["event_id"] == event_id
    # Nothing deleted yet.
    assert any(e["event_id"] == event_id for e in list_kid_calendar_events(TOMORROW, TOMORROW)["events"])

    rejected = remove_kid_calendar_event(event_id, "I'm tired", fake_tool_context_cls(confirmed=False))
    assert rejected["status"] == "rejected"
    assert any(e["event_id"] == event_id for e in list_kid_calendar_events(TOMORROW, TOMORROW)["events"])

    approved = remove_kid_calendar_event(event_id, "I'm tired", fake_tool_context_cls(confirmed=True))
    assert approved["status"] == "success"
    assert not any(e["event_id"] == event_id for e in list_kid_calendar_events(TOMORROW, TOMORROW)["events"])


def test_remove_unknown_event_guides_agent_to_list_first(tool_context):
    result = remove_kid_calendar_event("evt_doesnotexist", "not needed", tool_context)
    assert result["error_code"] == "EVENT_NOT_FOUND"
    assert "list_kid_calendar_events" in result["recovery_instructions"]


def test_protected_events_are_flagged_in_hitl_hint(fake_tool_context_cls):
    created = schedule_kid_calendar_event("Spelling quiz", f"{TOMORROW}T07:00", 30, "exam", fake_tool_context_cls())
    ctx = fake_tool_context_cls()
    remove_kid_calendar_event(created["event"]["event_id"], "don't want to", ctx)
    assert "IMPORTANT" in ctx.requested[0]["hint"]
    assert ctx.requested[0]["payload"]["protected"] is True


# ----------------------------------------------------------------- school materials
def test_search_school_materials_finds_teacher_method():
    result = search_school_materials("adding with regrouping carry the ten", "math", 3)
    assert result["status"] == "success"
    assert any("Ones First" in p["excerpt"] or "carry" in p["excerpt"].lower() for p in result["passages"])
    assert all(0 <= p["relevance_score"] <= 1 for p in result["passages"])


def test_search_school_materials_no_match_returns_guided_error():
    result = search_school_materials("quantum chromodynamics", "science", 2)
    assert result["error_code"] == "NO_MATCHING_MATERIALS"
    assert "list_available_study_guides" in result["recovery_instructions"]


def test_search_school_materials_validates_max_results():
    assert search_school_materials("fractions", "math", 50)["error_code"] == "INVALID_ARGUMENTS"


def test_list_study_guides():
    guides = list_available_study_guides()["guides"]
    assert {g["subject"] for g in guides} >= {"math", "science", "english"}


# ----------------------------------------------------------------- whiteboard
def test_draw_on_whiteboard_persists_validated_scene(tool_context):
    result = draw_on_whiteboard(
        "Quarters of a pizza",
        [
            {"type": "fraction_shape", "shape": "pizza", "parts": 4, "shaded": 1, "label": "1 out of 4"},
            {"type": "equation", "expression": "1/4 + 1/4 = ?"},
            {"type": "sticker", "sticker": "🍕"},
        ],
        tool_context,
    )
    assert result["status"] == "success" and result["scene_version"] == 1
    scene = tool_context.state[WHITEBOARD_STATE_KEY]
    assert scene["title"] == "Quarters of a pizza" and len(scene["elements"]) == 3


def test_draw_on_whiteboard_rejects_invalid_elements_with_guide(tool_context):
    result = draw_on_whiteboard(
        "Bad", [{"type": "fraction_shape", "parts": 4, "shaded": 7}, {"type": "sticker", "sticker": "🔫"}], tool_context
    )
    assert result["error_code"] == "INVALID_ARGUMENTS"
    assert "Allowed element types" in result["recovery_instructions"]
    assert WHITEBOARD_STATE_KEY not in tool_context.state


def test_draw_on_whiteboard_rejects_unknown_type(tool_context):
    result = draw_on_whiteboard("x", [{"type": "laser_beam"}], tool_context)
    assert result["status"] == "error"


def test_clear_whiteboard_bumps_version(tool_context):
    draw_on_whiteboard("A", [{"type": "text", "text": "hi"}], tool_context)
    clear_whiteboard(tool_context)
    assert tool_context.state[WHITEBOARD_STATE_KEY]["elements"] == []
    assert tool_context.state[WHITEBOARD_STATE_KEY]["version"] == 2
