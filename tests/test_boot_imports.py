"""Boot-latency guards: keep heavy imports off paths that don't need them.

Measured 2026-09-21 (`python -X importtime -c "import server"`): 22 of the MCP
server's 26.6s import went to the full LiveKit + Google Cloud voice stack,
pulled in only because friday/tools/claude_delegate.py imports
friday.tasking.models and friday/tasking/__init__.py eagerly imported the
executor (→ friday.providers). The agent's session.start() waits on that MCP
server, so it sat directly on the boot critical path.

Each check runs in a fresh interpreter so sys.modules reflects only that import.
"""
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _modules_loaded_after(import_stmt: str, candidates: list[str]) -> list[str]:
    code = (
        f"{import_stmt}\n"
        "import sys\n"
        f"print('|'.join(m for m in {candidates!r} if m in sys.modules))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, capture_output=True,
        text=True, timeout=240,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    line = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
    return [m for m in line.split("|") if m]


VOICE_STACK = ["friday.providers", "livekit.agents", "livekit.plugins.google"]


def test_tool_modules_do_not_import_voice_stack():
    """server.py registers friday.tools; none of it may drag in the voice stack."""
    assert _modules_loaded_after("import friday.tools", VOICE_STACK) == []


def test_tasking_models_import_is_lightweight():
    assert _modules_loaded_after("import friday.tasking.models", VOICE_STACK) == []


def test_providers_does_not_import_unselected_plugins():
    """friday.providers used to import deepgram+google+openai+sarvam at module
    level, though only one of each is ever built. livekit.plugins.openai alone
    was ~0.85s of boot (measured 2026-09-22) for a provider this config never
    selects. Each branch imports its own plugin now."""
    from friday.config import STT_PROVIDER, LLM_PROVIDER, TTS_PROVIDER

    selected = {STT_PROVIDER, LLM_PROVIDER, TTS_PROVIDER}
    # "gemini" selects the google plugin; groq/whisper select the openai one.
    plugin_of = {"gemini": "google", "groq": "openai", "whisper": "openai"}
    in_use = {plugin_of.get(p, p) for p in selected}

    unused = [f"livekit.plugins.{p}"
              for p in ("deepgram", "google", "openai", "sarvam")
              if p not in in_use]
    assert unused, "config selects every plugin; this guard would prove nothing"

    assert _modules_loaded_after("import friday.providers", unused) == []


def test_building_the_configured_providers_still_resolves_its_plugin():
    """Lazy imports must not leave a branch referencing an unimported name."""
    from friday.providers import build_stt, build_llm, build_tts

    for build in (build_stt, build_llm, build_tts):
        assert build() is not None


def test_providers_can_be_built_off_the_main_thread():
    """agent_friday warms STT/LLM/TTS via loop.run_in_executor, so build_*()
    runs on worker threads. livekit's Plugin.register_plugin raises
    "Plugins must be registered on the main thread" if a plugin is first
    imported there — so the selected plugins must already be registered by the
    time providers is imported. Moving the plugin imports into the build_*
    branches broke boot outright; this is the guard.
    """
    import concurrent.futures

    from friday.providers import build_stt, build_llm, build_tts

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(b) for b in (build_stt, build_llm, build_tts)]
        for f in futures:
            assert f.result() is not None  # re-raises RuntimeError if it broke


def test_tasking_public_api_still_importable():
    """agent_friday does `from friday.tasking import register_toolset, ...`."""
    from friday.tasking import (  # noqa: F401
        register_toolset, start_worker, set_completion_callback,
        start_task, get_task_status, summarize_task, classify_request,
    )


def test_routing_pool_import_does_not_load_tool_modules():
    """The agent imports the pool at startup; it must not import (and register)
    every tool module just to build a name map nothing reads at boot."""
    loaded = _modules_loaded_after(
        "import friday.routing.pool",
        ["friday.tools.calculate", "friday.tools.google_suite", "sympy"],
    )
    assert loaded == []


def test_domain_tool_names_still_resolve_lazily():
    from friday.routing import pool
    names = pool.DOMAIN_TOOL_NAMES
    assert "calculate" in names["core"]
    assert names["media"], "media domain should own tools"


def test_disabled_session_gate_does_not_load_encoder(monkeypatch):
    """SESSION_SPEAKER_GATE_ENABLED=False still loaded the encoder in the
    agent — on the boot critical path, for a gate that never runs."""
    import friday.speaker_gate as sg
    import friday.voice_encoder as ve

    # Record construction rather than raising: SessionSpeakerGate swallows
    # init exceptions, which would hide a load attempt.
    constructed = []

    class _SpyEncoder:
        def __init__(self, *a, **k):
            constructed.append(True)

    monkeypatch.setattr(ve, "VoiceEncoder", _SpyEncoder)
    monkeypatch.setattr(sg, "SESSION_SPEAKER_GATE_ENABLED", False)

    gate = sg.SessionSpeakerGate()
    assert constructed == [], "VoiceEncoder must not load when the gate is off"
    assert gate._encoder is None
    assert gate.enabled is False
    assert gate.verify(__import__("numpy").zeros(16000, dtype="int16")) is True
