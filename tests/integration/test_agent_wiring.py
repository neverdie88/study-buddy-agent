"""Offline integration tests: multi-agent wiring, plugins, memory config and HTTP API."""

from fastapi.testclient import TestClient


def test_multi_agent_topology_and_patterns():
    from google.adk.agents import LlmAgent, SequentialAgent

    from app.agent import app, root_agent

    assert root_agent.name == "study_buddy_coordinator"
    names = {a.name: a for a in root_agent.sub_agents}
    assert set(names) == {"schedule_manager_agent", "homework_tutor_pipeline", "progress_coach_agent"}

    pipeline = names["homework_tutor_pipeline"]
    assert isinstance(pipeline, SequentialAgent)
    assert [a.name for a in pipeline.sub_agents] == [
        "curriculum_researcher_agent",
        "socratic_tutor_agent",
        "pedagogy_critic_agent",
    ]
    researcher, tutor, critic = pipeline.sub_agents
    assert researcher.output_key == "curriculum_brief"
    assert tutor.before_model_callback is not None  # dynamic Flash/Pro routing
    assert critic.output_schema.__name__ == "PedagogyReview"  # self-eval guardrail
    assert all(isinstance(a, LlmAgent) for a in (researcher, tutor, critic))


def test_tools_are_wired_to_the_right_specialists():
    from app.agent import progress_coach_agent, schedule_manager_agent, socratic_tutor_agent

    def tool_names(agent):
        return {getattr(t, "__name__", getattr(t, "name", "")) for t in agent.tools}

    assert tool_names(schedule_manager_agent) == {
        "list_kid_calendar_events",
        "schedule_kid_calendar_event",
        "remove_kid_calendar_event",
    }
    assert {"draw_on_whiteboard", "record_learning_milestone"} <= tool_names(socratic_tutor_agent)
    assert tool_names(progress_coach_agent) == {"retrieve_learner_profile"}


def test_app_level_plugins_compaction_and_caching():
    from app.agent import app

    plugin_names = {p.name for p in app.plugins}
    assert {"child_safety_and_pedagogy", "observability_audit"} <= plugin_names
    assert app.events_compaction_config.compaction_interval > 0
    assert app.events_compaction_config.overlap_size >= 0
    assert app.context_cache_config is not None


def test_model_tiers_differ_between_roles():
    from app.agent import root_agent, socratic_tutor_agent

    assert root_agent.model != socratic_tutor_agent.model


def test_http_api_health_calendar_profile():
    from app.fast_api_app import app

    client = TestClient(app)
    assert client.get("/health").json()["status"] == "ok"
    events = client.get("/api/calendar?days=7").json()["events"]
    assert isinstance(events, list)
    profile = client.get("/api/profile").json()
    assert profile["grade_level"] == 3
    assert "bunnies" in profile["interests"]
    assert client.get("/").status_code == 200


def test_chat_rejects_empty_and_extra_fields():
    from app.fast_api_app import app

    client = TestClient(app)
    assert client.post("/api/chat", json={"text": "  "}).status_code == 400
    assert client.post("/api/chat", json={"text": "hi", "admin": True}).status_code == 422
