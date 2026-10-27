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

"""Child-safety and pedagogy guardrails implemented as an ADK ``BasePlugin``.

Layers (defence in depth):

1. **Input screening** (``on_user_message_callback``): blocks prompt-injection
   attempts and unsafe topics, and scrubs PII the kid may type (address,
   phone...) before it ever reaches the model or session history.
2. **Model Armor** (optional, ``BUDDY_MODEL_ARMOR_TEMPLATE``): Google Cloud
   Model Armor ``sanitizeUserPrompt`` for jailbreak / harmful-content detection.
3. **Tool-result screening** (``after_tool_callback``): treats retrieved
   documents as untrusted data and neutralises embedded instructions.
4. **Output self-evaluation**: the ``pedagogy_critic_agent`` reviews every
   tutor draft (no answer-dumping, age-appropriate) - see ``app/agent.py``.
5. **Gemini safety settings**: strict ``HarmBlockThreshold`` on all agents.
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

from google.adk.agents.invocation_context import InvocationContext
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from app.observability.logger import get_logger
from app.observability.pii_redactor import get_pii_scrubber
from app.observability.tracing import traced_span

logger = get_logger(__name__)

INJECTION_PATTERNS = re.compile(
    r"(ignore (all |your |the )?(previous|prior|above) (instructions|rules)|"
    r"you are no longer|pretend you are not|system prompt|developer mode|jailbreak|"
    r"reveal your (instructions|prompt)|act as (an? )?(adult|unfiltered)|disregard (the|your) rules)",
    re.IGNORECASE,
)
UNSAFE_TOPICS = re.compile(
    r"\b(kill|suicide|self[- ]?harm|weapon|gun|knife fight|drugs?|alcohol|sexy|porn|naked|"
    r"meet (me|up) (in person|alone)|send (me )?(a )?photo of (you|yourself))\b",
    re.IGNORECASE,
)
DISTRESS_SIGNALS = re.compile(r"\b(someone (hurt|hit) me|i('m| am) scared|i('m| am) not safe|bully|bullied)\b", re.IGNORECASE)

SAFE_REDIRECT = (
    "That's a question for a grown-up you trust. 💛 "
    "Shall we get back to our homework adventure or check your calendar?"
)
DISTRESS_REPLY = (
    "Thank you for telling me. 💛 That sounds really important. Please tell a parent or your teacher "
    "right now - they want to help you and keep you safe."
)

KID_SAFETY_SETTINGS = [
    types.SafetySetting(category=c, threshold=types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE)
    for c in (
        types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
    )
]


def screen_text(text: str) -> tuple[str, str | None]:
    """Return (verdict, canned_reply). Verdict: allow | block_injection | block_unsafe | distress."""
    if DISTRESS_SIGNALS.search(text):
        return "distress", DISTRESS_REPLY
    if INJECTION_PATTERNS.search(text):
        return "block_injection", "Hehe, I'm Pip and I only know how to be your study buddy! 🐰 " \
            "What homework shall we try together?"
    if UNSAFE_TOPICS.search(text):
        return "block_unsafe", SAFE_REDIRECT
    return "allow", None


def _model_armor_flags(text: str) -> bool:
    """Optional Google Cloud Model Armor screening. Returns True if flagged."""
    template = os.getenv("BUDDY_MODEL_ARMOR_TEMPLATE")
    if not template:
        return False
    try:
        from google.cloud import modelarmor_v1

        location = template.split("/locations/")[1].split("/")[0]
        client = modelarmor_v1.ModelArmorClient(
            client_options={"api_endpoint": f"modelarmor.{location}.rep.googleapis.com"}
        )
        resp = client.sanitize_user_prompt(
            request=modelarmor_v1.SanitizeUserPromptRequest(
                name=template, user_prompt_data=modelarmor_v1.DataItem(text=text)
            )
        )
        return resp.sanitization_result.filter_match_state == modelarmor_v1.FilterMatchState.MATCH_FOUND
    except Exception as exc:  # noqa: BLE001
        logger.warning("model_armor_unavailable", extra={"error_type": type(exc).__name__})
        return False


class ChildSafetyAndPedagogyPlugin(BasePlugin):
    """Global guardrails applied to every agent, model and tool call."""

    def __init__(self) -> None:
        super().__init__(name="child_safety_and_pedagogy")
        self._scrubber = get_pii_scrubber()

    async def on_user_message_callback(
        self, *, invocation_context: InvocationContext, user_message: types.Content
    ) -> Optional[types.Content]:
        if not user_message or not user_message.parts:
            return None
        text = " ".join(p.text for p in user_message.parts if getattr(p, "text", None))
        if not text:
            return None
        with traced_span("guardrail.input_screen") as span:
            verdict, reply = screen_text(text)
            if verdict == "allow" and _model_armor_flags(text):
                verdict, reply = "block_model_armor", SAFE_REDIRECT
            span.set_attribute("buddy.verdict", verdict)

        invocation_context.session.state["temp:guardrail_verdict"] = verdict
        if verdict != "allow":
            logger.warning("guardrail_triggered", extra={"verdict": verdict, "stage": "input"})
            invocation_context.session.state["temp:guardrail_reply"] = reply
            # Replace the message with a neutral marker so nothing unsafe reaches the model.
            return types.Content(role="user", parts=[types.Part(text=f"[guardrail:{verdict}]")])

        scrubbed = self._scrubber.redact_text(text)
        if scrubbed != text:
            logger.info("guardrail_pii_scrubbed", extra={"stage": "input"})
            kept = [p for p in user_message.parts if not getattr(p, "text", None)]
            return types.Content(role="user", parts=[types.Part(text=scrubbed), *kept])
        return None

    async def before_model_callback(self, *, callback_context, llm_request):
        verdict = callback_context.state.get("temp:guardrail_verdict")
        if verdict and verdict != "allow":
            from google.adk.models import LlmResponse

            reply = callback_context.state.get("temp:guardrail_reply") or SAFE_REDIRECT
            return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=reply)]))
        return None

    async def after_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext, result: dict
    ) -> Optional[dict]:
        if tool.name not in {"search_school_materials"} or not isinstance(result, dict):
            return None
        flagged = False
        for passage in result.get("passages", []) or []:
            excerpt = passage.get("excerpt", "")
            if INJECTION_PATTERNS.search(excerpt):
                passage["excerpt"] = INJECTION_PATTERNS.sub("[removed untrusted instruction]", excerpt)
                flagged = True
        if flagged:
            logger.warning("guardrail_triggered", extra={"verdict": "tool_result_injection", "tool": tool.name})
            return result
        return None
