"""CPU extraction of real model boundary methods; no Torch/NeMo/GPU import."""
import ast
import json
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest


def model_harness(enabled=True):
    source = Path(__file__).parents[2] / "opt/tritonserver/backends/nemotron-voicechat/model.py"
    tree = ast.parse(source.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TritonPythonModel")
    names = {"_voice_diagnostic", "_trace_function_tokens", "_new_rnnt_display_state", "_reset_rnnt_display_buffer",
             "_append_rnnt_display_output", "_apply_turn_taking_from_rnnt_states"}
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(methods) == len(names)
    logs = []
    logger = SimpleNamespace(log_info=logs.append, log_verbose=lambda _: None)
    namespace = {"VOICECHAT_DIAGNOSTICS": enabled, "threading": threading, "time": time,
                 "deque": deque, "json": json, "pb_utils": SimpleNamespace(Logger=logger),
                 "RNNT_DISPLAY_FALLBACK_CLEAR_FRAMES": 100, "FORCE_TURN_TAKING_THRESHOLD": 5,
                 "RNNT_BOU_MIN_FRAMES_FIRST_TURN": 2, "RNNT_DENSITY_THRESHOLD": .2,
                 "RNNT_DENSITY_LOW_MIN": 3, "RNNT_BOU_MIN_FRAMES": 4,
                 "RNNT_NOISE_RESET_FRAMES": 100, "RNNT_EOS_SILENCE_FRAMES": 2,
                 "RNNT_MAX_AGENT_RESPONSE_FRAMES": 0, "RNNT_TTS_RATIO_CAP": 0,
                 "RNNT_BARGE_IN_FRAMES": 10, "RNNT_EOU_EXTRA_PAD_THRESHOLD": 100}
    isolated = ast.Module(body=[ast.ClassDef(name="Boundary", bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[])
    exec(compile(ast.fix_missing_locations(isolated), str(source), "exec"), namespace)
    model = namespace["Boundary"]()
    model._rnnt_display_decode_text = lambda tokens: "".join(tokens)
    model.model = SimpleNamespace(tokenizer=SimpleNamespace(bos_id=1, eos_id=2))
    model.pad_id = 0
    model.sotc_id = 10
    model.eotc_id = 12
    return model, logs


def state():
    return SimpleNamespace(seq_id=42, decoder_global_step=0, voice_diagnostics={},
                           rnnt_display_state={"y_sequence": [], "speech_confirmed": True,
                                               "blank_count": 0, "nonblank_total": 1},
                           rnnt_display_emitted_len=0, rnnt_display_turn_open=False,
                           rnnt_display_pending_text="")


def test_diagnostic_limits_count_bytes_and_never_mutate_stored_entries():
    model, logs = model_harness()
    seq = state()
    for index in range(500):
        model._voice_diagnostic(seq, "test.boundary", index=index, reason="r" * 200)
    diag = seq.voice_diagnostics
    assert len(diag["events"]) <= 256 and diag["bytes"] <= 65536 and diag["dropped"] > 0
    assert diag["bytes"] == sum(len(json.dumps(entry, ensure_ascii=True).encode()) for _, entry in diag["events"])
    assert len(json.dumps([entry for _, entry in diag["events"]], ensure_ascii=True).encode()) <= 65536
    assert json.loads(logs[-1].split(" ", 1)[1])["dropped_events"] == diag["dropped"]


@pytest.mark.parametrize("enabled", [False, True])
def test_raw_function_categories_and_counts_use_actual_tokenizer_bos(enabled):
    model, logs = model_harness(enabled)
    seq = state()
    seq.fn_call = {"state": "process_response"}
    for token in [0, 0, 10, 11, 12]:
        model._trace_function_tokens(seq, token, 1 if token == 12 else 0)
    if enabled:
        records = [json.loads(line.split(" ", 1)[1]) for line in logs]
        assert [e["kind"] for e in records] == ["pad", "sotc", "other", "eotc"]
        assert records[-1]["counts"] == {"pad": 2, "sotc": 1, "eotc": 1, "other": 1}
        assert records[-1]["text_bos"] and records[-1]["state"] == "process_response"
    else:
        assert seq.voice_diagnostics == {} and logs == []


@pytest.mark.parametrize("enabled", [False, True])
def test_diagnostics_preserve_complete_display_delta_and_bos_reset(enabled):
    model, logs = model_harness(enabled)
    seq = state()
    seq.rnnt_display_state["y_sequence"] = ["HELLO "]
    model._append_rnnt_display_output(seq, False)
    seq.rnnt_display_state["y_sequence"] += ["THAO MOONCAKE"]
    model._append_rnnt_display_output(seq, True)
    assert seq.rnnt_display_pending_text == "<s>HELLO THAO MOONCAKE</s>"
    assert seq.rnnt_display_state["y_sequence"] == [] and seq.rnnt_display_emitted_len == 0
    assert all("HELLO" not in line and "MOONCAKE" not in line for line in logs)
    if enabled:
        records = [json.loads(line.split(" ", 1)[1]) for line in logs]
        final = next(e for e in records if e["event"] == "asr.final")
        assert final["final_chars"] == 19 and final["reason"] == "agent_bos"
        assert records[-1]["event"] == "asr.reset" and records[-1]["cumulative_chars"] == 19
    else:
        assert seq.voice_diagnostics == {} and logs == []


@pytest.mark.parametrize("source,token,fn_state", [("model_native", 1, "idle"), ("tool_ack", 1, "speaking_ack"), ("forced_eou", 0, "idle")])
def test_bos_source_records_user_state_before_turn_mutation(source, token, fn_state):
    model, logs = model_harness()
    seq = state()
    seq.generated_text_tokens = np.array([[token]], dtype=np.int64)
    seq.fn_call = {"state": fn_state}
    seq.rnnt_rolling_density = .5
    seq.rnnt_first_turn = True
    seq.rnnt_user_speaking = True
    seq.rnnt_agent_speaking = False
    seq.rnnt_silent_frames = 2
    seq.rnnt_nonblank_total = 3
    seq.rnnt_consecutive_speech_frames = 0
    seq.rnnt_turn_text_tokens = seq.rnnt_agent_talking_frames = 0
    model._apply_turn_taking_from_rnnt_states(42, seq)
    records = [json.loads(line.split(" ", 1)[1]) for line in logs]
    bos = next(e for e in records if e["event"] == "agent.bos")
    assert bos["source"] == source and bos["user_speaking_before"]
    assert bos["nonblank_total"] == 3 and not seq.rnnt_user_speaking
    assert seq.generated_text_tokens[0, 0] == 1 and seq.rnnt_agent_speaking
