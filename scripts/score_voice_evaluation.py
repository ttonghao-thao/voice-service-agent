"""Summarize human-reviewed real-service cases; never invent missing observations."""

import argparse
import json
import math
from pathlib import Path


def summarize_group(rows):
    ids = [(row.get("execution_mode", "unspecified"), row["case_id"]) for row in rows]
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
    durations = [
        r["answer_first_audio_ms"]
        for r in rows
        if r.get("real_service") is True and type(r.get("answer_first_audio_ms")) in (int, float)
    ]
    if any(not math.isfinite(v) or v < 0 for v in durations):
        raise ValueError("Answer latency must be finite and nonnegative")
    durations.sort()
    report["answer_first_audio_ms"] = {
        "definition": "End of user utterance to first valid answer audio, excluding tool ACK",
        "evaluated": len(durations),
        "p50": durations[max(0, math.ceil(len(durations) * 0.50) - 1)] if durations else None,
        "p95": durations[max(0, math.ceil(len(durations) * 0.95) - 1)] if durations else None,
    }
    return report


def summarize(rows):
    modes = {row.get("execution_mode", "unspecified") for row in rows}
    if not modes.issubset({"direct", "external", "unspecified"}):
        raise ValueError("Unknown execution mode in evaluation")
    groups = {
        mode: summarize_group([r for r in rows if r.get("execution_mode", "unspecified") == mode])
        for mode in sorted(modes)
    }
    # Keep legacy single-mode reports readable, while never pooling direct and external latency.
    report = summarize_group(rows) if len(modes) <= 1 else {"submitted_cases": len(rows)}
    report["by_execution_mode"] = groups
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
