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
from openwakeword.model import Model as WakeWordModel
from friday_overlay import FridayOverlay


# ---------------------------------------------------------------------------
# Disable Windows EcoQoS / power throttling
# ---------------------------------------------------------------------------
# When launched silently from startup, Windows marks us as a background
# process and throttles CPU to favour battery life. That makes the overlay
# visibly laggy and slows model inference. Opt out explicitly.

_PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
_PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
_ProcessPowerThrottling = 4

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

LOG_DIR = Path(__file__).parent / "logs"
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


class WakeWordListener:
    """Continuously listens for the wake word on the mic."""

    def __init__(self, model_name: str = WAKE_MODEL, threshold: float = WAKE_THRESHOLD):
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

    def verify(self, wav_float: np.ndarray) -> tuple[bool, float]:
        """Return (matched, similarity). If disabled, always (True, 1.0)."""
        if not self.enabled:
            return True, 1.0
        try:
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
# Main launcher loop
# ---------------------------------------------------------------------------

class AgentProcess:
    """Manages the single long-lived agent_friday.py subprocess.

    Protocol (line-based, over stdin/stdout):
        launcher → subprocess stdin:   START | QUIT
        subprocess stdout → launcher:  FRIDAY_READY | SESSION_STARTED |
                                       SESSION_LISTENING | SESSION_DONE |
                                       PROCESSING | SPEAKING
    """

    def __init__(self, on_processing=None, on_speaking=None, on_listening=None):
        self._proc: subprocess.Popen | None = None
        self._ready = threading.Event()
        self._session_done = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self._on_processing = on_processing  # callback when LLM starts thinking
        self._on_speaking = on_speaking       # callback when first TTS content arrives
        self._on_listening = on_listening     # callback when mic is live again

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self):
        """Spawn the subprocess and wait for FRIDAY_READY."""
        if self.alive:
            return
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        self._ready.clear()
        self._session_done.clear()
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
        try:
            PROCESS_SET_INFORMATION = 0x0200
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = _kernel32.OpenProcess(
                PROCESS_SET_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION,
                False, self._proc.pid,
            )
            if h:
                _disable_power_throttling(h)
                _kernel32.CloseHandle(h)
        except Exception as e:
            logger.debug("Could not unthrottle child process: %s", e)
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
                stripped = line.rstrip()
                if "FRIDAY_READY" in stripped:
                    logger.info("Agent subprocess signalled FRIDAY_READY")
                    self._ready.set()
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
                elif stripped:
                    # Skip noisy LiveKit SDK internal debug lines.
                    if any(p in stripped for p in self._NOISE_PATTERNS):
                        continue
                    # Log via the agent-tagged logger (goes to file + console).
                    logging.getLogger("friday-agent").info(stripped)
        except Exception as e:
            logger.debug("stdout reader error: %s", e)
        finally:
            self._ready.set()
            self._session_done.set()

    async def wait_ready(self, timeout: float = 60.0) -> bool:
        """Block until FRIDAY_READY (or timeout/death)."""
        return await asyncio.get_event_loop().run_in_executor(
            None, self._ready.wait, timeout
        )

    def send_start(self):
        """Tell the subprocess to begin a new voice session."""
        if not self.alive:
            logger.warning("Cannot send START — agent subprocess is dead")
            return
        self._session_done.clear()
        assert self._proc and self._proc.stdin
        try:
            self._proc.stdin.write("START\n")
            self._proc.stdin.flush()
        except Exception as e:
            logger.error("Failed to write START: %s", e)

    async def wait_session_done(self):
        """Block until SESSION_DONE (or subprocess death)."""
        await asyncio.get_event_loop().run_in_executor(
            None, self._session_done.wait
        )

    def send_interrupt(self):
        """Tell the subprocess to interrupt the current in-flight turn."""
        if not self.alive:
            return
        try:
            assert self._proc and self._proc.stdin
            self._proc.stdin.write("INTERRUPT\n")
            self._proc.stdin.flush()
        except Exception as e:
            logger.error("Failed to write INTERRUPT: %s", e)

    def stop(self):
        """Gracefully shut down the subprocess."""
        if not self.alive:
            return
        try:
            assert self._proc and self._proc.stdin
            self._proc.stdin.write("QUIT\n")
            self._proc.stdin.flush()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            self._proc.wait(timeout=3)
        logger.info("Agent subprocess stopped")


async def launcher_loop():
    if _disable_power_throttling():
        logger.info("Power throttling: DISABLED (full CPU)")
    else:
        logger.warning("Power throttling: could not disable (err=%r)", _THROTTLE_ERR)
    wakeword = WakeWordListener()
    overlay = FridayOverlay()
    verifier = SpeakerVerifier()
    agent = AgentProcess(
        on_processing=lambda: overlay.show_loading("Thinking..."),
        on_speaking=lambda: overlay.hide_loading(),
        on_listening=lambda: overlay.show(),
    )

    overlay.start()

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
        # Spawn the silent .vbs wrapper fully detached, so it survives us dying.
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP unlinks it from our job.
        try:
            subprocess.Popen(
                ["wscript.exe", str(SILENT_LAUNCHER)],
                cwd=str(SILENT_LAUNCHER.parent),
                creationflags=(
                    subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NEW_PROCESS_GROUP
                ),
                close_fds=True,
            )
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

    # ---- One-time cold boot: spawn the agent and load models ----
    overlay.show_loading("Booting up Omnicron...")
    _ensure_boot_ack_clip()
    agent.start()
    ready = await agent.wait_ready(timeout=60.0)
    if not ready or not agent.alive:
        logger.error("Agent subprocess failed to start — exiting")
        overlay.stop()
        return
    overlay.hide()           # hide the initial boot overlay
    logger.info("Agent subprocess ready — models loaded")

    wakeword.start_stream()
    state = State.SLEEPING
    logger.info("JARVIS launcher ready — say 'Hey Jarvis' to activate")

    try:
        while True:
            if state == State.SLEEPING:
                # If the agent subprocess died between sessions, respawn it.
                if not agent.alive:
                    logger.warning("Agent subprocess died — respawning…")
                    overlay.show_loading("Rebooting JARVIS...")
                    wakeword.stop_stream()
                    agent.start()
                    await agent.wait_ready(timeout=60.0)
                    overlay.hide()
                    wakeword.start_stream()

                # Push-to-talk has priority — explicit user action skips
                # both wake-word match and speaker verification.
                if ptt_event.is_set():
                    ptt_event.clear()
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
                    # Instant visual feedback the moment the wake word is
                    # recognised; the spoken acknowledgement follows successful
                    # speaker verification.
                    overlay.show_loading("Waking up...")

                    if verifier.enabled:
                        wav = wakeword.recent_audio_float()
                        matched, sim = await asyncio.get_event_loop().run_in_executor(
                            None, verifier.verify, wav
                        )
                        if not matched:
                            logger.info(
                                "Wake word ignored — speaker mismatch (sim=%.3f < %.2f)",
                                sim, SPEAKER_SIM_THRESHOLD,
                            )
                            overlay.hide()
                            continue
                        logger.info("Speaker verified (sim=%.3f)", sim)
                    play_activation_ack()
                    state = State.ACTIVE
                    logger.info("State → ACTIVE")

            elif state == State.ACTIVE:
                wakeword.stop_stream()

                # Tell the already-running subprocess to start a session.
                # The overlay shows "Waking up..." from the wake word handler;
                # it'll switch to bars when the agent sends SPEAKING, or
                # show "Thinking..." on PROCESSING signals.
                agent.send_start()

                # Wait for the session to end (dismissal or crash).
                await agent.wait_session_done()

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
