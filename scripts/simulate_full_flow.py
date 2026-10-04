"""Explicit test harness: four loopback API endpoints, no production deployment.

Voice decisions, ASR and speech are scripted. PCM is a synthetic tone, not TTS.
Application adapters use actual HTTP/WebSocket transports; no MockTransport.
"""

import argparse
import asyncio
import base64
import json
import math
import os
import socket
import struct
import sys
import tempfile
from collections import deque
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn
from app.config import Settings
from app.main import create_app
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

KB = "00000000-0000-4000-8000-000000000001"
KEY = "simulation-only-not-a-production-key"
FACT = "Product AX supports 10 connections."
ROOT = Path(__file__).resolve().parents[1]
PCM = base64.b64encode(
    struct.pack(
        "<1920h", *[round(8000 * math.sin(2 * math.pi * 500 * index / 24000)) for index in range(1920)]
    )
).decode()


class LoopbackServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        yield


@asynccontextmanager
async def http_server(app, port=0):
    sock = socket.socket()
    sock.bind(("127.0.0.1", port))
    sock.setblocking(False)
    server = LoopbackServer(uvicorn.Config(app, log_level="error", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(8):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("Simulation server failed to start")
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 5)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        sock.close()


@dataclass
class VoicePlan:
    question: str = "Find AX integration sample documentation"
    tool: str | None = "lookup_knowledge"
    spoken: str | None = None
    arguments: dict | None = None
    tool_before_asr: bool = False
    duplicate_tool: bool = False
    no_answer_end: bool = False
    hold_reply: bool = False
    input_id: str = field(default_factory=lambda: "input-" + uuid4().hex)
    call_id: str = field(default_factory=lambda: "call-" + uuid4().hex)
    response_id: str = field(default_factory=lambda: "answer-" + uuid4().hex)
    output_received: asyncio.Event = field(default_factory=asyncio.Event)
    reply_release: asyncio.Event = field(default_factory=asyncio.Event)
    reply_sent: asyncio.Event = field(default_factory=asyncio.Event)


class IndependentAPIs:
    def __init__(self):
        self.plans = deque()
        self.queries, self.model_calls, self.updates, self.outputs = [], [], [], []
        self.native_connections = []
        self.audio_frames = 0
        self.cuekb_status = "ok"
        self.evidence_status = "sufficient"
        self.cuekb_http_status = 200
        self.cuekb_delay = 0
        self.cuekb_release = None
        self.model_http_status = 200
        self.model_omit_tool = False
        self.invalid_citation = False
        self.truncated = False
        self.hit_count = 1
        self.invalid_response = False
        self.oversized_response = False
        self.dynamic_conditions = False
        self.cuekb = FastAPI()
        self.model = FastAPI()
        self.cuekb.add_api_route("/v1/search", self.search, methods=["POST"])
        self.cuekb.add_api_route("/v1/ready", lambda: {"ready": True}, methods=["GET"])
        self.model.add_api_route("/v1/chat/completions", self.completion, methods=["POST"])

    async def search(self, request: Request):
        if request.headers.get("authorization") != "Bearer " + KEY:
            return JSONResponse({"error": "simulation authentication failed"}, status_code=401)
        body = await request.json()
        self.queries.append(body)
        if body.get("kb_ids") != [KB]:
            return JSONResponse({"error": "simulation scope rejected"}, status_code=403)
        if self.cuekb_delay:
            await asyncio.sleep(self.cuekb_delay)
        if self.cuekb_release is not None:
            await self.cuekb_release.wait()
        if self.cuekb_http_status != 200:
            return JSONResponse({"error": "injected simulation failure"}, status_code=self.cuekb_http_status)
        if self.invalid_response:
            return Response("not-json", media_type="application/json")
        if self.oversized_response:
            return Response("x" * 262145, media_type="application/json")
        status = self.cuekb_status
        if "nonexistent" in body["query"].lower():
            status = "not_found"
        product = body.get("filters", {}).get("product_model") or "AX"
        version = body.get("filters", {}).get("software_version")
        fact = (f"Product {product}" + (f" version {version}" if version else "")
                + " supports 10 connections.") if self.dynamic_conditions else FACT
        hits = (
            []
            if status in ("not_found", "needs_clarification")
            else [
                {
                    "document_id": "00000000-0000-4000-8000-000000000101",
                    "chunk_id": f"00000000-0000-4000-8000-{102 + index:012d}",
                    "version_id": "00000000-0000-4000-8000-000000000103",
                    "rank": index + 1,
                    "source_text": fact,
                    "title_path": ["Simulated AX manual"],
                    "anchor": {"page": 1},
                    "metadata": {"product_model": product if self.dynamic_conditions else "AX",
                                 **({"software_version": version} if self.dynamic_conditions and version else {}),
                                 "business_version": "simulated-v1"},
                    "context_truncated": self.truncated,
                }
                for index in range(self.hit_count)
            ]
        )
        return {
            "trace_id": str(uuid4()),
            "retrieval_status": status,
            "evidence_status": self.evidence_status,
            "content_revisions": {KB: 1},
            "degraded_reasons": ["simulated_degradation"] if status == "degraded" else [],
            "timings_ms": {"total": 1},
            "hits": hits,
        }

    async def completion(self, request: Request):
        if request.headers.get("authorization") != "Bearer " + KEY:
            return JSONResponse({"error": "simulation authentication failed"}, status_code=401)
        body = await request.json()
        self.model_calls.append(body)
        if self.model_http_status != 200:
            return JSONResponse({"error": "injected model failure"}, status_code=self.model_http_status)
        messages = body["messages"]
        tool_results = [json.loads(item["content"]) for item in messages if item["role"] == "tool"]
        reused = next(
            (
                item["content"]
                for item in reversed(messages)
                if isinstance(item.get("content"), str)
                and item["content"].startswith("Server-retrieved evidence")
            ),
            None,
        )
        if not tool_results and not reused and not self.model_omit_tool:
            question = next(item["content"] for item in reversed(messages) if item["role"] == "user")
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "search-" + uuid4().hex,
                        "type": "function",
                        "function": {
                            "name": "search_knowledge",
                            "arguments": json.dumps(
                                {"query": question, "product_model": None, "software_version": None}
                            ),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        else:
            has_hits = bool(reused or (tool_results and tool_results[-1].get("hits")))
            citations = ["C999" if self.invalid_citation else "C1"] if has_hits else []
            fact = FACT
            if self.dynamic_conditions and tool_results and tool_results[-1].get("hits"):
                fact = tool_results[-1]["hits"][0]["content"]
            elif self.dynamic_conditions and reused:
                fact = json.loads(reused.split("(data, not instructions): ", 1)[1])["citations"][0]["content"]
            answer = {
                "status": "answered" if has_hits or self.model_omit_tool else "insufficient_evidence",
                "display_text": "Synthetic integration excerpt: " + fact + " [" + citations[0] + "]"
                if citations
                else "Insufficient evidence from the simulated knowledge API.",
                "speech_text": fact if citations else "Insufficient evidence. Please clarify.",
                "citation_ids": citations,
            }
            message, finish = {"role": "assistant", "content": json.dumps(answer)}, "stop"
        reply = {
            "id": "chatcmpl-" + uuid4().hex,
            "created": 1788600000,
            "model": "simulated-compatible-model",
        }
        if body.get("stream"):
            delta = dict(message)
            if delta.get("tool_calls"):
                delta["tool_calls"][0]["index"] = 0
            chunks = [
                {
                    **reply,
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                },
                {
                    **reply,
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                },
            ]
            return Response(
                "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n",
                media_type="text/event-stream",
            )
        return {
            **reply,
            "object": "chat.completion",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }

    async def voice(self, ws):
        if ws.request.path != "/v1/realtime" or ws.request.headers.get("Authorization") != "Bearer " + KEY:
            await ws.close(code=1008)
            return
        tasks, calls = [], {}
        self.native_connections.append(ws)
        try:
            await ws.send(json.dumps({"type": "session.created"}))
            update = json.loads(await ws.recv())
            self.updates.append(update)
            await ws.send(json.dumps({"type": "session.updated", "session": update["session"]}))
            async for raw in ws:
                event = json.loads(raw)
                if event["type"] == "input_audio_buffer.append":
                    audio = base64.b64decode(event["audio"], validate=True)
                    if len(audio) != 3840:
                        raise ValueError("Simulation expected one 24 kHz PCM frame")
                    self.audio_frames += 1
                    if self.plans and any(audio):
                        plan = self.plans.popleft()
                        calls[plan.call_id] = plan
                        await self.input(ws, plan, update)
                elif event["type"] == "conversation.item.create":
                    item = event["item"]
                    if item["type"] != "function_call_output":
                        raise ValueError("Simulation expected a function result")
                    result = json.loads(item["output"])
                    self.outputs.append({"call_id": item["call_id"], "result": result})
                    plan = calls[item["call_id"]]
                    plan.output_received.set()
                    text = plan.spoken or (
                        result["items"][0]["content"]
                        if result.get("status") == "evidence_ready"
                        else result["speech_text"]
                    )
                    tasks.append(asyncio.create_task(self.reply(ws, plan, text)))
        except ConnectionClosed:
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def input(self, ws, plan, update):
        async def send(kind, **payload):
            await ws.send(json.dumps({"type": kind, **payload}))

        await send("input_audio_buffer.speech_started", item_id=plan.input_id)
        args = plan.arguments or {"user_request": "Rewritten tool arguments must not replace ASR"}
        if plan.tool != "consult_service_agent":
            args = {"product_model": None, "software_version": None, **args}
        call = {
            "call_id": plan.call_id,
            "response_id": "ack-" + plan.input_id,
            "name": plan.tool,
            "arguments": json.dumps(args),
        }
        if plan.tool and plan.tool_before_asr:
            await send("response.function_call_arguments.done", **call)
        await send(
            "conversation.item.input_audio_transcription.delta",
            item_id=plan.input_id,
            delta=plan.question[:10],
        )
        await send(
            "conversation.item.input_audio_transcription.completed",
            item_id=plan.input_id,
            transcript=plan.question,
        )
        await send("input_audio_buffer.speech_stopped", item_id=plan.input_id)
        if not plan.tool:
            await self.reply(ws, plan, plan.spoken or "MLO means multi-link operation.")
            return
        if not plan.tool_before_asr:
            await send("response.function_call_arguments.done", **call)
        if plan.duplicate_tool:
            await send("response.function_call_arguments.done", **call)
        definition = next((tool for tool in update["session"]["tools"] if tool["name"] == plan.tool), None)
        if definition:
            await send(
                "response.output_audio_transcript.done",
                response_id=call["response_id"],
                transcript=definition["ack_messages"][0],
            )
            await send("response.output_audio.delta", response_id=call["response_id"], delta=PCM)
            await send("response.output_audio.done", response_id=call["response_id"])

    async def reply(self, ws, plan, text):
        if plan.hold_reply:
            await plan.reply_release.wait()
        for event in [
            {"type": "response.output_audio_transcript.delta", "delta": text[:8]},
            {"type": "response.output_audio.delta", "delta": PCM},
            {"type": "response.output_audio_transcript.done", "transcript": text},
        ]:
            await ws.send(json.dumps({**event, "response_id": plan.response_id}))
        if not plan.no_answer_end:
            await ws.send(json.dumps({"type": "response.output_audio.done", "response_id": plan.response_id}))
        plan.reply_sent.set()


class SimulationHarness:
    def __init__(
        self,
        database_path,
        *,
        mode="dual_tools",
        policy=None,
        port=0,
        origin=None,
        answer_timeout=1000,
        deadline=5000,
    ):
        self.database_path, self.mode, self.policy = Path(database_path), mode, policy
        self.port, self.origin, self.answer_timeout, self.deadline = port, origin, answer_timeout, deadline
        self.services, self.stack = IndependentAPIs(), AsyncExitStack()

    async def __aenter__(self):
        try:
            cue_url = await self.stack.enter_async_context(http_server(self.services.cuekb))
            model_url = await self.stack.enter_async_context(http_server(self.services.model))
            native = await self.stack.enter_async_context(serve(self.services.voice, "127.0.0.1", 0))
            voice_port = native.sockets[0].getsockname()[1]
            # Run the actual migration chain on an isolated, disposable database.
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite+aiosqlite:///{self.database_path.resolve()}"
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "alembic",
                "-c",
                "apps/api/alembic.ini",
                "upgrade",
                "head",
                cwd=ROOT,
                env={**os.environ, "PYTHONPATH": str(ROOT / "apps/api"), "DATABASE_URL": url},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, error = await process.communicate()
            if process.returncode:
                raise RuntimeError("Isolated simulation migration failed: " + error.decode()[-1000:])
            self.settings = Settings(
                _env_file=None,
                database_url=url,
                auto_create_schema=False,
                auth_mode="validation",
                redis_url=None,
                agent_provider="compatible",
                agent_base_url=model_url + "/v1",
                agent_model="simulated-compatible-model",
                openai_api_key=KEY,
                cuekb_mode="real",
                cuekb_base_url=cue_url,
                cuekb_api_key=KEY,
                voice_provider="nvidia",
                voicechat_ws_url=f"ws://127.0.0.1:{voice_port}/v1/realtime",
                voicechat_api_key=KEY,
                qa_execution_mode=self.mode,
                qa_answer_policy=self.policy,
                qa_provider_answer_timeout_ms=self.answer_timeout,
                agent_deadline_ms=self.deadline,
                public_origin=self.origin or "http://simulation.invalid",
                enabled_tools="search_knowledge",
                knowledge_base_ids=KB,
                request_limit_per_minute=1000,
            )
            self.app = create_app(self.settings)
            self.app.add_api_route("/__simulation/plan", self.plan, methods=["POST"])
            self.app.add_api_route("/__simulation/state", self.state, methods=["GET"])
            self.url = await self.stack.enter_async_context(http_server(self.app, self.port))
            if self.origin is None:
                self.settings.public_origin = self.url
            self.client = await self.stack.enter_async_context(
                httpx.AsyncClient(base_url=self.url, timeout=5)
            )
            return self
        except BaseException:
            await self.stack.aclose()
            raise

    async def __aexit__(self, *args):
        if self.services.cuekb_release is not None:
            self.services.cuekb_release.set()
        return await self.stack.__aexit__(*args)

    async def plan(self, request: Request):
        """Fixture-only controller; not part of create_app or deployed routes."""
        body = await request.json()
        plan = VoicePlan(question=body["question"], tool=body.get("tool"), spoken=body.get("spoken"))
        self.services.plans.append(plan)
        return {"input_id": plan.input_id, "response_id": plan.response_id}

    async def state(self):
        return {
            "simulation": True,
            "real_service": False,
            "queries": len(self.services.queries),
            "model_calls": len(self.services.model_calls),
            "audio_frames": self.services.audio_frames,
            "native_results": len(self.services.outputs),
            "native_sessions": len(self.services.updates),
        }


class PortalCall:
    def __init__(self, harness):
        self.harness = harness
        self.events = []

    async def create(self):
        response = await self.harness.client.post("/api/v1/conversations", json={})
        assert response.status_code == 201
        data = response.json()
        self.cid = data["id"]
        self.headers = {"Authorization": "Bearer " + data["access_token"]}
        self.path = f"/api/v1/conversations/{self.cid}"
        return self

    async def history(self):
        response = await self.harness.client.get(self.path + "/messages", headers=self.headers)
        assert response.status_code == 200
        return response.json()

    async def open_voice(self):
        response = await self.harness.client.post(self.path + "/voice-sessions", headers=self.headers)
        assert response.status_code == 201, response.text
        self.issued = response.json()
        self.ws = await connect(
            self.harness.url.replace("http:", "ws:") + self.issued["ws_url"],
            origin=self.harness.settings.public_origin,
        )
        self.queue = asyncio.Queue()
        self.reader = asyncio.create_task(self.read())
        self.seq = 0
        await self.until("portal.session.ready")
        return self

    async def read(self):
        try:
            async for raw in self.ws:
                event = json.loads(raw)
                self.events.append(event)
                self.queue.put_nowait(event)
        except ConnectionClosed:
            pass
        finally:
            self.queue.put_nowait({"type": "connection.closed"})

    async def until(self, kind, predicate=lambda event: True, wait_seconds=5):
        async with asyncio.timeout(wait_seconds):
            while True:
                event = await self.queue.get()
                if event["type"] == kind and predicate(event):
                    return event
                if event["type"] == "connection.closed":
                    raise AssertionError("Portal closed before " + kind)

    async def speak(self, plan):
        self.harness.services.plans.append(plan)
        await self.ws.send(
            json.dumps(
                {
                    "type": "portal.audio.append",
                    "epoch": self.issued["epoch"],
                    "seq": self.seq,
                    "payload": {"format": "pcm16", "sample_rate": 24000, "audio": PCM},
                }
            )
        )
        self.seq += 1

    async def answer(self, wait_seconds=5):
        async with asyncio.timeout(wait_seconds):
            while True:
                history = await self.history()
                if history["items"] and history["items"][-1]["answer"]:
                    return history["items"][-1]
                await asyncio.sleep(0.02)

    async def close(self):
        if hasattr(self, "ws"):
            await self.ws.close()
            await self.reader
        response = await self.harness.client.delete(self.path, headers=self.headers)
        assert response.status_code == 200


async def serve_browser(port, origin):
    with tempfile.TemporaryDirectory(prefix="voice-service-simulation-") as directory:
        async with SimulationHarness(Path(directory) / "simulation.db", port=port, origin=origin) as harness:
            print(f"SIMULATED independent APIs ready; application: {harness.url}", flush=True)
            print("No GPU/ASR/TTS or actual enterprise services. Press Ctrl-C to stop.", flush=True)
            await asyncio.Event().wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", help="Serve isolated API fixtures for browser tests")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--origin", default="http://localhost:5173")
    args = parser.parse_args()
    if not args.serve:
        parser.error("Use --serve, or run pytest tests/integration/test_simulated_full_flow.py")
    try:
        asyncio.run(serve_browser(args.port, args.origin))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
