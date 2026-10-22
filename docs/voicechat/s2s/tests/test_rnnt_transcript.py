"""CPU regressions for the actual archived RNNT display method.

Reference: Speech 097dfe9, RNNTDecoder.predict and GreedyRNNTInfer._greedy_decode.
The small, controlled LSTM/joint prove the decoder-state contract and a prefix
counterexample; they are not trained speech recognition or GPU accuracy tests.
"""

import ast
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest
import torch


class Predictor(torch.nn.Module):
    """Relevant RNNTDecoder.predict semantics with a real recurrent predictor."""

    def __init__(self, vocab_size):
        super().__init__()
        self.embed = torch.nn.Embedding(vocab_size, 1, dtype=torch.float64)
        self.rnn = torch.nn.LSTM(1, 1, batch_first=True, dtype=torch.float64)
        self.calls = []
        with torch.no_grad():
            for parameter in self.parameters():
                parameter.zero_()
            self.rnn.bias_ih_l0.fill_(0.8)

    def predict(self, y=None, state=None, add_sos=True, batch_size=1):
        self.calls.append((y is None, add_sos))
        values = self.embed(y) if y is not None else torch.zeros(batch_size, 1, 1, dtype=torch.float64)
        if add_sos:
            values = torch.cat((torch.zeros(batch_size, 1, 1, dtype=values.dtype), values), dim=1)
        return self.rnn(values, state)


class Joint(torch.nn.Module):
    def __init__(self, vocab_size):
        super().__init__()
        self.anchor = torch.nn.Parameter(torch.zeros((), dtype=torch.float64))
        self.vocab_size = vocab_size
        self._num_classes = vocab_size + 1

    def joint(self, frame, prediction):
        logits = torch.full((1, 1, 1, self.vocab_size + 1), -100.0, dtype=torch.float64)
        if frame[0, 0, 0].item() == 0:
            logits[..., self.vocab_size] = 100.0
        else:
            label, threshold = int(frame[0, 0, 1].item()), frame[0, 0, 2]
            logits[..., label] = 0.0
            logits[..., self.vocab_size] = prediction[0, -1, 0] - threshold
        return logits


def harness(vocab, legacy=False):
    path = Path(__file__).parents[2] / "opt/tritonserver/backends/nemotron-voicechat/model.py"
    tree = ast.parse(path.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TritonPythonModel")
    names = {"_new_rnnt_display_state", "_rnnt_display_decode_text", "_rnnt_display_decode_frame"}
    methods = [copy.deepcopy(n) for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    if legacy:
        # Reproduce the former bootstrap without changing the input, reference,
        # expected tokens or any later predictor transition.
        count = 0
        for node in ast.walk(methods[-1]):
            if (
                not isinstance(node, ast.Call)
                or not isinstance(node.func, ast.Attribute)
                or node.func.attr != "predict"
            ):
                continue
            if any(
                k.arg == "y" and isinstance(k.value, ast.Constant) and k.value.value is None
                for k in node.keywords
            ):
                next(k for k in node.keywords if k.arg == "add_sos").value = ast.Constant(value=True)
                count += 1
        assert count == 1
    namespace = {
        "torch": torch,
        "VOICECHAT_DIAGNOSTICS": False,
        "RNNT_DISPLAY_MAX_SYMBOLS": 10,
        "RNNT_DISPLAY_GATE_FRAMES": 3,
        "pb_utils": SimpleNamespace(
            Logger=SimpleNamespace(log_verbose=lambda _: None, log_warning=lambda _: None)
        ),
    }
    module = ast.Module(
        body=[ast.ClassDef(name="Display", bases=[], keywords=[], body=methods, decorator_list=[])],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    model = namespace["Display"]()
    predictor, joint = Predictor(len(vocab)), Joint(len(vocab))
    model.model = SimpleNamespace(
        rnnt_decoder=SimpleNamespace(
            decoding=SimpleNamespace(decoder=predictor, joint=joint), blank_id=len(vocab)
        )
    )
    model._rnnt_vocab, model._rnnt_punct_ids, model._rnnt_punct_ids_set = vocab, [], set()
    state = SimpleNamespace(
        seq_id=1, decoder_global_step=0, rnnt_display_state=None, rnnt_display_emitted_len=0
    )
    model._seq_mgr = {1: state}
    return model, predictor, joint


def acoustic_frames(predictor, labels):
    with torch.no_grad():
        outputs, _ = predictor.rnn(torch.zeros(1, len(labels) + 2, 1, dtype=torch.float64))
    frames = [[0.0, 0.0, 0.0]] * 3  # Blank lead-in must not advance predictor state.
    frames += [
        [1.0, float(label), float((outputs[0, i, 0] + outputs[0, i + 1, 0]) / 2)]
        for i, label in enumerate(labels)
    ]
    return torch.tensor([frames], dtype=torch.float64)


def reference_greedy(predictor, joint, frames):
    """NeMo reference recurrence: commit state only on a non-blank label."""
    state, last_label, tokens = None, None, []
    for index in range(frames.shape[1]):
        for _ in range(10):
            label = None if last_label is None else torch.tensor([[last_label]], dtype=torch.long)
            prediction, next_state = predictor.predict(label, state, add_sos=False)
            token = int(joint.joint(frames[:, index : index + 1], prediction).argmax(-1).item())
            if token == joint.vocab_size:
                break
            tokens.append(token)
            state, last_label = next_state, token
    return tokens


def actual_decode(model, frames):
    tokens = []
    for index in range(frames.shape[1]):
        tokens += model._rnnt_display_decode_frame(frames, 0, 1, index)
        model._seq_mgr[1].decoder_global_step += 1
    return tokens


def test_single_zero_bootstrap_matches_real_lstm_state_and_blank_leadin():
    model, predictor, _ = harness(["Hell", "o"])
    frames = torch.zeros(1, 3, 3, dtype=torch.float64)
    assert actual_decode(model, frames) == []
    assert predictor.calls == [(True, False)]
    _, expected = predictor.rnn(torch.zeros(1, 1, 1, dtype=torch.float64))
    _, doubled = predictor.rnn(torch.zeros(1, 2, 1, dtype=torch.float64))
    actual = model._seq_mgr[1].rnnt_display_state["pred_hidden"]
    assert all(torch.equal(left, right) for left, right in zip(actual, expected, strict=True))
    assert any(not torch.equal(left, right) for left, right in zip(actual, doubled, strict=True))


@pytest.mark.parametrize(
    "vocab",
    [
        ["Hell", "o", "▁how", "▁are", "▁you", "▁today"],
        ["▁What", "▁is", "▁the", "▁difference", "▁between", "▁TCP", "▁and", "▁UDP"],
    ],
)
def test_transcript_labels_match_nemo_greedy_from_the_first_label(vocab):
    model, predictor, joint = harness(vocab)
    frames = acoustic_frames(predictor, list(range(len(vocab))))
    expected = reference_greedy(copy.deepcopy(predictor), joint, frames)
    assert expected == list(range(len(vocab)))
    assert actual_decode(model, frames) == expected


def test_legacy_double_zero_can_drop_hell_without_dropping_audio_frames():
    vocab = ["Hell", "o", "▁how", "▁are", "▁you", "▁today"]
    fixed, predictor, joint = harness(vocab)
    legacy, _, _ = harness(vocab, legacy=True)
    frames = acoustic_frames(predictor, list(range(len(vocab))))
    before = frames.clone()
    expected = reference_greedy(copy.deepcopy(predictor), joint, frames)
    old_tokens = actual_decode(legacy, frames)
    assert old_tokens == expected[1:]
    assert legacy._rnnt_display_decode_text(old_tokens) == "o how are you today"
    assert fixed._rnnt_display_decode_text(actual_decode(fixed, frames)) == "Hello how are you today"
    assert torch.equal(frames, before)
