"""User-owned voice style, snapshotted before a call; policy stays server-owned."""

from app.config import ROOT
from app.contracts import StrictModel
from pydantic import Field

DEFAULT_ID = "default-naonao"
DEFAULT_INSTRUCTIONS = "你是闹闹，一个开朗，幽默的语音伙伴，说话简短而自信，像朋友之间的聊天"


class VoiceProfileInput(StrictModel):
    name: str = Field(min_length=1, max_length=80, pattern=r".*\S.*")
    description: str = Field(default="", max_length=800)
    language_style: str = Field(default="", max_length=800)


class VoiceSessionInput(StrictModel):
    voice_profile_id: str | None = Field(default=None, min_length=1, max_length=36)


class VoiceProfilePatch(VoiceProfileInput):
    name: str = Field(default="", min_length=1, max_length=80, pattern=r".*\S.*")


def persona(name, description, language_style):
    return "，".join(part.strip() for part in ("你是" + name, description, language_style) if part.strip())


def default_profile():
    return {"id": DEFAULT_ID, "name": "闹闹", "description": "一个开朗，幽默的语音伙伴",
            "language_style": "说话简短而自信，像朋友之间的聊天",
            "persona_instructions": DEFAULT_INSTRUCTIONS, "is_default": True}


def profile_view(row):
    VoiceProfileInput(name=row.name, description=row.description, language_style=row.language_style)
    return {"id": row.id, "name": row.name, "description": row.description,
            "language_style": row.language_style,
            "persona_instructions": persona(row.name, row.description, row.language_style), "is_default": False}


def instructions(profile, locale, *, livekit):
    language = "zh" if locale == "zh-CN" else "en"
    path = ROOT / "config" / f"sophon-{'livekit' if livekit else 'live'}-prompt-{language}.txt"
    # Style is deliberately before immutable policy. Execution permissions are
    # checked independently by Registry, even if a malicious persona is obeyed.
    return profile["persona_instructions"] + "\n\n" + path.read_text(encoding="utf-8").strip()
