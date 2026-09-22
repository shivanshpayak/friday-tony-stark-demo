"""
JARVIS Launcher — Wake Word Activated Voice Assistant
=====================================================
Single entry point that manages:
1. MCP Server subprocess (always running)
2. OpenWakeWord listener (always running)
3. LiveKit voice sessions (on-demand)

Usage:
    uv run friday_start
"""

import asyncio
import ctypes
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
import time
import winsound
from ctypes import wintypes
from enum import Enum
from pathlib import Path

import keyboard
import numpy as np
import pyaudio
from dotenv import load_dotenv
from friday_overlay import FridayOverlay
from friday.recovery import (
    next_respawn_failures, respawn_backoff, should_announce_recovery,
)
from friday.config import FASTPATH_ENABLED


# ---------------------------------------------------------------------------
# Disable Windows EcoQoS / power throttling
# ---------------------------------------------------------------------------
# When launched silently from startup, Windows marks us as a background
# process and throttles CPU to favour battery life. That makes the overlay
# visibly laggy and slows model inference. Opt out explicitly.

_PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
_PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
_ProcessPowerThrottling = 4

# Priority classes — ABOVE_NORMAL keeps audio/VAD threads scheduled promptly
# under load without starving foreground apps the way HIGH_PRIORITY would.
_ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.SetProcessInformation.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
]
_kernel32.SetProcessInformation.restype = wintypes.BOOL
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.SetPriorityClass.restype = wintypes.BOOL


def _bump_priority(process_handle) -> bool:
    """Raise process to ABOVE_NORMAL so the audio thread stays scheduled."""
    try:
        return bool(_kernel32.SetPriorityClass(
            process_handle, _ABOVE_NORMAL_PRIORITY_CLASS,
        ))
    except Exception:
        return False


class _POWER_THROTTLING_STATE(ctypes.Structure):
    _fields_ = [
        ("Version", wintypes.ULONG),
        ("ControlMask", wintypes.ULONG),
        ("StateMask", wintypes.ULONG),
    ]


def _disable_power_throttling(process_handle=None) -> bool:
    """Tell Windows: don't throttle this process. Returns True on success."""
    try:
        if process_handle is None:
            process_handle = _kernel32.GetCurrentProcess()
        state = _POWER_THROTTLING_STATE()
        state.Version = _PROCESS_POWER_THROTTLING_CURRENT_VERSION
        state.ControlMask = _PROCESS_POWER_THROTTLING_EXECUTION_SPEED
        state.StateMask = 0  # 0 = opt OUT of throttling
        ok = _kernel32.SetProcessInformation(
            process_handle,
            _ProcessPowerThrottling,
            ctypes.byref(state),
            ctypes.sizeof(state),
        )
        if not ok:
            err = ctypes.get_last_error()
            # Stash so main() can log it once the logger is configured.
            globals()["_THROTTLE_ERR"] = err
        return bool(ok)
    except Exception as e:
        globals()["_THROTTLE_ERR"] = repr(e)
        return False


_THROTTLE_ERR = None
_disable_power_throttling()  # apply to self ASAP, before heavy imports run

# Global hotkeys — registered on the OS hook, so they fire from any app.
KILL_HOTKEY = "ctrl+alt+shift+q"      # Hard kill: tear down and exit.
RESTART_HOTKEY = "ctrl+alt+shift+r"   # Kill + relaunch (picks up code edits).
PTT_HOTKEY = "ctrl+alt+space"         # Force-activate a session — bypasses
                                       # wake word & speaker check. For noisy
                                       # rooms or when you want guaranteed activation.

# Path to the silent .vbs wrapper used by the restart hotkey.
SILENT_LAUNCHER = Path(__file__).parent / "start_friday.vbs"

load_dotenv()

# ---------------------------------------------------------------------------
# Logging — to console AND to a rotating file. The .vbs silent launcher
# discards stdout, so the file is the only post-mortem source. Capture
# everything: launcher logs, agent subprocess stdout, unhandled exceptions.
# ---------------------------------------------------------------------------

# FRIDAY_LOG_DIR overrides the location — the test suite sets it so importing
# this module never writes fake entries into the real log.
LOG_DIR = Path(os.getenv("FRIDAY_LOG_DIR") or Path(__file__).parent / "logs")
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "friday.log"

_log_fmt = logging.Formatter(
    "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
)
_file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8"
)
_file_handler.setFormatter(_log_fmt)
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_fmt)

logging.basicConfig(
    level=logging.INFO,
    handlers=[_file_handler, _console_handler],
    force=True,           # clear any pre-existing root handlers
)
logger = logging.getLogger("friday-launcher")

# Route uncaught exceptions into the log file too.
def _excepthook(exc_type, exc_value, exc_tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))

sys.excepthook = _excepthook
logger.info("=" * 70)
logger.info("JARVIS launcher starting — log file: %s", LOG_FILE)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WAKE_MODEL = "hey_jarvis_v0.1"  # built-in OpenWakeWord model
WAKE_THRESHOLD = 0.7          # fewer false wakes from background noise
SILENCE_TIMEOUT = 30.0
AUDIO_RATE = 16000
AUDIO_CHUNK = 1280  # 80ms at 16kHz
CHIME_PATH = Path(__file__).parent / "sounds" / "activate.wav"
BOOT_ACK_PATH = Path(__file__).parent / "sounds" / "booting_ack.wav"
BOOT_ACK_TEXT = "Hello sir, give me a second to get everything booted up."
BOOT_ACK_VOICE = os.getenv("LOCAL_TTS_VOICE", "").strip()
try:
    BOOT_ACK_RATE = int(os.getenv("LOCAL_TTS_RATE", "1"))
except ValueError:
    BOOT_ACK_RATE = 1

# Mic selection — name substring (case-insensitive) of the device to use.
# Empty / unset = system default. Run `python list_audio_devices.py` to see
# what's available. WAKE_MIC governs the always-on wake-word listener;
# SESSION_MIC governs the agent subprocess (set via env var, applied below).
WAKE_MIC = os.getenv("WAKE_MIC", "").strip()
SESSION_MIC = os.getenv("SESSION_MIC", "").strip()
# MCP server no longer needed — tools are registered directly on the agent

# Speaker verification
VOICE_EMBEDDING_PATH = Path(__file__).parent / "voice_embedding.npy"
SPEAKER_SIM_THRESHOLD = 0.70   # was 0.70 — stricter "is this the enrolled voice"
SPEAKER_BUFFER_SECONDS = 2.5   # last N seconds of mic audio to verify against

# openwakeword occasionally fires on a near-silent mic (idle, or at night).
# A wake whose buffer never peaks above this (float, ~100 int16 LSB) cannot be
# the user speaking: Resemblyzer scores such audio ≤ 0.63, under the threshold
# above, so dropping it early only skips work that would be rejected anyway.
WAKE_SILENCE_PEAK = 0.003

# Agent boot. Cold boots after a reboot (disk cache empty) have run past 60s;
# a missed window used to make the launcher exit for good. Now it retries.
BOOT_TIMEOUT = 120.0
BOOT_ATTEMPTS = 3


class State(Enum):
    SLEEPING = "sleeping"
    ACTIVE = "active"


# ---------------------------------------------------------------------------
# Wake word listener
# ---------------------------------------------------------------------------

def _resolve_input_device(pa: pyaudio.PyAudio, name_substr: str) -> int | None:
    """Return the index of the first input device whose name contains
    `name_substr` (case-insensitive). None means use system default."""
    if not name_substr:
        return None
    needle = name_substr.lower()
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if int(info["maxInputChannels"]) <= 0:
            continue
        if needle in info["name"].lower():
            logger.info("Resolved mic %r → index %d (%s)",
                        name_substr, i, info["name"])
            return i
    logger.warning("WAKE_MIC %r not found — falling back to system default",
                   name_substr)
    return None


def _is_near_silent(wav_float: np.ndarray) -> bool:
    """True when a wake buffer holds no audible sound (see WAKE_SILENCE_PEAK)."""
    return wav_float.size == 0 or float(np.max(np.abs(wav_float))) < WAKE_SILENCE_PEAK


class WakeWordListener:
    """Continuously listens for the wake word on the mic."""

    def __init__(self, model_name: str = WAKE_MODEL, threshold: float = WAKE_THRESHOLD):
        # Imported here, not at module top: openwakeword's package import takes
        # ~12s cold, and at module level it delayed spawning the agent by that
        # much. Here it overlaps the agent's boot. See launcher_loop.
        from openwakeword.model import Model as WakeWordModel

        # Use onnx instead of tflite — tflite-runtime has no Windows wheels.
        self._model = WakeWordModel(
            wakeword_models=[model_name],
            inference_framework="onnx",
        )
        self._threshold = threshold
        self._audio = pyaudio.PyAudio()
        self._stream = None
        self._enabled = True
        self._device_index = _resolve_input_device(self._audio, WAKE_MIC)
        # Ring buffer of the last SPEAKER_BUFFER_SECONDS of int16 audio.
        self._buffer_max = int(AUDIO_RATE * SPEAKER_BUFFER_SECONDS)
        self._buffer = np.zeros(self._buffer_max, dtype=np.int16)
        self._buffer_pos = 0

    def start_stream(self):
        self._stream = self._audio.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=AUDIO_RATE,
            input=True,
            input_device_index=self._device_index,
            frames_per_buffer=AUDIO_CHUNK,
        )
        logger.info("Mic stream opened for wake word detection (device=%s)",
                    self._device_index if self._device_index is not None else "default")

    def stop_stream(self):
        if self._stream:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None

    def reset(self):
        """Clear openwakeword's internal feature/prediction buffers and the
        rolling audio ring buffer. Call this after a voice session ends so
        stale state can't produce an instant false wake.
        """
        try:
            self._model.reset()
        except Exception as e:
            logger.debug("openwakeword reset failed (non-fatal): %s", e)
        self._buffer[:] = 0
        self._buffer_pos = 0

    def _append_to_buffer(self, audio: np.ndarray) -> None:
        n = len(audio)
        if n >= self._buffer_max:
            self._buffer = audio[-self._buffer_max:].copy()
            self._buffer_pos = 0
            return
        end = self._buffer_pos + n
        if end <= self._buffer_max:
            self._buffer[self._buffer_pos:end] = audio
        else:
            split = self._buffer_max - self._buffer_pos
            self._buffer[self._buffer_pos:] = audio[:split]
            self._buffer[: n - split] = audio[split:]
        self._buffer_pos = end % self._buffer_max

    def recent_audio_float(self) -> np.ndarray:
        """Return the rolling buffer as a contiguous float32 waveform in [-1, 1]."""
        ordered = np.concatenate(
            (self._buffer[self._buffer_pos:], self._buffer[: self._buffer_pos])
        )
        return ordered.astype(np.float32) / 32768.0

    def read_chunk(self) -> bytes:
        """Read one raw AUDIO_CHUNK of int16 bytes from the open stream.

        Used by the fast-path tail recognizer, which consumes the stream
        directly after a wake-word hit. Frames read here bypass the ring
        buffer and the wake model, so callers MUST call reset() afterwards.
        """
        if not self._stream:
            self.start_stream()
        return self._stream.read(AUDIO_CHUNK, exception_on_overflow=False)

    def listen_once(self) -> bool:
        """Block until wake word is detected. Returns True if detected."""
        if not self._stream:
            self.start_stream()

        raw = self._stream.read(AUDIO_CHUNK, exception_on_overflow=False)
        audio = np.frombuffer(raw, dtype=np.int16)
        self._append_to_buffer(audio)
        prediction = self._model.predict(audio)

        for model_name, score in prediction.items():
            if score > 0.1:
                logger.debug("Wake word trace: %s = %.3f", model_name, score)
            if score >= self._threshold:
                logger.info("Wake word detected! (model=%s, score=%.3f)", model_name, score)
                return True
        return False

    def cleanup(self):
        self.stop_stream()
        self._audio.terminate()


# ---------------------------------------------------------------------------
# Speaker verification (Resemblyzer)
# ---------------------------------------------------------------------------

class SpeakerVerifier:
    """Compares incoming audio against an enrolled voice embedding."""

    def __init__(self, embedding_path: Path = VOICE_EMBEDDING_PATH,
                 threshold: float = SPEAKER_SIM_THRESHOLD):
        self._threshold = threshold
        self._encoder = None
        self._reference = None
        # Serialises encoder use so the boot warmup can't race a wake word.
        self._lock = threading.Lock()
        if embedding_path.exists():
            try:
                self._reference = np.load(embedding_path)
                from resemblyzer import VoiceEncoder  # heavy import, lazy
                self._encoder = VoiceEncoder(verbose=False)
                logger.info(
                    "SpeakerVerifier loaded (threshold=%.2f, ref shape=%s)",
                    threshold, self._reference.shape,
                )
            except Exception as e:
                logger.warning("SpeakerVerifier init failed (%s); gate disabled", e)
                self._encoder = None
                self._reference = None
        else:
            logger.warning(
                "No voice embedding at %s — speaker gate disabled. "
                "Run: python enroll_voice.py",
                embedding_path,
            )

    @property
    def enabled(self) -> bool:
        return self._encoder is not None and self._reference is not None

    def warm(self) -> None:
        """Pay Resemblyzer's JIT cost at boot instead of on the first wake word.

        The first embed_utterance() call triggers librosa/numba JIT compilation
        and takes ~4.6s; every call after is ~20ms. Without this the FIRST wake
        word after every boot stalled ~5s inside verification — long enough to
        blow past the agent's PREPARE handshake timeout and abort the
        activation. Runs on a background thread during boot, where it overlaps
        the agent subprocess's own cold start and costs nothing.
        """
        if not self.enabled:
            return
        try:
            started = time.monotonic()
            dummy = (np.random.randn(AUDIO_RATE) * 0.01).astype(np.float32)
            with self._lock:
                self._encoder.embed_utterance(dummy)
            logger.info("Speaker verifier warm (%.1fs JIT paid at boot)",
                        time.monotonic() - started)
        except Exception as e:
            logger.debug("Speaker verifier warmup failed (non-fatal): %s", e)

    def verify(self, wav_float: np.ndarray) -> tuple[bool, float]:
        """Return (matched, similarity). If disabled, always (True, 1.0)."""
        if not self.enabled:
            return True, 1.0
        try:
            with self._lock:
                emb = self._encoder.embed_utterance(wav_float)
            ref = self._reference
            sim = float(np.dot(emb, ref) / ((np.linalg.norm(emb) * np.linalg.norm(ref)) + 1e-9))
            return sim >= self._threshold, sim
        except Exception as e:
            logger.warning("Speaker verify failed (%s); allowing", e)
            return True, 1.0


# ---------------------------------------------------------------------------
# Chime
# ---------------------------------------------------------------------------

def play_chime():
    """Play activation chime in a background thread."""
    def _play():
        try:
            winsound.PlaySound(str(CHIME_PATH), winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception as e:
            logger.warning("Could not play chime: %s", e)
    threading.Thread(target=_play, daemon=True).start()


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _ensure_boot_ack_clip() -> bool:
    """Create a cached local TTS clip for the wake acknowledgement if needed."""
    if BOOT_ACK_PATH.exists() and BOOT_ACK_PATH.stat().st_size > 0:
        return True

    BOOT_ACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    script_lines = [
        "Add-Type -AssemblyName System.Speech",
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer",
        "try {",
        f"  $s.Rate = {BOOT_ACK_RATE}",
    ]
    if BOOT_ACK_VOICE:
        script_lines.append(f"  $s.SelectVoice({_ps_quote(BOOT_ACK_VOICE)})")
    script_lines.extend(
        [
            f"  $s.SetOutputToWaveFile({_ps_quote(str(BOOT_ACK_PATH))})",
            f"  $s.Speak({_ps_quote(BOOT_ACK_TEXT)})",
            "} finally {",
            "  $s.Dispose()",
            "}",
        ]
    )

    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", "\n".join(script_lines)],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception as e:
        logger.warning("Could not build local wake acknowledgement clip: %s", e)
        return False

    if not BOOT_ACK_PATH.exists() or BOOT_ACK_PATH.stat().st_size <= 0:
        logger.warning("Local wake acknowledgement clip was not created")
        return False

    logger.info("Local wake acknowledgement clip ready (%s)", BOOT_ACK_PATH.name)
    return True


def play_activation_ack():
    """Play the immediate local wake acknowledgement."""
    play_chime()


# ---------------------------------------------------------------------------
# Fast-path command ack
# ---------------------------------------------------------------------------

FASTPATH_ACK_PATH = Path(__file__).parent / "sounds" / "fastpath_ack.wav"


def _ensure_fastpath_ack_clip() -> bool:
    """Generate the fast-path confirmation tick once.

    A short, bright, decaying blip — deliberately unlike activate.wav, so the
    fast path and a normal wake are audibly distinct.
    """
    if FASTPATH_ACK_PATH.exists() and FASTPATH_ACK_PATH.stat().st_size > 0:
        return True
    try:
        import math
        import wave

        rate, duration, freq = 22050, 0.06, 1400.0
        frames = bytearray()
        total = int(rate * duration)
        for i in range(total):
            envelope = (1.0 - i / total) ** 2
            sample = int(18000 * envelope * math.sin(2 * math.pi * freq * i / rate))
            frames += sample.to_bytes(2, "little", signed=True)

        FASTPATH_ACK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(FASTPATH_ACK_PATH), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(bytes(frames))
    except Exception as e:
        logger.warning("Could not build fast-path ack clip: %s", e)
        return False
    logger.info("Fast-path ack clip ready (%s)", FASTPATH_ACK_PATH.name)
    return True


def play_fastpath_ack():
    """Play the fast-path confirmation tick (async, non-blocking)."""
    try:
        winsound.PlaySound(str(FASTPATH_ACK_PATH),
                           winsound.SND_FILENAME | winsound.SND_ASYNC)
    except Exception as e:
        logger.debug("Fast-path ack playback failed (non-fatal): %s", e)


def _run_fast_action(cmd) -> None:
    """Run a fast-path action, converting any failure into an error beep."""
    try:
        cmd.action()
    except Exception as e:
        logger.exception("fastpath action %s failed: %s", cmd.name, e)
        try:
            winsound.MessageBeep(winsound.MB_ICONHAND)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main launcher loop
# ---------------------------------------------------------------------------

class AgentProcess:
    """Manages the single long-lived agent_friday.py subprocess.

    Protocol (line-based, over stdin/stdout):
        launcher → subprocess stdin:   START | QUIT
        subprocess stdout → launcher:  FRIDAY_READY | SESSION_STARTED |
                                       SESSION_LISTENING | SESSION_DONE |
                                       SESSION_FATAL | PROCESSING | SPEAKING |
                                       TURN_IDLE
    """

    def __init__(self, on_processing=None, on_speaking=None, on_listening=None,
                 on_fatal=None, on_idle=None):
        self._proc: subprocess.Popen | None = None
        self._ready = threading.Event()
        self._session_done = threading.Event()
        # Set on SESSION_FATAL. The agent's session is dead even if its process
        # lingers, so it must be torn down and respawned — see needs_respawn.
        self._fatal = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self._on_processing = on_processing  # callback when LLM starts thinking
        self._on_speaking = on_speaking       # callback when first TTS content arrives
        self._on_listening = on_listening     # callback when mic is live again
        self._on_fatal = on_fatal             # callback on SESSION_FATAL (session died)
        self._on_idle = on_idle               # callback when a turn ends with nothing said

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def needs_respawn(self) -> bool:
        """True when the agent can't serve a session: its process is gone, or
        its session died (SESSION_FATAL) — a fatal-closed agent whose process
        didn't exit would otherwise swallow every later wake word."""
        return not self.alive or self._fatal.is_set()

    def start(self):
        """Spawn the subprocess and wait for FRIDAY_READY."""
        if self.alive:
            return
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        self._ready.clear()
        self._session_done.clear()
        self._fatal.clear()
        self._proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).parent / "agent_friday.py"), "console"],
            cwd=Path(__file__).parent,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # merge stderr into stdout pipe — prevents double-logging
            bufsize=1,
            encoding="utf-8",
            errors="replace",
        )
        logger.info("Agent subprocess started (PID %d) — cold-booting models…",
                     self._proc.pid)
        # Opt the child out of Windows EcoQoS too — it inherits the throttle
        # bit from us inconsistently, so set it explicitly via OpenProcess.
        # Also bump priority to ABOVE_NORMAL so VAD/STT threads stay scheduled
        # promptly under memory pressure (paged-out ONNX inference is what
        # produces the "VAD slower than realtime" warnings).
        try:
            PROCESS_SET_INFORMATION = 0x0200
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = _kernel32.OpenProcess(
                PROCESS_SET_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION,
                False, self._proc.pid,
            )
            if h:
                _disable_power_throttling(h)
                if _bump_priority(h):
                    logger.info("Agent priority bumped to ABOVE_NORMAL")
                _kernel32.CloseHandle(h)
        except Exception as e:
            logger.debug("Could not unthrottle/prioritize child process: %s", e)
        self._reader_thread = threading.Thread(
            target=self._read_stdout, daemon=True
        )
        self._reader_thread.start()

    # Substrings in agent stdout lines that are too noisy to log.
    _NOISE_PATTERNS = (
        "Processing VAD event",
        "START_SPEECH",
        "END_SPEECH",
        "Speech started",
        "Speech ended",
        "flush triggered",
        "flush message",
        "Sent ",              # "Sent 1100 audio chunks"
        "empty transcript",
        "audio chunks",
        "signal_type",
        "client_request_id",
        "server_request_id",
        "connection_state",
    )

    def _read_stdout(self):
        """Drain subprocess stdout, dispatch signals, echo everything else."""
        try:
            assert self._proc and self._proc.stdout
            for line in self._proc.stdout:
                self._dispatch_line(line.rstrip())
        except Exception as e:
            logger.debug("stdout reader error: %s", e)
        finally:
            self._ready.set()
            self._session_done.set()

    def _dispatch_line(self, stripped: str):
        if "FRIDAY_READY" in stripped:
            logger.info("Agent subprocess signalled FRIDAY_READY")
            self._ready.set()
        elif "SESSION_FATAL" in stripped:
            logger.error("Agent subprocess signalled SESSION_FATAL — session died, will respawn")
            self._fatal.set()
            if self._on_fatal:
                self._on_fatal()
            # No SESSION_DONE follows a fatal close; release an ACTIVE wait.
            self._session_done.set()
        elif "SESSION_STARTED" in stripped:
            logger.info("Agent subprocess signalled SESSION_STARTED")
        elif "SESSION_LISTENING" in stripped:
            logger.info("Agent subprocess signalled SESSION_LISTENING")
            if self._on_listening:
                self._on_listening()
        elif "SESSION_DONE" in stripped:
            logger.info("Agent subprocess signalled SESSION_DONE")
            self._session_done.set()
        elif "PROCESSING" in stripped:
            if self._on_processing:
                self._on_processing()
        elif "SPEAKING" in stripped:
            if self._on_speaking:
                self._on_speaking()
        elif "TURN_IDLE" in stripped:
            if self._on_idle:
                self._on_idle()
        elif stripped:
            # Skip noisy LiveKit SDK internal debug lines.
            if any(p in stripped for p in self._NOISE_PATTERNS):
                return
            # Log via the agent-tagged logger (goes to file + console).
            logging.getLogger("friday-agent").info(stripped)

    async def wait_ready(self, timeout: float = 60.0) -> bool:
        """Block until FRIDAY_READY (or timeout/death)."""
        return await asyncio.get_event_loop().run_in_executor(
            None, self._ready.wait, timeout
        )

    async def boot(self, timeout: float) -> bool:
        """Spawn the agent and wait for FRIDAY_READY. On failure the process
        tree is killed, so a retry starts clean. Returns True when ready."""
        self.start()
        ready = await self.wait_ready(timeout=timeout)
        if ready and self.alive:
            return True
        logger.error("Agent failed to boot (%s)",
                     "died during boot" if not self.alive
                     else f"no FRIDAY_READY within {timeout:.0f}s")
        self.stop()
        return False

    def _send(self, cmd: str) -> bool:
        """Write one protocol command to the subprocess's stdin."""
        if not self.alive:
            logger.warning("Cannot send %s — agent subprocess is dead", cmd)
            return False
        try:
            assert self._proc and self._proc.stdin
            self._proc.stdin.write(f"{cmd}\n")
            self._proc.stdin.flush()
            return True
        except Exception as e:
            logger.error("Failed to write %s: %s", cmd, e)
            return False

    def send_start(self):
        """Tell the subprocess to begin a new voice session (prep + greeting)."""
        self._session_done.clear()
        return self._send("START")

    def send_start_recovered(self):
        """Begin a session that opens with the recovery line (post-crash respawn)."""
        self._session_done.clear()
        return self._send("START_RECOVERED")

    def send_prepare(self):
        """Begin silent prep. Must be followed by send_greet() or send_abort().

        Sent the instant the wake word fires so the agent's STT reconnect
        overlaps with the launcher's fast-path tail capture.
        """
        self._session_done.clear()
        return self._send("PREPARE")

    def send_greet(self):
        """Complete a PREPARE: speak the ready line and open the mic."""
        return self._send("GREET")

    def send_abort(self):
        """Cancel a PREPARE — the fast path handled it, or the speaker failed
        verification. No session begins and no SESSION_DONE is printed."""
        return self._send("ABORT")

    async def wait_session_done(self):
        """Block until SESSION_DONE (or subprocess death)."""
        await asyncio.get_event_loop().run_in_executor(
            None, self._session_done.wait
        )

    def send_interrupt(self):
        """Tell the subprocess to interrupt the current in-flight turn."""
        if not self.alive:
            return False
        return self._send("INTERRUPT")

    def stop(self):
        """Gracefully shut down the subprocess and its entire descendant tree.

        The agent spawns an MCP server (server.py) as a child, which may itself
        spawn task-executor grandchildren. subprocess.terminate() only kills the
        immediate process — descendants get reparented and become multi-GB
        orphans that accumulate across restarts. Use taskkill /T /F to wipe
        the whole tree for sure.
        """
        if not self.alive:
            return
        pid = self._proc.pid
        try:
            assert self._proc and self._proc.stdin
            self._proc.stdin.write("QUIT\n")
            self._proc.stdin.flush()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        # Even after a graceful exit, MCP children sometimes outlive their
        # parent on Windows. Hard-kill the entire tree to be sure.
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except Exception as e:
            logger.warning("taskkill /T failed for PID %d: %s", pid, e)
        logger.info("Agent subprocess stopped (tree killed)")


async def boot_with_retries(agent, *, attempts: int, timeout: float,
                            sleep=asyncio.sleep) -> bool:
    """Boot the agent, retrying with backoff. True once it reports ready."""
    for attempt in range(attempts):
        if attempt:
            delay = respawn_backoff(attempt)
            logger.warning("Retrying agent boot in %.0fs (attempt %d/%d)",
                           delay, attempt + 1, attempts)
            await sleep(delay)
        if await agent.boot(timeout):
            return True
    return False


def _spawn_relaunch() -> bool:
    """Start a fresh, detached JARVIS via the silent launcher script.

    Returns False — and spawns nothing — when the script is missing: wscript.exe
    "succeeds" even for a missing file, so the restart hotkey used to kill
    JARVIS and leave nothing running in its place.
    """
    if not SILENT_LAUNCHER.exists():
        logger.error("Restart aborted — %s is missing; JARVIS stays up",
                     SILENT_LAUNCHER.name)
        return False
    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP unlinks it from our job, so it
    # survives us dying.
    subprocess.Popen(
        ["wscript.exe", str(SILENT_LAUNCHER)],
        cwd=str(SILENT_LAUNCHER.parent),
        creationflags=(
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
        ),
        close_fds=True,
    )
    return True


async def launcher_loop():
    if _disable_power_throttling():
        logger.info("Power throttling: DISABLED (full CPU)")
    else:
        logger.warning("Power throttling: could not disable (err=%r)", _THROTTLE_ERR)
    overlay = FridayOverlay()
    pending_recovery_cue = threading.Event()
    # Set only while a session is live (mic active / user conversing). A fatal
    # close while this is clear (idle, awaiting wake word) recovers silently.
    session_active = threading.Event()

    def _on_fatal():
        if should_announce_recovery(session_active=session_active.is_set()):
            pending_recovery_cue.set()

    agent = AgentProcess(
        on_processing=lambda: overlay.show_loading("Thinking..."),
        on_speaking=lambda: overlay.hide_loading(),
        on_listening=lambda: overlay.show(),
        on_fatal=_on_fatal,
        on_idle=lambda: overlay.show(),   # silent turn ended → back to listening
    )

    overlay.start()

    # ---- Cold boot: spawn the agent FIRST ----------------------------------
    # Its ~20s boot is the long pole. Starting it before the launcher loads its
    # own models (openwakeword ~12s cold, Resemblyzer, Vosk) lets the two
    # overlap instead of adding up. The mic stream still opens only after
    # FRIDAY_READY. Guarded by tests/test_launcher_boot_order.py.
    overlay.show_loading("Booting up Omnicron...")
    _ensure_boot_ack_clip()
    boot_task = asyncio.create_task(
        boot_with_retries(agent, attempts=BOOT_ATTEMPTS, timeout=BOOT_TIMEOUT)
    )
    await asyncio.sleep(0)   # let the task run up to its first await: agent spawned

    try:
        wakeword = WakeWordListener()
        verifier = SpeakerVerifier()
        # Pay Resemblyzer's ~4.6s JIT now, in parallel with the agent's cold
        # boot, so the first wake word doesn't stall in verification. See warm().
        if verifier.enabled:
            threading.Thread(target=verifier.warm, daemon=True,
                             name="verifier-warmup").start()

        # Fast path: fixed commands ("mute", "play", "next") spoken in the same
        # breath as the wake word run here, never reaching the agent. Import is
        # lazy so a disabled fast path costs no vosk load at boot.
        fastpath = None
        if FASTPATH_ENABLED:
            _ensure_fastpath_ack_clip()
            try:
                from friday.fastpath.recognizer import TailRecognizer

                fastpath = TailRecognizer()
                if not fastpath.available:
                    fastpath = None
            except Exception as e:
                logger.warning("Fast path init failed (%s) — disabled", e)
                fastpath = None
            if fastpath is None:
                logger.warning("Fast path unavailable — commands will route normally")
    except BaseException:
        # Launcher init failed — don't leave the already-spawned agent orphaned.
        boot_task.cancel()
        agent.stop()
        overlay.stop()
        raise

    # ---- Global hotkeys --------------------------------------------------
    # Registered on the OS hook, so they fire even when JARVIS isn't focused.

    def _teardown():
        """Best-effort shutdown of every resource we own."""
        try:
            overlay.stop()
        except Exception:
            pass
        try:
            wakeword.cleanup()
        except Exception:
            pass
        try:
            agent.stop()
        except Exception:
            pass

    def _kill_switch():
        logger.warning("KILL SWITCH pressed (%s) — shutting down NOW", KILL_HOTKEY)
        _teardown()
        # Hard-exit — skip asyncio teardown so nothing can hang.
        os._exit(0)

    def _restart_switch():
        logger.warning("RESTART pressed (%s) — relaunching to pick up code changes",
                       RESTART_HOTKEY)
        try:
            if not _spawn_relaunch():
                return
            logger.info("New JARVIS instance spawned — dying now")
        except Exception as e:
            logger.error("Restart spawn failed: %s", e)
            return
        _teardown()
        os._exit(0)

    try:
        keyboard.add_hotkey(KILL_HOTKEY, _kill_switch)
        logger.info("Kill switch armed → press %s to terminate JARVIS", KILL_HOTKEY)
    except Exception as e:
        logger.warning("Could not register kill-switch hotkey: %s", e)

    try:
        keyboard.add_hotkey(RESTART_HOTKEY, _restart_switch)
        logger.info("Restart armed → press %s to reload JARVIS with new code",
                    RESTART_HOTKEY)
    except Exception as e:
        logger.warning("Could not register restart hotkey: %s", e)

    # ---- Push-to-talk activation -----------------------------------------
    # The SLEEPING loop checks this event every wake-word tick (~80ms), so
    # perceived latency from key press to chime is < 100ms. The loop is
    # what decides when to honour it — stale presses during ACTIVE are
    # cleared on re-entry to SLEEPING.
    ptt_event = threading.Event()

    def _ptt_pressed():
        # Context-aware: ACTIVE → interrupt the in-flight turn; otherwise → wake.
        # `state` is closure-captured from launcher_loop and updated by reference.
        if state == State.ACTIVE:
            logger.info("PTT pressed (%s) — interrupting active turn", PTT_HOTKEY)
            agent.send_interrupt()
        else:
            logger.info("PTT pressed (%s) — wake", PTT_HOTKEY)
            ptt_event.set()

    try:
        keyboard.add_hotkey(PTT_HOTKEY, _ptt_pressed)
        logger.info("Push-to-talk armed → press %s to activate without wake word",
                    PTT_HOTKEY)
    except Exception as e:
        logger.warning("Could not register PTT hotkey: %s", e)

    # ---- Wait for the agent boot started at the top of this function ----
    if not await boot_task:
        logger.error("Agent subprocess failed to start after %d attempts — exiting",
                     BOOT_ATTEMPTS)
        overlay.stop()
        return
    overlay.hide()           # hide the initial boot overlay
    logger.info("Agent subprocess ready — models loaded")

    wakeword.start_stream()
    state = State.SLEEPING
    recovery_started = False  # set when START_RECOVERED was already sent post-respawn
    greet_sent = False        # PREPARE+GREET already sent; ACTIVE must not re-send
    respawn_failures = 0      # consecutive failed respawns → backoff
    ready_at = time.monotonic()  # when the current agent reported FRIDAY_READY
    logger.info("JARVIS launcher ready — say 'Hey Jarvis' to activate")

    try:
        while True:
            if state == State.SLEEPING:
                # Respawn when the agent process died OR its session died
                # (SESSION_FATAL) — a fatal-closed agent can't serve sessions
                # even if its process lingers.
                if agent.needs_respawn:
                    logger.warning("Agent %s — respawning…",
                                   "session died" if agent.alive else "subprocess died")
                    overlay.show_loading("Rebooting JARVIS...")
                    wakeword.stop_stream()
                    # An agent that died right after booting (network down at
                    # boot) counts as a failure, so the backoff keeps growing.
                    respawn_failures = next_respawn_failures(
                        respawn_failures, uptime=time.monotonic() - ready_at)
                    await asyncio.sleep(respawn_backoff(respawn_failures))
                    agent.stop()   # no-op when already dead; kills a lingering tree
                    if not await agent.boot(BOOT_TIMEOUT):
                        ready_at = time.monotonic()  # boot failure = zero uptime
                        continue   # retry after the next backoff
                    ready_at = time.monotonic()
                    overlay.hide()
                    wakeword.start_stream()
                    # If the death was a fatal session close (SESSION_FATAL),
                    # come back speaking — re-activate with the recovery line
                    # instead of waiting silently for the next wake word.
                    if pending_recovery_cue.is_set():
                        pending_recovery_cue.clear()
                        logger.info("Recovering from fatal close — re-activating with recovery line")
                        play_activation_ack()
                        overlay.show_loading("Reconnecting...")
                        agent.send_start_recovered()
                        recovery_started = True
                        state = State.ACTIVE
                        logger.info("State → ACTIVE (recovery)")
                        continue

                # Push-to-talk has priority — explicit user action skips
                # both wake-word match and speaker verification.
                if ptt_event.is_set():
                    ptt_event.clear()
                    greet_sent = False   # PTT sends nothing; ACTIVE must send START
                    logger.info("Manual activation via PTT — bypassing wake/speaker checks")
                    play_activation_ack()
                    overlay.show_loading("Waking up...")
                    state = State.ACTIVE
                    logger.info("State → ACTIVE")
                    continue

                detected = await asyncio.get_event_loop().run_in_executor(
                    None, wakeword.listen_once
                )
                if detected:
                    # Snapshot the ring buffer BEFORE tail capture starts —
                    # capture consumes the stream, so the buffer stops updating
                    # and verification must not race it.
                    wav = wakeword.recent_audio_float()

                    # False trigger on a silent mic: drop it before it flashes
                    # the overlay or wakes the agent. See WAKE_SILENCE_PEAK.
                    if _is_near_silent(wav):
                        logger.info("Wake word ignored — mic buffer is silent "
                                    "(false trigger)")
                        wakeword.reset()
                        continue

                    # Instant visual feedback the moment the wake word is
                    # recognised; the spoken acknowledgement follows successful
                    # speaker verification.
                    overlay.show_loading("Waking up...")
                    loop = asyncio.get_event_loop()

                    # Send PREPARE now so the agent's STT reconnect overlaps
                    # with tail capture instead of following it.
                    agent.send_prepare()

                    verify_task = loop.run_in_executor(None, verifier.verify, wav)
                    if fastpath is not None:
                        tail_task = loop.run_in_executor(
                            None, fastpath.capture_and_match, wakeword.read_chunk
                        )
                    else:
                        tail_task = asyncio.sleep(0, result=None)

                    (matched, sim), fast_cmd = await asyncio.gather(
                        verify_task, tail_task
                    )

                    if verifier.enabled and not matched:
                        logger.info(
                            "Wake word ignored — speaker mismatch (sim=%.3f < %.2f)",
                            sim, SPEAKER_SIM_THRESHOLD,
                        )
                        agent.send_abort()
                        overlay.hide()
                        wakeword.reset()
                        continue
                    if verifier.enabled:
                        logger.info("Speaker verified (sim=%.3f)", sim)

                    if fast_cmd is not None:
                        logger.info("fastpath: executing %s", fast_cmd.name)
                        play_fastpath_ack()
                        await loop.run_in_executor(None, _run_fast_action, fast_cmd)
                        agent.send_abort()
                        overlay.hide()
                        wakeword.reset()
                        continue

                    play_activation_ack()
                    agent.send_greet()
                    greet_sent = True
                    state = State.ACTIVE
                    logger.info("State → ACTIVE")

            elif state == State.ACTIVE:
                wakeword.stop_stream()
                session_active.set()   # mid-conversation: a fatal drop now re-announces

                # Tell the already-running subprocess to start a session.
                # The overlay shows "Waking up..." from the wake word handler;
                # it'll switch to bars when the agent sends SPEAKING, or
                # show "Thinking..." on PROCESSING signals.
                if recovery_started or greet_sent:
                    # START_RECOVERED, or PREPARE+GREET, already sent.
                    recovery_started = False
                    greet_sent = False
                else:
                    agent.send_start()

                # Wait for the session to end (dismissal or crash).
                await agent.wait_session_done()
                session_active.clear()  # back to idle; further drops recover silently

                overlay.hide()
                await asyncio.sleep(1.5)
                wakeword.reset()
                wakeword.start_stream()
                ptt_event.clear()  # discard any presses during the session
                state = State.SLEEPING
                logger.info("State → SLEEPING")

    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        overlay.stop()
        wakeword.cleanup()
        agent.stop()
        logger.info("JARVIS launcher stopped")


def main():
    asyncio.run(launcher_loop())


if __name__ == "__main__":
    main()
