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

"""Pip the Study Buddy - ADK multi-agent application.

Topology::

    study_buddy_coordinator (LlmAgent, Flash)          <- root, routes by intent
     ├── schedule_manager_agent (LlmAgent, Flash)       <- calendar tools + HITL
     ├── homework_tutor_pipeline (SequentialAgent)
     │    ├── curriculum_researcher_agent (Flash)       <- RAG over school materials
     │    ├── socratic_tutor_agent (Pro/Flash routed)   <- Socratic hints + whiteboard
     │    └── pedagogy_critic_agent (Flash)             <- self-eval guardrail (JSON schema)
     └── progress_coach_agent (LlmAgent, Flash)         <- learner memory

App-level: ChildSafetyAndPedagogyPlugin, ObservabilityAuditPlugin,
EventsCompactionConfig (sliding-window summarisation) and ContextCacheConfig.
"""

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps.app import App, EventsCompactionConfig
from google.adk.tools import FunctionTool
from google.genai import types

from app.config.settings import get_settings
from app.memory.async_memory_worker import memory_manager
from app.memory.session_store import get_or_create_profile
from app.observability.audit_plugin import ObservabilityAuditPlugin
from app.observability.logger import configure_logging, get_logger
from app.observability.tracing import init_tracing
from app.orchestration.guardrails_plugin import KID_SAFETY_SETTINGS, ChildSafetyAndPedagogyPlugin
from app.orchestration.model_router import model_for_role, route_model_by_complexity
from app.prompts.constitution import (
    COORDINATOR_INSTRUCTION,
    CURRICULUM_RESEARCHER_INSTRUCTION,
    PEDAGOGY_CRITIC_INSTRUCTION,
    PROGRESS_COACH_INSTRUCTION,
    SCHEDULE_MANAGER_INSTRUCTION,
    SOCRATIC_TUTOR_INSTRUCTION,
)
from app.schemas.curriculum_schemas import PedagogyReview
from app.tools.calendar_tools import (
    list_kid_calendar_events,
    remove_kid_calendar_event,
    schedule_kid_calendar_event,
)
from app.tools.progress_memory_tools import record_learning_milestone, retrieve_learner_profile
from app.tools.school_material_tools import list_available_study_guides, search_school_materials
from app.tools.whiteboard_tools import clear_whiteboard, draw_on_whiteboard

configure_logging()
init_tracing()
logger = get_logger(__name__)
settings = get_settings()

os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true" if settings.project_id else "false")


# --------------------------------------------------------------------------- #
# Callbacks
# --------------------------------------------------------------------------- #
def _profile_summary() -> str:
    try:
        p = get_or_create_profile(settings.kid_id)
    except Exception:  # noqa: BLE001
        return "- (profile unavailable)"
    lines = [
        f"- Nickname: {p.display_name} (Year {p.grade_level})",
        f"- Loves: {', '.join(p.interests) or 'unknown yet'}",
        f"- Learns best with: {p.preferred_style.value.replace('_', ' ')} explanations",
        f"- Encourage her in a {p.encouragement_style} way",
    ]
    if p.strengths():
        lines.append(f"- Strengths: {', '.join(p.strengths())}")
    if p.focus_areas():
        lines.append(f"- Gently practise: {', '.join(p.focus_areas())}")
    lines += [f"- Tip: {n}" for n in p.interaction_notes[-3:]]
    return "\n".join(lines)


def inject_session_context(callback_context: CallbackContext) -> None:
    """``before_agent_callback`` (root): refresh date + learner profile in state."""
    now = datetime.now(ZoneInfo(settings.kid_timezone))
    callback_context.state["current_date"] = now.strftime("%Y-%m-%d")
    callback_context.state["current_weekday"] = now.strftime("%A")
    callback_context.state["learner_profile_summary"] = _profile_summary()
    return None


def _last_text(events, author_predicate) -> str:
    for event in reversed(events):
        if author_predicate(event.author) and event.content and event.content.parts:
            text = " ".join(p.text for p in event.content.parts if getattr(p, "text", None) and not p.thought)
            if text.strip():
                return text
    return ""


def schedule_memory_consolidation(callback_context: CallbackContext) -> None:
    """``after_agent_callback`` (root): enqueue async memory work - never blocks the reply."""
    invocation = callback_context.get_invocation_context()
    kid_text = ""
    if invocation.user_content and invocation.user_content.parts:
        kid_text = " ".join(p.text for p in invocation.user_content.parts if getattr(p, "text", None))
    review = callback_context.state.get("pedagogy_review") or {}
    buddy_text = review.get("final_reply_to_kid") or _last_text(invocation.session.events, lambda a: a != "user")
    memory_manager.enqueue(settings.kid_id, kid_text, buddy_text)
    return None


def capture_tutor_draft(callback_context: CallbackContext) -> None:
    """``after_agent_callback`` (tutor): copy the draft for the critic to review."""
    invocation = callback_context.get_invocation_context()
    callback_context.state["tutor_draft"] = _last_text(
        invocation.session.events, lambda a: a == "socratic_tutor_agent"
    )
    return None


# --------------------------------------------------------------------------- #
# Agents
# --------------------------------------------------------------------------- #
def _gen_config(temperature: float = 0.6) -> types.GenerateContentConfig:
    return types.GenerateContentConfig(temperature=temperature, safety_settings=KID_SAFETY_SETTINGS)


schedule_manager_agent = LlmAgent(
    name="schedule_manager_agent",
    model=model_for_role("schedule_manager_agent"),
    description="Reads, adds and removes events on the kid's calendar. Removals need a grown-up's approval.",
    instruction=SCHEDULE_MANAGER_INSTRUCTION,
    tools=[
        list_kid_calendar_events,
        schedule_kid_calendar_event,
        remove_kid_calendar_event,
    ],
    generate_content_config=_gen_config(0.2),
)

curriculum_researcher_agent = LlmAgent(
    name="curriculum_researcher_agent",
    model=model_for_role("curriculum_researcher_agent"),
    description="Looks up how the kid's class teaches the concept in her real school materials.",
    instruction=CURRICULUM_RESEARCHER_INSTRUCTION,
    tools=[search_school_materials, list_available_study_guides],
    output_key="curriculum_brief",
    generate_content_config=_gen_config(0.1),
)

socratic_tutor_agent = LlmAgent(
    name="socratic_tutor_agent",
    model=model_for_role("socratic_tutor_agent"),
    description="Guides the kid through homework with Socratic hints and whiteboard drawings.",
    instruction=SOCRATIC_TUTOR_INSTRUCTION,
    tools=[draw_on_whiteboard, clear_whiteboard, record_learning_milestone],
    before_model_callback=route_model_by_complexity,
    after_agent_callback=capture_tutor_draft,
    generate_content_config=_gen_config(0.7),
)

pedagogy_critic_agent = LlmAgent(
    name="pedagogy_critic_agent",
    model=model_for_role("pedagogy_critic_agent"),
    description="Self-evaluation guardrail: blocks answer-dumping and age-inappropriate replies.",
    instruction=PEDAGOGY_CRITIC_INSTRUCTION,
    include_contents="none",
    output_schema=PedagogyReview,
    output_key="pedagogy_review",
    generate_content_config=_gen_config(0.0),
)

homework_tutor_pipeline = SequentialAgent(
    name="homework_tutor_pipeline",
    description=(
        "Homework helper: researches the class materials, tutors Socratically with the whiteboard, "
        "then self-checks the reply for safety and no answer-giving."
    ),
    sub_agents=[curriculum_researcher_agent, socratic_tutor_agent, pedagogy_critic_agent],
)

progress_coach_agent = LlmAgent(
    name="progress_coach_agent",
    model=model_for_role("progress_coach_agent"),
    description="Explains what Pip remembers, celebrates strengths and suggests what to practise next.",
    instruction=PROGRESS_COACH_INSTRUCTION,
    tools=[retrieve_learner_profile],
    generate_content_config=_gen_config(0.6),
)

root_agent = LlmAgent(
    name="study_buddy_coordinator",
    model=model_for_role("study_buddy_coordinator"),
    description="Pip the bunny: friendly front door that routes to calendar, homework or progress helpers.",
    instruction=COORDINATOR_INSTRUCTION,
    sub_agents=[schedule_manager_agent, homework_tutor_pipeline, progress_coach_agent],
    before_agent_callback=inject_session_context,
    after_agent_callback=schedule_memory_consolidation,
    generate_content_config=_gen_config(0.6),
)

app = App(
    name="app",
    root_agent=root_agent,
    plugins=[ChildSafetyAndPedagogyPlugin(), ObservabilityAuditPlugin()],
    events_compaction_config=EventsCompactionConfig(
        compaction_interval=settings.compaction_interval,
        overlap_size=settings.compaction_overlap,
    ),
    context_cache_config=ContextCacheConfig(min_tokens=4096, ttl_seconds=1800, cache_intervals=10),
)

__all__ = ["app", "root_agent", "FunctionTool"]
