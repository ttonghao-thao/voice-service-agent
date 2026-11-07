import pytest
from app.agent_runtime.evidence import EvidenceGate
from app.tools.adapters import CueKBAdapter
from app.tools.entity_context import context_binding

ENTITY = {'id': '11111111-1111-4111-8111-111111111111', 'name': 'Golden Yolk', 'aliases': []}

@pytest.mark.parametrize('phrase', ["I don't mean", 'I don’t mean', "It isn't", "It wasn't"])
def test_retracted_subject_never_authorizes_binding(phrase):
    assert context_binding(f'{phrase} Golden Yolk. Can it be shipped?', [], [ENTITY]) == []

def test_property_negation_keeps_confirmed_subject():
    assert context_binding('Can it not be shipped?', [{'text': 'Golden Yolk'}], [ENTITY])

def test_parallel_attributes_have_exact_separate_spans():
    question = 'What is its price and weight?'
    facets = CueKBAdapter._question_facets(question)
    assert [f.text for f in facets] == ['price', 'weight']
    assert all(question[slice(*f.question_span)] == f.text for f in facets)

def test_conjunction_in_product_name_is_not_split():
    assert len(CueKBAdapter._question_facets('What is Salt and Pepper mooncake?')) == 1

def test_other_negative_clause_cannot_cover_shipping_contradiction():
    envelope = {'items': [{'text': 'It cannot be shipped. Do not freeze it.'}], 'facets': []}
    assert EvidenceGate.check_facets('It can be shipped. Do not freeze it.', envelope)
    assert EvidenceGate.check_facets('It cannot be shipped. Do not freeze it.', envelope) is None

def test_all_attributes_omitted_is_not_a_complete_answer():
    envelope = {'facets': [{'id': 'f1', 'text': 'price', 'support': 'direct'}], 'items': []}
    assert EvidenceGate.check_facets('Please check with the manufacturer.', envelope)
    assert EvidenceGate.check_facets('The price is RMB 42.', envelope) is None

def test_budget_omission_does_not_require_unseen_fact():
    envelope = {'facets': [{'id': 'f1', 'text': 'price', 'support': 'direct'}],
                'wire_admission': {'retained_ids': []}, 'group_supports': {}, 'items': []}
    assert EvidenceGate.check_facets('I cannot confirm this information.', envelope) is None
