"""Run the complete fixed Native CueKB deployment check from the API image.

The command is intended to be started with one host-side ``docker exec``.  It
checks the deployed API contract and the image-bundled inputs before sending
the four real WAV cases through the portal.  Detailed JSON and reply audio stay
under /tmp; stdout is a bounded set of fields used for fault localization.
"""

import argparse
import asyncio
import re
import sys
from datetime import UTC, datetime

import httpx
from app.config import Settings

from scripts.validate_native_cuekb import load_cases, passed, run
from scripts.verify_deployment import validate_ready, validate_voice_policy

PROMPT_RE = re.compile(
    r"Prompt prepared: tools_count=(\d+) tool_names=(\S+) "
    r"prompt_chars=(\d+) prompt_sha256=([0-9a-f]{64})"
)
BACKEND_PROMPT_RE = re.compile(
    r"System prompt received for sequence (\d+): tool_names=(\S+) "
    r"chars=(\d+) sha256=([0-9a-f]{64})"
)
MODEL_FUNCTION_RE = re.compile(
    r"Function call decoded from model for sequence (\d+): tool_names=(\S+)"
)
VOICECHAT_LOG_MARKERS = (
    "Prompt prepared:",
    "System prompt received for sequence",
    "Function call decoded from model for sequence",
    '"event": "function.parsed"',
    '"event":"function.parsed"',
    '"event": "function.result"',
    '"event":"function.result"',
    '"kind": "response.output_audio.done"',
    '"kind":"response.output_audio.done"',
)


def csv(values):
    return ",".join(str(value) for value in values) if values else "-"


def safe_detail(value):
    return " ".join(str(value).split())[:240]


def classify(report):
    cases = report.get("cases", [])
    if report.get("error_type") in ("ConnectError", "ConnectionError") or not cases:
        return "PORTAL_CONNECT", "api_portal_connectivity"
    if any(row.get("portal_error") == "VOICE_TOOL_UNKNOWN" for row in cases):
        return "UNKNOWN_TOOL_NAME", "api_tool_name_validation"
    product = [row for row in cases if row.get("expected_tool") is not None]
    if any(
        not row.get("input_item_ids") and not row.get("input_items_observed")
        for row in product
    ):
        return "INPUT_NOT_OBSERVED", "voicechat_audio_or_asr_transport"
    if any(row.get("selection") != "passed" and not row.get("actual_tools") for row in product):
        return "TOOL_NOT_CALLED", "voicechat_prompt_or_model_function_channel"
    if any(row.get("selection") != "passed" for row in cases):
        return "WRONG_TOOL_SELECTION", "voicechat_selection"
    if any(row.get("arguments") in ("failed", "unverified") for row in product):
        return "TOOL_ARGUMENTS", "voicechat_function_arguments"
    if any(row.get("retrieval") != "passed" for row in product):
        return "CUEKB_RETRIEVAL", "api_cuekb_trace"
    if report.get("error_type") == "TimeoutError" or any(
        row.get("speech_transport") != "passed" for row in cases
    ):
        return "VOICE_RESPONSE_COMPLETION", "voicechat_audio_done_or_eos"
    if passed(report):
        return "PASS", "human_transcription_and_spoken_faithfulness"
    return "INCOMPLETE", "report_json"


def case_line(row):
    return " ".join((
        f"case={row.get('case_id')}",
        f"expected={row.get('expected_tool') or '-'}",
        f"actual={csv(row.get('actual_tools', []))}",
        f"selection={row.get('selection')}",
        f"arguments={row.get('arguments')}",
        f"retrieval={row.get('retrieval')}",
        f"speech={row.get('speech_transport')}",
        f"reason={row.get('reason_code') or '-'}",
        f"portal_error={row.get('portal_error') or '-'}",
        f"elapsed_ms={row.get('elapsed_ms')}",
        f"input_ids={csv(row.get('input_item_ids', []))}",
        f"trace_ids={csv(row.get('source_trace_ids', []))}",
    ))


def json_field_count(text, field, value):
    return len(re.findall(rf'"{re.escape(field)}"\s*:\s*"{re.escape(value)}"', text))


def summarize_voicechat_log(text, expected_tool):
    prompts = PROMPT_RE.findall(text)
    backend = BACKEND_PROMPT_RE.findall(text)
    decoded = MODEL_FUNCTION_RE.findall(text)
    prompt = prompts[-1] if prompts else None
    received = backend[-1] if backend else None
    prompt_match = (
        "passed" if prompt and received and prompt[0] == "1"
        and prompt[1] == expected_tool and prompt[1:] == received[1:]
        else "failed" if prompt and received else "unverified"
    )
    decoded_names = [name for _, name in decoded]
    model_function = (
        "passed" if expected_tool in decoded_names
        else "wrong_name" if decoded_names else "absent"
    )
    lines = [
        (
            f"voicechat_prompt count={prompt[0]} tools={prompt[1]} chars={prompt[2]} sha256={prompt[3]}"
            if prompt else "voicechat_prompt absent"
        ),
        (
            f"backend_prompt sequence={received[0]} tools={received[1]} "
            f"chars={received[2]} sha256={received[3]} match={prompt_match}"
            if received else "backend_prompt absent match=unverified"
        ),
        (
            f"model_function status={model_function} "
            f"sequences={csv(sequence for sequence, _ in decoded)} "
            f"tools={csv(decoded_names)}"
        ),
        (
            f"voicechat_events function_parsed={json_field_count(text, 'event', 'function.parsed')} "
            f"function_result={json_field_count(text, 'event', 'function.result')} "
            f"audio_done={json_field_count(text, 'kind', 'response.output_audio.done')}"
        ),
    ]
    if prompt_match != "passed":
        result = ("failed", "PROMPT_CHAIN")
    elif model_function != "passed":
        result = ("failed", "MODEL_FUNCTION_" + model_function.upper())
    else:
        result = ("passed", "NONE")
    lines.append(f"voicechat_diagnostic={result[0]} fault={result[1]}")
    return lines, result[0] == "passed"


def read_voicechat_evidence(stream):
    selected = []
    for line in stream:
        if any(marker in line for marker in VOICECHAT_LOG_MARKERS):
            selected.append(line)
            if len(selected) > 4096:
                raise ValueError("VoiceChat diagnostic window contains more than 4096 relevant lines")
    return "".join(selected)


async def preflight(args, settings):
    cases = load_cases(args.cases)
    async with httpx.AsyncClient(base_url=args.base_url, timeout=5, follow_redirects=False) as client:
        ready_response = await client.get("/health/ready")
        ready_response.raise_for_status()
        ready = ready_response.json()
        validate_ready(ready, settings.enabled_tool_names, require_text=False)
        capabilities_response = await client.get("/api/v1/capabilities")
        capabilities_response.raise_for_status()
        capabilities = capabilities_response.json()
        validate_voice_policy(capabilities, cuekb_only=True)
    return cases, capabilities


async def execute(args):
    settings = Settings()
    started = datetime.now(UTC).isoformat()
    try:
        cases, capabilities = await preflight(args, settings)
    except Exception as exc:
        print(f"preflight=failed error={type(exc).__name__} detail={safe_detail(exc)!r}")
        print("validation=failed fault=PREFLIGHT focus=api_configuration_or_image exit_code=2")
        return 2

    print(
        "preflight=passed "
        f"provider={capabilities.get('provider')} "
        f"mode={capabilities.get('qa_execution_mode')} "
        f"policy={capabilities.get('answer_policy')} "
        f"tools={csv(capabilities.get('native_tools', []))} cases={len(cases)}"
    )
    report = await run(args)
    finished = datetime.now(UTC).isoformat()
    print(
        f"window_start={started} window_end={finished} "
        f"conversation_id={report.get('conversation_id') or '-'} report={args.output}"
    )
    for row in report.get("cases", []):
        print(case_line(row))
    fault, focus = classify(report)
    exit_code = 0 if fault == "PASS" else 1
    print(
        f"validation={'passed' if exit_code == 0 else 'failed'} "
        f"fault={fault} focus={focus} error_type={report.get('error_type') or '-'} "
        f"exit_code={exit_code}"
    )
    return exit_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summarize-voicechat-log", action="store_true",
                        help="Read a bounded docker-log window from stdin and print five diagnostic lines")
    parser.add_argument("--expected-tool", default="search_knowledge_base")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--origin", help="Defaults to the API container PUBLIC_ORIGIN")
    parser.add_argument("--cases", default="/app/validation/native-cuekb/cases.json")
    parser.add_argument("--case-timeout", type=float, default=90)
    parser.add_argument("--audio-dir", default="/tmp/native-cuekb/replies")
    parser.add_argument("--output", default="/tmp/native-cuekb/report.json")
    args = parser.parse_args()
    if args.summarize_voicechat_log:
        try:
            evidence = read_voicechat_evidence(sys.stdin)
        except ValueError as exc:
            print(f"voicechat_diagnostic=failed fault=LOG_WINDOW detail={safe_detail(exc)!r}")
            raise SystemExit(2) from exc
        lines, ok = summarize_voicechat_log(evidence, args.expected_tool)
        print("\n".join(lines))
        raise SystemExit(0 if ok else 1)
    if args.origin is None:
        args.origin = Settings().public_origin
    raise SystemExit(asyncio.run(execute(args)))


if __name__ == "__main__":
    main()
