import logging
import os
from pathlib import Path

import torch


def setup_deterministic_pytorch(seed=42, force_fp32_precision=False):
    """Setup PyTorch for deterministic behavior"""
    import os
    import random

    import numpy as np
    import torch

    # Set seeds
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)

    # Set environment variables
    os.environ["PYTHONHASHSEED"] = str(seed)
    os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"

    # Set PyTorch deterministic mode
    torch.use_deterministic_algorithms(True)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

    # Conditional TF32 settings
    if force_fp32_precision:
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        print("TF32 disabled for full FP32 precision")
    else:
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
        print("TF32 enabled for better performance (may have slight precision differences)")

    print(f"PyTorch deterministic mode enabled with seed {seed}")


DETERMINISTIC = os.environ.get("DETERMINISTIC", "0").lower() in ("1", "true", "yes")
if DETERMINISTIC:
    setup_deterministic_pytorch(seed=0, force_fp32_precision=False)
    torch.set_float32_matmul_precision("medium")
    torch.backends.cuda.enable_flash_sdp(False)
    torch.backends.cuda.enable_mem_efficient_sdp(False)
else:
    torch.backends.cudnn.allow_tf32 = True
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.set_float32_matmul_precision("medium")

logging.getLogger("nemo").setLevel(logging.WARNING)

DTYPE = torch.bfloat16
DEVICE = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

# Configurable via environment variables
MODEL_STEPS_PER_CALL = int(os.environ.get("MODEL_STEPS_PER_CALL", "2"))
MODEL_OUTPUT_SAMPLE_RATE = 22050  # codec output sample rate (Hz)
VOICECHAT_DIAGNOSTICS = os.environ.get("VOICECHAT_DIAGNOSTICS", "0").lower() in ("1", "true", "yes")


def _isolated_capture_config():
    raw_dir = os.environ.get("VOICECHAT_CAPTURE_DIR", "").strip()
    raw_sequence = os.environ.get("VOICECHAT_CAPTURE_SEQUENCE_ID", "").strip()
    if not raw_dir and not raw_sequence:
        return None, None
    if not raw_dir or not raw_sequence:
        raise RuntimeError("VOICECHAT_CAPTURE_DIR and VOICECHAT_CAPTURE_SEQUENCE_ID must be set together")
    root = Path(raw_dir)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise RuntimeError("VOICECHAT_CAPTURE_DIR must be an existing absolute non-symlink directory")
    try:
        sequence_id = int(raw_sequence)
    except ValueError as exc:
        raise RuntimeError("VOICECHAT_CAPTURE_SEQUENCE_ID must be an integer") from exc
    if not 0 < sequence_id < 2**32:
        raise RuntimeError("VOICECHAT_CAPTURE_SEQUENCE_ID must fit an unsigned 32-bit sequence id")
    return root, sequence_id


VOICECHAT_CAPTURE_DIR, VOICECHAT_CAPTURE_SEQUENCE_ID = _isolated_capture_config()
VOICECHAT_CAPTURE_MAX_STEPS = int(os.environ.get("VOICECHAT_CAPTURE_MAX_STEPS", "96"))
VOICECHAT_CAPTURE_MAX_BYTES = int(os.environ.get("VOICECHAT_CAPTURE_MAX_BYTES", str(16 * 1024 * 1024)))
if not 1 <= VOICECHAT_CAPTURE_MAX_STEPS <= 512:
    raise RuntimeError("VOICECHAT_CAPTURE_MAX_STEPS must be between 1 and 512")
if not 1024 <= VOICECHAT_CAPTURE_MAX_BYTES <= 64 * 1024 * 1024:
    raise RuntimeError("VOICECHAT_CAPTURE_MAX_BYTES must be between 1 KiB and 64 MiB")
FORCE_TURN_TAKING_THRESHOLD = int(os.environ.get("FORCE_TURN_TAKING_THRESHOLD", "40"))
DEBUG_MODE = os.environ.get("DEBUG_MODE", "0").lower() in ("1", "true", "yes")
MAX_TOOL_TOKENS = int(os.environ.get("MAX_TOOL_TOKENS", "512"))
# Number of consecutive blank RNNT frames (each ~80ms) before declaring EOS (NeMo: rnnt_eou_frames=40).
RNNT_EOS_SILENCE_FRAMES = int(os.environ.get("RNNT_EOS_SILENCE_FRAMES", "40"))  # 3.2s
# Consecutive non-blank RNNT frames during an active function call before cancelling fast extraction
RNNT_FC_INTERRUPT_FRAMES = int(os.environ.get("RNNT_FC_INTERRUPT_FRAMES", "40"))  # 3.2s
# RNNT transcript display side-channel runs by default when RNNT is available.
# These settings affect UI text only; the original RNNT blank/non-blank stream
# still owns EOU/BOU decisions.
RNNT_DISPLAY_GATE_FRAMES = int(os.environ.get("RNNT_DISPLAY_GATE_FRAMES", "3"))  # 240ms
RNNT_DISPLAY_FALLBACK_CLEAR_FRAMES = int(os.environ.get("RNNT_DISPLAY_FALLBACK_CLEAR_FRAMES", "10"))  # 800ms
RNNT_DISPLAY_MAX_SYMBOLS = int(os.environ.get("RNNT_DISPLAY_MAX_SYMBOLS", "10"))
RNNT_MAX_SYMBOLS = int(os.environ.get("RNNT_MAX_SYMBOLS", "10"))
RNNT_PUNCT_BIAS_ENABLED = os.environ.get("RNNT_PUNCT_BIAS_ENABLED", "1").lower() in ("1", "true", "yes")
RNNT_PUNCT_BIAS_TOKENS = tuple(os.environ.get("RNNT_PUNCT_BIAS_TOKENS", ".,?!"))
RNNT_PUNCT_BIAS_INCREMENT = float(os.environ.get("RNNT_PUNCT_BIAS_INCREMENT", "1.5"))
RNNT_PUNCT_BIAS_MIN_SILENCE_FRAMES = int(os.environ.get("RNNT_PUNCT_BIAS_MIN_SILENCE_FRAMES", "5"))  # 400ms
# Minimum speech frames before confirming a normal user turn (NeMo: asr_min_speech_frames=3).
RNNT_BOU_MIN_FRAMES = int(os.environ.get("RNNT_BOU_MIN_FRAMES", "3"))  # 240ms
# Keep ordinary barge-in independently configurable; TT-2 alignment must not implicitly change TT-4.
RNNT_BARGE_IN_FRAMES = int(os.environ.get("RNNT_BARGE_IN_FRAMES", "40"))  # 3.2s
# Rolling-density EMA parameters (NeMo values).
# density = alpha * is_speech + (1 - alpha) * prev_density
# When density is below RNNT_DENSITY_THRESHOLD (but > 0), require the NeMo
# low-density minimum to avoid confirming sparse noise as user speech.
RNNT_DENSITY_ALPHA = float(os.environ.get("RNNT_DENSITY_ALPHA", "0.1"))
RNNT_DENSITY_THRESHOLD = float(os.environ.get("RNNT_DENSITY_THRESHOLD", "0.15"))
RNNT_DENSITY_LOW_MIN = int(os.environ.get("RNNT_DENSITY_LOW_MIN", "6"))
# Pre-BOU noise immunity: reset nonblank_total after this many consecutive blank frames (NeMo: nonblank_reset_after_silence).
RNNT_NOISE_RESET_FRAMES = int(os.environ.get("RNNT_NOISE_RESET_FRAMES", "10"))
# First-turn BOU threshold: relaxed min-frames on the very first user utterance (NeMo: asr_min_speech_frames_first_turn=2).
RNNT_BOU_MIN_FRAMES_FIRST_TURN = int(os.environ.get("RNNT_BOU_MIN_FRAMES_FIRST_TURN", "2"))
# First-turn fallback: if speech_confirmed but no EOU injected after N steps, inject agent BOS (NeMo: first_turn_fallback_frames=50).
RNNT_FIRST_TURN_FALLBACK_FRAMES = int(os.environ.get("RNNT_FIRST_TURN_FALLBACK_FRAMES", "50"))
# TTS token-ratio cap: force agent EOS when talking_frames >= ratio x text_token_count (NeMo: ratio=16.0, min=5).
RNNT_TTS_RATIO_CAP = float(os.environ.get("RNNT_TTS_RATIO_CAP", "16.0"))
RNNT_TTS_MIN_TOKENS = int(os.environ.get("RNNT_TTS_MIN_TOKENS", "5"))
# Max agent response: absolute frame ceiling regardless of content (NeMo: max_agent_response_sec=0 disables it).
_rnnt_max_resp_sec = float(os.environ.get("RNNT_MAX_AGENT_RESPONSE_SEC", "0"))
RNNT_MAX_AGENT_RESPONSE_FRAMES = int(_rnnt_max_resp_sec / 0.08) if _rnnt_max_resp_sec > 0 else 0
