import asyncio
import json
import logging
from types import SimpleNamespace

import httpx
import pytest
from app.agent_runtime.context import RunContext
from app.contracts import AgentAnswer, Principal
from openai import AsyncOpenAI

KB_SUPPORT = "00000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def capture_application_logs(app, caplog):
    logger = logging.getLogger("app")
    logger.addHandler(caplog.handler)
    try:
        yield
    finally:
        logger.removeHandler(caplog.handler)


async def test_non_ascii_speech_keeps_written_answer_but_uses_ascii_voice_fallback(app, conversation):
    principal = Principal(
        user_id="dev-operator",
        scopes=frozenset({"knowledge:read"}),
        knowledge_base_ids=(KB_SUPPORT,),
    )
    context = RunContext(principal, conversation, "turn-1", 0)
    answer = AgentAnswer(
        status="needs_clarification",
        display_text="Please confirm the café product model.",
        speech_text="Please confirm the café product model.",
        citation_ids=[],
    )
    result = await app.state.coordinator.runtime.validate(answer, context)
    assert "café" in result.display_text
    assert result.speech_text.isascii()
    assert result.reason_code == "VOICE_NON_ASCII_SPEECH"


@pytest.mark.parametrize("citation_case", [None, "undeclared", "unknown", "uuid", "history", "repeat", "unsupported"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_actual_sdk_runner_executes_registered_tool_and_validates_output(app, conversation, streaming, caplog, citation_case):
    p = Principal(
        user_id="dev-operator",
        scopes=frozenset({"knowledge:read"}),
        knowledge_base_ids=(KB_SUPPORT,),
    )
    turn, _, _ = await app.state.store.begin_turn(p, conversation, "sdk", "Find the integration sample", "voice", 0)
    ctx = RunContext(p, conversation, turn.id, turn.epoch, request_revision=turn.request_revision)
    caplog.set_level("INFO", logger="app.agent_runtime.runtime")
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            assert body["tool_choice"] == "required"
            output = [
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "search_knowledge",
                    "arguments": '{"query":"Find the integration sample"}',
                    "status": "completed",
                }
            ]
        else:
            tool = next(x for x in body["input"] if x.get("type") == "function_call_output")
            assert tool["call_id"] == "call_1"
            assert "Synthetic integration excerpt" in tool["output"]
            output = [
                {
                    "type": "message",
                    "id": "msg_1",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                {
                                    "status": "answered",
                                    "display_text": "This is a synthetic integration excerpt, not policy [C1]",
                                    "speech_text": "This is a synthetic integration excerpt.",
                                    "citation_ids": ["C1"],
                                },
                                ensure_ascii=False,
                            ),
                            "annotations": [],
                        }
                    ],
                }
            ]
        if len(requests) > 1:
            item = output[0]["content"][0]
            item["text"] = json.dumps(citation_answer(json.loads(item["text"]), citation_case, len(requests)))
        if len(requests) == 3:
            assert not body.get("tools")
            assert body["tool_choice"] == "none"
            assert "Allowed citation IDs" in json.dumps(body["input"])
        response = {
            "id": f"resp_{len(requests)}",
            "object": "response",
            "created_at": 1788600000,
            "model": "contract-fixture",
            "status": "completed",
            "output": output,
            "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
        }
        if body.get("stream"):
            event = {"type": "response.completed", "sequence_number": 1, "response": response}
            return httpx.Response(
                200,
                content="event: response.completed\ndata: " + json.dumps(event) + "\n\n",
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(200, json=response)

    runtime = app.state.coordinator.runtime
    app.state.settings.agent_provider = "openai"
    app.state.settings.agent_model = "contract-fixture"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        runtime.client = AsyncOpenAI(api_key="synthetic-test-key", http_client=http, max_retries=0)
        progress = []

        async def report(message):
            progress.append(message)

        result = await runtime.run("Find the integration sample", ctx, [], report if streaming else None)
        if streaming:
            assert progress == ["Searching Knowledge base"]
    assert_citation_result(result, requests, citation_case, caplog)
    assert "previous_response_id" not in requests[0]

    timings = [record.message for record in caplog.records if "agent_model_call_finished" in record.message]
    assert len(timings) == (3 if citation_case else 2)
    assert "call_index=1" in timings[0] and "call_index=2" in timings[1]
    assert all("status=completed" in line and "duration_ms=" in line for line in timings)
    assert all("integration sample" not in line for line in timings)


async def test_knowledge_answer_fails_when_required_tool_was_not_called(app, conversation):
    principal = Principal(
        user_id="dev-operator",
        scopes=frozenset({"knowledge:read"}),
        knowledge_base_ids=(KB_SUPPORT,),
    )
    context = RunContext(principal, conversation, "turn-1", 0)
    context.allowed_tools = {"search_knowledge"}
    answer = AgentAnswer(
        status="insufficient_evidence",
        display_text="I could not find this in the knowledge base.",
        speech_text="I could not find this.",
        citation_ids=[],
    )

    result = await app.state.coordinator.runtime.validate(answer, context)

    assert result.status == "failed"
    assert result.reason_code == "AGENT_REQUIRED_TOOL_NOT_CALLED"
    assert "not executed" in result.display_text


async def test_tool_failure_cannot_be_rewritten_as_insufficient_evidence(app, conversation):
    principal = Principal(
        user_id="dev-operator",
        scopes=frozenset({"knowledge:read"}),
        knowledge_base_ids=(KB_SUPPORT,),
    )
    context = RunContext(principal, conversation, "turn-1", 0)
    context.allowed_tools = {"search_knowledge"}
    context.invoked.add("search_knowledge")
    context.tool_errors.append("CUEKB_AUTH_FAILED")
    answer = AgentAnswer(
        status="insufficient_evidence",
        display_text="No matching information was found.",
        speech_text="No matching information was found.",
        citation_ids=[],
    )

    result = await app.state.coordinator.runtime.validate(answer, context)

    assert result.status == "failed"
    assert result.reason_code == "CUEKB_AUTH_FAILED"
    assert "not configured correctly" in result.display_text


@pytest.mark.parametrize("citation_case", [None, "undeclared", "unknown", "uuid", "history", "repeat", "unsupported"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_compatible_chat_completions_runs_required_tool_loop(
    app, conversation, streaming, caplog, citation_case
):
    principal = Principal(
        user_id="dev-operator",
        scopes=frozenset({"knowledge:read"}),
        knowledge_base_ids=(KB_SUPPORT,),
    )
    turn, _, _ = await app.state.store.begin_turn(
        principal,
        conversation,
        f"compatible-{streaming}",
        "Find the integration sample",
        "text" if streaming else "voice",
        0,
    )
    context = RunContext(
        principal,
        conversation,
        turn.id,
        turn.epoch,
        request_revision=turn.request_revision,
    )
    caplog.set_level("INFO", logger="app.agent_runtime.runtime")
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        first = len(requests) == 1
        if first:
            assert body["tool_choice"] == "required"
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_compatible",
                        "type": "function",
                        "function": {
                            "name": "search_knowledge",
                            "arguments": '{"query":"Find the integration sample"}',
                        },
                    }
                ],
            }
            finish_reason = "tool_calls"
        else:
            tool_message = next(item for item in body["messages"] if item.get("role") == "tool")
            assert tool_message["tool_call_id"] == "call_compatible"
            assert "Synthetic integration excerpt" in tool_message["content"]
            message = {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "status": "answered",
                        "display_text": "Compatible model used the knowledge result [C1]",
                        "speech_text": "Compatible model used the knowledge result.",
                        "citation_ids": ["C1"],
                    }
                ),
            }
            finish_reason = "stop"
        if not first:
            message["content"] = json.dumps(citation_answer(json.loads(message["content"]), citation_case, len(requests)))
        if len(requests) == 3:
            assert not body.get("tools")
            assert body["tool_choice"] == "none"
            assert "Allowed citation IDs" in json.dumps(body["messages"])
        if body.get("stream"):
            delta = {"role": "assistant", **message}
            if delta.get("tool_calls"):
                delta["tool_calls"][0]["index"] = 0
            chunk = {
                "id": f"chatcmpl-{len(requests)}",
                "object": "chat.completion.chunk",
                "created": 1788600000,
                "model": "contract-fixture",
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
            finished = {
                **chunk,
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            }
            content = "".join(
                "data: " + json.dumps(item) + "\n\n" for item in (chunk, finished)
            ) + "data: [DONE]\n\n"
            return httpx.Response(200, content=content, headers={"Content-Type": "text/event-stream"})
        return httpx.Response(
            200,
            json={
                "id": f"chatcmpl-{len(requests)}",
                "object": "chat.completion",
                "created": 1788600000,
                "model": "contract-fixture",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": finish_reason,
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            },
        )

    runtime = app.state.coordinator.runtime
    app.state.settings.agent_provider = "compatible"
    app.state.settings.agent_model = "contract-fixture"

    async def report_progress(_):
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        runtime.client = AsyncOpenAI(
            api_key="synthetic-test-key",
            base_url="http://compatible.test/v1",
            http_client=http,
            max_retries=0,
        )
        result = await runtime.run(
            "Find the integration sample",
            context,
            [],
            report_progress if streaming else None,
        )

    assert_citation_result(result, requests, citation_case, caplog)

    timings = [record.message for record in caplog.records if "agent_model_call_finished" in record.message]
    assert len(timings) == (3 if citation_case else 2)
    assert "call_index=1" in timings[0] and "call_index=2" in timings[1]
    assert all("status=completed" in line and "duration_ms=" in line for line in timings)
    assert all("integration sample" not in line for line in timings)


PRIVATE_CITATION = "document-secret-uuid\napi-key-do-not-log"


def citation_answer(answer, case, request_count):
    if case and (request_count == 2 or case == "repeat"):
        if case == "undeclared":
            answer["citation_ids"] = []
        elif case == "uuid":
            answer["citation_ids"] = [PRIVATE_CITATION]
        else:
            # C88 represents a citation from a previous turn, never this turn's C1.
            identifier = "C88" if case == "history" else "C99"
            answer["display_text"] = "Unsupported reference [" + identifier + "]"
            answer["citation_ids"] = [identifier]
    elif case == "unsupported":
        answer.update(status="insufficient_evidence", display_text="The evidence is insufficient.",
                      speech_text="The evidence is insufficient.", citation_ids=[])
    return answer


def assert_citation_result(result, requests, case, caplog):
    if case == "repeat":
        assert result.status == "failed"
        assert result.reason_code == "RAG_INVALID_CITATION"
    elif case == "unsupported":
        assert result.status == "insufficient_evidence"
        assert not result.citations
    else:
        assert result.status == "answered", result
        assert result.citations[0].citation_id == "C1"
    assert len(requests) == (3 if case else 2)
    messages = [record.getMessage() for record in caplog.records]
    assert sum("agent_tool_call_received" in line for line in messages) == 1
    failures = [line for line in messages if "agent_citation_validation_failed" in line]
    assert len(failures) == (2 if case == "repeat" else 1 if case else 0)
    if case == "undeclared":
        assert "failure_types=undeclared_reference" in failures[0]
    if case == "uuid":
        assert "sha256:" in failures[0]
    assert "api-key-do-not-log" not in "\n".join(messages)
    assert "document-secret-uuid" not in "\n".join(messages)


@pytest.mark.parametrize("boundary", ["runtime_timeout", "coordinator_timeout", "cancel", "stale", "revoked"])
async def test_citation_repair_preserves_deadline_cancellation_and_permissions(
    app, conversation, monkeypatch, caplog, boundary
):
    from agents import Runner

    principal = Principal(user_id="dev-operator", scopes=frozenset({"knowledge:read"}),
                          knowledge_base_ids=(KB_SUPPORT,))
    runtime = app.state.coordinator.runtime
    settings = app.state.settings
    settings.agent_provider = "compatible"
    settings.agent_model = "contract-fixture"
    settings.agent_deadline_ms = 300
    runtime.client = AsyncOpenAI(api_key="fixture", max_retries=0)
    repair_started = asyncio.Event()
    calls = []

    async def run(agent, inputs, *, context, **kwargs):
        calls.append(agent)
        if len(calls) == 1:
            if boundary in ("runtime_timeout", "coordinator_timeout"):
                await asyncio.sleep(0.18)
            await runtime.registry.invoke("search_knowledge", {"query": "integration sample"}, context)
            if boundary == "stale":
                await app.state.store.invalidate(principal, conversation, context.epoch)
            answer = AgentAnswer(status="answered", display_text="Fixture [C1]",
                                 speech_text="Fixture", citation_ids=[])
            return SimpleNamespace(final_output=answer, to_input_list=lambda: [])
        assert not agent.tools
        assert kwargs["max_turns"] == 1
        repair_started.set()
        if boundary == "revoked":
            original = runtime.registry.store.tool_revision

            async def changed_revision(name):
                return await original(name) + 1

            monkeypatch.setattr(runtime.registry.store, "tool_revision", changed_revision)
            return SimpleNamespace(final_output=AgentAnswer(
                status="answered", display_text="Fixture [C1]", speech_text="Fixture", citation_ids=["C1"]
            ))
        await asyncio.sleep(0.18 if boundary in ("runtime_timeout", "coordinator_timeout") else 5)
        return SimpleNamespace(final_output=AgentAnswer(
            status="answered", display_text="Fixture [C1]", speech_text="Fixture", citation_ids=["C1"]
        ))

    monkeypatch.setattr(Runner, "run", run)
    try:
        if boundary == "coordinator_timeout":
            _, task = await app.state.coordinator.submit(principal, conversation, "repair", "integration sample", "voice")
        else:
            turn, _, _ = await app.state.store.begin_turn(principal, conversation, "repair", "integration sample", "voice", 0)
            ctx = RunContext(principal, conversation, turn.id, turn.epoch, request_revision=turn.request_revision)
            task = asyncio.create_task(runtime.run("integration sample", ctx, []))
        if boundary == "cancel":
            await asyncio.wait_for(repair_started.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await asyncio.wait_for(task, timeout=2)
            expected = {"stale": "STALE_EPOCH", "revoked": "FORBIDDEN"}.get(boundary, "AGENT_TIMEOUT")
            assert result.reason_code == expected
        assert len(calls) == (1 if boundary == "stale" else 2)
        logs = "\n".join(record.getMessage() for record in caplog.records)
        if boundary == "runtime_timeout":
            assert "agent_run_timed_out" in logs
        if boundary == "coordinator_timeout":
            assert "agent_turn_timed_out" in logs
            assert "status=failed reason_code=AGENT_TIMEOUT" in logs
        if boundary == "cancel":
            assert "agent_run_timed_out" not in logs and "agent_turn_timed_out" not in logs
        assert logs.count("tool_run_started") == 1
    finally:
        await runtime.client.close()


def test_citation_diagnostics_are_bounded_and_do_not_log_provider_text():
    from app.agent_runtime.runtime import BusinessRuntime

    values = [f"C{i}" for i in range(100)] + [PRIVATE_CITATION, "x" * 10000]
    result = BusinessRuntime.citation_log_ids(values)
    assert len(result) < 600
    payload = json.loads(result)
    assert payload["count"] == 102 and len(payload["ids"]) == 20 and payload["omitted"] == 82
    result = BusinessRuntime.citation_log_ids([PRIVATE_CITATION, "x" * 10000])
    assert PRIVATE_CITATION not in result and "x" * 100 not in result
    assert "sha256:" in result and "\n" not in result
