import json
from pathlib import Path

from app.config import ROOT, Settings
from app.contracts import BridgeArguments
from app.voice.provider import session_update


def test_english_knowledge_release_samples_match_voice_contract():
    path = Path(__file__).parents[1] / "fixtures/english-knowledge-cases.jsonl"
    samples = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(samples) >= 6
    assert len({sample["case_id"] for sample in samples}) == len(samples)
    assert {sample["category"] for sample in samples} == {"knowledge", "clarification", "small_talk"}
    assert Settings(_env_file=None).enabled_tool_names == {"search_knowledge"}
    for sample in samples:
        assert sample["text"].isascii()
        assert sample["expected"].isascii()
        assert sample["audio_path"] is None and sample["result"] is None
        assert "weather" not in sample["text"].lower()
        BridgeArguments(user_request=sample["text"])
    assert (ROOT / "config/voice-prompt.txt").read_text().isascii()
    assert (ROOT / "config/agent-prompt.txt").read_text().isascii()
    assert json.dumps(session_update("Confirmed model AX"), ensure_ascii=False).isascii()
