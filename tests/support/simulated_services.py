"""Explicit loopback-only API simulators; never loaded by a deployment entry point."""

import asyncio
import base64
import json
import math
import os
import struct
from uuid import UUID, uuid4

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

KB = "00000000-0000-4000-8000-000000000001"
KEY = "synthetic-network-test-key"
SOURCE = "Synthetic AX guide: disconnect power before servicing. This is a test fixture, not policy."
QUESTION = "Find the synthetic AX guide for software version 3.2."
SPOKEN = "The synthetic AX guide says to disconnect power before servicing."
PCM = base64.b64encode(
    b"".join(struct.pack("<h", int(4000 * math.sin(2 * math.pi * 440 * i / 24000))) for i in range(1920))
).decode()


def control_app():
    app = FastAPI()
    app.state.requests = []

    @app.get("/health")
    async def health():
        return {"status": "ready", "simulation": True}

    @app.get("/__test__/requests")
    async def requests():
        return app.state.requests

    @app.delete("/__test__/requests")
    async def reset():
        app.state.requests.clear()
        return {"simulation": True}

    return app


def cuekb_app():
    app = control_app()

    @app.post("/v1/search")
    async def search(request: Request):
        if request.headers.get("authorization") != "Bearer " + KEY:
            return JSONResponse({"detail": "Invalid synthetic key"}, status_code=401)
        # Independently check the frozen /v1/search wire contract; do not reuse
        # the client's request model on both sides of the network boundary.
        body = await request.json()
        assert set(body) == {"query", "kb_ids", "mode", "top_k", "filters", "include_context"}
        assert isinstance(body["query"], str) and 0 < len(body["query"]) <= 2000
        assert body["mode"] in {"auto", "exact", "hybrid", "related"}
        assert type(body["top_k"]) is int and 1 <= body["top_k"] <= 20
        assert body["include_context"] is True
        assert set(body["filters"]) == {"document_ids", "product_model", "software_version"}
        assert body["filters"]["document_ids"] == []
        for field in ("product_model", "software_version"):
            assert body["filters"][field] is None or isinstance(body["filters"][field], str)
        for kb_id in body["kb_ids"]:
            UUID(kb_id)
        app.state.requests.append(body)
        assert body["kb_ids"] == [KB]
        query = body["query"].lower()
        errors = {"unauthorized": 401, "forbidden": 403, "rate limited": 429, "service error": 503}
        for marker, status in errors.items():
            if marker in query:
                return JSONResponse({"detail": "Simulated service error"}, status_code=status)
        if "slow" in query:
            await asyncio.sleep(1.5)
        if "malformed" in query:
            return {"trace_id": "invalid-uuid", "hits": []}
        missing = "no evidence" in query
        result = {
            "trace_id": str(uuid4()),
            "retrieval_status": "not_found" if missing else ("degraded" if "degraded" in query else "ok"),
            "evidence_status": "unassessed",
            "degraded_reasons": ["synthetic-stage-unavailable"] if "degraded" in query else [],
            "scope_limited": False,
            "content_revisions": {KB: 7},
            "timings_ms": {"total": 1.0},
            "retrieval_path": "keyword",
            "executed_stages": ["keyword"],
            "skipped_stages": [],
            "hits": []
            if missing
            else [
                {
                    "document_id": "00000000-0000-4000-8000-000000000101",
                    "chunk_id": "00000000-0000-4000-8000-000000000102",
                    "version_id": "00000000-0000-4000-8000-000000000103",
                    "rank": 1,
                    "source_text": SOURCE,
                    "context": "Synthetic conditions: AX, software version 3.2.",
                    "title_path": ["Synthetic AX support guide"],
                    "anchor": {"page": 2, "heading_path": ["Servicing"]},
                    "metadata": {
                        "product_model": "AX",
                        "software_version": "3.2",
                        "business_version": "test-v7",
                    },
                    "retrieval_sources": ["keyword"],
                    "context_parts": [],
                    "context_truncated": False,
                    "relations": [],
                }
            ],
        }
        return result

    return app


def voicechat_app():
    app = control_app()
    app.state.scenario = {"name": "answer", "questions": [QUESTION]}

    @app.post("/__test__/scenario")
    async def configure(request: Request):
        body = await request.json()
        app.state.scenario = {
            "name": body.get("name", "answer"),
            "questions": body.get("questions", [QUESTION]),
        }
        return {"simulation": True}

    @app.websocket("/v1/realtime")
    async def realtime(ws: WebSocket):
        if ws.headers.get("authorization") != "Bearer " + KEY:
            await ws.close(code=1008)
            return
        await ws.accept()
        scenario = dict(app.state.scenario)
        session_id = str(uuid4())
        app.state.requests.append({"type": "connected", "session_id": session_id})
        index, waiting = 0, False
        try:
            await ws.send_json({"type": "session.created", "session": {"id": session_id}})
            update = await ws.receive_json()
            assert update["type"] == "session.update"
            assert update["session"]["tools"][0]["name"] == "consult_service_agent"
            app.state.requests.append({"session_id": session_id, **update})
            configured = json.loads(json.dumps(update["session"]))
            if scenario["name"] == "bad_format":
                configured["audio"]["output"]["format"]["rate"] = 16000
            await ws.send_json({"type": "session.updated", "session": configured})
            direct = "query" in update["session"]["tools"][0]["parameters"]["properties"]
            while True:
                event = await ws.receive_json()
                kind = event["type"]
                if kind == "input_audio_buffer.append":
                    audio = base64.b64decode(event["audio"], validate=True)
                    assert len(audio) == 3840
                    app.state.requests.append({"type": kind, "session_id": session_id, "bytes": len(audio)})
                    if waiting or index >= len(scenario["questions"]):
                        continue
                    waiting = True
                    text = scenario["questions"][index]
                    item_id, rid, call_id = f"input-{index}", f"tool-{index}", f"call-{index}"
                    for event in [
                        {"type": "input_audio_buffer.speech_started", "item_id": item_id},
                        {
                            "type": "conversation.item.input_audio_transcription.delta",
                            "item_id": item_id,
                            "delta": text[:20],
                        },
                        {
                            "type": "conversation.item.input_audio_transcription.completed",
                            "item_id": item_id,
                            "transcript": text,
                        },
                        {"type": "input_audio_buffer.speech_stopped", "item_id": item_id},
                    ]:
                        await ws.send_json(event)
                    if scenario["name"] == "unbridged":
                        await ws.send_json(
                            {
                                "type": "response.output_audio_transcript.done",
                                "response_id": rid,
                                "transcript": SPOKEN,
                            }
                        )
                        continue
                    args = {"user_request": "Rewritten model text that must not replace ASR"}
                    if direct:
                        args.update(
                            query=text,
                            product_model="AX" if " AX " in text else None,
                            software_version="3.2" if "3.2" in text else None,
                        )
                    call = {
                        "type": "response.function_call_arguments.done",
                        "response_id": rid,
                        "call_id": call_id,
                        "name": "consult_service_agent",
                        "arguments": json.dumps(args),
                    }
                    await ws.send_json(call)
                    if scenario["name"] == "duplicate":
                        await ws.send_json(call)
                    await ws.send_json(
                        {
                            "type": "response.output_audio_transcript.done",
                            "response_id": rid,
                            "transcript": "Please wait while I check the knowledge base.",
                        }
                    )
                    await ws.send_json({"type": "response.output_audio.done", "response_id": rid})
                elif kind == "conversation.item.create":
                    assert event["item"]["type"] == "function_call_output"
                    assert event["item"]["call_id"] == f"call-{index}"
                    output = json.loads(event["item"]["output"])
                    app.state.requests.append(
                        {
                            "type": kind,
                            "session_id": session_id,
                            "output": output,
                            "call_id": event["item"]["call_id"],
                        }
                    )
                    if scenario["name"] == "disconnect":
                        await ws.close()
                        return
                    if scenario["name"] == "voice_error":
                        await ws.send_json(
                            {"type": "error", "error": {"message": "Synthetic provider failure"}}
                        )
                        continue
                    if scenario["name"] == "no_completion":
                        continue
                    if direct:
                        directive = output["directive"]
                        if directive == "answer_from_evidence":
                            assert output["evidence"][0]["source_text"] == SOURCE
                            text = SPOKEN
                        elif directive == "report_insufficient":
                            text = "No matching synthetic evidence is available. Please clarify your request."
                        elif directive == "ask_clarification":
                            text = "Please confirm the model and software version."
                        else:
                            text = "Knowledge search is unavailable. Please try again later."
                    else:
                        text = output["speech_text"]
                    rid = f"answer-{index}"
                    await ws.send_json(
                        {
                            "type": "response.output_audio_transcript.delta",
                            "response_id": rid,
                            "delta": text[:20],
                        }
                    )
                    for _ in range(12):
                        await ws.send_json(
                            {"type": "response.output_audio.delta", "response_id": rid, "delta": PCM}
                        )
                    if scenario["name"] == "delayed_answer":
                        await asyncio.sleep(0.8)
                    await ws.send_json(
                        {
                            "type": "response.output_audio_transcript.done",
                            "response_id": rid,
                            "transcript": text,
                        }
                    )
                    await ws.send_json({"type": "response.output_audio.done", "response_id": rid})
                    await ws.send_json({"type": "response.done", "response_id": rid})
                    index += 1
                    await asyncio.sleep(0.15)
                    waiting = False
                else:
                    raise AssertionError("Unsupported client event: " + kind)
        except WebSocketDisconnect:
            pass
        finally:
            app.state.requests.append({"type": "closed", "session_id": session_id})

    return app


def llm_app():
    app = control_app()

    @app.post("/v1/chat/completions")
    async def completion(request: Request):
        if request.headers.get("authorization") != "Bearer " + KEY:
            return JSONResponse({"error": "Invalid synthetic key"}, status_code=401)
        body = await request.json()
        app.state.requests.append(body)
        tool_message = next((m for m in reversed(body["messages"]) if m["role"] == "tool"), None)
        if tool_message is None:
            assert body["tool_choice"] == "required"
            text = next(m["content"] for m in reversed(body["messages"]) if m["role"] == "user")
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "search-call",
                        "type": "function",
                        "function": {"name": "search_knowledge", "arguments": json.dumps({"query": text})},
                    }
                ],
            }
            finish = "tool_calls"
        else:
            result = json.loads(tool_message["content"])
            found = bool(result.get("hits"))
            message = {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "status": "answered" if found else "insufficient_evidence",
                        "display_text": SPOKEN + " [C1]"
                        if found
                        else "No matching synthetic evidence is available.",
                        "speech_text": SPOKEN if found else "No matching synthetic evidence is available.",
                        "citation_ids": ["C1"] if found else [],
                    }
                ),
            }
            finish = "stop"
        response = {
            "id": "chatcmpl-synthetic",
            "object": "chat.completion",
            "created": 1790985600,
            "model": "synthetic-network-model",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }
        if body.get("stream"):
            delta = dict(message)
            if delta.get("tool_calls"):
                delta["tool_calls"][0]["index"] = 0
            chunk = {
                **response,
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
            end = {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}
            data = "".join("data: " + json.dumps(x) + "\n\n" for x in [chunk, end]) + "data: [DONE]\n\n"
            return StreamingResponse(iter([data]), media_type="text/event-stream")
        return response

    return app


def portal_app():
    from app.config import Settings
    from app.main import create_app

    external = os.environ["SIMULATION_MODE"] == "external"
    # Explicit settings injection keeps simulators out of normal deployment startup.
    return create_app(
        Settings(
            _env_file=None,
            auth_mode="validation",
            auto_create_schema=False,
            database_url=os.environ["DATABASE_URL"],
            redis_url=None,
            public_origin="http://localhost:5173",
            request_limit_per_minute=1000,
            agent_provider="compatible" if external else "none",
            agent_model="synthetic-network-model" if external else "",
            agent_base_url=os.environ["SIMULATION_LLM_URL"] + "/v1" if external else None,
            openai_api_key=KEY if external else "",
            agent_deadline_ms=2500,
            cuekb_mode="real",
            cuekb_base_url=os.environ["SIMULATION_CUEKB_URL"],
            cuekb_api_key=KEY,
            voice_provider="nvidia",
            voicechat_ws_url=os.environ["SIMULATION_VOICE_URL"].replace("http:", "ws:") + "/v1/realtime",
            voicechat_api_key=KEY,
            knowledge_base_ids=KB,
        )
    )
