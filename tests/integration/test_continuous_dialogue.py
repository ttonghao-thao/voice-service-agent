import json

import pytest

from scripts.evaluate_continuous_dialogue import MANIFEST, run_case
from scripts.score_voice_evaluation import summarize

CASES = json.loads(MANIFEST.read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["case_id"] + "-" + case["scenario"])
async def test_continuous_dialogue_contract(case, tmp_path):
    row = await run_case(case, tmp_path / "continuous.db")
    assert row["status"] == "completed", row
    report = summarize([row], {"cases": [case]}, mode="simulation")
    assert report["complete"], report
    for field in case["required_metrics"]:
        assert report["metrics"][field] == {"evaluated": 1, "passed": 1, "rate": 1.0}, row
