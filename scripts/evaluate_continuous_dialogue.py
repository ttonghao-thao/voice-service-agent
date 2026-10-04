"""Run the fixed continuous-dialogue corpus against independently listening API fixtures.

Tool decisions/ASR/TTS are scripted. This measures application contracts, not model accuracy.
"""

import argparse
import asyncio
import hashlib
import json
import tempfile
from pathlib import Path

from app.storage.models import Turn, Utterance
from sqlalchemy import select

from scripts.score_voice_evaluation import summarize
from scripts.simulate_full_flow import PortalCall, SimulationHarness, VoicePlan

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tests/fixtures/continuous-dialogue-cases.json"
BACKGROUND = "My model is AX100, software version 2.1."


async def eventually(callback):
    async with asyncio.timeout(5):
        while True:
            result = await callback()
            if result:
                return result
            await asyncio.sleep(0.01)


async def stored_input(harness, call, plan):
    async with harness.app.state.store.sessions() as db:
        return (await db.execute(select(Utterance).where(
            Utterance.conversation_id == call.cid, Utterance.input_item_id == plan.input_id))).scalar_one_or_none()


async def complete_general(harness, call, plan):
    await call.speak(plan)
    await call.until("portal.audio.done", lambda e: e["payload"]["response_id"] == plan.response_id)

    async def completed():
        item = await stored_input(harness, call, plan)
        return item if item and item.answer else None

    return await eventually(completed)


async def completed_turn(call, plan):
    async def completed():
        history = await call.history()
        return next((t for t in history["items"] if t["input_item_id"] == plan.input_id and t["answer"]), None)

    return await eventually(completed)


async def task_snapshot(harness, turn):
    async with harness.app.state.store.sessions() as db:
        return (await db.get(Turn, turn["id"])).task_context


def versions():
    files = sorted([*(ROOT / "apps/api/app").rglob("*.py"), *(ROOT / "config").glob("*.*"),
                    *(ROOT / "apps/api/migrations").rglob("*.py")])
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    fixture = hashlib.sha256((ROOT / "scripts/simulate_full_flow.py").read_bytes()).hexdigest()
    return {"application": digest.hexdigest(), "voice": "scripted-voice-fixture-v1:" + fixture,
            "cuekb": "cuekb-m3-fixture-v1:" + fixture, "text_model": "compatible-fixture-v1:" + fixture,
            "context_schema": "task-context-v1"}


async def run_case(case, database_path):
    row = {"case_id": case["case_id"], "real_service": False,
           "evidence_mode": "simulated_independent_apis", "versions": versions(), "tool_observations": []}
    scenario = case["scenario"]
    plans = {}
    async with SimulationHarness(database_path, answer_timeout=2500) as harness:
        harness.services.dynamic_conditions = True
        call = await PortalCall(harness).create()
        await call.open_voice()
        try:
            if scenario in ("general_to_lookup", "general_to_reasoned", "unverified_general_reply",
                            "correction_stale_arguments", "ambiguous_correction", "reconnect_conditions"):
                general = VoicePlan(question=BACKGROUND, tool=None,
                    spoken="AX100 supports 99 connections." if scenario == "unverified_general_reply"
                    else "Thanks, I will keep your context.")
                plans["background"] = general
                await complete_general(harness, call, general)
            if scenario in ("general_to_lookup", "general_to_reasoned", "unverified_general_reply", "reconnect_conditions"):
                if scenario == "reconnect_conditions":
                    await call.ws.close()
                    await call.reader
                    await call.open_voice()
                    instructions = harness.services.updates[-1]["session"]["instructions"]
                    row["control_correct"] = ("AX100" in instructions and "2.1" in instructions
                        and general.input_id in instructions and "may not have been fully heard" in instructions)
                tool = "reason_over_knowledge" if scenario in ("general_to_reasoned", "unverified_general_reply") else "lookup_knowledge"
                plan = VoicePlan(question="What is its documented connection limit?", tool=tool)
                plans["question"] = plan
                await call.speak(plan)
                turn = await completed_turn(call, plan)
                ctx = await task_snapshot(harness, turn)
                query = harness.services.queries[-1]
                row["context_correct"] = (turn["answer"]["status"] == "answered"
                    and query["filters"] == {"document_ids": [], "product_model": "AX100", "software_version": "2.1"}
                    and ctx["original_request"] == plan.question and "AX100" in ctx["resolved_request"]
                    and "2.1" in ctx["resolved_request"]
                    and ctx["conditions"]["product_model"]["source"]["input_item_id"] == general.input_id
                    and any(x["content"] == BACKGROUND for x in ctx["history"])
                    and len(harness.services.queries) == 1
                    and len(harness.services.model_calls) == (2 if tool == "reason_over_knowledge" else 0))
                if scenario == "unverified_general_reply":
                    row["context_correct"] &= ("99" not in turn["answer"]["display_text"]
                        and "10" in turn["answer"]["display_text"] and any(
                        "[Unverified general reply; not knowledge evidence] AX100 supports 99" in x["content"]
                        for x in ctx["history"]))
            elif scenario in ("correction_stale_arguments", "ambiguous_correction"):
                plan = VoicePlan(question="Actually use AX200 instead." if scenario == "correction_stale_arguments"
                    else "Wrong model.", arguments={"user_request": "Rewritten request", "product_model":
                    "AX100" if scenario == "correction_stale_arguments" else None, "software_version": None})
                plans["correction"] = plan
                await call.speak(plan)
                turn = await completed_turn(call, plan)
                ctx = await task_snapshot(harness, turn)
                row["context_correct"] = (turn["answer"]["status"] == "needs_clarification"
                    and not harness.services.queries and not harness.services.model_calls
                    and "software_version" not in ctx["conditions"])
                row["correction_correct"] = (ctx["conditions"].get("product_model", {}).get("value") == "AX200"
                    if scenario == "correction_stale_arguments" else ctx["unresolved"] == ["product_model"]
                    and not ctx["conditions"])
            elif scenario == "correction_late_reply":
                first = VoicePlan(question="Find the documented connection limit for model AX100, version 2.1.", hold_reply=True)
                plans["first"] = first
                await call.speak(first)
                await call.until("portal.speech_text.done", lambda e: e["payload"]["phase"] == "status")
                await call.until("portal.audio.done", lambda e: e["payload"]["phase"] == "status")

                await asyncio.wait_for(first.output_received.wait(), 5)
                previous_event_count = len(call.events)
                correction = VoicePlan(question="Actually use AX200 instead; what is its documented connection limit?")
                plans["correction"] = correction
                await call.speak(correction)
                turn = await completed_turn(call, correction)
                ctx = await task_snapshot(harness, turn)
                # Inject stale frames with the old call's known response identity.
                first.response_id = "ack-" + first.input_id
                first.reply_release.set()
                await asyncio.wait_for(first.reply_sent.wait(), 5)
                # A following response is a receive-order barrier for the old upstream frames.
                await complete_general(harness, call, VoicePlan(question="Thank you", tool=None, spoken="You are welcome."))
                history = await call.history()
                row["context_correct"] = (turn["answer"]["status"] == "answered"
                    and harness.services.queries[-1]["filters"] == {"document_ids": [], "product_model": "AX200", "software_version": None}
                    and ctx["conditions"]["product_model"]["source"]["input_item_id"] == correction.input_id
                    and "AX100" not in ctx["resolved_request"] and "2.1" not in ctx["resolved_request"])
                row["correction_correct"] = (history["items"][0]["status"] == "superseded"
                    and history["items"][0]["answer"] is None and "Previous knowledge request" in ctx["resolved_request"])
                row["stale_result_leaks"] = sum(e["type"] in ("portal.audio.delta", "portal.speech_text.done", "portal.answer.final")
                    and (e["payload"].get("response_id") == first.response_id
                         or e.get("turn_id") == history["items"][0]["id"]) for e in call.events[previous_event_count:])
                row["stale_output_isolated"] = row["stale_result_leaks"] == 0
            elif scenario in ("ack_does_not_cancel", "explicit_cancel", "stop_preserves_task"):
                harness.services.cuekb_release = asyncio.Event()
                plan = VoicePlan(question="Find the documented connection limit for model AX100.")
                plans["question"] = plan
                await call.speak(plan)
                await call.until("portal.speech_text.done", lambda e: e["payload"]["phase"] == "status")

                async def pending():
                    history = await call.history()
                    return history if history["items"] and harness.services.queries else None

                history = await eventually(pending)
                revision = history["request_revision"]
                pending_turn = history["items"][-1]
                if scenario == "ack_does_not_cancel":
                    native = harness.services.native_connections[-1]
                    await native.send(json.dumps({"type": "input_audio_buffer.speech_started", "item_id": "brief-input"}))
                    await call.until("portal.input.state", lambda e: e["payload"]["state"] == "speaking")
                    row["control_correct"] = (await call.history())["request_revision"] == revision
                    harness.services.cuekb_release.set()
                    row["control_correct"] &= (await completed_turn(call, plan))["status"] == "answered"
                elif scenario == "stop_preserves_task":
                    stopped = await harness.client.post(call.path + "/playback/stop", headers=call.headers,
                        json={"expected_epoch": pending_turn["epoch"], "expected_revision": revision})
                    row["control_correct"] = stopped.status_code == 200
                    harness.services.cuekb_release.set()
                    turn = await completed_turn(call, plan)
                    row["control_correct"] &= turn["status"] == "answered" and turn["request_revision"] == revision
                    ctx = await task_snapshot(harness, turn)
                    row["context_correct"] = ctx["conditions"]["product_model"]["value"] == "AX100"
                else:
                    canceled = await harness.client.post(call.path + "/tasks/current/cancel", headers=call.headers,
                        json={"expected_epoch": pending_turn["epoch"], "expected_revision": revision})
                    await call.reader
                    harness.services.cuekb_release.set()
                    await asyncio.gather(*tuple(harness.app.state.coordinator.all_tasks), return_exceptions=True)
                    history = await call.history()
                    row["control_correct"] = (canceled.status_code == 200 and canceled.json()["status"] == "canceled"
                        and history["items"][0]["status"] == "canceled" and history["items"][0]["answer"] is None)
                    row["stale_result_leaks"] = len(harness.services.outputs) + sum(
                        e["type"] == "portal.audio.delta" and e["payload"]["response_id"] == plan.response_id for e in call.events)
                    row["cancel_leaks"] = row["stale_result_leaks"]
                    row["stale_output_isolated"] = row["stale_result_leaks"] == 0
            else:
                raise ValueError("Unknown corpus scenario")
            row["status"] = "completed"
        except Exception as exc:
            row["status"], row["error_type"] = "failed", type(exc).__name__
        finally:
            for step_id, plan in plans.items():
                item = await stored_input(harness, call, plan)
                async with harness.app.state.store.sessions() as db:
                    turn = await db.get(Turn, item.turn_id) if item and item.turn_id else None
                if item:
                    row["tool_observations"].append({"step_id": step_id, "actual_tool": turn.selected_tool if turn else None})
            row["observed_counts"] = {"cuekb_requests": len(harness.services.queries),
                                      "external_model_calls": len(harness.services.model_calls)}
            await call.close()
    return row


async def evaluate(output):
    manifest = json.loads(MANIFEST.read_text())
    rows = []
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="continuous-dialogue-") as directory:
        for case in manifest["cases"]:
            row = await run_case(case, Path(directory) / (case["case_id"] + ".db"))
            rows.append(row)
            print(case["case_id"] + ": " + row["status"], flush=True)
    (output / "results.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    report = summarize(rows, manifest, mode="simulation")
    report["manifest_sha256"] = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report["complete"] and all(row["status"] == "completed" for row in rows) and all(
        metric["evaluated"] == metric["passed"] for metric in report["metrics"].values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/continuous-dialogue")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(evaluate(args.output)) else 1)


if __name__ == "__main__":
    main()
