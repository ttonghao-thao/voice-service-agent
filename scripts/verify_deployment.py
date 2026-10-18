"""Check the deployed API's non-sensitive readiness contract.

This is a release gate for container startup only. It deliberately does not
claim that VoiceChat, CueKB, test login, or spoken answers have passed end-to-end
acceptance; those require the authorized D07 production samples and review.
"""

import argparse
import json
from datetime import UTC, datetime
from urllib.error import URLError
from urllib.request import urlopen


def validate_ready(payload, expected_tools, *, require_text=True):
    if payload.get("status") != "ready":
        raise ValueError("API readiness is not ready")
    if payload.get("is_mock") is not False:
        raise ValueError("Deployment readiness reports a mock provider")
    if set(payload.get("enabled_tools", [])) != expected_tools:
        raise ValueError("API enabled tools do not match deployment configuration")
    if (require_text and payload.get("text_configured") is not True) or payload.get("voice_configured") is not True:
        raise ValueError("Text model and VoiceChat must both be configured")
    return {
        "status": payload["status"],
        "enabled_tools": sorted(expected_tools),
        "text_configured": payload.get("text_configured") is True,
        "voice_configured": payload.get("voice_configured") is True,
    }


def validate_voice_policy(payload, *, cuekb_only=False):
    """Check the deployed mode, not the model's ability to choose or use tools."""
    expected = {"provider": "nvidia", "qa_execution_mode": "dual_tools", "answer_policy": "general_qa"}
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ValueError(
            "VoiceChat autonomous answers require provider=nvidia, "
            "QA_EXECUTION_MODE=dual_tools and QA_ANSWER_POLICY=general_qa. "
            "knowledge_required rejects replies without a tool call. Create a new conversation after changing policy."
        )
    if payload.get("is_mock") is not False or payload.get("voice_available") is not True:
        raise ValueError("A real configured VoiceChat service is required")
    if cuekb_only and (
        payload.get("native_tools") != ["search_knowledge_base"]
        or payload.get("agent_provider") != "disabled"
    ):
        raise ValueError(
            "Phase-1 requires only search_knowledge_base and AGENT_PROVIDER=disabled; apply the native catalog"
        )
    return expected


def main():
    parser = argparse.ArgumentParser(description="Verify the running validation API readiness contract")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--expect-autonomous-voice", action="store_true",
                        help="Require VoiceChat to be allowed to answer directly or call tools")
    parser.add_argument("--expect-cuekb-native", action="store_true",
                        help="Require only search_knowledge_base and no extra answer model")
    args = parser.parse_args()
    from app.config import Settings

    url = args.base_url.rstrip("/") + "/health/ready"
    try:
        with urlopen(url, timeout=5) as response:  # noqa: S310 -- operator-provided loopback API URL
            payload = json.load(response)
    except (OSError, URLError, ValueError) as exc:
        raise SystemExit("Deployment readiness endpoint is unavailable: " + str(exc)) from exc
    try:
        settings = Settings()
        check = validate_ready(payload, settings.enabled_tool_names, require_text=settings.agent_provider != "disabled")
        if args.expect_autonomous_voice or args.expect_cuekb_native:
            with urlopen(args.base_url.rstrip("/") + "/api/v1/capabilities", timeout=5) as response:  # noqa: S310
                check["voice_policy"] = validate_voice_policy(json.load(response), cuekb_only=args.expect_cuekb_native)
    except OSError as exc:
        raise SystemExit("Deployment voice capabilities endpoint is unavailable") from exc
    except ValueError as exc:
        raise SystemExit("Deployment readiness check failed: " + str(exc)) from exc
    print(
        json.dumps(
            {
                "checked_at": datetime.now(UTC).isoformat(),
                "readiness": check,
                "limitations": [
                    "Container readiness only; it is not a VoiceChat, CueKB, login, or spoken-answer acceptance result."
                ],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
