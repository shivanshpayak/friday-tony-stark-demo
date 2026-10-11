"""
Provider factories — build STT, LLM, and TTS instances.

Each factory creates a fresh provider instance. Call once per session
since the SDK tears down internal WebSocket connections on session.aclose().
"""

import os
from pathlib import Path
from .config import (
    STT_PROVIDER, LLM_PROVIDER, TTS_PROVIDER,
    GEMINI_LLM_MODEL, OPENAI_LLM_MODEL, GROQ_LLM_MODEL, OLLAMA_LLM_MODEL,
    OPENAI_TTS_MODEL, OPENAI_TTS_VOICE, TTS_SPEED,
    GOOGLE_TTS_MODEL, GOOGLE_TTS_VOICE, GOOGLE_TTS_PROMPT,
    GOOGLE_TTS_VOLUME_GAIN_DB,
    SARVAM_TTS_LANGUAGE, SARVAM_TTS_SPEAKER,
    FAST_THINKING_BUDGET, PLANNER_THINKING_BUDGET,
    logger,
)

from typing import Literal

import importlib

from livekit.agents import APIConnectOptions
from livekit.agents.voice.agent_session import SessionConnectOptions
from .config import LLM_MAX_RETRY, STT_MAX_RETRY, STT_TIMEOUT


# ---------------------------------------------------------------------------
# Plugin loading
# ---------------------------------------------------------------------------
# Two constraints pull in opposite directions:
#
#  * This module used to import all four livekit plugins at module level even
#    though only one of each kind is ever built. livekit.plugins.openai alone
#    is ~0.85s of boot (measured 2026-09-22) for a provider this config never
#    selects, and it sits on the critical path.
#  * They cannot simply be imported inside the build_* branches instead:
#    a plugin registers itself on import, and livekit's Plugin.register_plugin
#    raises "Plugins must be registered on the main thread". agent_friday warms
#    the providers through loop.run_in_executor, so build_*() runs on worker
#    threads — making the branches lazy broke boot outright.
#
# So: import exactly the plugins this config selects, here, on the main thread
# at import time. The `from livekit.plugins import ...` lines inside the
# branches below are then pure sys.modules lookups that register nothing.
# Guarded by tests/test_boot_imports.py.

_PLUGIN_FOR_PROVIDER = {
    "deepgram": "deepgram",
    "sarvam": "sarvam",
    "google": "google",
    "gemini": "google",
    "openai": "openai",
    "whisper": "openai",   # OpenAI Whisper STT
    "groq": "openai",      # Groq speaks the OpenAI API
    "ollama": "openai",    # so does Ollama
}


def _register_selected_plugins() -> set[str]:
    """Import (and so register) only the plugins the current config needs."""
    loaded: set[str] = set()
    for provider in (STT_PROVIDER, LLM_PROVIDER, TTS_PROVIDER):
        name = _PLUGIN_FOR_PROVIDER.get(provider)
        if name is None or name in loaded:
            continue
        importlib.import_module(f"livekit.plugins.{name}")
        loaded.add(name)
    return loaded


LOADED_PLUGINS = _register_selected_plugins()


def build_stt(http_session=None, room_name=None):
    if STT_PROVIDER == "sarvam":
        from livekit.plugins import sarvam
        logger.info("STT → Sarvam Saaras v3")
        return sarvam.STT(
            language="en-IN",
            model="saaras:v3",
            mode="transcribe",
            flush_signal=True,
            sample_rate=16000,
            http_session=http_session,
        )
    elif STT_PROVIDER == "whisper":
        from livekit.plugins import openai as lk_openai
        logger.info("STT → OpenAI Whisper")
        return lk_openai.STT(model="whisper-1")
    elif STT_PROVIDER == "google":
        from livekit.plugins import google as lk_google
        logger.info("STT → Google Speech-to-Text")
        sa_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
        kwargs = {}
        if sa_file:
            import json
            sa_path = Path(__file__).parents[1] / sa_file
            if sa_path.exists():
                with open(sa_path) as f:
                    kwargs["credentials_info"] = json.load(f)
        return lk_google.STT(**kwargs)
    elif STT_PROVIDER == "deepgram":
        from livekit.plugins import deepgram as lk_deepgram
        logger.info("STT → Deepgram")
        # Deepgram ends the user's turn (agent_friday._stt_ends_turns): 300 ms
        # of silence, so a breath mid-sentence doesn't cut the request short.
        return lk_deepgram.STT(endpointing_ms=300)
    elif STT_PROVIDER == "groq":
        from livekit.plugins import openai as lk_openai
        logger.info("STT → Groq Whisper (turbo)")
        return lk_openai.STT(
            model="whisper-large-v3-turbo",
            api_key=os.getenv("GROQ_API_KEY"),
            base_url="https://api.groq.com/openai/v1",
        )
    else:
        raise ValueError(f"Unknown STT_PROVIDER: {STT_PROVIDER!r}")


def build_llm(mode: Literal["fast", "planner"] = "fast"):
    budget = PLANNER_THINKING_BUDGET if mode == "planner" else FAST_THINKING_BUDGET
    
    if LLM_PROVIDER == "openai":
        from livekit.plugins import openai as lk_openai
        logger.info("LLM → OpenAI (%s) [mode=%s]", OPENAI_LLM_MODEL, mode)
        return lk_openai.LLM(model=OPENAI_LLM_MODEL)
    elif LLM_PROVIDER == "gemini":
        from livekit.plugins import google as lk_google
        logger.info("LLM → Google Gemini (%s) [mode=%s, budget=%d]", GEMINI_LLM_MODEL, mode, budget)
        return lk_google.LLM(
            model=GEMINI_LLM_MODEL,
            api_key=os.getenv("GOOGLE_API_KEY"),
            # Only supply a thinking_config if budget is positive
            thinking_config={"thinking_budget": budget} if budget > 0 else None,
        )
    elif LLM_PROVIDER == "groq":
        from livekit.plugins import openai as lk_openai
        logger.info("LLM → Groq (%s)", GROQ_LLM_MODEL)
        return lk_openai.LLM(
            model=GROQ_LLM_MODEL,
            api_key=os.getenv("GROQ_API_KEY"),
            base_url="https://api.groq.com/openai/v1",
        )
    elif LLM_PROVIDER == "ollama":
        from livekit.plugins import openai as lk_openai
        logger.info("LLM → Ollama (%s)", OLLAMA_LLM_MODEL)
        import httpx
        return lk_openai.LLM(
            model=OLLAMA_LLM_MODEL,
            api_key="ollama",
            base_url="http://localhost:11434/v1",
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
    else:
        raise ValueError(f"Unknown LLM_PROVIDER: {LLM_PROVIDER!r}")


def build_tts(http_session=None):
    if TTS_PROVIDER == "sarvam":
        from livekit.plugins import sarvam
        logger.info("TTS → Sarvam Bulbul v3")
        return sarvam.TTS(
            target_language_code=SARVAM_TTS_LANGUAGE,
            model="bulbul:v3",
            speaker=SARVAM_TTS_SPEAKER,
            pace=TTS_SPEED,
            http_session=http_session,
        )
    elif TTS_PROVIDER == "openai":
        from livekit.plugins import openai as lk_openai
        logger.info("TTS → OpenAI TTS (%s / %s)", OPENAI_TTS_MODEL, OPENAI_TTS_VOICE)
        return lk_openai.TTS(model=OPENAI_TTS_MODEL, voice=OPENAI_TTS_VOICE, speed=TTS_SPEED)
    elif TTS_PROVIDER == "deepgram":
        from livekit.plugins import deepgram as lk_deepgram
        logger.info("TTS → Deepgram Aura 2")
        return lk_deepgram.TTS(
            model="aura-2-andromeda-en",
            encoding="linear16",
            sample_rate=24000,
            http_session=http_session,
        )
    elif TTS_PROVIDER == "google":
        from livekit.plugins import google as lk_google
        logger.info("TTS → Google Gemini TTS (Charon)")
        sa_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
        kwargs = {}
        if sa_file:
            import json
            sa_path = Path(__file__).parents[1] / sa_file
            if sa_path.exists():
                with open(sa_path) as f:
                    kwargs["credentials_info"] = json.load(f)
        return lk_google.TTS(
            model_name=GOOGLE_TTS_MODEL,
            voice_name=GOOGLE_TTS_VOICE,
            prompt=GOOGLE_TTS_PROMPT,
            speaking_rate=1.0,  # ignored by Gemini TTS — pace is controlled by prompt
            volume_gain_db=GOOGLE_TTS_VOLUME_GAIN_DB,
            **kwargs,
        )
    else:
        raise ValueError(f"Unknown TTS_PROVIDER: {TTS_PROVIDER!r}")


def tts_voice_id() -> str:
    """Fingerprint of the configured voice, used to key cached audio.

    friday/voice_cache.py stores pre-rendered lines under this id, so any
    retune here (voice, model, delivery prompt, gain) invalidates them rather
    than replaying the old voice. Every value it reads is also what build_tts
    passes, so the two cannot drift.
    """
    if TTS_PROVIDER == "google":
        parts = (GOOGLE_TTS_MODEL, GOOGLE_TTS_VOICE,
                 f"{GOOGLE_TTS_VOLUME_GAIN_DB}", GOOGLE_TTS_PROMPT)
    elif TTS_PROVIDER == "openai":
        parts = (OPENAI_TTS_MODEL, OPENAI_TTS_VOICE, f"{TTS_SPEED}")
    elif TTS_PROVIDER == "sarvam":
        parts = ("bulbul:v3", SARVAM_TTS_SPEAKER, SARVAM_TTS_LANGUAGE, f"{TTS_SPEED}")
    elif TTS_PROVIDER == "deepgram":
        parts = ("aura-2-andromeda-en",)
    else:
        parts = ()
    return "|".join((TTS_PROVIDER,) + tuple(parts))


def build_session_conn_options() -> SessionConnectOptions:
    """Connection options for the AgentSession.

    Widens STT retry/timeout beyond livekit defaults (max_retry=3, timeout=10)
    so a brief blip on the STT path is ridden out instead of closing the session.
    Narrows LLM retries so an intentionally empty Gemini reply doesn't stall
    the conversation for ~6s (see LLM_MAX_RETRY).
    """
    return SessionConnectOptions(
        stt_conn_options=APIConnectOptions(
            max_retry=STT_MAX_RETRY,
            timeout=STT_TIMEOUT,
        ),
        llm_conn_options=APIConnectOptions(max_retry=LLM_MAX_RETRY),
    )
