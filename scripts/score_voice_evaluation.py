"""Summarize human-reviewed real-service cases; never invent missing observations."""

import argparse
import json
from pathlib import Path

FIELDS = ("bridge_correct", "arguments_correct", "rag_evidence_correct", "spoken_facts_correct",
          "tool_selection_correct", "context_correct", "correction_correct", "control_correct",
          "authorization_correct", "stale_output_isolated")
VERSION_KEYS = {"application", "voice", "cuekb", "text_model", "context_schema"}


def summarize(rows, manifest=None, mode="real"):
    if mode not in ("real", "simulation"):
        raise ValueError("Evidence mode must be real or simulation")
    ids = [row["case_id"] for row in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate case IDs would distort the denominator")
    eligible = [dict(r) for r in rows if
                (r.get("real_service") is True and r.get("evidence_mode", "real") == "real" if mode == "real"
                 else r.get("real_service") is False and r.get("evidence_mode") == "simulated_independent_apis")]
    report = {"submitted_cases": len(rows), "eligible_cases": len(eligible),
              "excluded_cases": len(rows) - len(eligible), "evidence_mode": mode, "metrics": {}}
    cases = (manifest or {}).get("cases", [])
    expected = {case["case_id"]: case for case in cases}
    if len(expected) != len(cases):
        raise ValueError("Duplicate manifest case IDs")
    if manifest is not None and set(ids) - expected.keys():
        raise ValueError("Result includes cases outside the selected manifest")
    tool_errors = {"expected_steps": sum(len(case.get("tool_steps", {})) for case in cases),
                   "observed_steps": 0, "missing_observations": 0,
                   "missing_tool": 0, "unexpected_tool": 0, "wrong_tool": 0}
    for row in eligible:
        if row["case_id"] not in expected:
            continue
        steps = expected[row["case_id"]].get("tool_steps", {})
        observed = row.get("tool_observations", [])
        step_ids = [x["step_id"] for x in observed]
        if len(step_ids) != len(set(step_ids)) or set(step_ids) - steps.keys():
            raise ValueError("Duplicate or unexpected tool observation step")
        correct = True
        tool_errors["observed_steps"] += len(observed)
        for observation in observed:
            want, actual = steps[observation["step_id"]], observation["actual_tool"]
            if actual is not None and (not isinstance(actual, str) or not actual):
                raise ValueError("Tool observation must be a tool name or explicit null")
            if actual != want:
                correct = False
                key = "missing_tool" if actual is None else "unexpected_tool" if want is None else "wrong_tool"
                tool_errors[key] += 1
        if steps and len(observed) == len(steps):
            row["tool_selection_correct"] = correct
        else:
            row.pop("tool_selection_correct", None)
    for field in FIELDS:
        values = [r[field] for r in eligible if type(r.get(field)) is bool]
        report["metrics"][field] = {
            "evaluated": len(values),
            "passed": sum(values),
            "rate": sum(values) / len(values) if values else None,
        }
    leaks = [
        r["cancel_leaks"]
        for r in eligible
        if type(r.get("cancel_leaks")) is int
    ]
    if any(value < 0 for value in leaks):
        raise ValueError("Negative leak counts are invalid")
    report["cancel_leaks"] = {"evaluated": len(leaks), "leaks": sum(leaks) if leaks else None}
    stale = [r["stale_result_leaks"] for r in eligible if type(r.get("stale_result_leaks")) is int]
    if any(value < 0 for value in stale):
        raise ValueError("Negative leak counts are invalid")
    report["stale_result_leaks"] = {"evaluated": len(stale), "leaks": sum(stale) if stale else None}
    tool_errors["missing_observations"] = tool_errors["expected_steps"] - tool_errors["observed_steps"]
    report["tool_errors"] = tool_errors
    if manifest is not None:
        by_id = {row["case_id"]: row for row in eligible}
        missing_metrics = {}
        missing_versions = []
        for case_id, case in expected.items():
            row = by_id.get(case_id, {})
            missing = [field for field in case.get("required_metrics", []) if type(row.get(field)) is not bool]
            if missing:
                missing_metrics[case_id] = missing
            versions = row.get("versions", {})
            if not VERSION_KEYS <= versions.keys() or any(not isinstance(versions[k], str) or not versions[k]
                                                         for k in VERSION_KEYS if k in versions):
                missing_versions.append(case_id)
        groups = {json.dumps(row["versions"], sort_keys=True) for row in eligible if row.get("versions")}
        report["coverage"] = {"expected_cases": len(expected), "missing_case_ids": sorted(expected.keys() - by_id.keys()),
                              "missing_metrics": missing_metrics, "missing_versions": missing_versions,
                              "failed_case_ids": [r["case_id"] for r in eligible if r.get("status") == "failed"],
                              "expected_metrics": {field: sum(field in case.get("required_metrics", []) for case in cases)
                                                   for field in FIELDS}}
        report["versions"] = {"group_count": len(groups), "comparable": len(groups) == 1,
                              "groups": [json.loads(group) for group in sorted(groups)]}
        report["complete"] = (not report["coverage"]["missing_case_ids"] and not missing_metrics
                              and not missing_versions and len(groups) == 1 and not report["coverage"]["failed_case_ids"])
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "results", help="Reviewed JSONL; each case requires real_service=true to enter a metric"
    )
    parser.add_argument("--manifest", help="Fixed continuous-dialogue JSON manifest")
    parser.add_argument("--mode", choices=("real", "simulation"), default="real")
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()
    report = summarize([json.loads(line) for line in Path(args.results).read_text().splitlines() if line.strip()],
                       json.loads(Path(args.manifest).read_text()) if args.manifest else None, args.mode)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.require_complete and report.get("complete") is not True:
        raise SystemExit(1)
