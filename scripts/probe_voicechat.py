"""P0: explicitly real VoiceChat protocol probe. Requires authorized audio and configured service.

Does not modify the application's verified capability flag. Tool responses are synthetic and labelled.
No raw input/output audio or private transcripts are written to the report.
"""

import argparse
import asyncio
import base64
import json
import time
import wave
from pathlib import Path

from app.config import Settings
from app.contracts import BridgeArguments, now
from app.voice.provider import BRIDGE_NAME, NvidiaVoiceChatAdapter


async def probe(args):
    settings = Settings()
    report = {
        "tested_at": now().isoformat(),
        "api_version": settings.voicechat_api_version or None,
        "requested_mode": "real",
        "real_service_connected": False,
        "synthetic_tool_result": True,
        "status": "blocked",
        "checks": {},
        "counts": {},
        "audio_output_samples": 0,
        "limitations": [
            "This probe does not establish Chinese acoustic quality, concurrency or spoken fact consistency."
        ],
    }
    if not settings.voicechat_ws_url:
        report["reason"] = "VOICECHAT_WS_URL not configured"
    else:
        provider = NvidiaVoiceChatAdapter(settings)
        try:
            await provider.connect("本次为接口联调，所有固定工具结果均为合成测试数据。")
            report["checks"]["handshake_and_24khz_echo"] = "passed"
            report["real_service_connected"] = True
            chunks = []
            if args.wav:
                with wave.open(args.wav, "rb") as wav:
                    if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 24000):
                        raise ValueError("Input WAV must be 24kHz, mono, PCM16")
                    while data := wav.readframes(1920):
                        chunks.append(data.ljust(3840, b"\x00"))
            chunks.extend([bytes(3840)] * max(1, int(args.seconds / 0.08)))
            lock = asyncio.Lock()
            seen = set()

            async def receive():
                async for event in provider.events():
                    report["counts"][event.kind] = report["counts"].get(event.kind, 0) + 1
                    if event.kind == "audio.delta":
                        report["audio_output_samples"] += (
                            len(base64.b64decode(event.payload["audio"], validate=True)) // 2
                        )
                    if event.kind == "tool":
                        p = event.payload
                        if p["call_id"] in seen:
                            continue
                        if p["name"] != BRIDGE_NAME:
                            raise ValueError("Unexpected tool")
                        BridgeArguments.model_validate_json(p["arguments"])
                        seen.add(p["call_id"])
                        async with lock:
                            await provider.submit_tool_result(
                                p["call_id"],
                                json.dumps(
                                    {
                                        "status": "answered",
                                        "speech_text": "这是合成联调结果：中文姓名张先生、城市杭州、型号 A一零，请勿当作真实业务信息。",
                                        "is_mock": True,
                                    },
                                    ensure_ascii=False,
                                ),
                            )
                        report["checks"]["native_tool_result_sent"] = "passed"

            receiver = asyncio.create_task(receive())
            try:
                deadline = time.monotonic()
                for chunk in chunks:
                    if receiver.done():
                        receiver.result()
                    async with lock:
                        await provider.send_audio(base64.b64encode(chunk).decode())
                    deadline += 0.08
                    await asyncio.sleep(max(0, deadline - time.monotonic()))
                report["status"] = "partial"
                report["checks"]["audio_output_observed"] = (
                    "passed" if report["audio_output_samples"] else "unverified"
                )
                report["checks"]["spoken_result_consistency"] = "unverified: requires human audio review"
                report["checks"]["old_connection_isolation"] = "unverified: run portal interruption scenarios"
            finally:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
        except Exception as exc:
            report["status"] = "failed"
            report["reason"] = type(exc).__name__  # No URL, token, transcript or raw exception payload.
        finally:
            await provider.close()
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(
        Path(args.output).write_text, json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", help="Authorized 24kHz mono PCM16 WAV; input not stored by application")
    parser.add_argument(
        "--seconds", type=float, default=10, help="Silence duration after WAV to sustain output"
    )
    parser.add_argument("--output", default="artifacts/voicechat-probe.json")
    asyncio.run(probe(parser.parse_args()))
