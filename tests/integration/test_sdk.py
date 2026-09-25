import json

import httpx
import pytest
from app.agent_runtime.context import RunContext
from app.contracts import AgentAnswer, Principal
from openai import AsyncOpenAI

KB_SUPPORT = "00000000-0000-4000-8000-000000000001"


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
async def test_actual_sdk_runner_executes_registered_tool_and_validates_output(app, conversation, streaming):
    p = Principal(
        user_id="dev-operator",
        scopes=frozenset({"knowledge:read"}),
        knowledge_base_ids=(KB_SUPPORT,),
    )
    turn, _, _ = await app.state.store.begin_turn(p, conversation, "sdk", "Find the integration sample", "voice", 0)
    ctx = RunContext(p, conversation, turn.id, turn.epoch, request_revision=turn.request_revision)
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
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
    assert result.status == "answered", result
    assert result.citations[0].citation_id == "C1"
    assert len(requests) == 2
    assert "previous_response_id" not in requests[0]
