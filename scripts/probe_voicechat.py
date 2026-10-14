"""Bounded real VoiceChat probe; synthetic tool results are not business evidence.

Keep input flowing while observing output: VoiceChat advances on audio frames,
including silence. A session.update snapshot is required for dual_tools mode.
"""

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
from app.contracts import now
from app.voice.provider import NvidiaVoiceChatAdapter, ascii_payload, session_update
from jsonschema import Draft202012Validator

FRAME_SECONDS = 0.08
FRAME_BYTES = 3840
MAX_INPUT_SECONDS = 120
MAX_EVENTS = 10000
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_CALLS = 32


class ProbeFailure(Exception):
    """A content-free diagnostic reason suitable for the report."""


def read_wav(path):
    chunks = []
    digest = hashlib.sha256()
    with wave.open(path, "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 24000):
            raise ProbeFailure("INPUT_FORMAT_REQUIRES_24KHZ_MONO_PCM16")
        if not 0 < wav.getnframes() <= MAX_INPUT_SECONDS * 24000:
            raise ProbeFailure("INPUT_DURATION_OUT_OF_BOUNDS")
        while data := wav.readframes(1920):
            data = data.ljust(FRAME_BYTES, b"\x00")
            chunks.append(data)
            digest.update(data)
    return chunks, digest.hexdigest()


def load_configuration(args, settings):
    if args.session_update:
        path = Path(args.session_update)
        if path.stat().st_size > 96 * 1024:
            raise ProbeFailure("SESSION_SNAPSHOT_TOO_LARGE")
        configuration = json.loads(path.read_text())
    else:
        if settings.qa_execution_mode != "legacy":
            raise ProbeFailure("DUAL_TOOLS_REQUIRES_SESSION_SNAPSHOT")
        configuration = session_update()
    if not isinstance(configuration, dict) or configuration.get("type") != "session.update":
        raise ProbeFailure("INVALID_SESSION_SNAPSHOT")
    session = configuration.get("session")
    if not isinstance(session, dict) or not ascii_payload(configuration):
        raise ProbeFailure("INVALID_SESSION_SNAPSHOT")
    if not isinstance(session.get("instructions"), str) or not session["instructions"]:
        raise ProbeFailure("INVALID_SESSION_INSTRUCTIONS")
    if "tool_choice" in session or "tool_choice" in configuration:
        raise ProbeFailure("UNSUPPORTED_TOOL_CHOICE")
    for direction in ("input", "output"):
        if session.get("audio", {}).get(direction, {}).get("format") != {"type": "audio/pcm", "rate": 24000}:
            raise ProbeFailure("INVALID_SESSION_AUDIO_FORMAT")
    tools = session.get("tools")
    if not isinstance(tools, list) or not 0 < len(tools) <= 64:
        raise ProbeFailure("INVALID_SESSION_TOOLS")
    validators = {}
    for tool in tools:
        name = tool.get("name") if isinstance(tool, dict) else None
        if not isinstance(name, str) or not name or name in validators:
            raise ProbeFailure("INVALID_SESSION_TOOL_NAME")
        schema = tool.get("parameters")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ProbeFailure("INVALID_SESSION_TOOL_SCHEMA")
        Draft202012Validator.check_schema(schema)
        validators[name] = Draft202012Validator(schema)
    return configuration, validators


def validate_durations(args):
    for key, minimum, maximum in (
        ("tool_delay_seconds", 0, 60), ("tool_wait_timeout", 0.01, 120), ("post_result_seconds", 0.01, 60),
    ):
        value = getattr(args, key)
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ProbeFailure("INVALID_PROBE_DURATION")


def leaf_exception(exc):
    while isinstance(exc, BaseExceptionGroup):
        exc = exc.exceptions[0]
    return exc


async def run_stream(provider, primary, barge, validators, args, report):
    """One audio writer and concurrent receive/result tasks; cancel all on failure."""
    primary_done, barge_requested, barge_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    tool_seen, first_result = asyncio.Event(), asyncio.Event()
    output_audio = bytearray()
    output_digest = hashlib.sha256()
    calls = {}
    delivered_calls = set()
    tasks = []
    started = time.monotonic()
    first_result_at = None
    previous_send = None

    def record(kind, payload=None):
        payload = payload or {}
        item = {"at_ms": int((time.monotonic() - started) * 1000), "kind": kind}
        for key in ("response_id", "item_id", "call_id"):
            if key in payload:
                item[key] = payload[key]
        # Metadata only: no transcript, prompt, arguments or answer in the report.
        if "text" in payload:
            item["text_length"] = len(payload["text"])
            item["text_sha256"] = hashlib.sha256(payload["text"].encode()).hexdigest()
        if len(report["event_timeline"]) < MAX_EVENTS:
            report["event_timeline"].append(item)
        else:
            report["timeline_dropped"] += 1

    async def send_chunks(chunks, phase):
        nonlocal previous_send
        for chunk in chunks:
            # Do not burst to catch up after backpressure or an inference stall.
            if previous_send is not None:
                await asyncio.sleep(max(0, previous_send + FRAME_SECONDS - time.monotonic()))
            await provider.send_audio(base64.b64encode(chunk).decode())
            sent_at = time.monotonic()
            if previous_send is not None:
                report["audio_evidence"]["max_send_gap_ms"] = max(
                    report["audio_evidence"]["max_send_gap_ms"], round((sent_at - previous_send) * 1000, 3))
            previous_send = sent_at
            report["audio_evidence"][phase + "_input_samples"] += len(chunk) // 2
            if phase == "barge_in" and not first_result.is_set():
                report["audio_evidence"]["barge_in_samples_before_first_result"] += len(chunk) // 2

    async def send():
        await send_chunks(primary, "primary")
        record("probe.primary_sent")
        primary_done.set()
        while True:
            if barge_requested.is_set() and not barge_done.is_set():
                record("probe.barge_in_started")
                await send_chunks(barge, "barge_in")
                barge_done.set()
                record("probe.barge_in_sent")
            else:
                await send_chunks((bytes(FRAME_BYTES),), "silence")

    async def delayed_result(call_id):
        nonlocal first_result_at
        await asyncio.sleep(args.tool_delay_seconds)
        await provider.submit_tool_result(call_id, json.dumps({
            "status": "answered",
            "speech_text": "This synthetic result arrived after a delay. It is not real business information.",
            "is_mock": True,
        }, ensure_ascii=True))
        delivered_calls.add(call_id)
        record("probe.tool_result_sent", {"call_id": call_id})
        if first_result_at is None:
            first_result_at = time.monotonic()
        first_result.set()
        report["checks"]["native_tool_result_sent"] = "passed"

    async def receive(group):
        async for event in provider.events():
            report["counts"][event.kind] = report["counts"].get(event.kind, 0) + 1
            record(event.kind, event.payload)
            if event.kind == "session.ended":
                raise ProbeFailure("PROVIDER_SESSION_ENDED")
            if event.kind == "audio.delta":
                raw = base64.b64decode(event.payload["audio"], validate=True)
                output_digest.update(raw)
                report["audio_evidence"]["output_samples"] += len(raw) // 2
                if args.output_wav:
                    remaining = MAX_OUTPUT_BYTES - len(output_audio)
                    output_audio.extend(raw[:remaining])
                    if len(raw) > remaining:
                        report["audio_evidence"]["output_wav_truncated"] = True
            if event.kind == "tool":
                payload = event.payload
                name, call_id = payload["name"], payload["call_id"]
                if not isinstance(name, str) or name not in validators:
                    raise ProbeFailure("UNREGISTERED_NATIVE_TOOL")
                arguments = json.loads(payload["arguments"])
                validators[name].validate(arguments)
                fingerprint = hashlib.sha256(json.dumps([name, arguments], sort_keys=True).encode()).hexdigest()
                if call_id in calls:
                    if calls[call_id] != fingerprint:
                        raise ProbeFailure("CONFLICTING_CALL_ID")
                    continue
                if len(calls) >= MAX_CALLS:
                    raise ProbeFailure("TOOL_CALL_BUDGET_EXCEEDED")
                calls[call_id] = fingerprint
                report["checks"]["native_tool_call_observed"] = "passed"
                tool_seen.set()
                tasks.append(group.create_task(delayed_result(call_id)))
        raise ProbeFailure("PROVIDER_EVENT_STREAM_ENDED")

    try:
        async with asyncio.TaskGroup() as group:
            tasks.extend((group.create_task(send()), group.create_task(receive(group))))
            try:
                report["phase"] = "primary_audio"
                async with asyncio.timeout(len(primary) * FRAME_SECONDS + 15):
                    await primary_done.wait()
                report["phase"] = "waiting_native_tool"
                async with asyncio.timeout(args.tool_wait_timeout):
                    await tool_seen.wait()
                report["phase"] = "waiting_tool_result"
                if barge:
                    barge_requested.set()
                async with asyncio.timeout(args.tool_delay_seconds + len(barge) * FRAME_SECONDS + 15):
                    if barge:
                        await barge_done.wait()
                    await first_result.wait()
                report["phase"] = "post_result_audio"
                await asyncio.sleep(args.post_result_seconds)
                report["status"] = "partial"
                report["checks"]["audio_output_observed"] = (
                    "passed" if report["audio_evidence"]["output_samples"] else "unverified")
                report["checks"]["barge_in_audio_sent_while_tool_pending"] = (
                    "passed" if report["audio_evidence"]["barge_in_samples_before_first_result"] else "unverified")
                report["checks"]["spoken_result_consistency"] = "manual_review_required"
                report["phase"] = "finished"
            finally:
                for task in tasks:
                    task.cancel()
    finally:
        # Preserve bounded diagnostic evidence on failure as well as success.
        report["native_calls_observed"] = len(calls)
        report["native_results_sent"] = len(delivered_calls)
        report["pending_call_ids_at_close"] = sorted(calls.keys() - delivered_calls)
        report["audio_evidence"]["output_sha256"] = output_digest.hexdigest()
        if args.output_wav and output_audio:
            output_path = Path(args.output_wav)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(output_path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes(output_audio)
            report["audio_evidence"]["output_wav"] = str(output_path)
            report["audio_evidence"]["output_wav_sha256"] = hashlib.sha256(output_audio).hexdigest()


async def probe(args):
    settings = Settings()
    report = {
        "tested_at": now().isoformat(), "api_version": args.api_version, "image_digest": args.image_digest,
        "requested_mode": "real", "real_service_connected": False, "synthetic_tool_result": True,
        "status": "blocked", "phase": "configuration", "checks": {}, "counts": {},
        "event_timeline": [], "timeline_dropped": 0,
        "audio_evidence": {
            "primary_input_samples": 0, "barge_in_input_samples": 0, "silence_input_samples": 0,
            "barge_in_samples_before_first_result": 0, "output_samples": 0, "max_send_gap_ms": 0,
            "output_wav_truncated": False,
        },
        "limitations": [
            "Synthetic tool results; this does not execute business tools or validate CueKB.",
            "Configuration echo is not proof of runtime template, weights or function head health.",
            "Silence advances inference; it does not repair ASR prefixes or force native tool selection.",
            "No capability flags are changed; acoustic quality and spoken facts require human review.",
            "API version and image digest are operator-supplied labels, not remotely attested versions.",
        ],
    }
    provider = None
    try:
        validate_durations(args)
        configuration, validators = load_configuration(args, settings)
        report["configuration_source"] = "explicit_snapshot" if args.session_update else "legacy_default"
        report["configuration_sha256"] = hashlib.sha256(
            json.dumps(configuration["session"], sort_keys=True).encode()).hexdigest()
        report["registered_tool_names"] = list(validators)
        primary, primary_hash = read_wav(args.wav)
        report["audio_evidence"]["primary_sha256"] = primary_hash
        report["audio_evidence"]["primary_planned_samples"] = len(primary) * 1920
        report["audio_evidence"]["input_hash_scope"] = "24khz_pcm_with_final_frame_zero_padding"
        barge = []
        if args.barge_in_wav:
            barge, barge_hash = read_wav(args.barge_in_wav)
            report["audio_evidence"]["barge_in_sha256"] = barge_hash
        report["audio_evidence"]["barge_in_planned_samples"] = len(barge) * 1920
        if not settings.voicechat_ws_url:
            raise ProbeFailure("VOICECHAT_WS_URL_NOT_CONFIGURED")
        provider = NvidiaVoiceChatAdapter(settings)
        provider.configuration = configuration
        report["phase"] = "handshake"
        await provider.connect("")
        report["checks"]["handshake_and_24khz_format"] = "passed"
        report["real_service_connected"] = True
        await run_stream(provider, primary, barge, validators, args, report)
    except Exception as exc:
        cause = leaf_exception(exc)
        report["status"] = "partial" if isinstance(cause, TimeoutError) and report["real_service_connected"] else "failed"
        report["reason"] = str(cause) if isinstance(cause, ProbeFailure) else type(cause).__name__
        if isinstance(cause, TimeoutError):
            report["reason"] = "TIMEOUT_" + report["phase"].upper()
    finally:
        if provider is not None:
            try:
                await provider.close()
            except Exception as exc:
                report["close_error"] = type(exc).__name__
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(output.write_text, json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", required=True, help="Authorized first-question 24kHz mono PCM16 WAV, at most 120s")
    parser.add_argument("--api-version", required=True, help="Operator-supplied VoiceChat API/service revision")
    parser.add_argument("--image-digest", required=True, help="Operator-supplied VoiceChat image digest")
    parser.add_argument("--session-update", help="Authorized exact session.update JSON snapshot; required for dual_tools")
    parser.add_argument("--barge-in-wav", help="Authorized distinct second-question WAV during delayed tool result")
    parser.add_argument("--tool-delay-seconds", type=float, default=5.0)
    parser.add_argument("--tool-wait-timeout", type=float, default=20.0)
    parser.add_argument("--post-result-seconds", type=float, default=20.0)
    parser.add_argument("--output", default="artifacts/voicechat-probe.json")
    parser.add_argument("--output-wav", help="Optional authorized output audio for human review, capped at 16 MiB")
    asyncio.run(probe(parser.parse_args()))
