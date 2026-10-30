import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from app.agent_runtime.context import RunContext
from app.agent_runtime.direct import EvidenceReady
from app.agent_runtime.native_cuekb import NativeCueKBExecutor, voice_result
from app.config import Settings
from app.contracts import Principal
from app.tools.adapters import CueKBAdapter
from app.tools.evidence_contract import EvidenceQueryResponse
from app.tools.schemas import CueKBSearchInput
from pydantic import ValidationError

KB = "11111111-1111-4111-8111-111111111111"
FIXTURES = Path(__file__).parents[1] / "fixtures"


def provider_response():
    return json.loads((FIXTURES / "cuekb-evidence-query-v2.1-response.json").read_text())


def validate_response():
    return json.loads((FIXTURES / "cuekb-evidence-validate-v2.1-response.json").read_text())


def context(question):
    return RunContext(
        Principal(user_id="test", scopes=frozenset({"knowledge:read"}), knowledge_base_ids=(KB,)),
        "conversation", "turn", 0, request_revision=3, native_voice_search=True,
        tool_arguments={"query": "Product A"},
        task_context={
            "original_request": question,
            "resolved_request": question,
            "request_revision": 3,
            "input_source": {"input_item_id": "input-1"},
        },
    )


async def test_voice_uses_one_problem_query_then_validates_package_and_projects_only_model_view():
    question = "What is Product A input voltage?"
    query_payload = provider_response()
    query_payload.update(request_id="input-1", input_revision=3)
    query_payload["delivery"].update(profile="voice", token_budget=300)
    query_payload["facets"][0].update(id="f1", text=question)
    query_payload["evidence"][0]["supports"] = ["f1"]
    query_payload["model_view"]["evidence"][0]["for"] = question
    validation = validate_response()
    validation.update(request_id="input-1", status="valid")
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/v2/evidence/query":
            return httpx.Response(200, json=query_payload)
        if request.url.path == "/v2/evidence/validate":
            return httpx.Response(200, json=validation)
        raise AssertionError(request.url.path)

    settings = Settings(_env_file=None, cuekb_mode="real", cuekb_base_url="https://cuekb.fixture",
                        cuekb_api_key="private", cuekb_voice_token_budget=300)
    ctx = context(question)
    ctx.task_context["resolved_request"] = question + "\nConfirmed user conditions: Product A."

    async def record(*args, **kwargs):
        pass

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = CueKBAdapter(settings, client)

        async def invoke(name, arguments, run_context):
            output = await adapter.invoke(CueKBSearchInput(**arguments), run_context)
            return adapter.output_adapter.validate_python(output).model_dump(mode="json")

        executor = NativeCueKBExecutor(settings, SimpleNamespace(invoke=invoke, store=SimpleNamespace(record=record)))
        ready = await executor.run("Product A", ctx, [])

    assert isinstance(ready, EvidenceReady)
    assert [request.url.path for request in requests] == ["/v2/evidence/query", "/v2/evidence/validate"]
    sent = json.loads(requests[0].content)
    assert sent["question"] == question
    assert sent["search_query"] == question + "\nConfirmed user conditions: Product A."
    assert sent["request_id"] == "input-1" and sent["input_revision"] == 3
    assert sent["delivery_profile"] == "voice" and sent["token_budget"] == 300
    assert sent["requested_facets"] == [{"id": "f1", "text": question, "question_span": [0, len(question)]}]
    assert json.loads(requests[1].content)["package_id"] == query_payload["package"]["id"]
    result = voice_result(ready)
    assert result["decision"] == "answer" and result["search_state"] == "finished"
    assert result["evidence"] == query_payload["model_view"]["evidence"]
    assert "sources" not in result and ready.citations[0].cuekb_schema_version == "2.1"


def test_provider_fixture_is_strict_and_unknown_fields_fail():
    payload = provider_response()
    assert EvidenceQueryResponse.model_validate(payload).contract_revision == "cuekb.evidence/2.1"
    payload["invented_field"] = True
    with pytest.raises(ValidationError):
        EvidenceQueryResponse.model_validate(payload)


@pytest.mark.parametrize(("question", "texts"), [
    (
        "Can it be shipped? How long is its shelf life, and how should it be stored after opening?",
        ["Can it be shipped?", "How long is its shelf life", "how should it be stored after opening?"],
    ),
    (
        "What does the documentation say about price, weight, pastry layers, and best consumption time?",
        ["What does the documentation say about price", "weight", "pastry layers", "best consumption time?"],
    ),
])
def test_question_facets_preserve_exact_spans_for_compound_voice_questions(question, texts):
    facets = CueKBAdapter._question_facets(question)
    assert [facet.text for facet in facets] == texts
    assert all(question[start:end] == facet.text
               for facet, (start, end) in ((facet, facet.question_span) for facet in facets))
