import json
import logging

import httpx
import pytest
from app.agent_runtime.context import RunContext
from app.agent_runtime.runtime import BusinessRuntime
from app.config import Settings
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


@pytest.mark.parametrize("streaming", [False, True])
async def test_actual_sdk_runner_executes_registered_tool_and_validates_output(app, conversation, streaming, caplog):
    p = Principal(
        user_id="dev-operator",
        scopes=frozenset({"knowledge:read"}),
        knowledge_base_ids=(KB_SUPPORT,),
    )
    turn, _, _ = await app.state.store.begin_turn(p, conversation, "sdk", "Find the integration sample", "voice", 0)
    ctx = RunContext(p, conversation, turn.id, turn.epoch, request_revision=turn.request_revision)
    caplog.set_level("INFO", logger="app.agent_runtime.external")
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
        response = {
            "id": f"resp_{len(requests)}",
            "object": "response",
            "created_at": 1788600000,
            "model": "contract-fixture",
            "status": "completed",
            "output": output,
            "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
        }
        if streaming:
            event = {"type": "response.completed", "sequence_number": 1, "response": response}
            return httpx.Response(
                200,
                content="event: response.completed\ndata: " + json.dumps(event) + "\n\n",
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(200, json=response)

    runtime = BusinessRuntime(Settings(_env_file=None, agent_provider="openai", agent_model="contract-fixture", openai_api_key="synthetic-test-key"), app.state.registry)
    await runtime.client.close()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        runtime.executor.client = AsyncOpenAI(api_key="synthetic-test-key", http_client=http, max_retries=0)
        progress = []

        async def report(message):
            progress.append(message)

        result = await runtime.run("Find the integration sample", ctx, [], report if streaming else None)
        if streaming:
            assert progress == ["Searching Knowledge base"]
    assert result.status == "answered", result
    assert result.citations[0].citation_id == "C1"
    assert len(requests) == 2
    assert "previous_response_id" not in requests[0]

    timings = [record.message for record in caplog.records if "agent_model_call_finished" in record.message]
    assert len(timings) == 2
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


@pytest.mark.parametrize("streaming", [False, True])
async def test_compatible_chat_completions_runs_required_tool_loop(
    app, conversation, streaming, caplog
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
    caplog.set_level("INFO", logger="app.agent_runtime.external")
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

    runtime = BusinessRuntime(Settings(_env_file=None, agent_provider="compatible", agent_model="contract-fixture", openai_api_key="synthetic-test-key", agent_base_url="http://compatible.test/v1"), app.state.registry)
    await runtime.client.close()

    async def report_progress(_):
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        runtime.executor.client = AsyncOpenAI(
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

    assert result.status == "answered", result
    assert result.citations[0].citation_id == "C1"
    assert len(requests) == 2

    timings = [record.message for record in caplog.records if "agent_model_call_finished" in record.message]
    assert len(timings) == 2
    assert "call_index=1" in timings[0] and "call_index=2" in timings[1]
    assert all("status=completed" in line and "duration_ms=" in line for line in timings)
    assert all("integration sample" not in line for line in timings)
