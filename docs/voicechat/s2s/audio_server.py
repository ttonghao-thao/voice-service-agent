#!/usr/bin/env python3
"""
Nemotron VoiceChat Server - Audio WebSocket with Triton Inference
Handles real-time audio streaming from clients and processes through Triton
Uses FastAPI for WebSocket handling
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import numpy as np
import soxr
import tritonclient.http.aio as httpclient
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from jinja2 import Environment, FileSystemLoader, select_autoescape
from tritonclient.utils import np_to_triton_dtype

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


# Audio configuration
INPUT_CLIENT_SAMPLE_RATE = 24000  # Default client input rate (24kHz) if none negotiated
INPUT_SAMPLE_RATE = 16000  # Model expects 16kHz
MODEL_OUTPUT_SAMPLE_RATE = 22050  # Triton model outputs at 22050Hz
OUTPUT_SAMPLE_RATE = 24000  # Client playback at 24kHz
MIN_INPUT_SAMPLE_RATE = 16000  # Lowest client input rate the server accepts
MAX_INPUT_SAMPLE_RATE = 48000  # Highest client input rate the server accepts
INPUT_AUDIO_FORMAT = "pcm16"  # PCM16 for receiving from client
OUTPUT_AUDIO_FORMAT = "pcm16"  # PCM16 for transmission
AUDIO_FORMAT = "pcm16"  # Internal processing format
BYTES_PER_SAMPLE = 2
CHANNELS = 1

# Triton configuration
TRITON_URL = "localhost:8000"  # Update with your Triton server URL
TRITON_MODEL_NAME = "nemotron-voicechat"  # Update with your model name

# Debug configuration
LOOPBACK_MODE = os.getenv("LOOPBACK_MODE", "false").lower() == "true"  # Set LOOPBACK_MODE=true to bypass Triton
USE_JINJA_TEMPLATE_PROMPT = os.getenv("USE_JINJA_TEMPLATE_PROMPT", "0").lower() in ("1", "true", "yes")
VOICECHAT_DIAGNOSTICS = os.getenv("VOICECHAT_DIAGNOSTICS", "0").lower() in ("1", "true", "yes")
VOICECHAT_CAPTURE_DIR = os.getenv("VOICECHAT_CAPTURE_DIR", "").strip()
VOICECHAT_CAPTURE_SEQUENCE_ID = os.getenv("VOICECHAT_CAPTURE_SEQUENCE_ID", "").strip()
VOICECHAT_CAPTURE_MAX_SECONDS = int(os.getenv("VOICECHAT_CAPTURE_MAX_SECONDS", "30"))


def _capture_configuration():
    """Validate the explicit, single-sequence raw artifact capture configuration."""
    if not VOICECHAT_CAPTURE_DIR and not VOICECHAT_CAPTURE_SEQUENCE_ID:
        return None
    if not VOICECHAT_CAPTURE_DIR or not VOICECHAT_CAPTURE_SEQUENCE_ID:
        raise RuntimeError("VOICECHAT_CAPTURE_DIR and VOICECHAT_CAPTURE_SEQUENCE_ID must be set together")
    root = Path(VOICECHAT_CAPTURE_DIR)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise RuntimeError("VOICECHAT_CAPTURE_DIR must be an existing absolute non-symlink directory")
    try:
        sequence_id = int(VOICECHAT_CAPTURE_SEQUENCE_ID)
    except ValueError as exc:
        raise RuntimeError("VOICECHAT_CAPTURE_SEQUENCE_ID must be an integer") from exc
    if not 0 < sequence_id < 2**32:
        raise RuntimeError("VOICECHAT_CAPTURE_SEQUENCE_ID must fit an unsigned 32-bit sequence id")
    if not 1 <= VOICECHAT_CAPTURE_MAX_SECONDS <= 120:
        raise RuntimeError("VOICECHAT_CAPTURE_MAX_SECONDS must be between 1 and 120")
    return root, sequence_id, VOICECHAT_CAPTURE_MAX_SECONDS


VOICECHAT_CAPTURE = _capture_configuration()

_jinja_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent),
    autoescape=select_autoescape(disabled_extensions=("jinja",)),
    keep_trailing_newline=False,
)
_JINJA_PROMPT_TEMPLATE = _jinja_env.get_template("prompt_template.jinja")

DEFAULT_INSTRUCTIONS = "You are an AI voice assistant developed by NVIDIA. Your name is NVIDIA Voice Chat. Answer in a spoken, conversational style rather than a written one. Do not repeat the same sentence over and over again. Start the conversation by greeting the user."

DEFAULT_INSTRUCTIONS_FC = "You are an AI voice assistant developed by NVIDIA. Your name is NVIDIA Voice Chat. Your job is to be helpful and harmless and have engaging conversations in English. Maintain a warm and friendly tone. Keep the dialogue open and ongoing. Be clear and direct, especially when answering yes or no questions and multiple-choice questions. Avoid long answers unless the user asks you to provide details or context. You must provide diverse responses and rephrase answers if the user asks the same question. DO NOT interrupt the user when they are speaking, let them finish their turn before answering."

# Default template for augmenting function-calling system prompts that only contain <AVAILABLE_TOOLS>...</AVAILABLE_TOOLS>.
# Use {tools_content} as placeholder; literal braces in the template are escaped ({{ }}) for .format(tools_content=...).
TOOLS_TEMPLATE = """
When you receive a request, follow this decision process:
1. Does the request match one of your available tools below? If yes, you MUST call that tool - never answer it directly from your own knowledge, even if you think you know the answer.
2. Is it a general knowledge question (history, science, geography, math, facts, etc.)? If yes, answer directly from your own knowledge - do not call any tool.
3. Does it require an external action or live data that none of your tools cover (e.g. ordering food, sending email)? If yes, politely say you don't have that capability.

NEVER say "I don't have a tool for that" for general knowledge questions you can answer yourself.

DO NOT use any tools when not needed to answer the user's requests, under no circumstance.

You are an expert across history, geography, science, math, literature, biographies, languages, recipes, programming, current affairs, and general knowledge. When the user asks about any of these, answer directly and conversationally from your own knowledge — no <TOOLCALL>.

Call a tool ONLY when the user's request matches one of the tools listed in <AVAILABLE_TOOLS> below. For every other request, do not call any tool — just answer from your knowledge. Never invent or call a tool name that is not literally in <AVAILABLE_TOOLS>.

Tool-call arguments must be values the user spoke. If a required argument is missing, ask the user; never guess.

If a tool call fails or returns an error, do not retry the tool call for the same request. Tell the user that the API has an issue.

You can use the following tools to assist the user if required:
<AVAILABLE_TOOLS>{tools_content}</AVAILABLE_TOOLS>

If you decide to call any tool(s), use the following format:
<TOOLCALL>[{{"name": "tool_name1", "arguments": "tool_args1"}}, {{"name": "tool_name2", "arguments": "tool_args2"}}]</TOOLCALL>

The user will execute tool-calls and return responses from tool(s) in this format:
<TOOL_RESPONSE>[{{"tool_response1"}}, {{"tool_response2"}}]</TOOL_RESPONSE>

Based on the tool responses, you can call additional tools if needed, correct tool calls if any errors are found, or just respond to the user.
"""

# Real-time processing configuration
MAX_INSTRUCTIONS_LENGTH = 32000  # Roughly corresponds to 8k tokens
MAX_QUEUE_SIZE = int(os.environ.get("MAX_QUEUE_SIZE", "100"))  # 1.6 seconds queue buffer
ENABLE_QUEUE_MONITORING = True  # Log when queue is backing up
MODEL_STEPS_PER_CALL = int(os.environ.get("MODEL_STEPS_PER_CALL", "2"))
NUM_CHUNKS_PER_INFERENCE = MODEL_STEPS_PER_CALL  # Align with model processing granularity (2 × 80ms = 160ms)
TRITON_INFER_TIMEOUT = float(os.environ.get("TRITON_INFER_TIMEOUT", "30"))
SESSION_DRAIN_TIMEOUT = float(os.environ.get("SESSION_DRAIN_TIMEOUT", "60"))
MAX_SESSION_DURATION = int(os.environ.get("MAX_SESSION_DURATION", "300"))  # 5 minutes
SEND_OUTPUT_TEXT_EVENTS = (
    os.getenv("SEND_OUTPUT_TEXT_EVENTS", "false").lower() == "true"
)  # Emit response.output_text.* for backward compatibility


class AudioCodec(ABC):
    """Abstract base class for audio codecs"""

    @abstractmethod
    def decode(self, data: bytes) -> np.ndarray:
        """
        Decode audio bytes to float32 PCM.

        Args:
            data: Encoded audio bytes

        Returns:
            Float32 PCM audio normalized to [-1.0, 1.0]
        """
        pass

    @abstractmethod
    def encode(self, audio_float32: np.ndarray) -> None:
        """
        Encode float32 PCM audio (buffers internally).

        Args:
            audio_float32: Float32 audio normalized to [-1.0, 1.0]
        """
        pass

    @abstractmethod
    def has_encoded_data(self) -> bool:
        """Check if there's encoded data ready to be read"""
        pass

    @abstractmethod
    def read_encoded_chunk(self) -> Optional[bytes]:
        """Read next chunk of encoded data, returns None if no data available"""
        pass


class PCM16Codec(AudioCodec):
    """PCM16 audio codec for encoding/decoding"""

    def __init__(self, sample_rate: int = 24000, chunk_size: int = 1920):
        self.sample_rate = sample_rate
        self.chunk_size = chunk_size  # 80ms at 24kHz
        self.encode_buffer = np.array([], dtype=np.float32)

    def decode(self, pcm_bytes: bytes) -> np.ndarray:
        """Decode PCM16 bytes to float32"""
        # Convert int16 PCM to float32 [-1.0, 1.0]
        pcm_int16 = np.frombuffer(pcm_bytes, dtype=np.int16)
        pcm_float32 = pcm_int16.astype(np.float32) / 32768.0
        return pcm_float32

    def encode(self, audio_float32: np.ndarray) -> None:
        """Encode float32 PCM (just buffers for chunking)"""
        # Flatten to 1D
        if audio_float32.ndim > 1:
            audio_float32 = audio_float32.flatten()

        # Add to buffer
        self.encode_buffer = np.concatenate([self.encode_buffer, audio_float32])

    def has_encoded_data(self) -> bool:
        """Check if there's a complete chunk ready"""
        return len(self.encode_buffer) >= self.chunk_size

    def read_encoded_chunk(self) -> Optional[bytes]:
        """Read next PCM16 chunk"""
        if len(self.encode_buffer) < self.chunk_size:
            return None

        # Extract chunk
        chunk = self.encode_buffer[: self.chunk_size]
        self.encode_buffer = self.encode_buffer[self.chunk_size :]

        # Convert float32 [-1.0, 1.0] to int16
        chunk_int16 = np.clip(chunk * 32768.0, -32768, 32767).astype("<i2")
        return chunk_int16.tobytes()

    def flush_encoded_chunk(self) -> bytes | None:
        """Return a final short PCM frame without padding or dropping samples."""
        if not self.encode_buffer.size:
            return None
        chunk = self.encode_buffer
        self.encode_buffer = np.array([], dtype=np.float32)
        return np.clip(chunk * 32768.0, -32768, 32767).astype("<i2").tobytes()

class TritonClientManager:
    """Manages Triton client connection"""

    def __init__(self):
        self.client: Optional[httpclient.InferenceServerClient] = None
        self.url = TRITON_URL

    async def get_client(self) -> httpclient.InferenceServerClient:
        """Get or create Triton client"""
        if self.client is None:
            logger.info(f"Creating Triton client for {self.url}")
            self.client = httpclient.InferenceServerClient(url=self.url, verbose=False)

            try:
                is_ready = await self.client.is_server_ready()
                if not is_ready:
                    raise Exception(f"Triton server at {self.url} is not ready")
                logger.info(f"Triton server is ready")
            except Exception as e:
                logger.error(f"Failed to connect to Triton: {e}")
                await self.client.close()
                self.client = None
                raise

        return self.client

    async def close(self):
        """Close Triton client"""
        if self.client:
            await self.client.close()
            self.client = None


# Global Triton client manager
triton_manager = TritonClientManager()


class InferenceManager:
    """
    Fair round-robin coordinator for Triton inference across all active streams.

    Collects one chunk from each session per round and submits them
    simultaneously via asyncio.gather so Triton can dynamically batch them.
    With NUM_CHUNKS_PER_INFERENCE aligned to MODEL_STEPS_PER_CALL, all
    streams stay in phase (all buffer on odd rounds, all infer on even
    rounds) so no stream blocks another.
    """

    STATS_INTERVAL = 50

    def __init__(self):
        self._sessions: dict[str, "AudioSession"] = {}
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._reset_stats()

    def _reset_stats(self):
        self._inference_times: dict[int, list[float]] = defaultdict(list)
        self._total_inferences = 0

    def start(self):
        if self._task is None or self._task.done():
            self._running = True
            self._task = asyncio.create_task(self._run())
            logger.info("InferenceManager: started")

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("InferenceManager: stopped")

    def register(self, session: "AudioSession"):
        if not self._sessions:
            self._reset_stats()
        self._sessions[session.client_id] = session
        logger.info(f"InferenceManager: registered {session.client_id} " f"({len(self._sessions)} active)")
        self.start()

    def unregister(self, session: "AudioSession"):
        removed = self._sessions.pop(session.client_id, None)
        if removed:
            logger.info(f"InferenceManager: unregistered {session.client_id} " f"({len(self._sessions)} active)")
            if not self._sessions:
                self._log_stats(final=True)

    def record_inference(self, infer_time_ms: float):
        active = len(self._sessions)
        self._inference_times[active].append(infer_time_ms)
        self._total_inferences += 1
        if self._total_inferences % self.STATS_INTERVAL == 0:
            self._log_stats()

    def _log_stats(self, final=False):
        label = "FINAL " if final else ""
        lines = [f"=== {label}Inference Stats (inferences: {self._total_inferences}) ==="]
        for n_streams in sorted(self._inference_times.keys()):
            times = self._inference_times[n_streams]
            arr = np.array(times)
            lines.append(
                f"  {n_streams} stream(s): n={len(times)}, "
                f"mean={arr.mean():.1f}ms, min={arr.min():.1f}ms, "
                f"max={arr.max():.1f}ms, p95={np.percentile(arr, 95):.1f}ms"
            )
        logger.info("\n".join(lines))

    def _collect_batch(self) -> list[tuple["AudioSession", tuple]]:
        """Collect at most one chunk from each session (fair round-robin)."""
        batch = []
        for session in list(self._sessions.values()):
            if not session.running:
                continue
            try:
                chunk = session.inference_queue.get_nowait()
                batch.append((session, chunk))
            except asyncio.QueueEmpty:
                continue
        return batch

    async def _process_batch(self, batch: list[tuple["AudioSession", tuple]]):
        """Submit all chunks concurrently — Triton dynamic-batches them."""
        try:
            results = await asyncio.gather(
                *[
                    session.process_audio_chunk_with_triton(audio_data, event_id)
                    for session, (audio_data, event_id) in batch
                ],
                return_exceptions=True,
            )
        finally:
            for session, _ in batch:
                session.inference_queue.task_done()
        for (session, _), result in zip(batch, results):
            if isinstance(result, (WebSocketDisconnect, ConnectionResetError)):
                logger.info(f"InferenceManager: {session.client_id} disconnected, unregistering")
                self.unregister(session)
                session.running = False
            elif isinstance(result, Exception):
                logger.error(f"InferenceManager: terminating {session.client_id} due to error: {result}")
                self.unregister(session)
                session.running = False
                try:
                    await session.websocket.close(1011, "Internal server error")
                except Exception:
                    pass

    async def _run(self):
        logger.info("InferenceManager: loop started")
        try:
            while self._running:
                batch = self._collect_batch()
                if batch:
                    await self._process_batch(batch)
                else:
                    await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            logger.info("InferenceManager: cancelled")
        except Exception as e:
            logger.error(f"InferenceManager: fatal error: {e}", exc_info=True)
        finally:
            logger.info("InferenceManager: loop stopped")


# Global inference manager (fair round-robin across all streams)
inference_manager = InferenceManager()


class AudioSession:
    """Represents an audio streaming session for a client"""

    def __init__(self, client_id: str, websocket: WebSocket):
        self.client_id = client_id
        self.websocket = websocket
        self.chunks_received = 0
        self.chunks_sent = 0
        self.chunks_dropped = 0
        self._session_end_sent = False
        self.sequence_id = VOICECHAT_CAPTURE[1] if VOICECHAT_CAPTURE else int(uuid.uuid4().int & 0xFFFFFFFF)
        self.triton_client: Optional[httpclient.InferenceServerClient] = None
        self.inference_queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
        self.send_task: Optional[asyncio.Task] = None
        self.output_events: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
        self._audio_send_deadline = 0.0
        self._prompt_task: Optional[asyncio.Task] = None
        self.running = True
        self.do_warmup = False  # Default: False, can be set to True to enable warmup
        self.sequence_started = asyncio.Event()  # Set once sequence_start has been sent to Triton
        self.instructions = None

        # Session configuration (can be updated via session.update)
        self.input_audio_format = "pcm16"
        self.output_audio_format = "pcm16"
        self.input_sample_rate = INPUT_CLIENT_SAMPLE_RATE  # Client sample rate: 24000

        # Initialize audio codecs
        self.input_codec = self._create_codec("pcm16", INPUT_CLIENT_SAMPLE_RATE)
        self.output_codec = self._create_codec("pcm16", OUTPUT_SAMPLE_RATE)

        # Stateful resamplers — preserve filter memory across streaming chunks
        self.input_resampler = soxr.ResampleStream(INPUT_CLIENT_SAMPLE_RATE, INPUT_SAMPLE_RATE, 1, dtype='float32')
        self._input_resampler_finalized = False
        self.output_resampler = soxr.ResampleStream(MODEL_OUTPUT_SAMPLE_RATE, OUTPUT_SAMPLE_RATE, 1, dtype='float32')

        # Buffer for Triton inference (accumulate NUM_CHUNKS_PER_INFERENCE chunks)
        self.triton_input_buffer = np.array([], dtype=np.float32)
        self.triton_chunk_size = int(INPUT_SAMPLE_RATE * 0.08)  # 80ms at 16kHz = 1280 samples
        self.triton_inferences = 0  # Track number of inferences sent
        self.audio_samples_received = 0  # Total decoded input samples (at the client input sample rate)
        self.triton_samples_submitted_16k = 0
        self.triton_samples_completed_16k = 0
        self._diagnostic_started = time.monotonic()
        self._diagnostic_seq = 0
        self._diagnostic_events = deque()
        self._diagnostic_bytes = self._diagnostic_dropped = 0
        self._output_queued_seq = self._output_sent_seq = 0
        self._audio_ranges = {}
        self._input_prefixes = {}
        self._asr_source_hash = hashlib.sha256()
        self._asr_source_chars = 0
        self._capture_dir = None
        self._capture_files = {}
        self._capture_hashes = {}
        self._capture_counts = {}
        self._capture_max_seconds = 0
        if VOICECHAT_CAPTURE:
            root, _, max_seconds = VOICECHAT_CAPTURE
            capture_dir = root / f"sequence-{self.sequence_id}"
            capture_dir.mkdir(mode=0o700, exist_ok=False)
            self._capture_dir = capture_dir
            self._capture_max_seconds = max_seconds
            logger.warning(
                "Isolated raw VoiceChat capture enabled for sequence %s in %s; use only authorized test audio",
                self.sequence_id,
                capture_dir,
            )

        # Accumulated transcripts
        self.accumulated_user_transcript = []
        self.accumulated_agent_transcript = []

        # Pipecat / OpenAI Realtime protocol state
        self._response_active = False  # True while agent is generating a response
        self._response_has_speech = False
        self._speech_active = False  # True while user is speaking
        self._response_id: Optional[str] = None
        self._output_item_id: Optional[str] = None
        self._input_item_id: Optional[str] = None
        self._response_input_item_id = None
        self._response_call_ids = []
        self._response_related_inputs = []
        self._function_inputs = {}
        self._awaiting_tool_answers = {}
        self._answer_inputs = []

        # Function calling state variables
        self.function_call_output = deque()
        self.function_batches = {}
        self.function_call_batches = {}
        self.tools = "[]"

    def _create_codec(self, format: str, sample_rate: int) -> AudioCodec:
        """Create appropriate codec based on format"""
        if format == "pcm16":
            return PCM16Codec(sample_rate)
        raise ValueError(f"Unsupported audio format: {format}")

    def _capture_array(self, name: str, values: np.ndarray, sample_rate: int) -> None:
        if self._capture_dir is None:
            return
        array = np.asarray(values, dtype="<f4").reshape(-1)
        payload = array.tobytes(order="C")
        current = self._capture_counts.get(name, 0)
        if current + len(payload) > self._capture_max_seconds * sample_rate * 4:
            raise RuntimeError(f"Isolated capture limit exceeded for {name}")
        path = self._capture_dir / f"{name}.f32le"
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            view = memoryview(payload)
            while view:
                view = view[os.write(descriptor, view) :]
        finally:
            os.close(descriptor)
        digest = self._capture_hashes.setdefault(name, hashlib.sha256())
        digest.update(payload)
        self._capture_counts[name] = current + len(payload)
        self._capture_files[name] = {"sample_rate": sample_rate, "format": "float32_le"}

    def _finish_capture(self, reason: str) -> None:
        if self._capture_dir is None:
            return
        files = {}
        for name, metadata in self._capture_files.items():
            byte_count = self._capture_counts[name]
            files[name] = {
                **metadata,
                "bytes": byte_count,
                "samples": byte_count // 4,
                "sha256": self._capture_hashes[name].hexdigest(),
            }
        summary = {
            "schema_version": 1,
            "scope": "authorized_isolated_voicechat_capture",
            "client_id": self.client_id,
            "sequence_id": self.sequence_id,
            "reason": reason,
            "files": files,
            "triton_submitted_samples_16k": self.triton_samples_submitted_16k,
            "triton_completed_samples_16k": self.triton_samples_completed_16k,
            "remaining_buffer_samples_16k": len(self.triton_input_buffer),
        }
        temporary = self._capture_dir / "ws-summary.json.tmp"
        temporary.write_text(json.dumps(summary, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self._capture_dir / "ws-summary.json")
        self._capture_dir = None

    async def _validate_input_rate(self, new_rate) -> Optional[int]:
        """Validate a client-supplied input rate.

        Returns the accepted rate, or the current rate when none was specified. Emits an
        error event to the client and returns None when the rate is present but non-numeric
        or outside MIN_INPUT_SAMPLE_RATE..MAX_INPUT_SAMPLE_RATE.
        """
        if new_rate is None:
            return self.input_sample_rate
        try:
            rate = int(new_rate)
        except (TypeError, ValueError):
            rate = 0
        if not (MIN_INPUT_SAMPLE_RATE <= rate <= MAX_INPUT_SAMPLE_RATE):
            await self._emit_error(
                error_message=(
                    f"Unsupported input sample rate {new_rate!r}; "
                    f"must be between {MIN_INPUT_SAMPLE_RATE} and {MAX_INPUT_SAMPLE_RATE} Hz"
                ),
                error_type="invalid_request_error",
                error_code="unsupported_sample_rate",
            )
            return None
        return rate

    async def process_loopback_audio(self, pcm_float32_24k: np.ndarray) -> None:
        """
        Process audio in loopback mode - echo back without Triton inference.

        Args:
            pcm_float32_24k: Decoded PCM audio at 24kHz, float32 format
        """
        process_start = time.time()

        num_samples = pcm_float32_24k.shape[-1]
        duration_ms = (num_samples / self.input_sample_rate) * 1000

        if self.chunks_received == 1:
            logger.info(
                f"Client {self.client_id}: [LOOPBACK] First chunk - "
                f"{num_samples} samples, {duration_ms:.1f}ms @ {self.input_sample_rate}Hz, "
                f"shape: {pcm_float32_24k.shape}, dtype: {pcm_float32_24k.dtype}, "
                f"range: [{pcm_float32_24k.min():.3f}, {pcm_float32_24k.max():.3f}]"
            )

        # Encode output (no resampling needed - already 24kHz)
        encode_start = time.time()

        # Clip audio to valid range [-1.0, 1.0] to prevent distortion
        audio_clipped = np.clip(pcm_float32_24k, -1.0, 1.0)

        if self.chunks_received == 1:
            logger.info(
                f"Client {self.client_id}: [LOOPBACK] Encoding - "
                f"shape: {audio_clipped.shape}, dtype: {audio_clipped.dtype}"
            )

        # Encode with proper frame buffering (send_loop will handle sending)
        if not self._response_active:
            await self._emit_response_created()
        self.encode_audio_output(audio_clipped)
        encode_time_ms = (time.time() - encode_start) * 1000

        process_time_ms = (time.time() - process_start) * 1000

        # Log processing timing
        if self.chunks_received == 1:
            logger.info(
                f"Client {self.client_id}: [LOOPBACK] First chunk encoded in {process_time_ms:.1f}ms "
                f"(encode: {encode_time_ms:.1f}ms)"
            )
        elif process_time_ms > duration_ms * 1.5:  # More than 1.5x real-time
            logger.warning(
                f"Client {self.client_id}: [LOOPBACK] Slow encoding: {process_time_ms:.1f}ms "
                f"(audio: {duration_ms:.1f}ms, encode: {encode_time_ms:.1f}ms)"
            )
        elif self.chunks_received % 50 == 0:
            logger.info(
                f"Client {self.client_id}: [LOOPBACK] Encoded {self.chunks_received} chunks "
                f"(avg: {process_time_ms:.1f}ms)"
            )

    def encode_audio_output(self, audio_float32: np.ndarray) -> None:
        """
        Encode audio for output using the configured output codec.
        Encoded data will be read by send_loop.

        Args:
            audio_float32: Float32 audio normalized to [-1.0, 1.0]
        """
        if not self._response_active:
            return
        self.output_codec.encode(audio_float32)
        while (chunk := self.output_codec.read_encoded_chunk()) is not None:
            self._queue_audio_chunk(chunk)

    def _voice_diagnostic(self, event, **fields):
        if not VOICECHAT_DIAGNOSTICS:
            return
        self._diagnostic_seq += 1
        entry = {"seq": self._diagnostic_seq, "event": event, "client_id": self.client_id,
                 "sequence_id": self.sequence_id,
                 "at_ms": int((time.monotonic() - self._diagnostic_started) * 1000),
                 "decoded_input_samples": self.audio_samples_received, "input_rate": self.input_sample_rate,
                 "triton_submitted_samples_16k": self.triton_samples_submitted_16k,
                 "triton_completed_samples_16k": self.triton_samples_completed_16k,
                 "triton_buffer_samples_16k": len(self.triton_input_buffer),
                 "inference_queue": self.inference_queue.qsize(),
                 "dropped_input_chunks": self.chunks_dropped,
                 "input_item_id": self._input_item_id, **fields}
        size = len(json.dumps(entry, ensure_ascii=True).encode())
        self._diagnostic_events.append((size, entry))
        self._diagnostic_bytes += size
        while len(self._diagnostic_events) > 256 or self._diagnostic_bytes + 2 * len(self._diagnostic_events) > 65536:
            removed_size, _ = self._diagnostic_events.popleft()
            self._diagnostic_bytes -= removed_size
            self._diagnostic_dropped += 1
        logger.info("voice_ws_diagnostic %s", json.dumps(
            {**entry, "dropped_events": self._diagnostic_dropped}, ensure_ascii=True))

    def _input_prefix(self, stream: str, values: np.ndarray, sample_rate: int) -> None:
        """Fingerprint only the first four seconds; never retain or log audio."""
        if not VOICECHAT_DIAGNOSTICS:
            return
        state = self._input_prefixes.setdefault(stream, {"samples": 0, "hash": hashlib.sha256()})
        limit = sample_rate * 4
        if state["samples"] >= limit:
            return
        samples = np.asarray(values).reshape(-1)[:limit - state["samples"]]
        # Match the existing 16 kHz submission quantization. No inference data
        # is modified; both sides of the resampling boundary use PCM16 hashes.
        payload = (samples * 32768.0).astype("<i2").tobytes()
        state["hash"].update(payload)
        state["samples"] += samples.size
        if state["samples"] == limit:
            self._voice_diagnostic("input.prefix", stream=stream, sample_rate=sample_rate,
                                   samples=limit, sha256=state["hash"].hexdigest(), format="pcm16_le")

    def _trace_output_event(self, event, phase, output_seq):
        if not VOICECHAT_DIAGNOSTICS:
            return
        kind = event.get("type", "")
        identifiers = {key: str(event[key])[:128] for key in ("response_id", "item_id", "call_id") if key in event}
        previous = self._audio_ranges.get(phase)
        if previous and kind == "response.output_audio.delta" and previous["identifiers"] == identifiers:
            previous["last_output_seq"] = output_seq
            previous["count"] += 1
            previous["audio_base64_chars"] += len(event.get("delta", ""))
            return
        self._flush_audio_range(phase)
        if kind == "response.output_audio.delta":
            self._audio_ranges[phase] = {"identifiers": identifiers, "first_output_seq": output_seq,
                                         "last_output_seq": output_seq, "count": 1,
                                         "audio_base64_chars": len(event.get("delta", ""))}
        lengths = {key + "_chars": len(event[key]) for key in ("delta", "transcript", "arguments")
                   if isinstance(event.get(key), str)}
        self._voice_diagnostic("output." + phase, output_seq=output_seq, kind=kind, **identifiers, **lengths)

    def _flush_audio_range(self, phase):
        previous = self._audio_ranges.pop(phase, None)
        if previous:
            self._voice_diagnostic("output.audio.range", phase=phase, **previous["identifiers"],
                                   **{k: v for k, v in previous.items() if k != "identifiers"})

    def _queue_output_event(self, event: dict) -> None:
        # Snapshot IDs at production time. A later model turn cannot relabel
        # packets waiting on a slow WebSocket connection. Input transcription
        # events have no response_id; never let None == None attach the previous
        # assistant response's ownership to the next user turn.
        response_id = event.get("response_id")
        if response_id is not None and response_id == self._response_id and self._response_input_item_id:
            event.setdefault("input_item_id", self._response_input_item_id)
            event["tool_call_ids"] = list(self._response_call_ids)
            event["related_input_item_ids"] = list(self._response_related_inputs)
        try:
            self.output_events.put_nowait(event)
        except asyncio.QueueFull as exc:
            raise RuntimeError("Voice output queue exceeded its bounded capacity") from exc
        self._output_queued_seq += 1
        self._trace_output_event(event, "queued", self._output_queued_seq)

    def _queue_audio_chunk(self, chunk: bytes) -> None:
        self._queue_output_event({
            "type": "response.output_audio.delta",
            "event_id": str(uuid.uuid4()),
            "response_id": self._response_id,
            "item_id": self._output_item_id,
            "output_index": 0,
            "content_index": 0,
            "delta": base64.b64encode(chunk).decode("ascii"),
        })

    def _flush_response_audio(self) -> None:
        tail = self.output_resampler.resample_chunk(np.array([], dtype=np.float32), last=True)
        self.encode_audio_output(tail)
        chunk = self.output_codec.flush_encoded_chunk()
        if chunk is not None:
            self._queue_audio_chunk(chunk)

    async def initialize_triton_client(self):
        """Initialize Triton client once per session"""
        if LOOPBACK_MODE:
            logger.info(f"Client {self.client_id}: Loopback mode enabled, skipping Triton client")
            return

        if self.triton_client is None:
            logger.info(f"Client {self.client_id}: Initializing Triton client")
            self.triton_client = await triton_manager.get_client()
            logger.info(f"Client {self.client_id}: Triton client initialized")
            model_config = await self.triton_client.get_model_config(TRITON_MODEL_NAME)
            # Warm up the model with silence
            if self.do_warmup:
                await self.warmup_model()
            else:
                logger.info(f"Client {self.client_id}: Warmup skipped")

    async def warmup_model(self):
        """Send 2 chunks of silence (80ms each) to pre-warm the model and reset KV cache"""
        if self.triton_client is None:
            return

        logger.info(f"Client {self.client_id}: Warming up model with 2 silence chunks...")
        warmup_start = time.time()

        try:
            # Send 2 separate 80ms chunks of silence
            silence_duration = 0.08  # 80ms per chunk
            silence_samples = int(INPUT_SAMPLE_RATE * silence_duration)
            silence_chunk = np.zeros((1, silence_samples), dtype=np.float32)

            # Prepare Triton outputs
            outputs = [
                httpclient.InferRequestedOutput("output_text"),
                httpclient.InferRequestedOutput("output_asr_text"),
                httpclient.InferRequestedOutput("output_function_text"),
                httpclient.InferRequestedOutput("output_audio"),
            ]

            # Send 2 warmup chunks
            for i in range(2):
                inputs = [
                    httpclient.InferInput("audio_signal", silence_chunk.shape, np_to_triton_dtype(silence_chunk.dtype))
                ]
                inputs[0].set_data_from_numpy(silence_chunk)

                await self.triton_client.infer(
                    model_name=TRITON_MODEL_NAME,
                    inputs=inputs,
                    outputs=outputs,
                    request_id=str(uuid.uuid4()),
                    sequence_id=self.sequence_id,
                    sequence_start=(i == 0),  # Only first chunk starts sequence
                    sequence_end=False,
                )

            warmup_time = time.time() - warmup_start
            logger.info(f"Client {self.client_id}: Model warmup completed in {warmup_time:.3f}s (2 chunks)")

        except Exception as e:
            logger.error(f"Client {self.client_id}: Error during model warmup: {e}")

    async def send_system_prompt_to_triton(self, prompt: str):
        """Send system prompt to Triton with a silence chunk, starting the sequence.

        By sending the system prompt during session setup (before audio streams),
        the prompt pre-fill cost is hidden and doesn't inflate TTFR.
        """
        if self.triton_client is None or self.sequence_started.is_set():
            return

        # Must send system prompt as first input to Triton (even if empty)
        prompt = prompt or ""

        logger.info(f"Client {self.client_id}: Pre-sending system prompt to Triton (seq={self.sequence_id})")
        prompt_start = time.time()

        silence_samples = int(INPUT_SAMPLE_RATE * 0.08)  # 80ms
        silence_chunk = np.zeros((1, silence_samples), dtype=np.float32)

        inputs = [
            httpclient.InferInput("audio_signal", silence_chunk.shape, np_to_triton_dtype(silence_chunk.dtype)),
            httpclient.InferInput("system_prompt", (1, 1), "BYTES"),
            httpclient.InferInput("function_response", (1, 1), "BYTES"),
        ]
        inputs[0].set_data_from_numpy(silence_chunk)
        inputs[1].set_data_from_numpy(np.array([[prompt.encode("utf-8")]], dtype=object))
        inputs[2].set_data_from_numpy(np.array([[b""]], dtype=object))

        outputs = [
            httpclient.InferRequestedOutput("output_text"),
            httpclient.InferRequestedOutput("output_asr_text"),
            httpclient.InferRequestedOutput("output_audio"),
        ]

        try:
            await asyncio.wait_for(
                self.triton_client.infer(
                    model_name=TRITON_MODEL_NAME,
                    inputs=inputs,
                    outputs=outputs,
                    request_id=str(uuid.uuid4()),
                    sequence_id=self.sequence_id,
                    sequence_start=True,
                    sequence_end=False,
                ),
                timeout=TRITON_INFER_TIMEOUT,
            )
            self.sequence_started.set()
            prompt_ms = (time.time() - prompt_start) * 1000
            logger.info(f"Client {self.client_id}: System prompt pre-sent in {prompt_ms:.0f}ms")
        except Exception as e:
            logger.error(f"Client {self.client_id}: Failed to pre-send system prompt: {e}")
            self.sequence_started.set()  # Unblock inference path; fallback will start sequence

    async def queue_audio_chunk(self, audio_data: str, event_id: str = None):
        """Decode, resample, and buffer audio.  Only enqueue inference-ready chunks."""
        self.chunks_received += 1

        try:
            audio_bytes = base64.b64decode(audio_data)

            if len(audio_bytes) == 0:
                logger.warning(f"Client {self.client_id}: Received empty audio data")
                return

            if self.chunks_received == 1:
                logger.info(
                    f"Client {self.client_id}: First chunk ({self.input_audio_format}) - {len(audio_bytes)} bytes"
                )

            try:
                pcm_float32_24k = self.input_codec.decode(audio_bytes)
            except Exception as e:
                logger.error(f"Client {self.client_id}: Failed to decode audio: {e}")
                try:
                    self.input_codec = self._create_codec(self.input_audio_format, self.input_sample_rate)
                    pcm_float32_24k = self.input_codec.decode(audio_bytes)
                except Exception as retry_error:
                    logger.error(
                        f"Client {self.client_id}: Failed to decode even after recreating codec: {retry_error}"
                    )
                    return

            if pcm_float32_24k.shape[-1] == 0:
                logger.debug(f"Client {self.client_id}: No PCM data decoded yet, buffering")
                return

            self.audio_samples_received += pcm_float32_24k.shape[-1]
            self._input_prefix("decoded", pcm_float32_24k, self.input_sample_rate)
            self._capture_array("ws-decoded-24k", pcm_float32_24k, self.input_sample_rate)

            if LOOPBACK_MODE:
                await self.process_loopback_audio(pcm_float32_24k)
                return

            audio_float32_16k = self.input_resampler.resample_chunk(pcm_float32_24k)
            self._input_prefix("resampled", audio_float32_16k, INPUT_SAMPLE_RATE)
            self._capture_array("soxr-output-16k", audio_float32_16k, INPUT_SAMPLE_RATE)
            self.triton_input_buffer = np.concatenate([self.triton_input_buffer, audio_float32_16k])
            if self.chunks_received == 1 or self.chunks_received % 12 == 0:
                self._voice_diagnostic("input.progress", received_chunks=self.chunks_received)

            required_samples = self.triton_chunk_size * NUM_CHUNKS_PER_INFERENCE
            if len(self.triton_input_buffer) < required_samples:
                if self.chunks_received == 1:
                    logger.info(
                        f"Client {self.client_id}: Buffering for Triton - need {NUM_CHUNKS_PER_INFERENCE} chunks "
                        f"({required_samples} samples), have {len(self.triton_input_buffer)}"
                    )
                return

            audio_float32_buffered = self.triton_input_buffer[:required_samples]
            self.triton_input_buffer = self.triton_input_buffer[required_samples:]

            audio_int16 = (audio_float32_buffered * 32768.0).astype(np.int16)
            audio_float32 = audio_int16.astype(np.float32) / 32768.0

            if len(audio_float32.shape) == 1:
                audio_float32 = np.expand_dims(audio_float32, axis=0)

            queue_size = self.inference_queue.qsize()
            if ENABLE_QUEUE_MONITORING and queue_size >= MAX_QUEUE_SIZE - 2:
                logger.warning(
                    f"Client {self.client_id}: Queue nearly full ({queue_size}/{MAX_QUEUE_SIZE}), "
                    f"processing may be delayed"
                )

            try:
                self.inference_queue.put_nowait((audio_float32, event_id))
            except asyncio.QueueFull:
                try:
                    self.inference_queue.get_nowait()
                    self.inference_queue.task_done()
                    self.chunks_dropped += 1
                    self.inference_queue.put_nowait((audio_float32, event_id))
                    if self.chunks_dropped % 10 == 1:
                        logger.warning(f"Client {self.client_id}: Dropped {self.chunks_dropped} inference chunks")
                except Exception as e:
                    logger.error(f"Client {self.client_id}: Error managing queue: {e}")

        except Exception as e:
            logger.error(f"Client {self.client_id}: Error preprocessing audio: {e}", exc_info=True)

    async def start_inference_worker(self):
        """Register with global manager for fair round-robin scheduling."""
        logger.info(f"Client {self.client_id}: Registering with inference manager")
        inference_manager.register(self)
        self.send_task = asyncio.create_task(self._send_loop())

    async def drain_inference_queue(self):
        """Enqueue residual input and wait for all accepted audio to finish."""
        required_samples = self.triton_chunk_size * NUM_CHUNKS_PER_INFERENCE

        if not self._input_resampler_finalized:
            flushed = self.input_resampler.resample_chunk(np.array([], dtype=np.float32), last=True)
            self._input_resampler_finalized = True
            if len(flushed):
                self._capture_array("soxr-output-16k", flushed, INPUT_SAMPLE_RATE)
                self.triton_input_buffer = np.concatenate([self.triton_input_buffer, flushed])

        while len(self.triton_input_buffer) >= required_samples:
            audio_float32 = self.triton_input_buffer[:required_samples]
            self.triton_input_buffer = self.triton_input_buffer[required_samples:]
            audio_int16 = (audio_float32 * 32768.0).astype(np.int16)
            audio_float32 = np.expand_dims(audio_int16.astype(np.float32) / 32768.0, axis=0)
            await self.inference_queue.put((audio_float32, None))

        residual_samples = len(self.triton_input_buffer)
        if residual_samples:
            audio_float32 = np.pad(self.triton_input_buffer, (0, required_samples - residual_samples))
            self.triton_input_buffer = np.array([], dtype=np.float32)
            audio_int16 = (audio_float32 * 32768.0).astype(np.int16)
            audio_float32 = np.expand_dims(audio_int16.astype(np.float32) / 32768.0, axis=0)
            await self.inference_queue.put((audio_float32, None))
            logger.debug(
                f"Client {self.client_id}: Padded and queued {residual_samples} residual samples "
                f"to {required_samples}"
            )

        pending = self.inference_queue.qsize()
        logger.debug(
            f"Client {self.client_id}: Draining inference queue "
            f"(pending={pending}, triton_inferences={self.triton_inferences})"
        )
        await asyncio.wait_for(self.inference_queue.join(), timeout=SESSION_DRAIN_TIMEOUT)
        logger.debug(f"Client {self.client_id}: Inference queue drained (triton_inferences={self.triton_inferences})")

    async def stop_inference_worker(self):
        """Unregister from manager and stop send loop."""
        self.running = False
        inference_manager.unregister(self)
        if self.send_task and not self.send_task.done():
            self.send_task.cancel()
            try:
                await self.send_task
            except asyncio.CancelledError:
                pass

    async def _pace_output_audio(self, event):
        """Send PCM by its sample clock, including fast decoder catch-up output.

        Keep this wait in the per-session writer, outside inference scheduling
        and input reception. Idle/network delays never earn burst credit. One
        shared clock also covers adjacent response IDs and final short chunks.
        """
        if event["type"] != "response.output_audio.delta":
            return
        duration = len(base64.b64decode(event["delta"])) / (OUTPUT_SAMPLE_RATE * BYTES_PER_SAMPLE * CHANNELS)
        delay = self._audio_send_deadline - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self._audio_send_deadline = time.monotonic() + duration

    async def _send_loop(self):
        """One ordered writer for audio, transcripts, tool events and turn boundaries."""
        logger.info(f"Client {self.client_id}: Send loop started")
        try:
            while self.running:
                event = await self.output_events.get()
                try:
                    await self._pace_output_audio(event)
                    await asyncio.wait_for(self.websocket.send_text(json.dumps(event)), timeout=2.0)
                    self._output_sent_seq += 1
                    self._trace_output_event(event, "sent", self._output_sent_seq)
                    if event["type"] == "response.output_audio.delta":
                        self.chunks_sent += 1
                        if self.chunks_sent % 50 == 0:
                            logger.info(f"Client {self.client_id}: Sent {self.chunks_sent} audio packets")
                finally:
                    self.output_events.task_done()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"Client {self.client_id}: Output writer failed")
            self.running = False
            await self.websocket.close(code=1011)
        finally:
            self._flush_audio_range("queued")
            self._flush_audio_range("sent")
            self._voice_diagnostic("transport.finished", queued_events=self._output_queued_seq,
                                   sent_events=self._output_sent_seq, output_queue=self.output_events.qsize(),
                                   received_chunks=self.chunks_received, sent_audio_chunks=self.chunks_sent)
            logger.info(f"Client {self.client_id}: Send loop stopped")

    async def send_session_end(self):
        """Send session.end with server-side stats before disconnect."""
        if self._session_end_sent:
            return
        from starlette.websockets import WebSocketState

        if self.websocket.client_state != WebSocketState.CONNECTED:
            return
        stats_msg = {
            "type": "session.end",
            "event_id": str(uuid.uuid4()),
            "stats": {
                "chunks_received": self.chunks_received,
                "chunks_sent": self.chunks_sent,
                "chunks_dropped": self.chunks_dropped,
                "triton_inferences": self.triton_inferences,
                "audio_duration_received_s": round(self.audio_samples_received / self.input_sample_rate, 2),
            },
        }
        try:
            await asyncio.wait_for(self.websocket.send_text(json.dumps(stats_msg)), timeout=5.0)
            self._session_end_sent = True
        except Exception as e:
            logger.warning(f"Client {self.client_id}: Failed to send session.end: {e}")

    def _normalize_format(self, fmt) -> tuple[Optional[str], Optional[int]]:
        """Normalize client format (string or OpenAI-style dict) to (internal format string, rate).

        Accepts:
          - Legacy string: "pcm16"
          - OpenAI structured: {"type": "audio/pcm", "rate": 24000}

        Returns (format_str, rate) where format_str is "pcm16", or (None, None) if unsupported.
        """
        if isinstance(fmt, dict):
            rate = fmt.get("rate")
            fmt_type = fmt.get("type", "")
            if "pcm" in fmt_type:
                return "pcm16", rate
            return None, None
        if isinstance(fmt, str):
            if "pcm" in fmt:
                return "pcm16", None
        return None, None

    def _format_to_structured(self, fmt: str) -> dict:
        """Convert internal format string to structured audio format for session messages."""
        return {"type": "audio/pcm", "rate": 24000}

    async def send_session_created(self):
        """Send session created message to client"""
        message = {
            "type": "session.created",
            "event_id": str(uuid.uuid4()),
            "session": {
                "type": "realtime",
                "id": self.client_id,
                "model": "nemotron-voicechat",
                "modalities": ["audio"],
                "audio": {
                    "input": {"format": self._format_to_structured(self.input_audio_format)},
                    "output": {"format": self._format_to_structured(self.output_audio_format)},
                },
                "instructions": self.get_instructions(),
            },
        }
        try:
            await asyncio.wait_for(self.websocket.send_text(json.dumps(message)), timeout=5.0)
        except asyncio.TimeoutError:
            logger.error(f"Client {self.client_id}: Timeout sending session.created")
            raise
        logger.info(
            f"Client {self.client_id}: Session created - "
            f"sequence_id={self.sequence_id}, input: {self.input_audio_format}, output: {self.output_audio_format}"
        )

    async def _emit_speech_started(self):
        """Emit VAD speech_started (Pipecat/OpenAI Realtime compatibility)."""
        self._speech_active = True
        self._input_item_id = str(uuid.uuid4())
        if self._awaiting_tool_answers or self.function_batches or self.function_call_output:
            if len(self._answer_inputs) >= 128:
                raise ValueError("Too many inputs awaiting a tool answer")
            self._answer_inputs.append(self._input_item_id)
        msg = {
            "type": "input_audio_buffer.speech_started",
            "event_id": str(uuid.uuid4()),
            "audio_start_ms": 0,
            "item_id": self._input_item_id,
        }
        self._queue_output_event(msg)

    async def _emit_speech_stopped(self):
        """Emit VAD speech_stopped (Pipecat/OpenAI Realtime compatibility)."""
        self._speech_active = False
        for msg in [
            {
                "type": "input_audio_buffer.speech_stopped",
                "event_id": str(uuid.uuid4()),
                "audio_end_ms": 0,
                "item_id": self._input_item_id or str(uuid.uuid4()),
            },
        ]:
            self._queue_output_event(msg)

    async def _emit_response_created(self):
        """Emit response.created + output_item.added + content_part.added."""
        if self._response_active:
            return
        self.output_codec = self._create_codec("pcm16", OUTPUT_SAMPLE_RATE)
        self.output_resampler = soxr.ResampleStream(MODEL_OUTPUT_SAMPLE_RATE, OUTPUT_SAMPLE_RATE, 1, dtype="float32")
        self._response_active = True
        self._response_has_speech = False
        self._response_id = str(uuid.uuid4())
        self._response_call_ids = list(self._awaiting_tool_answers)
        self._response_input_item_id = (list(self._awaiting_tool_answers.values())[-1]
                                        if self._awaiting_tool_answers else self._input_item_id)
        self._response_related_inputs = list(dict.fromkeys(
            item for item in [*self._answer_inputs, self._input_item_id]
            if self._awaiting_tool_answers and item and item != self._response_input_item_id))
        self._output_item_id = str(uuid.uuid4())
        for msg in [
            {
                "type": "response.created",
                "event_id": str(uuid.uuid4()),
                "response": {
                    "id": self._response_id,
                    "object": "realtime.response",
                    "status": "in_progress",
                    "status_details": None,
                    "output": [],
                },
            },
            {
                "type": "response.output_item.added",
                "event_id": str(uuid.uuid4()),
                "response_id": self._response_id,
                "output_index": 0,
                "item": {
                    "id": self._output_item_id,
                    "object": "realtime.item",
                    "type": "message",
                    "role": "assistant",
                },
            },
            {
                "type": "response.content_part.added",
                "event_id": str(uuid.uuid4()),
                "response_id": self._response_id,
                "item_id": self._output_item_id,
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "audio"},
            },
        ]:
            self._queue_output_event(msg)

    async def _emit_response_done(self):
        """Emit response.audio.done + content_part.done + output_item.done + response.done."""
        if not self._response_active:
            return
        self._response_active = False
        for msg in [
            {
                "type": "response.output_audio.done",
                "event_id": str(uuid.uuid4()),
                "response_id": self._response_id,
                "item_id": self._output_item_id,
                "output_index": 0,
                "content_index": 0,
            },
            {
                "type": "response.content_part.done",
                "event_id": str(uuid.uuid4()),
                "response_id": self._response_id,
                "item_id": self._output_item_id,
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "audio"},
            },
            {
                "type": "response.output_item.done",
                "event_id": str(uuid.uuid4()),
                "response_id": self._response_id,
                "output_index": 0,
                "item": {
                    "id": self._output_item_id,
                    "object": "realtime.item",
                    "type": "message",
                    "role": "assistant",
                },
            },
            {
                "type": "response.done",
                "event_id": str(uuid.uuid4()),
                "response": {
                    "id": self._response_id,
                    "object": "realtime.response",
                    "status": "completed",
                    "status_details": None,
                    "output": [],
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                        "input_token_details": {"cached_tokens": 0},
                        "output_token_details": {"text_tokens": 0, "audio_tokens": 0},
                    },
                },
            },
        ]:
            self._queue_output_event(msg)
        self._response_id = None
        self._output_item_id = None

    async def _emit_output_text_delta(self, delta: str):
        """Emit response.output_text.delta (backward compatibility)."""
        msg = {
            "type": "response.output_text.delta",
            "event_id": str(uuid.uuid4()),
            "response_id": self._response_id,
            "item_id": self._output_item_id,
            "output_index": 0,
            "content_index": 0,
            "delta": delta,
        }
        self._queue_output_event(msg)

    async def _emit_output_text_done(self, text: str):
        """Emit response.output_text.done (backward compatibility)."""
        msg = {
            "type": "response.output_text.done",
            "event_id": str(uuid.uuid4()),
            "response_id": self._response_id,
            "item_id": self._output_item_id,
            "output_index": 0,
            "content_index": 0,
            "text": text,
        }
        self._queue_output_event(msg)

    async def _emit_audio_transcript_delta(self, delta: str):
        """Emit response.output_audio_transcript.delta."""
        msg = {
            "type": "response.output_audio_transcript.delta",
            "event_id": str(uuid.uuid4()),
            "response_id": self._response_id,
            "item_id": self._output_item_id,
            "output_index": 0,
            "content_index": 0,
            "delta": delta,
        }
        self._queue_output_event(msg)

    async def _emit_audio_transcript_done(self, transcript: str):
        """Emit response.output_audio_transcript.done."""
        msg = {
            "type": "response.output_audio_transcript.done",
            "event_id": str(uuid.uuid4()),
            "response_id": self._response_id,
            "item_id": self._output_item_id,
            "output_index": 0,
            "content_index": 0,
            "transcript": transcript,
        }
        self._queue_output_event(msg)

    async def _emit_input_audio_transcription_delta(self, delta: str):
        """Emit conversation.item.input_audio_transcription.delta."""
        msg = {
            "type": "conversation.item.input_audio_transcription.delta",
            "event_id": str(uuid.uuid4()),
            "item_id": self._input_item_id or str(uuid.uuid4()),
            "content_index": 0,
            "delta": delta,
        }
        self._queue_output_event(msg)

    async def _emit_input_audio_transcription_completed(self, transcript: str):
        """Emit conversation.item.input_audio_transcription.completed."""
        msg = {
            "type": "conversation.item.input_audio_transcription.completed",
            "event_id": str(uuid.uuid4()),
            "item_id": self._input_item_id or str(uuid.uuid4()),
            "content_index": 0,
            "transcript": transcript,
        }
        self._queue_output_event(msg)

    async def _dispatch_asr_text(self, asr_text_response: str):
        """Send one ASR output chunk to the websocket client."""
        if not asr_text_response:
            return

        has_start = "<s>" in asr_text_response
        has_done = "</s>" in asr_text_response
        clean_asr_text = asr_text_response.replace("<s>", "").replace("</s>", "")
        if VOICECHAT_DIAGNOSTICS:
            self._asr_source_hash.update(clean_asr_text.encode("utf-8"))
            self._asr_source_chars += len(clean_asr_text)
        self._voice_diagnostic("asr.dispatch", raw_chars=len(asr_text_response), clean_chars=len(clean_asr_text),
                               has_start=has_start, has_done=has_done, speech_active=self._speech_active,
                               accumulated_before_chars=sum(map(len, self.accumulated_user_transcript)))
        logger.debug(
            f"Client {self.client_id}: ASR dispatch chars={len(asr_text_response)} "
            f"clean_chars={len(clean_asr_text)} has_start={has_start} "
            f"has_done={has_done} speech_active={self._speech_active}"
        )

        if has_start and not self._speech_active:
            logger.debug(f"Client {self.client_id}: emitting speech_started for ASR turn")
            await self._emit_speech_started()

        if clean_asr_text:
            logger.debug(f"Client {self.client_id}: emitting transcription delta chars={len(clean_asr_text)}")
            await self._emit_input_audio_transcription_delta(clean_asr_text)
            self.accumulated_user_transcript.append(clean_asr_text)

        if has_done and self._speech_active:
            full_transcript = "".join(self.accumulated_user_transcript)
            self._voice_diagnostic("asr.completed", chars=len(full_transcript),
                                   sha256=hashlib.sha256(full_transcript.encode("utf-8")).hexdigest(),
                                   source_chars_total=self._asr_source_chars,
                                   source_sha256_total=self._asr_source_hash.hexdigest())
            logger.debug(f"Client {self.client_id}: emitting transcription completed chars={len(full_transcript)}")
            await self._emit_input_audio_transcription_completed(full_transcript)
            logger.debug(f"Client {self.client_id}: emitting speech_stopped for ASR turn")
            await self._emit_speech_stopped()
            self.accumulated_user_transcript = []

    async def _emit_error(
        self, error_message: str, error_type: str = "invalid_request_error", error_code: str = "invalid_event"
    ):
        """Emit error message to client."""
        msg = {
            "type": "error",
            "event_id": str(uuid.uuid4()),
            "error": {"type": error_type, "code": error_code, "message": error_message, "param": None,},
        }
        await self.websocket.send_text(json.dumps(msg))

    def get_instructions(self, user_instructions=None, tools=None) -> str:
        env_instructions = os.environ.get("DEFAULT_INSTRUCTIONS")
        if user_instructions is not None:
            return user_instructions
        if env_instructions is not None:
            return env_instructions
        if tools and tools not in ("[]", ""):
            return DEFAULT_INSTRUCTIONS_FC
        return DEFAULT_INSTRUCTIONS

    @staticmethod
    def build_prompt(instructions: str, tools: str) -> str:
        """Render the system prompt by merging instructions and tools via the Jinja template.

        Args:
            instructions: The system prompt text.
            tools: JSON-encoded list of OpenAI-format tool definitions.

        Returns:
            The rendered prompt string to send to Triton.
        """
        prompt = ""
        tools_list = []
        if USE_JINJA_TEMPLATE_PROMPT:
            logger.info("Preparing prompt using jinja template")
            try:
                tools_list = json.loads(tools) if tools and tools not in ("[]", "") else []
            except json.JSONDecodeError:
                logger.warning(f"Invalid tools JSON, using empty list: {tools}")
                tools_list = []
            prompt = _JINJA_PROMPT_TEMPLATE.render(system_message=instructions or "", tools=tools_list or None).strip()
        else:
            if instructions:
                prompt += instructions
            if tools and tools not in ("[]", ""):
                try:
                    tools_list = json.loads(tools)
                except json.JSONDecodeError:
                    logger.warning(f"Invalid tools JSON, skipping tools: {tools}")
                    return prompt
                clean_tools = []
                ack_list = []
                for tool in tools_list:
                    t = tool.get("function", tool)
                    clean = {k: v for k, v in t.items() if k not in ("type", "ack_messages")}
                    clean_tools.append(clean)
                    if t.get("ack_messages"):
                        ack_list.append({"name": t.get("name"), "ack_messages": t["ack_messages"]})
                if prompt:
                    prompt += "\n\n"
                clean_tools_json = "[" + ", ".join(json.dumps(t, ensure_ascii=False) for t in clean_tools) + "]"
                prompt += TOOLS_TEMPLATE.format(tools_content=clean_tools_json)
                if ack_list:
                    prompt += (
                        "\n<TOOL_ACK_MESSAGES>" + json.dumps(ack_list, ensure_ascii=False) + "</TOOL_ACK_MESSAGES>"
                    )

        tool_names = []
        for tool in tools_list:
            definition = tool.get("function", tool) if isinstance(tool, dict) else {}
            name = definition.get("name") if isinstance(definition, dict) else None
            if isinstance(name, str):
                tool_names.append(name)
        logger.info(
            "Prompt prepared: tools_count=%s tool_names=%s prompt_chars=%s prompt_sha256=%s",
            len(tool_names),
            ",".join(tool_names) or "none",
            len(prompt),
            hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        )
        return prompt

    async def process_audio_chunk_with_triton(self, audio_float32: np.ndarray, event_id: str = None):
        """
        Send an inference-ready audio chunk to Triton and handle the response.

        Args:
            audio_float32: Preprocessed float32 array of shape (1, num_samples) ready for Triton
            event_id: Optional event ID from the client
        """
        try:
            num_samples = audio_float32.shape[1]
            send_duration_ms = (num_samples / INPUT_SAMPLE_RATE) * 1000
            chunk_budget_ms = NUM_CHUNKS_PER_INFERENCE * 80

            # Wait for the background system-prompt call to finish before first inference
            if not self.sequence_started.is_set():
                await asyncio.wait_for(self.sequence_started.wait(), timeout=TRITON_INFER_TIMEOUT)

            if self.triton_inferences == 0:
                logger.info(
                    f"Client {self.client_id}: Sending to Triton - "
                    f"{num_samples} samples ({NUM_CHUNKS_PER_INFERENCE} chunks), {send_duration_ms:.1f}ms @ {INPUT_SAMPLE_RATE}Hz, "
                    f"budget: {chunk_budget_ms:.0f}ms per call"
                )

            # Ensure Triton client is initialized
            if self.triton_client is None:
                raise Exception("Triton client not initialized for session")

            self._capture_array("triton-submitted-16k", audio_float32, INPUT_SAMPLE_RATE)
            self._input_prefix("submitted", audio_float32, INPUT_SAMPLE_RATE)

            # Prepare Triton inputs
            inputs = [
                httpclient.InferInput("audio_signal", audio_float32.shape, np_to_triton_dtype(audio_float32.dtype)),
                httpclient.InferInput("system_prompt", (1, 1), "BYTES"),
                httpclient.InferInput("function_response", (1, 1), "BYTES"),
            ]

            inputs[0].set_data_from_numpy(audio_float32)
            needs_start = not self.sequence_started.is_set() and not self.do_warmup and self.triton_inferences == 0
            if needs_start and (self.instructions or self.tools):
                inputs[1].set_data_from_numpy(
                    np.array([[self.build_prompt(self.instructions, self.tools).encode("utf-8")]], dtype=object)
                )
            else:
                inputs[1].set_data_from_numpy(np.array([[b""]], dtype=object))

            # Send function call output to Triton if available
            function_call_item = self.function_call_output.popleft() if self.function_call_output else None
            if function_call_item:
                self._activate_function_result(function_call_item)
                output = function_call_item.get("output", "")
                output_str = output if isinstance(output, str) else json.dumps(output)
                logger.info(f"Client {self.client_id}: Sending function call output to Triton: chars={len(output_str)}")
                self._voice_diagnostic("function.result", chars=len(output_str),
                                       call_id=str(function_call_item.get("call_id", ""))[:128],
                                       native_batch_id=function_call_item.get("native_batch_id"),
                                       calls=len(function_call_item.get("native_call_ids", [None])))
                inputs[2].set_data_from_numpy(np.array([[output_str.encode("utf-8")]], dtype=object))
            else:
                inputs[2].set_data_from_numpy(np.array([[b""]], dtype=object))

            # Prepare Triton outputs
            outputs = [
                httpclient.InferRequestedOutput("output_text"),
                httpclient.InferRequestedOutput("output_asr_text"),
                httpclient.InferRequestedOutput("output_function_text"),
                httpclient.InferRequestedOutput("output_audio"),
            ]

            # Call Triton inference
            request_id = str(uuid.uuid4())
            is_last_chunk = False  # Set to True when client disconnects

            # Track inference time
            infer_start = time.time()
            self.triton_samples_submitted_16k += num_samples

            response = await asyncio.wait_for(
                self.triton_client.infer(
                    model_name=TRITON_MODEL_NAME,
                    inputs=inputs,
                    outputs=outputs,
                    request_id=request_id,
                    sequence_id=self.sequence_id,
                    sequence_start=needs_start,
                    sequence_end=is_last_chunk,
                ),
                timeout=TRITON_INFER_TIMEOUT,
            )

            infer_time_ms = (time.time() - infer_start) * 1000
            self.triton_inferences += 1
            self.triton_samples_completed_16k += num_samples
            qsize = self.inference_queue.qsize()

            # Record in global tracker (grouped by active stream count)
            inference_manager.record_inference(infer_time_ms)

            # Log inference timing (RTFx = budget / infer_time; >1 means keeping up)
            rtfx = chunk_budget_ms / infer_time_ms if infer_time_ms > 0 else 0
            if self.triton_inferences <= 3:
                logger.info(
                    f"Client {self.client_id}: Inference #{self.triton_inferences} completed in {infer_time_ms:.1f}ms "
                    f"(budget: {chunk_budget_ms:.0f}ms, {rtfx:.1f}x real-time, queue: {qsize})"
                )
            elif infer_time_ms > chunk_budget_ms:
                logger.warning(
                    f"Client {self.client_id}: Slow inference #{self.triton_inferences}: {infer_time_ms:.1f}ms "
                    f"(budget: {chunk_budget_ms:.0f}ms, {rtfx:.1f}x real-time, queue: {qsize})"
                )
            elif self.triton_inferences % 50 == 0:
                logger.info(
                    f"Client {self.client_id}: Inference #{self.triton_inferences}: {infer_time_ms:.1f}ms "
                    f"(budget: {chunk_budget_ms:.0f}ms, {rtfx:.1f}x real-time, queue: {qsize})"
                )

            # Extract outputs
            output_text = response.as_numpy("output_text")
            output_asr_text = response.as_numpy("output_asr_text")
            output_function_text = response.as_numpy("output_function_text")
            output_audio = response.as_numpy("output_audio")
            text_response = "".join(t.decode("utf-8") for t in output_text)
            asr_response = "".join(t.decode("utf-8") for t in output_asr_text)
            function_response = "".join(t.decode("utf-8") for t in output_function_text)
            if asr_response or function_response or "<s>" in text_response or "</s>" in text_response:
                self._voice_diagnostic("triton.output", inference=self.triton_inferences,
                                       text_chars=len(text_response), asr_chars=len(asr_response),
                                       function_chars=len(function_response), audio_samples_22050=output_audio.size)

            await self._dispatch_model_output(
                output_audio.astype(np.float32).reshape(-1),
                text_response,
                asr_response,
                function_response,
            )

            if self.triton_inferences % 50 == 0:
                logger.info(
                    f"Client {self.client_id}: Processed {self.chunks_received} chunks, sent {self.chunks_sent}"
                )

        except asyncio.TimeoutError:
            logger.error(f"Client {self.client_id}: Triton inference timeout after {TRITON_INFER_TIMEOUT}s")
            try:
                await self._emit_error(
                    error_message="Inference timeout", error_type="internal_error", error_code="inference_timeout"
                )
            except Exception:
                logger.debug(f"Client {self.client_id}: Could not send error message (client may have disconnected)")
            raise
        except (WebSocketDisconnect, ConnectionResetError, RuntimeError) as e:
            # Client disconnected - this is normal, just log and stop processing
            logger.info(f"Client {self.client_id}: Client disconnected during processing: {e}")
            raise  # Re-raise to stop the session
        except Exception as e:
            logger.error(f"Client {self.client_id}: Error processing audio: {e}", exc_info=True)
            try:
                code = "voice_session_rotation_required" if "VOICE_SESSION_ROTATION_REQUIRED:" in str(e) else "inference_error"
                await self._emit_error(error_message="Voice model request ended. Restart voice.",
                                       error_type="internal_error", error_code=code)
            except Exception:
                logger.debug(f"Client {self.client_id}: Could not send error message (client may have disconnected)")
            raise

    async def _dispatch_model_output(self, audio, text, asr_text, function_text):
        """Triton text/audio cover the same decoder-step interval.

        Idle codec arrays are not spoken responses. A spoken BOS/content or a
        tool call opens a response; EOS drains that response's resampler/PCM
        tail before its done events. Never infer turns from PCM amplitude.
        """
        # With the configured two decoder steps, EOS+BOS is exactly two
        # 80 ms codec frames. Split at the source-defined frame boundary, not
        # at an amplitude threshold or a guessed wall-clock delay.
        if "</s>" in text and "<s>" in text.split("</s>", 1)[1]:
            frame_samples = int(MODEL_OUTPUT_SAMPLE_RATE * 0.08)
            if text != "</s><s>" or audio.size != 2 * frame_samples:
                raise ValueError("Ambiguous multi-turn Triton batch: expected two 80 ms EOS/BOS frames")
            await self._dispatch_model_output(audio[:frame_samples], "</s>", asr_text, "")
            await self._dispatch_model_output(audio[frame_samples:], "<s>", "", function_text)
            return
        if asr_text:
            await self._dispatch_asr_text(asr_text)
        # A new BOS ends an interrupted prior spoken turn. A tool-reserved
        # response with no speech yet is retained for its first ACK/BOS.
        if "<s>" in text and self._response_active and self._response_has_speech:
            await self._finish_spoken_response()
        # Function parsing occurs after token generation. If this batch ends
        # a prior turn, its audio/EOS belongs to that turn, not to the new call.
        if function_text and (
            ("</s>" in text and "<s>" not in text)
            or (self._response_active and self._response_has_speech)
        ):
            await self._dispatch_model_output(audio, text, "", "")
            if self._response_active:
                await self._finish_spoken_response()
            await self._process_function_text(function_text)
            return
        # Bind user input and authorize the tool before any same-batch ACK audio.
        if function_text:
            await self._process_function_text(function_text)
        clean_text = text.replace("^", "").replace("<s>", "").replace("</s>", "")
        if not self._response_active and ("<s>" in text or clean_text.strip()):
            await self._emit_response_created()
        if not self._response_active:
            return
        if "<s>" in text or clean_text.strip():
            self._response_has_speech = True
        if audio.size:
            audio = self.output_resampler.resample_chunk(audio)
            self.encode_audio_output(np.clip(audio, -1.0, 1.0))
        if clean_text:
            self.accumulated_agent_transcript.append(clean_text)
            await self._emit_audio_transcript_delta(clean_text)
            if SEND_OUTPUT_TEXT_EVENTS:
                await self._emit_output_text_delta(clean_text)
        if "</s>" in text:
            await self._finish_spoken_response()

    async def _finish_spoken_response(self):
        if not self._response_active:
            return
        response_call_ids = list(self._response_call_ids)
        self._flush_response_audio()
        transcript = "".join(self.accumulated_agent_transcript)
        await self._emit_audio_transcript_done(transcript)
        if SEND_OUTPUT_TEXT_EVENTS:
            await self._emit_output_text_done(transcript)
        self.accumulated_agent_transcript = []
        await self._emit_response_done()
        tools = json.loads(self.tools) if isinstance(self.tools, str) else self.tools
        acknowledgments = {" ".join(ack.split()) for tool in tools
                           for ack in tool.get("ack_messages", [])}
        if transcript.strip() and " ".join(transcript.split()) not in acknowledgments:
            for call_id in response_call_ids:
                self._awaiting_tool_answers.pop(call_id, None)
            if not self._awaiting_tool_answers:
                self._answer_inputs.clear()
        # Every queued response event already carries its immutable ownership
        # snapshot. Clear the mutable response context before the next ASR turn.
        self._response_input_item_id = None
        self._response_call_ids = []
        self._response_related_inputs = []

    async def _process_function_text(self, function_text: str) -> None:
        """Process the function text response from the Triton inference"""
        try:
            parsed = json.loads(function_text)
            calls = parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            raise ValueError("Invalid model tool-call JSON") from None

        if not calls:
            return
        if len(calls) > 16 or len(self.function_call_batches) + len(calls) > 128:
            raise ValueError("Too many pending native tool calls")
        if not all(isinstance(call, dict) for call in calls):
            raise ValueError("Tool calls must be JSON objects")
        self._voice_diagnostic("function.parsed", chars=len(function_text), calls=len(calls))
        if not self._response_active:
            await self._emit_response_created()
        self._response_input_item_id = self._input_item_id
        self._response_call_ids = []
        self._response_related_inputs = []
        response_id = self._response_id
        batch_id = str(uuid.uuid4())
        call_ids = [str(uuid.uuid4()) for _ in calls]
        self.function_batches[batch_id] = call_ids
        self.function_call_batches.update({call_id: batch_id for call_id in call_ids})
        for output_index, call in enumerate(calls):
            self._function_inputs[call_ids[output_index]] = self._input_item_id
            arguments = call.get("arguments", call.get("parameters", {}))
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            if not isinstance(arguments, dict):
                raise ValueError("Tool arguments must be a JSON object")
            fn_done = {
                "event_id": str(uuid.uuid4()),
                "type": "response.function_call_arguments.done",
                "response_id": response_id,
                "item_id": str(uuid.uuid4()),
                "output_index": output_index,
                "call_id": call_ids[output_index],
                "input_item_id": self._input_item_id,
                "native_batch_id": batch_id,
                "native_batch_size": len(calls),
                "name": call.get("name", ""),
                "arguments": json.dumps(arguments),
            }
            self._voice_diagnostic("function.call", action="queued", tool=call.get("name", ""),
                                   call_id=call_ids[output_index], native_batch_id=batch_id,
                                   native_batch_size=len(calls), output_index=output_index,
                                   input_item_id=str(self._input_item_id or "")[:128])
            self._queue_output_event(fn_done)

    async def finalize_sequence(self):
        """Send final empty chunk to close Triton sequence"""
        try:
            if LOOPBACK_MODE:
                logger.info(f"Client {self.client_id}: [LOOPBACK] Skipping sequence finalization")
                return

            if self.triton_client is None:
                logger.warning(f"Client {self.client_id}: No Triton client to finalize")
                return

            logger.info(f"Client {self.client_id}: Finalizing inference sequence")

            # Send empty chunk with sequence_end=True
            empty_audio = np.zeros((1, int(0.08 * INPUT_SAMPLE_RATE)), dtype=np.float32)

            inputs = [
                httpclient.InferInput("audio_signal", empty_audio.shape, np_to_triton_dtype(empty_audio.dtype)),
                httpclient.InferInput("system_prompt", (1, 1), "BYTES"),
                httpclient.InferInput("function_response", (1, 1), "BYTES"),
            ]
            inputs[0].set_data_from_numpy(empty_audio)
            inputs[1].set_data_from_numpy(np.array([[b""]], dtype=object))
            inputs[2].set_data_from_numpy(np.array([[b""]], dtype=object))

            outputs = [
                httpclient.InferRequestedOutput("output_text"),
                httpclient.InferRequestedOutput("output_asr_text"),
                httpclient.InferRequestedOutput("output_function_text"),
                httpclient.InferRequestedOutput("output_audio"),
            ]

            response = await self.triton_client.infer(
                model_name=TRITON_MODEL_NAME,
                inputs=inputs,
                outputs=outputs,
                request_id=str(uuid.uuid4()),
                sequence_id=self.sequence_id,
                sequence_start=False,
                sequence_end=True,
            )

            output_asr_text = response.as_numpy("output_asr_text")
            asr_text_response = "".join([t.decode("utf-8") for t in output_asr_text])
            await self._dispatch_asr_text(asr_text_response)

            logger.info(f"Client {self.client_id}: Sequence finalized")

        except Exception as e:
            logger.error(f"Client {self.client_id}: Error finalizing sequence: {e}")

    async def handle_session_update(self, session_config: dict):
        """Handle session configuration update"""
        logger.info(f"Client {self.client_id}: Session update: instructions_chars={len(session_config.get('instructions', ''))} "
                    f"tools={len(session_config.get('tools', []))}")

        # Parse audio configuration
        audio_config = session_config.get("audio", {})
        if audio_config:
            # Update input audio format
            input_config = audio_config.get("input", {})
            if "format" in input_config:
                new_format, new_rate = self._normalize_format(input_config["format"])
                if new_format:
                    rate = await self._validate_input_rate(new_rate)
                    if rate is None:
                        return  # invalid input rate; error already emitted to client
                    if new_format != self.input_audio_format or rate != self.input_sample_rate:
                        self.input_audio_format = new_format
                        self.input_sample_rate = rate
                        self.input_codec = self._create_codec(new_format, rate)
                        # Rebuild the resampler so any accepted client rate maps to the model's 16kHz
                        self.input_resampler = soxr.ResampleStream(rate, INPUT_SAMPLE_RATE, 1, dtype='float32')
                        self._input_resampler_finalized = False
                        logger.info(
                            f"Client {self.client_id}: Input audio format set to {new_format} @ {rate}Hz "
                            f"(resampling {rate}Hz → {INPUT_SAMPLE_RATE}Hz)"
                        )
                else:
                    logger.warning(f"Client {self.client_id}: Unsupported input format: {input_config['format']}")

            # Update output audio format
            output_config = audio_config.get("output", {})
            if "format" in output_config:
                new_format, new_rate = self._normalize_format(output_config["format"])
                if new_format:
                    rate = new_rate or OUTPUT_SAMPLE_RATE
                    if new_format != self.output_audio_format:
                        self.output_audio_format = new_format
                        self.output_codec = self._create_codec(new_format, rate)
                        logger.info(f"Client {self.client_id}: Output audio format changed to {new_format} @ {rate}Hz")
                else:
                    logger.warning(f"Client {self.client_id}: Unsupported output format: {output_config['format']}")

        tools = session_config.get("tools", "[]")
        self.tools = json.dumps(tools) if isinstance(tools, list) else (tools if tools else "[]")

        self.instructions = self.get_instructions(
            user_instructions=session_config.get("instructions"), tools=self.tools,
        )

        # Combine instructions and tools for function calling
        prompt = self.build_prompt(self.instructions, self.tools)
        if prompt and len(prompt) > MAX_INSTRUCTIONS_LENGTH:
            await self._emit_error(
                error_message=f"Instructions too long (max {MAX_INSTRUCTIONS_LENGTH} characters)",
                error_type="invalid_request_error",
                error_code="instructions_too_long",
            )
            return

        # Fire system prompt in background so audio processing isn't blocked
        if not self.sequence_started.is_set() and (self._prompt_task is None or self._prompt_task.done()):
            self._prompt_task = asyncio.create_task(self.send_system_prompt_to_triton(prompt))

        # Send session.updated response with current configuration
        response_config = {
            **session_config,
            "audio": {
                "input": {"format": self._format_to_structured(self.input_audio_format)},
                "output": {"format": self._format_to_structured(self.output_audio_format)},
            },
            "instructions": self.instructions,
            "tools": self.tools,
        }
        message = {"type": "session.updated", "event_id": str(uuid.uuid4()), "session": response_config}
        try:
            await asyncio.wait_for(self.websocket.send_text(json.dumps(message)), timeout=5.0)
        except asyncio.TimeoutError:
            logger.error(f"Client {self.client_id}: Timeout sending session.updated")

    async def handle_function_call_output(self, item: dict):
        """Handle a function_call_output conversation item"""
        if item:
            call_id = item.get("call_id")
            batch_id = self.function_call_batches.get(call_id)
            expected = self.function_batches.get(batch_id)
            if not expected:
                raise ValueError("Unknown or already settled tool result")
            if len(expected) > 1 or "native_batch_id" in item:
                if (item.get("native_batch_id") != batch_id or item.get("native_call_ids") != expected):
                    raise ValueError("Native batch requires exactly one complete aggregate result")
            output = item.get("output", "")
            # One function-channel injection per decoded model batch. The API
            # has already applied budgets and assembled cumulative evidence.
            for member in expected:
                del self.function_call_batches[member]
            del self.function_batches[batch_id]
            self._voice_diagnostic("function.batch_result", native_batch_id=batch_id,
                                   calls=len(expected), call_id=call_id)
            logger.info(f"Client {self.client_id}: Function call output received: "
                        f"call_id={str(item.get('call_id', ''))[:128]} "
                        f"chars={len(output if isinstance(output, str) else json.dumps(output))}")
            self.function_call_output.append(item)

    def _activate_function_result(self, item):
        """The result's native call owns its next spoken answer, including follow-ups.

        This is consumption provenance, not a claim that the model answered all
        questions. The API still checks the actual spoken text against evidence.
        """
        for call_id in item.get("native_call_ids", [item["call_id"]]):
            self._awaiting_tool_answers[call_id] = self._function_inputs.pop(call_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown events"""
    # Get port from environment variable (set by main() before uvicorn.run)
    port = int(os.getenv("SERVER_PORT", "9000"))
    ssl_enabled = os.getenv("SSL_ENABLED", "false") == "true"
    protocol = "wss" if ssl_enabled else "ws"

    logger.info("=" * 60)
    logger.info("Nemotron VoiceChat Server - Audio WebSocket with Triton Inference")
    logger.info("=" * 60)
    logger.info(f"Server: {protocol}://0.0.0.0:{port}/v1/realtime")

    if LOOPBACK_MODE:
        logger.warning("*" * 60)
        logger.warning("LOOPBACK MODE ENABLED - Triton inference bypassed")
        logger.warning("Audio will be echoed back without processing")
        logger.warning("Set LOOPBACK_MODE=false or unset to enable Triton")
        logger.warning("*" * 60)
    else:
        logger.info(f"Triton: {TRITON_URL}, Model: {TRITON_MODEL_NAME}")

    logger.info(
        f"Format: Input {INPUT_AUDIO_FORMAT}@{INPUT_CLIENT_SAMPLE_RATE}Hz → Output {OUTPUT_AUDIO_FORMAT}@{OUTPUT_SAMPLE_RATE}Hz (Model: {INPUT_SAMPLE_RATE}Hz)"
    )
    logger.info(f"Health: http://0.0.0.0:{port}/v1/realtime/health")
    logger.info("Press Ctrl+C to stop")
    logger.info("=" * 60)

    # Initialize Triton client (skip in loopback mode)
    if not LOOPBACK_MODE:
        try:
            await triton_manager.get_client()
        except Exception as e:
            logger.warning(f"Could not connect to Triton at startup: {e}")
            logger.warning("Will retry on first request")

    yield

    # Cleanup
    logger.info("\nShutting down Nemotron VoiceChat Server")
    inference_manager.stop()
    await triton_manager.close()


# Create FastAPI app
app = FastAPI(title="Nemotron VoiceChat Server", version="0.2.0", lifespan=lifespan)


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": "nemotron-voicechat",
        "version": "0.2.0",
        "websocket": "/v1/realtime",
        "health": "/v1/realtime/health",
        "loopback_mode": LOOPBACK_MODE,
        "triton_url": TRITON_URL if not LOOPBACK_MODE else "disabled",
        "model_name": TRITON_MODEL_NAME if not LOOPBACK_MODE else "disabled",
    }


@app.get("/v1/realtime/health")
async def health_check():
    """Health check endpoint"""
    if LOOPBACK_MODE:
        return {
            "status": "ok",
            "service": "nemotron-voicechat-websocket-server",
            "mode": "loopback",
            "triton_status": "disabled",
        }

    triton_status = "unknown"
    error_message = None
    try:
        client = await triton_manager.get_client()
        is_ready = await client.is_server_ready()
        triton_status = "ready" if is_ready else "not_ready"

        if not is_ready:
            error_message = "Triton server is not ready"
        else:
            is_model_ready = await client.is_model_ready(TRITON_MODEL_NAME)
            if not is_model_ready:
                error_message = f"Model '{TRITON_MODEL_NAME}' is not ready"
    except Exception as e:
        triton_status = "error"
        error_message = str(e)

    # Determine overall status
    status = "ok" if triton_status == "ready" and not error_message else "error"

    response = {
        "status": status,
        "service": "nemotron-voicechat-websocket-server",
        "mode": "triton",
        "triton_status": triton_status,
    }

    if error_message:
        response["error"] = error_message

    # Fetch model inference statistics if server and model are ready
    if status == "ok":
        try:
            stats = await client.get_inference_statistics(TRITON_MODEL_NAME)
            model_stats = stats.get("model_stats", [{}])[0].get("inference_stats", {})
            success_count = model_stats.get("success", {}).get("count", 0)
            fail_count = model_stats.get("fail", {}).get("count", 0)
            response["model_inference_stats"] = {
                "success_count": success_count,
                "fail_count": fail_count,
            }
            if fail_count > 0:
                response["status"] = "error"
                response["error"] = f"Model has {fail_count} failed inference(s)"
        except Exception as e:
            response["model_inference_stats"] = {"error": str(e)}

    return response


async def handle_websocket(websocket: WebSocket):
    """Handle WebSocket connection for real-time audio communication"""
    client_id = str(uuid.uuid4())[:8]

    # Echo the function-id subprotocol entry so browsers don't close the connection.
    subprotocol = None
    protocol_header = websocket.headers.get("sec-websocket-protocol", "")
    for entry in (e.strip() for e in protocol_header.split(",") if e.strip()):
        if entry.startswith("function-id"):
            subprotocol = entry
            break

    await websocket.accept(subprotocol=subprotocol)
    logger.info(f"Client {client_id} connected from {websocket.client}")

    try:
        session = AudioSession(client_id, websocket)
    except FileExistsError:
        logger.error("Client %s rejected: isolated capture sequence directory already exists", client_id)
        await websocket.close(code=1013, reason="Isolated capture sequence already used")
        return
    graceful_close = False

    try:
        # Initialize Triton client once for this session
        await session.initialize_triton_client()

        # Start background inference worker
        await session.start_inference_worker()

        # Send session created message
        await session.send_session_created()

        while True:
            # Check audio-based duration limit (from the client's actual input rate)
            audio_duration = session.audio_samples_received / session.input_sample_rate
            if audio_duration > MAX_SESSION_DURATION:
                logger.info(
                    f"Client {client_id}: Session audio duration limit reached "
                    f"({audio_duration:.1f}s >= {MAX_SESSION_DURATION}s)"
                )
                try:
                    await session._emit_error(
                        error_message="Session duration limit reached",
                        error_type="invalid_request_error",
                        error_code="session_timeout",
                    )
                except Exception:
                    pass
                break

            try:
                # Use a short timeout so we can re-check the audio duration
                # even if the client goes silent after hitting the limit
                raw_message = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)

                try:
                    data = json.loads(raw_message)
                    message_type = data.get("type")

                    if message_type == "input_audio_buffer.append":
                        audio_data = data.get("audio")
                        event_id = data.get("event_id")

                        if audio_data:
                            # Queue audio for async processing (non-blocking)
                            await session.queue_audio_chunk(audio_data, event_id)
                        else:
                            logger.warning(f"Client {client_id}: Audio message without data")

                    elif message_type == "session.update":
                        session_config = data.get("session", {})
                        await session.handle_session_update(session_config)

                    elif message_type == "session.close":
                        logger.info(f"Client {client_id}: Received session.close")
                        graceful_close = True
                        break

                    elif message_type == "conversation.item.create":
                        item = data.get("item", {})
                        if item.get("type") == "function_call_output":
                            if session.tools:
                                await session.handle_function_call_output(item)
                            else:
                                logger.warning(
                                    f"Client {client_id}: Function call output received but tools are not set"
                                )
                                await session._emit_error(
                                    error_message="Tools are not set for this session",
                                    error_type="invalid_request_error",
                                    error_code="tools_not_set",
                                )

                    else:
                        logger.warning(f"Client {client_id}: Ignored message type: {message_type}")

                except json.JSONDecodeError as e:
                    logger.error(f"Client {client_id}: Invalid JSON: {e}")
                except Exception as e:
                    logger.error(f"Client {client_id}: Error processing message: {e}", exc_info=True)
                    if message_type == "conversation.item.create":
                        # Never leave a rejected/partial batch waiting silently.
                        await session._emit_error(error_message="Invalid tool result batch",
                                                  error_type="invalid_request_error",
                                                  error_code="invalid_tool_result_batch")
                        break

            except asyncio.TimeoutError:
                continue  # Re-check audio duration at top of loop
            except WebSocketDisconnect:
                logger.info(f"Client {client_id}: WebSocket disconnected")
                break

    except Exception as e:
        logger.error(f"Client {client_id}: Unexpected error: {e}", exc_info=True)

    finally:
        capture_reason = "graceful_close" if graceful_close else "disconnect_or_error"
        if graceful_close and session.chunks_received > 0:
            try:
                await session.drain_inference_queue()
                await session.finalize_sequence()
                if session._response_active:
                    await session._finish_spoken_response()
                await asyncio.wait_for(session.output_events.join(), timeout=SESSION_DRAIN_TIMEOUT)
            except Exception as e:
                logger.warning(f"Client {client_id}: Error draining/finalizing sequence: {e}")

            # A graceful client waits for this acknowledgement before disconnecting.
            await session.stop_inference_worker()
            await session.send_session_end()
        else:
            # Do not spend time draining after an unexpected client disconnect.
            await session.stop_inference_worker()
            if session.chunks_received > 0:
                try:
                    await session.finalize_sequence()
                except Exception as e:
                    logger.warning(f"Client {client_id}: Error finalizing sequence: {e}")
            await session.send_session_end()

        session._finish_capture(capture_reason)

        logger.info(
            f"Client {client_id}: Session ended - "
            f"Received: {session.chunks_received}, Sent: {session.chunks_sent}, "
            f"Dropped: {session.chunks_dropped}"
        )


# Register WebSocket routes (support both paths for compatibility)
@app.websocket("/v1/realtime")
async def websocket_endpoint_v1(websocket: WebSocket):
    """WebSocket endpoint at /v1/realtime (original path)"""
    await handle_websocket(websocket)


@app.websocket("/realtime")
async def websocket_endpoint_root(websocket: WebSocket):
    """WebSocket endpoint at /realtime (for OpenAI SDK compatibility)"""
    await handle_websocket(websocket)


if __name__ == "__main__":
    import argparse

    import uvicorn

    # Parse command-line arguments
    parser = argparse.ArgumentParser(description="Nemotron VoiceChat Server - Audio WebSocket with Triton Inference")
    parser.add_argument("--port", type=int, default=9000, help="Port to run the server on (default: 9000)")
    parser.add_argument("--ssl-certfile", type=str, help="SSL certificate file (for HTTPS/WSS)")
    parser.add_argument("--ssl-keyfile", type=str, help="SSL private key file (for HTTPS/WSS)")
    args = parser.parse_args()

    # Set port and SSL status in environment so lifespan can access it
    os.environ["SERVER_PORT"] = str(args.port)
    os.environ["SSL_ENABLED"] = "true" if args.ssl_certfile and args.ssl_keyfile else "false"

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=args.port,
        log_level="info",
        ssl_certfile=args.ssl_certfile,
        ssl_keyfile=args.ssl_keyfile,
        ws_max_size=10 * 1024 * 1024,
        ws_ping_interval=30.0,  # Send ping every 30 seconds
        ws_ping_timeout=120.0,  # Wait up to 120 seconds for pong
        timeout_keep_alive=300,  # Keep connection alive for 5 minutes
    )
