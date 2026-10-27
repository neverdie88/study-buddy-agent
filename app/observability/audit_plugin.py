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

"""Intent-vs-Outcome audit plugin.

For every tool call we emit a pair of correlated structured log records:

* ``TOOL_INTENT`` - *before* execution: which agent intends to do what, with
  which (PII-scrubbed) arguments, and whether it is a high-stakes action.
* ``TOOL_OUTCOME`` - *after* execution: status, latency, error code, and a
  compact result summary.

Both share ``call_id``, ``invocation_id`` and the OpenTelemetry trace id, so a
parent can see "Pip *wanted* to delete Dance class -> a grown-up *rejected* it".
Agent and model lifecycle events are logged too.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from app.observability.logger import get_logger
from app.observability.pii_redactor import get_pii_scrubber

logger = get_logger("study_buddy.audit")

HIGH_STAKES_TOOLS = {"remove_kid_calendar_event", "schedule_kid_calendar_event"}

_INTENT_DESCRIPTIONS = {
    "list_kid_calendar_events": "read the kid's schedule",
    "schedule_kid_calendar_event": "add an event to the kid's calendar",
    "remove_kid_calendar_event": "remove an event from the kid's calendar (requires grown-up approval)",
    "search_school_materials": "look up the kid's class materials",
    "list_available_study_guides": "list available study guides",
    "draw_on_whiteboard": "draw a visual explanation on the whiteboard",
    "clear_whiteboard": "clear the whiteboard",
    "retrieve_learner_profile": "read the learner profile",
    "record_learning_milestone": "update the kid's learning progress",
    "transfer_to_agent": "hand the conversation to a specialist agent",
}


def _summarize_result(result: Any) -> dict:
    if not isinstance(result, dict):
        return {"type": type(result).__name__}
    summary = {k: result[k] for k in ("status", "error_code", "count", "scene_version", "removed_event_id") if k in result}
    if "passages" in result:
        summary["passages"] = len(result["passages"])
    return summary


class ObservabilityAuditPlugin(BasePlugin):
    """Structured intent/outcome logging across all agents and tools."""

    def __init__(self) -> None:
        super().__init__(name="observability_audit")
        self._starts: dict[str, float] = {}
        self._scrubber = get_pii_scrubber()

    # ---------------- agents ----------------
    async def before_agent_callback(self, *, agent, callback_context) -> None:
        logger.info(
            "AGENT_START",
            extra={"agent": agent.name, "invocation_id": callback_context.invocation_id},
        )
        return None

    async def after_agent_callback(self, *, agent, callback_context) -> None:
        logger.info(
            "AGENT_END",
            extra={"agent": agent.name, "invocation_id": callback_context.invocation_id},
        )
        return None

    # ---------------- models ----------------
    async def after_model_callback(self, *, callback_context, llm_response) -> None:
        usage = getattr(llm_response, "usage_metadata", None)
        logger.info(
            "MODEL_RESPONSE",
            extra={
                "agent": callback_context.agent_name,
                "prompt_tokens": getattr(usage, "prompt_token_count", None),
                "output_tokens": getattr(usage, "candidates_token_count", None),
                "cached_tokens": getattr(usage, "cached_content_token_count", None),
                "finish_reason": str(getattr(llm_response, "finish_reason", "") or ""),
            },
        )
        return None

    # ---------------- tools ----------------
    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
    ) -> Optional[dict]:
        call_id = tool_context.function_call_id or f"{tool.name}-{time.time_ns()}"
        self._starts[call_id] = time.perf_counter()
        logger.info(
            "TOOL_INTENT",
            extra={
                "call_id": call_id,
                "invocation_id": tool_context.invocation_id,
                "agent": tool_context.agent_name,
                "tool": tool.name,
                "intent": _INTENT_DESCRIPTIONS.get(tool.name, f"call {tool.name}"),
                "high_stakes": tool.name in HIGH_STAKES_TOOLS,
                "is_confirmation_resume": tool_context.tool_confirmation is not None,
                "tool_args": self._scrubber.redact({k: v for k, v in tool_args.items() if k != "tool_context"}),
            },
        )
        return None

    async def after_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext, result: dict
    ) -> Optional[dict]:
        call_id = tool_context.function_call_id or ""
        started = self._starts.pop(call_id, None)
        logger.info(
            "TOOL_OUTCOME",
            extra={
                "call_id": call_id,
                "invocation_id": tool_context.invocation_id,
                "agent": tool_context.agent_name,
                "tool": tool.name,
                "outcome": _summarize_result(result),
                "latency_ms": round((time.perf_counter() - started) * 1000, 1) if started else None,
                "confirmed_by_grown_up": (
                    tool_context.tool_confirmation.confirmed if tool_context.tool_confirmation else None
                ),
            },
        )
        return None

    async def on_tool_error_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext, error: Exception
    ) -> Optional[dict]:
        logger.error(
            "TOOL_OUTCOME",
            extra={
                "call_id": tool_context.function_call_id,
                "tool": tool.name,
                "outcome": {"status": "exception", "error_type": type(error).__name__},
            },
        )
        return {
            "status": "error",
            "error_code": "UNEXPECTED_TOOL_FAILURE",
            "message": f"{tool.name} failed unexpectedly.",
            "recovery_instructions": "Do not retry. Apologise kindly and continue helping without this tool.",
        }
