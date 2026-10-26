"""Protect current provider fields without weakening closed evidence types."""
import json
from pathlib import Path

import pytest
from app.tools.schemas import CueKBSearchResponse
from pydantic import ValidationError


def test_current_provider_fields_preserve_scope_and_reject_unknown_fields():
    payload = json.loads(Path('tests/fixtures/cuekb-v2-response.json').read_text())
    hit = payload['hits'][0]
    hit.update(fact_kind='specification', dependency_issues=['unmatched_reference'])
    payload['diagnostics']['selection'] = {
        'method': 'adaptive', 'calibration_status': 'not_configured',
        'stop_reason': 'max_hits', 'considered': 5,
    }
    for part in hit.get('context_parts', []):
        part['applicability'] = {'status': 'unknown'}
        part['link_provenance'] = None
    result = CueKBSearchResponse.model_validate(payload)
    assert result.hits[0].dependency_issues == ['unmatched_reference']
    assert result.diagnostics.selection.coverage_assessment == 'unassessed'
    hit['invented_truth_probability'] = 1.0
    with pytest.raises(ValidationError):
        CueKBSearchResponse.model_validate(payload)
