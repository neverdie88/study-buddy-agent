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

"""Observability tests: intent/outcome audit records, JSON logs and PII scrubbing."""

import asyncio
import json
import logging

from app.observability.audit_plugin import ObservabilityAuditPlugin
from app.observability.logger import configure_logging


class _Tool:
    name = "remove_kid_calendar_event"


class _Ctx:
    function_call_id = "call-42"
    invocation_id = "inv-1"
    agent_name = "schedule_manager_agent"
    tool_confirmation = None


def test_audit_plugin_emits_intent_and_outcome_at_info_level(caplog):
    configure_logging()
    plugin = ObservabilityAuditPlugin()
    with caplog.at_level(logging.INFO, logger="study_buddy.audit"):
        asyncio.run(
            plugin.before_tool_callback(
                tool=_Tool(), tool_args={"event_id": "evt_1234", "reason": "email me at a@b.com"}, tool_context=_Ctx()
            )
        )
        asyncio.run(
            plugin.after_tool_callback(
                tool=_Tool(),
                tool_args={"event_id": "evt_1234"},
                tool_context=_Ctx(),
                result={"status": "pending_confirmation"},
            )
        )
    records = {r.getMessage(): r for r in caplog.records}
    intent, outcome = records["TOOL_INTENT"], records["TOOL_OUTCOME"]
    assert intent.call_id == outcome.call_id == "call-42"
    assert intent.high_stakes is True
    assert "a@b.com" not in json.dumps(intent.tool_args)
    assert outcome.outcome == {"status": "pending_confirmation"}
    assert outcome.latency_ms is not None
