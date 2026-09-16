"""Real VoiceChat contract/capability probe with privacy-preserving evidence.

The probe never changes deployment capability flags. A human must review the
optional output WAV before selecting basic/enhanced mode in configuration.
"""

import argparse
import asyncio
import base64
import hashlib
import json
import time
import wave
from pathlib import Path

from app.config import Settings
from app.contracts import BridgeArguments, now
from app.voice.provider import BRIDGE_NAME, NvidiaVoiceChatAdapter


def read_wav(path):
    chunks = []
    digest = hashlib.sha256()
    with wave.open(path, "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 24000):
            raise ValueError("Input WAV must be 24kHz, mono, PCM16")
        while data := wav.readframes(1920):
            data = data.ljust(3840, b"\x00")
            chunks.append(data)
            digest.update(data)
    return chunks, digest.hexdigest()


async def send_realtime(provider, chunks, phase, report):
    deadline = time.monotonic()
    for chunk in chunks:
        await provider.send_audio(base64.b64encode(chunk).decode())
        report["audio_evidence"][phase + "_input_samples"] += len(chunk) // 2
        deadline += 0.08
        await asyncio.sleep(max(0, deadline - time.monotonic()))


async def probe(args):
    settings = Settings()
    report = {
        "tested_at": now().isoformat(),
        "api_version": settings.voicechat_api_version or None,
        "image_digest": settings.voicechat_image_digest or None,
        "requested_mode": "real",
        "real_service_connected": False,
        "synthetic_tool_result": True,
        "status": "blocked",
        "checks": {},
        "counts": {},
        "event_timeline": [],
        "audio_evidence": {
            "primary_input_samples": 0,
            "barge_in_input_samples": 0,
            "output_samples": 0,
        },
        "limitations": [
            "No capability flag is changed automatically.",
            "Enhanced mode requires a distinct second question and human review of event timing and output audio.",
            "This probe does not establish Chinese acoustic quality, concurrency, cancellation of provider inference, or spoken fact consistency.",
        ],
    }
    if not settings.voicechat_ws_url:
        report["reason"] = "VOICECHAT_WS_URL not configured"
    elif not settings.voicechat_api_version or not settings.voicechat_image_digest:
        report["reason"] = "VOICECHAT_API_VERSION and VOICECHAT_IMAGE_DIGEST must pin the target deployment"
    else:
        provider = NvidiaVoiceChatAdapter(settings)
        result_tasks = set()
        output_audio = bytearray()
        tool_seen = asyncio.Event()
        tool_result_sent_at = None
        started = time.monotonic()
        try:
            await provider.connect("本次为接口联调，固定工具结果是合成测试数据，不是业务事实。")
            report["checks"]["handshake_and_24khz_format"] = "passed"
            report["real_service_connected"] = True
            primary, primary_hash = read_wav(args.wav)
            report["audio_evidence"]["primary_sha256"] = primary_hash
            barge, barge_hash = ([], None)
            if args.barge_in_wav:
                barge, barge_hash = read_wav(args.barge_in_wav)
                report["audio_evidence"]["barge_in_sha256"] = barge_hash
            seen_calls = set()

            async def delayed_result(call_id):
                nonlocal tool_result_sent_at
                await asyncio.sleep(args.tool_delay_seconds)
                await provider.submit_tool_result(
                    call_id,
                    json.dumps(
                        {
                            "status": "answered",
                            "speech_text": "这是延迟五秒后的合成联调结果，请勿当作真实业务信息。",
                            "is_mock": True,
                        },
                        ensure_ascii=False,
                    ),
                )
                tool_result_sent_at = time.monotonic()
                report["checks"]["native_tool_result_sent"] = "passed"

            async def receive():
                async for event in provider.events():
                    elapsed_ms = int((time.monotonic() - started) * 1000)
                    report["counts"][event.kind] = report["counts"].get(event.kind, 0) + 1
                    report["event_timeline"].append(
                        {
                            "at_ms": elapsed_ms,
                            "kind": event.kind,
                            "response_id": event.payload.get("response_id"),
                            "item_id": event.payload.get("item_id"),
                        }
                    )
                    if event.kind == "audio.delta":
                        raw = base64.b64decode(event.payload["audio"], validate=True)
                        output_audio.extend(raw)
                        report["audio_evidence"]["output_samples"] += len(raw) // 2
                    if event.kind == "tool":
                        payload = event.payload
                        if payload["call_id"] in seen_calls:
                            continue
                        if payload["name"] != BRIDGE_NAME:
                            raise ValueError("Unexpected tool")
                        BridgeArguments.model_validate_json(payload["arguments"])
                        seen_calls.add(payload["call_id"])
                        report["checks"]["native_tool_call_observed"] = "passed"
                        tool_seen.set()
                        task = asyncio.create_task(delayed_result(payload["call_id"]))
                        result_tasks.add(task)

            receiver = asyncio.create_task(receive())
            try:
                await send_realtime(provider, primary, "primary", report)
                tool_wait = asyncio.create_task(tool_seen.wait())
                done, _ = await asyncio.wait(
                    (tool_wait, receiver),
                    timeout=args.tool_wait_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if receiver in done:
                    tool_wait.cancel()
                    receiver.result()
                if tool_wait not in done:
                    tool_wait.cancel()
                    raise TimeoutError
                if barge:
                    await send_realtime(provider, barge, "barge_in", report)
                    if receiver.done():
                        receiver.result()
                    report["checks"]["barge_in_audio_sent_while_tool_pending"] = "passed"
                if result_tasks:
                    await asyncio.gather(*list(result_tasks))
                if receiver.done():
                    receiver.result()
                await send_realtime(
                    provider,
                    [bytes(3840)] * max(1, int(args.post_result_seconds / 0.08)),
                    "primary",
                    report,
                )
                await asyncio.sleep(1)
                report["status"] = "partial"
                report["checks"]["audio_output_observed"] = (
                    "passed" if output_audio else "unverified"
                )
                result_ms = (
                    int((tool_result_sent_at - started) * 1000)
                    if tool_result_sent_at is not None
                    else -1
                )
                pre_result_responses = {
                    item["response_id"]
                    for item in report["event_timeline"]
                    if item["response_id"] and 0 <= item["at_ms"] < result_ms
                }
                report["checks"]["enhanced_mode_candidate"] = (
                    "manual_review_required" if barge and pre_result_responses else "unverified"
                )
                report["checks"]["spoken_result_consistency"] = "manual_review_required"
                report["checks"]["stale_call_settlement"] = "verify_through_portal_interrupt_scenario"
                if args.output_wav and output_audio:
                    output_path = Path(args.output_wav)
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    with wave.open(str(output_path), "wb") as wav:
                        wav.setnchannels(1)
                        wav.setsampwidth(2)
                        wav.setframerate(24000)
                        wav.writeframes(output_audio)
                    report["audio_evidence"]["output_wav"] = str(output_path)
                    report["audio_evidence"]["output_sha256"] = hashlib.sha256(output_audio).hexdigest()
            finally:
                receiver.cancel()
                for task in result_tasks:
                    task.cancel()
                await asyncio.gather(receiver, *result_tasks, return_exceptions=True)
        except TimeoutError:
            report["status"] = "partial" if report["real_service_connected"] else "failed"
            report["reason"] = "No native tool call observed before timeout"
        except Exception as exc:
            report["status"] = "failed"
            report["reason"] = type(exc).__name__
        finally:
            await provider.close()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(output.write_text, json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", required=True, help="Authorized first-question 24kHz mono PCM16 WAV")
    parser.add_argument(
        "--barge-in-wav",
        help="Authorized distinct second-question WAV sent while the tool result is delayed",
    )
    parser.add_argument("--tool-delay-seconds", type=float, default=5.0)
    parser.add_argument("--tool-wait-timeout", type=float, default=20.0)
    parser.add_argument("--post-result-seconds", type=float, default=5.0)
    parser.add_argument("--output", default="artifacts/voicechat-probe.json")
    parser.add_argument("--output-wav", help="Optional authorized output audio for human review")
    asyncio.run(probe(parser.parse_args()))
