"""Actual backend lifecycle methods, controlled engine outputs, no GPU."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import torch

BACKEND = Path(__file__).parents[2] / "opt/tritonserver/backends/nemotron-voicechat/model.py"


def harness(finished=False, absent=False, empty=False, abort_fails=False, abort_hangs=False):
    names = {"_get_vllm_llm_next_token", "_vllm_llm_cleanup", "_finalize_sequence", "execute"}
    cls = next(n for n in ast.parse(BACKEND.read_text()).body if isinstance(n, ast.ClassDef) and n.name == "TritonPythonModel")
    methods = [n for n in cls.body if getattr(n, "name", None) in names]
    for method in methods:
        method.decorator_list = []
    messages, appended, cleaned = [], [], []
    pb = SimpleNamespace(Logger=SimpleNamespace(log_info=messages.append, log_error=messages.append, log_verbose=lambda _: None),
                         get_input_tensor_by_name=lambda request, name: torch.tensor([request.end]),
                         Tensor=lambda name, data: (name, data), InferenceResponse=lambda **kwargs: kwargs)
    namespace = {"torch": torch, "List": list, "np": np, "asyncio": asyncio, "pb_utils": pb, "from_dlpack": lambda value: value}
    module = ast.Module(body=[ast.ClassDef(name="Backend", bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(BACKEND), "exec"), namespace)

    class Engine:
        async def generate(self, *args, **kwargs):
            if not empty:
                yield SimpleNamespace(finished=finished, outputs=[SimpleNamespace(token_ids=[7], custom_outputs={}, finish_reason="length")])

        async def append_request(self, **kwargs):
            appended.append(kwargs["request_id"])
            if absent:
                raise ValueError("request not found")

        async def abort(self, request_id):
            cleaned.append("llm")
            if abort_hangs:
                await asyncio.Event().wait()
            if abort_fails:
                raise ValueError("already ended")

    class Manager(dict):
        def mark_end(self, key):
            self[key].end = True

        def remove(self, key):
            del self[key]

    state = SimpleNamespace(inference_error=None, vllm_llm_generator=None, vllm_llm_start=False,
                            decoder_global_step=123, fn_call={"state": "idle"}, end=False)
    model = namespace["Backend"]()
    model.model = SimpleNamespace(llm=Engine(), llm_sampling_params=None,
                                  llm_custom_input_specs=[SimpleNamespace(dtype="float32")],
                                  tts=SimpleNamespace(stream_abort_request=lambda _: cleaned.append("tts")))
    model._seq_mgr = Manager({1: state})
    model._run_coro = asyncio.run
    model._injection_threads, model._extraction_threads = {}, {}
    model._voice_diagnostic = lambda *args, **kwargs: None
    model._append_rnnt_display_output = lambda *args, **kwargs: None
    state.rnnt_display_pending_text = "unfinished transcript"
    return model, state, appended, cleaned, messages


@pytest.mark.parametrize("empty", [False, True])
def test_finished_engine_never_receives_another_append(empty):
    model, state, appended, _, logs = harness(finished=True, empty=empty)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="VOICE_SESSION_ROTATION_REQUIRED"):
            asyncio.run(model._get_vllm_llm_next_token(1, [1], [torch.zeros(1, 2)]))
    assert not appended and not state.vllm_llm_start
    if not empty:
        assert any("reason=length" in row for row in logs)


def test_missing_engine_request_fails_once_without_replay():
    model, state, appended, _, _ = harness(absent=True)
    assert asyncio.run(model._get_vllm_llm_next_token(1, [1], [torch.zeros(1, 2)]))["token"] == 7
    for _ in range(2):
        with pytest.raises(RuntimeError, match="unavailable"):
            asyncio.run(model._get_vllm_llm_next_token(1, [1], [torch.zeros(1, 2)]))
    assert appended == ["1"] and state.inference_error


@pytest.mark.parametrize("abort_fails,abort_hangs", [(False, False), (True, False), (False, True)])
def test_end_skips_inference_and_cleans_failed_request_idempotently(abort_fails, abort_hangs):
    model, state, _, cleaned, _ = harness(abort_fails=abort_fails, abort_hangs=abort_hangs)
    state.inference_error = "failed"
    active = SimpleNamespace(end=False, correlation_id=lambda: 2)
    end = SimpleNamespace(end=True, correlation_id=lambda: 1)
    model._execute_active = lambda requests: ["active"] if requests == [active] else pytest.fail("wrong active batch")
    results = model.execute([active, end])
    assert results[0] == "active" and len(results[1]["output_tensors"]) == 4
    assert dict(results[1]["output_tensors"])["output_asr_text"].tolist() == [b"unfinished transcript"]
    assert cleaned == ["llm", "tts"] and not model._seq_mgr
    assert state.vllm_llm_generator is None and not state.vllm_llm_start
    model.execute([end])
    assert cleaned == ["llm", "tts"]
