"""Automated evaluation harness for Pip the Study Buddy.

Runs every case in ``golden_dataset.json`` against the *real* multi-agent
system (ADK Runner + Gemini on Vertex AI), records which agents and tools were
used, the final reply, whiteboard and HITL state, then checks deterministic
expectations. Optionally adds an LLM-as-judge score for tutoring quality.

Usage::

    python -m tests.eval.run_evaluation                 # all cases
    python -m tests.eval.run_evaluation --category hitl # one category
    python -m tests.eval.run_evaluation --judge         # + Socratic quality judge
    python -m tests.eval.run_evaluation --min-pass-rate 0.9

Exits non-zero when the pass rate is below ``--min-pass-rate`` so CI can gate
deployments on agent regressions. Results are written to ``eval_results.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Isolate eval state from any local dev data.
_TMP = tempfile.mkdtemp(prefix="buddy_eval_")
os.environ.setdefault("BUDDY_SQLITE_PATH", f"{_TMP}/buddy.db")
os.environ.setdefault("BUDDY_SESSION_DB_URL", f"sqlite+aiosqlite:///{_TMP}/sessions.db")

from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types  # noqa: E402

from app.agent import app as adk_app  # noqa: E402
from app.config.settings import get_settings  # noqa: E402

DATASET = Path(__file__).with_name("golden_dataset.json")
JUDGE_PROMPT = """You grade a study-buddy reply for an 8-year-old. Score 1-5 for each:
socratic (guides with a question/hint instead of giving the final answer),
age_appropriate (simple, kind, short), helpful (moves learning forward).
Return JSON: {{"socratic": int, "age_appropriate": int, "helpful": int}}.
KID: {kid}
REPLY: {reply}"""


async def run_case(case: dict) -> dict:
    settings = get_settings()
    session_service = InMemorySessionService()
    runner = Runner(app=adk_app, session_service=session_service)
    session = await session_service.create_session(app_name=adk_app.name, user_id=settings.kid_id)
    agents, tools, tool_results, confirmations, replies = set(), [], [], [], []
    critic_reply = None

    for turn in case["turns"]:
        texts = []
        async for event in runner.run_async(
            user_id=settings.kid_id,
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=turn)]),
        ):
            agents.add(event.author)
            for call in event.get_function_calls() or []:
                if call.name == "adk_request_confirmation":
                    confirmations.append(call.id)
                else:
                    tools.append(call.name)
            for resp in event.get_function_responses() or []:
                tool_results.append({"name": resp.name, "status": (resp.response or {}).get("status")})
            if event.content and event.content.parts and not event.partial:
                text = "".join(p.text for p in event.content.parts if getattr(p, "text", None) and not p.thought)
                if text.strip():
                    texts.append((event.author, text.strip()))
        final = await session_service.get_session(app_name=adk_app.name, user_id=settings.kid_id, session_id=session.id)
        review = final.state.get("pedagogy_review") if "pedagogy_critic_agent" in {a for a, _ in texts} else None
        critic_reply = review.get("final_reply_to_kid") if isinstance(review, dict) else None
        visible = [t for a, t in texts if a not in {"curriculum_researcher_agent", "socratic_tutor_agent", "pedagogy_critic_agent"}]
        replies.append(critic_reply or (visible[-1] if visible else (texts[-1][1] if texts else "")))

    session_dump = json.dumps([e.model_dump(mode="json") for e in final.events], ensure_ascii=False)
    guardrail_verdict = None
    for e in final.events:
        if e.content and e.content.parts and e.content.parts[0].text and e.content.parts[0].text.startswith("[guardrail:"):
            guardrail_verdict = e.content.parts[0].text[len("[guardrail:"):-1]
    return {
        "agents": sorted(agents - {"user"}),
        "tools": tools,
        "tool_results": tool_results,
        "confirmations": confirmations,
        "reply": replies[-1] if replies else "",
        "whiteboard": final.state.get("whiteboard_scene"),
        "guardrail_verdict": guardrail_verdict,
        "session_dump": session_dump,
    }


def check(case: dict, obs: dict) -> list[str]:
    exp, failures = case["expect"], []
    reply = obs["reply"]
    for a in exp.get("agents_include", []):
        if a not in obs["agents"]:
            failures.append(f"agent '{a}' not used (used: {obs['agents']})")
    for t in exp.get("tools_include", []):
        if t not in obs["tools"]:
            failures.append(f"tool '{t}' not called (called: {obs['tools']})")
    for t in exp.get("tools_exclude_success", []):
        if any(r["name"] == t and r["status"] == "success" for r in obs["tool_results"]):
            failures.append(f"tool '{t}' should not have succeeded")
    if exp.get("pending_confirmation") and not obs["confirmations"]:
        failures.append("expected a HITL confirmation request")
    if exp.get("whiteboard") and not (obs["whiteboard"] or {}).get("elements"):
        failures.append("expected a whiteboard drawing")
    if "guardrail_verdict" in exp and obs["guardrail_verdict"] != exp["guardrail_verdict"]:
        failures.append(f"guardrail verdict {obs['guardrail_verdict']} != {exp['guardrail_verdict']}")
    for s in exp.get("reply_must_not_contain", []):
        if s.lower() in reply.lower():
            failures.append(f"reply contains forbidden '{s}'")
    if exp.get("reply_must_contain_any") and not any(s.lower() in reply.lower() for s in exp["reply_must_contain_any"]):
        failures.append(f"reply lacks any of {exp['reply_must_contain_any']}")
    if "reply_max_words" in exp and len(reply.split()) > exp["reply_max_words"]:
        failures.append(f"reply too long ({len(reply.split())} words)")
    for s in exp.get("session_must_not_contain", []):
        if s in obs["session_dump"]:
            failures.append(f"session history stored sensitive '{s}'")
    return failures


async def judge(kid: str, reply: str) -> dict:
    from google import genai

    s = get_settings()
    client = genai.Client(vertexai=True, project=s.project_id, location=s.location)
    resp = await client.aio.models.generate_content(
        model=s.fast_model,
        contents=JUDGE_PROMPT.format(kid=kid, reply=reply),
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0),
    )
    return json.loads(resp.text)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--category")
    parser.add_argument("--case")
    parser.add_argument("--judge", action="store_true")
    parser.add_argument("--min-pass-rate", type=float, default=0.85)
    parser.add_argument("--output", default=str(ROOT / "eval_results.json"))
    args = parser.parse_args()

    cases = json.loads(DATASET.read_text())["cases"]
    if args.category:
        cases = [c for c in cases if c["category"] == args.category]
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]

    results = []
    for case in cases:
        started = time.perf_counter()
        try:
            obs = await run_case(case)
            failures = check(case, obs)
        except Exception as exc:  # noqa: BLE001
            obs, failures = {"reply": "", "agents": [], "tools": []}, [f"exception: {type(exc).__name__}: {exc}"]
        row = {
            "id": case["id"],
            "category": case["category"],
            "passed": not failures,
            "failures": failures,
            "latency_s": round(time.perf_counter() - started, 2),
            "agents": obs.get("agents"),
            "tools": obs.get("tools"),
            "reply": obs.get("reply"),
        }
        if args.judge and case["category"] == "tutoring" and obs.get("reply"):
            try:
                row["judge"] = await judge(case["turns"][-1], obs["reply"])
            except Exception as exc:  # noqa: BLE001
                row["judge"] = {"error": type(exc).__name__}
        results.append(row)
        print(f"{'✅' if row['passed'] else '❌'} {case['id']:<32} {row['latency_s']:>6}s  {'; '.join(failures)}")

    pass_rate = sum(r["passed"] for r in results) / max(1, len(results))
    Path(args.output).write_text(json.dumps({"pass_rate": pass_rate, "results": results}, indent=2, ensure_ascii=False))
    print(f"\nPass rate: {pass_rate:.0%} ({sum(r['passed'] for r in results)}/{len(results)}) -> {args.output}")
    return 0 if pass_rate >= args.min_pass_rate else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
