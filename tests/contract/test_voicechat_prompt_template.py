"""Render the deployed template; do not infer native model ability from text."""

import hashlib
import json
import re

import pytest
from app.config import ROOT
from app.voice.provider import session_update
from jinja2 import Environment, StrictUndefined
from jsonschema import validate

POLICY = (
    "Follow the session instructions and tool contracts when handling tool results.",
    "If a registered tool covers the request, call it instead of saying that you lack access to the information.",
    "A result with search finished or search_state finished is terminal for that user input.",
    "do not retry, generalize, split, or issue another tool call for that input.",
    "Do not issue multiple tool calls simultaneously.",
    "Speak only as permitted by the session answer policy and the returned tool result.",
)


@pytest.mark.parametrize("count", [0, 1, 2, 3])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("policy", ["general_qa", "knowledge_required"])
def test_deployed_template_keeps_policy_schema_and_separate_ack(count, nested, policy):
    path = ROOT / "docs/voicechat/s2s/prompt_template.jinja"
    manifest = json.loads((ROOT / "docs/voicechat/snapshot-manifest.json").read_text())
    assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest["files"]["s2s/prompt_template.jinja"]
    tools = [{"type": "function", "name": f"knowledge_{i}", "description": "Retrieve authorized evidence.",
              "parameters": {"type": "object", "properties": {"user_request": {"type": "string"}},
                             "required": ["user_request"], "additionalProperties": False},
              "ack_messages": ["Please wait while I check that."]} for i in range(count)]
    instructions = session_update(tools=tools, answer_policy=policy)["session"]["instructions"]
    template = Environment(undefined=StrictUndefined).from_string(path.read_text())
    rendered = template.render(system_message=instructions,
                               tools=[{"function": t} for t in tools] if nested else tools)
    assert rendered.startswith(instructions)
    assert "wait about three seconds" in rendered
    assert "ask one brief welcoming question" in rendered
    if policy == "knowledge_required":
        assert "Do not speak additional greetings" in rendered
    else:
        assert "A tool call is not required for every utterance." in rendered
        assert "Do not speak additional greetings" not in rendered
    if not tools:
        assert rendered == instructions
        return
    catalog = json.loads(re.search(r"<AVAILABLE_TOOLS>(.*?)</AVAILABLE_TOOLS>", rendered, re.S)[1])
    assert catalog == [{k: v for k, v in t.items() if k not in ("type", "ack_messages")} for t in tools]
    ack = json.loads(re.search(r"<TOOL_ACK_MESSAGES>(.*?)</TOOL_ACK_MESSAGES>", rendered, re.S)[1])
    assert ack == [{"name": t["name"], "ack_messages": t["ack_messages"]} for t in tools]
    assert all(sentence in rendered for sentence in POLICY)
    assert "call additional tools if needed" not in rendered
    example = json.loads(re.search(r"<TOOLCALL>(.*?)</TOOLCALL>", rendered, re.S)[1])
    assert len(example) == 1 and isinstance(example[0]["arguments"], dict)
    assert isinstance(json.loads(re.search(r"<TOOL_RESPONSE>(.*?)</TOOL_RESPONSE>", rendered, re.S)[1]), list)


def test_single_knowledge_base_call_example_matches_registered_query_schema():
    from app.contracts import CueKBArguments
    path = ROOT / "docs/voicechat/s2s/prompt_template.jinja"
    rendered = Environment(undefined=StrictUndefined).from_string(path.read_text()).render(
        system_message="Answer in English.",
        tools=[{"name": "search_knowledge_base", "parameters": CueKBArguments.model_json_schema()}],
    )
    example = json.loads(re.search(r"<TOOLCALL>(.*?)</TOOLCALL>", rendered, re.S)[1])
    assert len(example) == 1 and example[0]["name"] == "search_knowledge_base"
    validate(example[0]["arguments"], CueKBArguments.model_json_schema())
    query = example[0]["arguments"]["query"].lower()
    assert query == "can product ax be shipped, and how should it be stored after opening?"
    assert "Never reuse example names or attributes" in rendered
    assert "even without previous tool history" in rendered
