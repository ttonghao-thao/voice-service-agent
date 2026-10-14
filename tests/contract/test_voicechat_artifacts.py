import json
import struct

import pytest

from scripts.inspect_voicechat_artifacts import checkpoint_header, inspect


def artifacts(tmp_path, *, function_outputs=True, function_head=True):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"hidden_size": 4, "vocab_size": 32,
        "custom_input_specs": [{"name": "combined_embeds", "dim": 4}],
        "custom_outputs": ["text_logits", "function_tokens", "function_logits"] if function_outputs else ["text_logits"]}))
    tokenizer = tmp_path / "tokenizer.json"
    tokenizer.write_text(json.dumps({"added_tokens": [
        {"content": f"<SPECIAL_{value}>", "id": value} for value in (20, 21, 22)]}))
    checkpoint = tmp_path / "model.safetensors"
    header = {"stt_model.lm_head.weight": {"shape": [32, 4], "dtype": "F32", "data_offsets": [0, 512]}}
    if function_head:
        header["stt_model.function_head.weight"] = {"shape": [32, 4], "dtype": "F32", "data_offsets": [512, 1024]}
    raw = json.dumps(header).encode()
    checkpoint.write_bytes(struct.pack("<Q", len(raw)) + raw + bytes(1024))
    return config, tokenizer, [checkpoint]


@pytest.mark.parametrize("function_outputs,function_head", [(True, True), (False, True), (True, False)])
def test_distinguishes_missing_head_from_missing_runtime_output_contract(tmp_path, function_outputs, function_head):
    report = inspect(*artifacts(tmp_path, function_outputs=function_outputs, function_head=function_head))
    assert report["checks"]["function_outputs_contract"] == ("passed" if function_outputs else "mismatch")
    assert report["checks"]["function_head_in_supplied_headers"] == ("present" if function_head else "missing")
    assert report["checks"]["atomic_special_token_entries"] == "passed"
    assert "weight_sha256" not in json.dumps(report)


@pytest.mark.parametrize("ids", [[20, 20, 22], [20, 21, 999], [20, 21]])
def test_special_token_mismatch_is_not_reported_healthy(tmp_path, ids):
    config, tokenizer, checkpoints = artifacts(tmp_path)
    tokenizer.write_text(json.dumps({"added_tokens": [
        {"content": f"<SPECIAL_{index + 20}>", "id": value} for index, value in enumerate(ids)]}))
    report = inspect(config, tokenizer, checkpoints)
    assert report["checks"]["special_ids_within_head"] == "unverified"


def test_oversized_header_is_rejected_without_reading_payload(tmp_path):
    checkpoint = tmp_path / "bad.safetensors"
    checkpoint.write_bytes(struct.pack("<Q", 2**63))
    with pytest.raises(ValueError, match="oversized"):
        checkpoint_header(checkpoint)


@pytest.mark.parametrize("changes,alignment,function_weight", [
    ({}, "matches", 2.0),
    ({"duplex_function_channel_weight": None}, "differs", 1.0),
    ({"use_function_head": False}, "differs", 0.0),
    ({"duplex_user_channel_weight": 0.0}, "differs", 2.0),
    ({"fuse_method": "gated_simple"}, "unverified", None),
    ({"duplex_function_channel_weight": "2"}, "unverified", None),
])
def test_paper_fusion_audit_detects_config_loss_without_changing_files(tmp_path, changes, alignment, function_weight):
    config, tokenizer, checkpoints = artifacts(tmp_path)
    stt = {"duplex_user_channel_weight": 1.0, "duplex_text_channel_weight": 1.0,
        "duplex_function_channel_weight": 2.0, "use_function_head": True}
    for name, value in changes.items():
        if value is None:
            stt.pop(name)
        else:
            stt[name] = value
    source = tmp_path / "voicechat.json"
    original = json.dumps({"model": {"stt": {"model": stt}}})
    source.write_text(original)
    report = inspect(config, tokenizer, checkpoints, voicechat_config_path=source)
    fusion = report["voicechat_fusion"]
    assert fusion["paper_weight_alignment"] == alignment
    weights = fusion["weights_in_pinned_wrapper"]
    assert (weights["function"] if weights else None) == function_weight
    if "duplex_function_channel_weight" not in stt:
        assert "duplex_function_channel_weight" in fusion["defaulted_fields"]
    assert source.read_text() == original


def test_wrong_config_path_cannot_attest_fusion(tmp_path):
    config, tokenizer, checkpoints = artifacts(tmp_path)
    report = inspect(config, tokenizer, checkpoints, voicechat_config_path=config)
    assert report["voicechat_fusion"]["paper_weight_alignment"] == "unverified"
