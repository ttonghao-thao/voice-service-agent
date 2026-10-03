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


def validate_ready(payload, expected_tools, expected_mode):
    if payload.get("status") != "ready":
        raise ValueError("API readiness is not ready")
    if payload.get("is_mock") is not False:
        raise ValueError("Deployment readiness reports a mock provider")
    if set(payload.get("enabled_tools", [])) != expected_tools:
        raise ValueError("API enabled tools do not match deployment configuration")
    if expected_mode not in ("direct", "external") or payload.get("execution_mode") != expected_mode:
        raise ValueError("API execution mode does not match deployment configuration")
    external = expected_mode == "external"
    if payload.get("voice_configured") is not True or any(
        payload.get(field) is not external
        for field in ("text_configured", "text_available", "external_llm_enabled")
    ):
        raise ValueError("Required mode capabilities are missing or inconsistent")
    return {
        "status": payload["status"],
        "execution_mode": expected_mode,
        "enabled_tools": sorted(expected_tools),
        "text_configured": payload.get("text_configured") is True,
        "voice_configured": payload.get("voice_configured") is True,
    }


def main():
    parser = argparse.ArgumentParser(description="Verify the running validation API readiness contract")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
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
        profile = settings.execution_profile()
        settings.validate_deployment(profile)
        check = validate_ready(payload, settings.enabled_tool_names, profile.mode)
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
