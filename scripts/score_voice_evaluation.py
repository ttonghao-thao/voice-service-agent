"""Summarize human-reviewed real-service cases; never invent missing observations."""

import argparse
import json
from pathlib import Path


def summarize(rows):
    ids = [row["case_id"] for row in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate case IDs would distort the denominator")
    report = {"submitted_cases": len(rows), "metrics": {}}
    for field in ("bridge_correct", "arguments_correct", "rag_evidence_correct", "spoken_facts_correct"):
        values = [r[field] for r in rows if type(r.get(field)) is bool and r.get("real_service") is True]
        report["metrics"][field] = {
            "evaluated": len(values),
            "passed": sum(values),
            "rate": sum(values) / len(values) if values else None,
        }
    leaks = [
        r["cancel_leaks"]
        for r in rows
        if type(r.get("cancel_leaks")) is int and r.get("real_service") is True
    ]
    if any(value < 0 for value in leaks):
        raise ValueError("Negative leak counts are invalid")
    report["cancel_leaks"] = {"evaluated": len(leaks), "leaks": sum(leaks) if leaks else None}
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "results", help="Reviewed JSONL; each case requires real_service=true to enter a metric"
    )
    args = parser.parse_args()
    print(
        json.dumps(
            summarize(
                [json.loads(line) for line in Path(args.results).read_text().splitlines() if line.strip()]
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
