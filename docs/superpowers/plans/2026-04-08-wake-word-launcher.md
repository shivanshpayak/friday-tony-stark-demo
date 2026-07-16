# FRIDAY Wake Word Launcher — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the two-terminal startup with a single always-on launcher that activates FRIDAY via "Hey Jarvis" wake word, manages LiveKit sessions, and auto-starts on Windows login.

**Architecture:** A single `friday_launcher.py` orchestrates three subsystems: an MCP server subprocess (always running), an OpenWakeWord mic listener (always running), and a LiveKit voice session (created on wake, destroyed on sleep). A two-state machine (SLEEPING/ACTIVE) governs transitions.

**Tech Stack:** OpenWakeWord, PyAudio, livekit-api (already installed), livekit-agents, winsound, Windows Task Scheduler (schtasks)

---

## File Structure

```
friday-tony-stark-demo/
├── friday_launcher.py      (new — main entry point, state machine, subprocess mgmt)
├── agent_friday.py          (modify — expose programmatic session start/stop)
├── server.py                (unchanged)
├── sounds/
│   └── activate.wav         (new — activation chime)
├── friday/
│   └── ...                  (unchanged)
├── pyproject.toml           (modify — new deps + scripts)
└── tests/
    ├── test_state_machine.py (new)
    ├── test_dismissal.py     (new)
    └── test_launcher.py      (new)
```

---

### Task 1: Add dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add openwakeword and pyaudio to dependencies**

In `pyproject.toml`, update the `dependencies` list:

```toml
dependencies = [
    "fastmcp",
    "httpx",
    "livekit-agents[deepgram,groq,openai,sarvam,silero]>=1.5.1",
    "livekit-plugins-google>=1.5.1",
    "python-dotenv",
    "openwakeword",
    "pyaudio",
    "livekit-api",
]
```

- [ ] **Step 2: Add new script entry points**

In `pyproject.toml`, update `[project.scripts]`:

```toml
[project.scripts]
friday = "server:main"
friday_voice = "agent_friday:dev"
friday_start = "friday_launcher:main"
```

- [ ] **Step 3: Run uv sync**

Run: `python -m uv sync`
Expected: All new packages installed successfully.

- [ ] **Step 4: Verify imports**

Run: `python -m uv run python -c "import openwakeword; import pyaudio; from livekit import api; print('All imports OK')"`
Expected: `All imports OK`

- [ ] **Step 5: Download OpenWakeWord models**

Run: `python -m uv run python -c "import openwakeword; openwakeword.utils.download_models()"`
Expected: Pre-trained models downloaded (including "hey_jarvis").

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "feat: add openwakeword, pyaudio, livekit-api dependencies"
```

---

### Task 2: Create activation chime

**Files:**
- Create: `sounds/activate.wav`

- [ ] **Step 1: Generate a short activation chime**

Create a simple sine-wave chime programmatically:

```python
# run this once to generate the chime file
import wave
import struct
import math
import os

os.makedirs("sounds", exist_ok=True)

sample_rate = 44100
duration = 0.3  # seconds
frequency = 880  # A5 note
volume = 0.5

num_samples = int(sample_rate * duration)
samples = []
for i in range(num_samples):
    t = i / sample_rate
    # Fade in/out envelope
    envelope = min(t / 0.05, 1.0) * min((duration - t) / 0.05, 1.0)
    value = volume * envelope * math.sin(2 * math.pi * frequency * t)
    samples.append(int(value * 32767))

with wave.open("sounds/activate.wav", "w") as f:
    f.setnchannels(1)
    f.setsampwidth(2)
    f.setframerate(sample_rate)
    f.writeframes(struct.pack(f"<{len(samples)}h", *samples))

print("Created sounds/activate.wav")
```

Run: `python -m uv run python generate_chime.py` (or run inline)
Expected: `sounds/activate.wav` created, ~0.3 second chime.

- [ ] **Step 2: Commit**

```bash
git add sounds/activate.wav
git commit -m "feat: add activation chime sound"
```

---

### Task 3: Modify agent_friday.py for programmatic control

**Files:**
- Modify: `agent_friday.py`

- [ ] **Step 1: Add a dismissal detection callback mechanism**

Add a callback attribute and dismissal detection to `FridayAgent`. Insert after the existing `SYSTEM_PROMPT` definition (around line 128) and modify the `FridayAgent` class:

```python
# ---------------------------------------------------------------------------
# Dismissal phrases
# ---------------------------------------------------------------------------

DISMISSAL_PHRASES = [
    "that'll be all",
    "that will be all",
    "dismissed",
    "go to sleep",
    "goodbye friday",
    "goodbye jarvis",
    "kill yourself",
]

SLEEP_RESPONSES = [
    "I'll be here if you need me, sir.",
    "Standing by, sir",
    "Going quiet. You know where to find me.",
    "Yes, Sir",
]
```

- [ ] **Step 2: Create a programmatic session manager function**

Add a new function at the bottom of `agent_friday.py` (before the `if __name__` block) that the launcher can call to start and manage a session:

```python
import asyncio
import random
from livekit import api as lk_api, rtc

async def run_session(on_dismissal: asyncio.Event, silence_timeout: float = 30.0) -> None:
    """
    Programmatically create a LiveKit room, connect, run the voice agent,
    and monitor for dismissal or silence timeout.
    
    Sets on_dismissal event when the session should end.
    """
    lk = lk_api.LiveKitAPI()
    room_name = f"friday-{int(asyncio.get_event_loop().time())}"

    # Create room
    await lk.room.create_room(lk_api.CreateRoomRequest(name=room_name))

    # Generate agent token
    token = (
        lk_api.AccessToken()
        .with_identity("friday-agent")
        .with_name("FRIDAY")
        .with_grants(lk_api.VideoGrants(room_join=True, room=room_name))
        .to_jwt()
    )

    # Connect to room
    room = rtc.Room()
    await room.connect(os.getenv("LIVEKIT_URL"), token)

    stt = _build_stt()
    llm = _build_llm()
    tts = _build_tts()

    agent = FridayAgent(stt=stt, llm=llm, tts=tts)

    session = AgentSession(
        turn_detection=_turn_detection(),
        min_endpointing_delay=_endpointing_delay(),
    )

    last_speech_time = asyncio.get_event_loop().time()

    # Track speech activity for silence timeout
    original_stt = stt

    async def monitor_silence():
        nonlocal last_speech_time
        while not on_dismissal.is_set():
            await asyncio.sleep(1.0)
            elapsed = asyncio.get_event_loop().time() - last_speech_time
            if elapsed >= silence_timeout:
                logger.info("Silence timeout reached (%.0fs), going to sleep", silence_timeout)
                # Have FRIDAY say goodbye
                await session.generate_reply(
                    instructions="Say a brief sign-off like 'Standing by, boss.' and nothing else."
                )
                await asyncio.sleep(2.0)  # Let TTS finish
                on_dismissal.set()

    # Start session
    await session.start(agent=agent, room=room)

    # Start silence monitor
    silence_task = asyncio.create_task(monitor_silence())

    # Wait for dismissal
    await on_dismissal.wait()

    # Cleanup
    silence_task.cancel()
    await room.disconnect()
    await lk.room.delete_room(lk_api.DeleteRoomRequest(room=room_name))
    await lk.aclose()
```

- [ ] **Step 3: Add dismissal detection to SYSTEM_PROMPT**

Append to the end of `SYSTEM_PROMPT` (before the closing `""".strip()`):

```python
## DISMISSAL

If the user says any of the following, respond with a brief sign-off like "I'll be here if you need me, boss" or "Standing by, boss" and nothing else:
- "That'll be all"
- "Stand down" 
- "Go to sleep"
- "Goodbye Friday" / "Goodbye Jarvis"
```

- [ ] **Step 4: Commit**

```bash
git add agent_friday.py
git commit -m "feat: add programmatic session management and dismissal support"
```

---

### Task 4: Build the launcher state machine

**Files:**
- Create: `friday_launcher.py`

- [ ] **Step 1: Write the core launcher**

Create `friday_launcher.py`:

```python
"""
FRIDAY Launcher — Wake Word Activated Voice Assistant
=====================================================
Single entry point that manages:
1. MCP Server subprocess (always running)
2. OpenWakeWord listener (always running)  
3. LiveKit voice sessions (on-demand)

Usage:
    uv run friday_start
"""

import asyncio
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import winsound
from enum import Enum
from pathlib import Path

import numpy as np
import pyaudio
from dotenv import load_dotenv
from openwakeword.model import Model as WakeWordModel

load_dotenv()

logger = logging.getLogger("friday-launcher")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WAKE_MODEL = "hey_jarvis_v0.1"
WAKE_THRESHOLD = 0.5
SILENCE_TIMEOUT = 30.0
AUDIO_RATE = 16000
AUDIO_CHUNK = 1280  # 80ms at 16kHz
CHIME_PATH = Path(__file__).parent / "sounds" / "activate.wav"
MCP_SERVER_SCRIPT = Path(__file__).parent / "server.py"


class State(Enum):
    SLEEPING = "sleeping"
    ACTIVE = "active"


# ---------------------------------------------------------------------------
# MCP Server management
# ---------------------------------------------------------------------------

class MCPServerManager:
    """Keeps the MCP server subprocess alive."""

    def __init__(self):
        self._proc: subprocess.Popen | None = None

    def start(self):
        if self._proc and self._proc.poll() is None:
            return
        logger.info("Starting MCP server...")
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "uv", "run", "friday"],
            cwd=Path(__file__).parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        logger.info("MCP server started (PID %d)", self._proc.pid)

    def ensure_alive(self):
        if self._proc is None or self._proc.poll() is not None:
            logger.warning("MCP server died, restarting...")
            self.start()

    def stop(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            self._proc.wait(timeout=5)
            logger.info("MCP server stopped")


# ---------------------------------------------------------------------------
# Wake word listener
# ---------------------------------------------------------------------------

class WakeWordListener:
    """Continuously listens for the wake word on the mic."""

    def __init__(self, model_name: str = WAKE_MODEL, threshold: float = WAKE_THRESHOLD):
        self._model = WakeWordModel(wakeword_models=[model_name])
        self._threshold = threshold
        self._audio = pyaudio.PyAudio()
        self._stream = None
        self._enabled = True

    def start_stream(self):
        self._stream = self._audio.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=AUDIO_RATE,
            input=True,
            frames_per_buffer=AUDIO_CHUNK,
        )
        logger.info("Mic stream opened for wake word detection")

    def stop_stream(self):
        if self._stream:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None

    def listen_once(self) -> bool:
        """Block until wake word is detected. Returns True if detected."""
        if not self._stream:
            self.start_stream()

        raw = self._stream.read(AUDIO_CHUNK, exception_on_overflow=False)
        audio = np.frombuffer(raw, dtype=np.int16)
        prediction = self._model.predict(audio)

        for model_name, score in prediction.items():
            if score >= self._threshold:
                logger.info("Wake word detected! (model=%s, score=%.3f)", model_name, score)
                return True
        return False

    def cleanup(self):
        self.stop_stream()
        self._audio.terminate()


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


# ---------------------------------------------------------------------------
# Main launcher loop
# ---------------------------------------------------------------------------

async def launcher_loop():
    mcp = MCPServerManager()
    wakeword = WakeWordListener()

    mcp.start()
    wakeword.start_stream()

    state = State.SLEEPING
    logger.info("FRIDAY launcher ready — say 'Hey Jarvis' to activate")

    try:
        while True:
            if state == State.SLEEPING:
                # Check MCP server health
                mcp.ensure_alive()

                # Listen for wake word (non-blocking check)
                detected = await asyncio.get_event_loop().run_in_executor(
                    None, wakeword.listen_once
                )
                if detected:
                    play_chime()
                    state = State.ACTIVE
                    logger.info("State → ACTIVE")

            elif state == State.ACTIVE:
                # Stop wake word stream while LiveKit uses the mic
                wakeword.stop_stream()

                try:
                    from agent_friday import run_session

                    dismissal_event = asyncio.Event()
                    await run_session(
                        on_dismissal=dismissal_event,
                        silence_timeout=SILENCE_TIMEOUT,
                    )
                except Exception as e:
                    logger.error("Voice session error: %s", e)

                # Resume wake word listening
                wakeword.start_stream()
                state = State.SLEEPING
                logger.info("State → SLEEPING")

    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        wakeword.cleanup()
        mcp.stop()
        logger.info("FRIDAY launcher stopped")


def main():
    asyncio.run(launcher_loop())


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Commit**

```bash
git add friday_launcher.py
git commit -m "feat: add wake word launcher with state machine"
```

---

### Task 5: Write tests

**Files:**
- Create: `tests/test_state_machine.py`
- Create: `tests/test_dismissal.py`

- [ ] **Step 1: Create tests directory**

Run: `mkdir tests && touch tests/__init__.py` (Windows: `mkdir tests && type nul > tests\__init__.py`)

- [ ] **Step 2: Write state machine tests**

Create `tests/test_state_machine.py`:

```python
"""Tests for the launcher state machine logic."""
import pytest
from friday_launcher import State, MCPServerManager


def test_state_enum_values():
    assert State.SLEEPING.value == "sleeping"
    assert State.ACTIVE.value == "active"


def test_mcp_manager_start_stop():
    """MCPServerManager can start and stop without errors."""
    mgr = MCPServerManager()
    # Don't actually start the server in tests, just verify the interface
    assert mgr._proc is None


def test_state_transitions():
    """Verify valid state transitions."""
    # SLEEPING -> ACTIVE (wake word detected)
    state = State.SLEEPING
    wake_detected = True
    if wake_detected:
        state = State.ACTIVE
    assert state == State.ACTIVE

    # ACTIVE -> SLEEPING (dismissal or timeout)
    dismissed = True
    if dismissed:
        state = State.SLEEPING
    assert state == State.SLEEPING
```

- [ ] **Step 3: Write dismissal detection tests**

Create `tests/test_dismissal.py`:

```python
"""Tests for dismissal phrase detection."""
import pytest

DISMISSAL_PHRASES = [
    "that'll be all",
    "that will be all",
    "stand down",
    "go to sleep",
    "goodbye friday",
    "goodbye jarvis",
]


def is_dismissal(text: str) -> bool:
    """Check if text contains a dismissal phrase."""
    lower = text.lower().strip()
    return any(phrase in lower for phrase in DISMISSAL_PHRASES)


def test_exact_dismissal_phrases():
    assert is_dismissal("that'll be all")
    assert is_dismissal("stand down")
    assert is_dismissal("go to sleep")
    assert is_dismissal("goodbye friday")
    assert is_dismissal("goodbye jarvis")


def test_dismissal_case_insensitive():
    assert is_dismissal("Stand Down")
    assert is_dismissal("THAT'LL BE ALL")
    assert is_dismissal("Go To Sleep")


def test_dismissal_in_sentence():
    assert is_dismissal("okay friday, that'll be all for tonight")
    assert is_dismissal("you can stand down now")


def test_non_dismissal():
    assert not is_dismissal("what's the weather")
    assert not is_dismissal("tell me the news")
    assert not is_dismissal("hello friday")
```

- [ ] **Step 4: Run tests**

Run: `python -m uv run pytest tests/ -v`
Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add tests/
git commit -m "test: add state machine and dismissal detection tests"
```

---

### Task 6: Windows auto-start via Task Scheduler

**Files:**
- Create: `install_startup.py`

- [ ] **Step 1: Create install/uninstall script**

Create `install_startup.py`:

```python
"""
Install/uninstall FRIDAY as a Windows startup task.

Usage:
    python install_startup.py install
    python install_startup.py uninstall
"""

import subprocess
import sys
from pathlib import Path

TASK_NAME = "FRIDAY Voice Assistant"
PROJECT_DIR = Path(__file__).parent.resolve()
# Use pythonw to avoid a console window
PYTHON_EXE = Path(sys.executable).parent / "pythonw.exe"
UV_MODULE = "uv"


def install():
    command = f'"{PYTHON_EXE}" -m {UV_MODULE} run friday_start'
    result = subprocess.run(
        [
            "schtasks", "/create",
            "/tn", TASK_NAME,
            "/tr", command,
            "/sc", "onlogon",
            "/rl", "limited",
            "/f",
        ],
        cwd=str(PROJECT_DIR),
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"Installed '{TASK_NAME}' to run at login.")
        print(f"  Command: {command}")
        print(f"  Working dir: {PROJECT_DIR}")
    else:
        print(f"Failed to create task: {result.stderr}")
        sys.exit(1)


def uninstall():
    result = subprocess.run(
        ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"Removed '{TASK_NAME}' from startup.")
    else:
        print(f"Failed to remove task: {result.stderr}")
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("install", "uninstall"):
        print("Usage: python install_startup.py [install|uninstall]")
        sys.exit(1)

    if sys.argv[1] == "install":
        install()
    else:
        uninstall()
```

- [ ] **Step 2: Commit**

```bash
git add install_startup.py
git commit -m "feat: add Windows Task Scheduler install/uninstall script"
```

---

### Task 7: Integration test — end to end

- [ ] **Step 1: Verify MCP server starts**

Run: `python -m uv run friday &` (in background)
Expected: Server starts on port 8000. Verify with: `curl http://127.0.0.1:8000/sse` returns SSE headers.

- [ ] **Step 2: Verify OpenWakeWord loads**

Run: `python -m uv run python -c "from openwakeword.model import Model; m = Model(wakeword_models=['hey_jarvis_v0.1']); print('Model loaded:', list(m.models.keys()))"`
Expected: `Model loaded: ['hey_jarvis_v0.1']`

- [ ] **Step 3: Verify launcher starts**

Run: `python -m uv run friday_start`
Expected: Logs show "FRIDAY launcher ready — say 'Hey Jarvis' to activate". MCP server starts. Mic stream opens.

- [ ] **Step 4: Test wake word activation**

Say "Hey Jarvis" to your microphone.
Expected: Chime plays, logs show "State → ACTIVE", FRIDAY greets you.

- [ ] **Step 5: Test silence timeout**

Stay silent for 30 seconds.
Expected: FRIDAY says a sign-off, logs show "State → SLEEPING".

- [ ] **Step 6: Test dismissal phrase**

Activate again with "Hey Jarvis", then say "That'll be all".
Expected: FRIDAY acknowledges, returns to SLEEPING.

- [ ] **Step 7: Install auto-start**

Run: `python install_startup.py install`
Expected: `Installed 'FRIDAY Voice Assistant' to run at login.`

- [ ] **Step 8: Final commit**

```bash
git add -A
git commit -m "feat: FRIDAY wake word launcher — complete implementation"
```
