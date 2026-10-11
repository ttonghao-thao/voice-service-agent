"""Real VoiceChat probe. Synthetic results and isolated output are not business acceptance."""

import argparse
import asyncio
import base64
import hashlib
import json
import math
import time
import wave
from pathlib import Path

from app.config import Settings
from app.contracts import BridgeArguments, now
from app.voice.provider import BRIDGE_NAME, NvidiaVoiceChatAdapter, VoiceEvent, VoiceTrace


def read_wav(path, max_seconds=60):
    chunks = []
    digest = hashlib.sha256()
    with wave.open(path, "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 24000):
            raise ValueError("Input WAV must be 24kHz, mono, PCM16")
        if not 0 < wav.getnframes() <= max_seconds * 24000:
            raise ValueError("Input WAV is empty or exceeds the observation budget")
        while data := wav.readframes(1920):
            data = data.ljust(3840, b"\x00")
            chunks.append(data)
            digest.update(data)
    return chunks, digest.hexdigest()


async def observe(provider, args, primary, barge, report):
    """One upstream writer continuously clocks audio and prioritizes ready tool results."""
    trace = VoiceTrace()
    started = time.monotonic()
    controls = asyncio.Queue(maxsize=16)
    workers = []
    seen_calls = set()
    primary_at = None
    result_at = None
    primary_index = barge_index = 0
    output_audio = bytearray()
    output_limit = int(args.total_timeout * 24000 * 2)
    tasks = []

    def record(event):
        trace.append(event, int((time.monotonic() - started) * 1000))

    async def delayed_result(call_id, response_id):
        await asyncio.sleep(args.tool_delay_seconds)
        await controls.put((call_id, response_id))

    async def receive():
        async for event in provider.events():
            record(event)
            report["counts"][event.kind] = report["counts"].get(event.kind, 0) + 1
            if event.kind == "session.ended":
                return
            if event.kind == "audio.delta":
                raw = base64.b64decode(event.payload["audio"], validate=True)
                if len(raw) % 2:
                    raise ValueError("Non-PCM16 output")
                report["audio_evidence"]["output_samples"] += len(raw) // 2
                if not seen_calls:
                    report["audio_evidence"]["pre_tool_output_samples"] += len(raw) // 2
                if args.output_wav:
                    if len(output_audio) + len(raw) > output_limit:
                        raise ValueError("Output audio exceeds bounded capture budget")
                    output_audio.extend(raw)
            if event.kind == "tool":
                payload = event.payload
                if payload["call_id"] in seen_calls:
                    continue
                if len(seen_calls) >= 128 or payload["name"] != BRIDGE_NAME:
                    raise ValueError("Unsupported or excessive tools")
                BridgeArguments.model_validate_json(payload["arguments"])
                seen_calls.add(payload["call_id"])
                report["tool_seen"] = True
                report["checks"]["native_tool_call_observed"] = "passed"
                workers.append(asyncio.create_task(delayed_result(payload["call_id"], payload.get("response_id"))))

    async def send():
        nonlocal primary_at, result_at, primary_index, barge_index
        deadline = started
        while True:
            for worker in workers:
                if worker.done():
                    worker.result()
            elapsed = time.monotonic() - started
            if elapsed >= args.total_timeout:
                return "total_timeout"
            while not controls.empty():
                call_id, response_id = controls.get_nowait()
                await provider.submit_tool_result(call_id, json.dumps({
                    "status": "answered", "is_mock": True,
                    "speech_text": "This synthetic result arrived after a delay. It is not real business information.",
                }, ensure_ascii=True))
                result_at = time.monotonic()
                report["checks"]["native_tool_result_sent"] = "passed"
                record(VoiceEvent("tool.result.submitted", {"call_id": call_id, "response_id": response_id}))
            current = time.monotonic()
            if primary_at is not None:
                if args.diagnostic and current - primary_at >= args.observation_seconds:
                    return "observation_complete"
                if not args.diagnostic:
                    if not seen_calls and current - primary_at >= args.tool_wait_timeout:
                        return "tool_timeout"
                    if result_at is not None and barge_index == len(barge):
                        if current - max(primary_at, result_at) >= args.post_result_seconds:
                            return "observation_complete"
            if primary_index < len(primary):
                chunk, phase = primary[primary_index], "primary"
            elif barge_index < len(barge) and seen_calls:
                chunk, phase = barge[barge_index], "barge_in"
            else:
                chunk, phase = bytes(3840), "silence"
            await provider.send_audio(base64.b64encode(chunk).decode())
            report["audio_evidence"][phase + "_input_samples"] += len(chunk) // 2
            if phase == "primary":
                primary_index += 1
                if primary_index == len(primary):
                    primary_at = time.monotonic()
                    report["input_complete"] = True
                    record(VoiceEvent("probe.input.complete"))
            elif phase == "barge_in":
                barge_index += 1
                report["checks"]["barge_in_audio_sent_while_tool_pending"] = (
                    "passed" if result_at is None else "unverified"
                )
            deadline += 0.08
            deadline = max(deadline, time.monotonic())
            await asyncio.sleep(max(0, deadline - time.monotonic()))

    try:
        receiver, sender = asyncio.create_task(receive()), asyncio.create_task(send())
        tasks = [receiver, sender]
        async with asyncio.timeout(args.total_timeout):
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if receiver in done:
                receiver.result()
                report["exit_reason"] = "peer_closed"
            else:
                report["exit_reason"] = sender.result()
            for task in done:
                task.result()
    except TimeoutError:
        report["exit_reason"] = "total_timeout"
    except Exception as exc:
        report["exit_reason"] = "error"
        report["reason"] = type(exc).__name__
        report["status"] = "failed"
    finally:
        for task in tasks + workers:
            task.cancel()
        await asyncio.gather(*tasks, *workers, return_exceptions=True)
        snapshot = trace.snapshot()
        report["event_timeline"] = snapshot.pop("events")
        report["trace_limits"] = snapshot
        report["unsent_input_samples"] = {
            "primary": (len(primary) - primary_index) * 1920,
            "barge_in": (len(barge) - barge_index) * 1920,
        }
        report["checks"]["input_fully_sent"] = "passed" if report["input_complete"] else "unverified"
        report["checks"]["audio_output_observed"] = (
            "passed" if report["audio_evidence"]["output_samples"] else "unverified"
        )
        report["checks"]["spoken_result_consistency"] = "manual_review_required"
        report["checks"]["enhanced_mode_candidate"] = (
            "manual_review_required" if barge and barge_index == len(barge) else "unverified"
        )
        report["checks"]["stale_call_settlement"] = "verify_through_portal_interrupt_scenario"
        report["provider"] = provider.diagnostics() if hasattr(provider, "diagnostics") else {}
        if args.output_wav and output_audio:
            output = Path(args.output_wav)
            output.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(output), "wb") as wav:
                wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                wav.writeframes(output_audio)
            report["audio_evidence"]["output_sha256"] = hashlib.sha256(output_audio).hexdigest()
            report["audio_evidence"]["output_wav"] = str(output)


async def probe(args):
    report = {
        "tested_at": now().isoformat(), "api_version": args.api_version, "image_digest": args.image_digest,
        "requested_mode": "real", "diagnostic": args.diagnostic, "real_service_connected": False,
        "synthetic_tool_result": True, "status": "blocked", "input_complete": False, "tool_seen": False,
        "checks": {}, "counts": {}, "event_timeline": [],
        "audio_evidence": {"primary_input_samples": 0, "barge_in_input_samples": 0,
                           "silence_input_samples": 0, "output_samples": 0, "pre_tool_output_samples": 0,
                           "input_rate": 24000, "output_rate": 24000, "samples_per_frame": 1920,
                           "input_counts_include_final_frame_padding": True},
        "limitations": [
            "No capability flag is changed automatically. Synthetic results are not CueKB acceptance.",
            "Output can contain unbridged speech: isolated diagnostic capture, never business-authorized playback.",
            "Input completion proves sending, not inference consumption or complete ASR.",
            "English acoustic quality, full transcription, barge-in and spoken consistency require human review.",
        ],
    }
    provider = None
    try:
        durations = (args.tool_delay_seconds, args.tool_wait_timeout, args.post_result_seconds,
                     args.observation_seconds, args.total_timeout)
        if not all(math.isfinite(value) and value >= 0 for value in durations) or not 0 < args.total_timeout <= 60:
            raise ValueError("Invalid probe observation budget")
        primary, primary_hash = read_wav(args.wav, args.total_timeout)
        barge, barge_hash = read_wav(args.barge_in_wav, args.total_timeout) if args.barge_in_wav else ([], None)
        tail = args.observation_seconds if args.diagnostic else args.post_result_seconds
        if (len(primary) + len(barge)) * 0.08 + tail > args.total_timeout:
            raise ValueError("Complete input and observation exceed total budget")
        report["audio_evidence"]["primary_sha256"] = primary_hash
        report["audio_evidence"]["barge_in_sha256"] = barge_hash
        report["unsent_input_samples"] = {"primary": len(primary) * 1920, "barge_in": len(barge) * 1920}
        settings = Settings()
        if not settings.voicechat_ws_url:
            report["reason"] = "VOICECHAT_WS_URL not configured"
        else:
            provider = NvidiaVoiceChatAdapter(settings)
            async with asyncio.timeout(args.total_timeout):
                await provider.connect("This is an interface probe. Tool results are synthetic test data, not business facts.")
                report["real_service_connected"] = True
                report["status"] = "partial"
                report["checks"]["handshake_and_24khz_format"] = "passed"
                await observe(provider, args, primary, barge, report)
    except TimeoutError:
        report["exit_reason"] = "total_timeout"
        report["reason"] = "Connection observation budget exhausted"
    except Exception as exc:
        report["status"], report["exit_reason"], report["reason"] = "failed", "error", type(exc).__name__
    finally:
        if provider:
            try:
                async with asyncio.timeout(2):
                    await provider.close()
            except Exception as exc:
                report["status"], report["close_error"] = "failed", type(exc).__name__
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(output.write_text, json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", required=True, help="Authorized 24kHz mono PCM16 WAV")
    parser.add_argument("--api-version", required=True, help="VoiceChat service revision under test")
    parser.add_argument("--image-digest", required=True, help="VoiceChat image digest under test")
    parser.add_argument("--barge-in-wav", help="Authorized distinct second-question WAV")
    parser.add_argument("--diagnostic", action="store_true", help="Observe entire input even without native tools")
    parser.add_argument("--observation-seconds", type=float, default=10.0)
    parser.add_argument("--total-timeout", type=float, default=60.0, help="Connect and observe budget, at most 60 seconds")
    parser.add_argument("--tool-delay-seconds", type=float, default=5.0)
    parser.add_argument("--tool-wait-timeout", type=float, default=20.0)
    parser.add_argument("--post-result-seconds", type=float, default=5.0)
    parser.add_argument("--output", default="artifacts/voicechat-probe.json")
    parser.add_argument("--output-wav", help="Optional isolated output capture for human review")
    asyncio.run(probe(parser.parse_args()))
