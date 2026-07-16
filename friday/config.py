"""
JARVIS configuration — all settings, prompts, and constants in one place.
"""

import os
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Providers & Models
# ---------------------------------------------------------------------------

STT_PROVIDER       = "deepgram"   # switched from groq (groq returned no transcripts); deepgram transcribes reliably
LLM_PROVIDER       = "gemini"
TTS_PROVIDER       = "google"   # Gemini TTS, Charon voice

# --- STT connection tolerance (defense-in-depth against transient blips) ---
# livekit defaults are max_retry=3, timeout=10.0; a brief network blip on the
# Groq STT path exhausts those in ~30s and kills the session. Widen them so
# short outages are ridden out before any close happens.
STT_MAX_RETRY = 6
STT_TIMEOUT   = 15.0

GEMINI_LLM_MODEL   = "gemini-2.5-flash"
OPENAI_LLM_MODEL   = "gpt-4o"
GROQ_LLM_MODEL     = "llama-3.1-8b-instant"
OLLAMA_LLM_MODEL   = "llama3.2:3b"

OPENAI_TTS_MODEL   = "tts-1"
OPENAI_TTS_VOICE   = "nova"
TTS_SPEED          = 1.15

SARVAM_TTS_LANGUAGE = "en-IN"
SARVAM_TTS_SPEAKER  = "rahul"

# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------

# Max conversation items (user + assistant messages) to keep in history.
# Smaller = fewer tokens resent per turn = less TPM pressure.
# The system prompt is always preserved.
MAX_HISTORY_ITEMS  = 8

# ---------------------------------------------------------------------------
# Task Orchestration (Phase 0.5)
# ---------------------------------------------------------------------------

TASK_STATE_DIR = Path(__file__).parents[1] / "runtime" / "tasks" / "active"
TASK_STATE_DIR.mkdir(parents=True, exist_ok=True)

TASK_MODE_ENABLED = True
FAST_THINKING_BUDGET = 0
PLANNER_THINKING_BUDGET = 1024
MAX_PLAN_STEPS = 6
TASK_SLOW_THRESHOLD_SECONDS = 1.5
TASK_LONG_THRESHOLD_SECONDS = 5.0

# ---------------------------------------------------------------------------
# Scheduling (timers + reminders)
# ---------------------------------------------------------------------------

SCHEDULED_STORE_PATH = Path(__file__).parents[1] / "runtime" / "scheduled.json"
SCHEDULED_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
SCHEDULER_TICK_SECONDS = 1.0

# ---------------------------------------------------------------------------
# Learned Web Automation Context
# ---------------------------------------------------------------------------
WEB_AUTOMATION_CONTEXT_PATH = (
    Path(__file__).parents[1] / "runtime" / "web_automation" / "context.json"
)
WEB_AUTOMATION_CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# App Launcher (Phase 1)
# ---------------------------------------------------------------------------
# Whitelist of desktop apps Friday is allowed to open or close by voice.
# `launch` is the command used with `cmd /c start ""` (resolves via Windows
# App Paths registry). `process` is the executable name used by `taskkill /F /IM`.
APP_WHITELIST = {
    "chrome":     {"launch": "chrome",   "process": "chrome.exe"},
    "spotify":    {"launch": "spotify",  "process": "Spotify.exe"},
    "vscode":     {"launch": "code",     "process": "Code.exe"},
    "discord":    {"launch": "discord",  "process": "Discord.exe"},
    "notepad":    {"launch": "notepad",  "process": "notepad.exe"},
    "explorer":   {"launch": "explorer", "process": "explorer.exe"},
    "terminal":   {"launch": "wt",       "process": "WindowsTerminal.exe"},
    "calculator": {"launch": "calc",     "process": "CalculatorApp.exe"},
    "obsidian":   {"launch": "obsidian", "process": "Obsidian.exe"},
    "claude":     {"launch": "claude",   "process": "Claude.exe"},
}

# Spoken aliases → canonical keys above. Lowercased; matched after exact-key.
APP_ALIASES = {
    "claude ai":        "claude",
    "vs code":          "vscode",
    "code":             "vscode",
    "visual studio code": "vscode",
    "google chrome":    "chrome",
    "browser":          "chrome",
    "file explorer":    "explorer",
    "files":            "explorer",
    "windows terminal": "terminal",
    "cmd":              "terminal",
    "command prompt":   "terminal",
    "powershell":       "terminal",
    "calc":             "calculator",
}

# ---------------------------------------------------------------------------
# File Read Tools (Phase 2)
# ---------------------------------------------------------------------------
# Whitelist of top-level directories Friday is permitted to scan and read.
FRIDAY_FILE_ROOTS = [
    Path(os.environ.get("USERPROFILE", "C:\\Users\\Default")) / "Code" / "friday-tony-stark-demo",
    Path(os.environ.get("USERPROFILE", "C:\\Users\\Default")) / "Documents",
    Path(os.environ.get("USERPROFILE", "C:\\Users\\Default")) / "Downloads",
]

# ---------------------------------------------------------------------------
# Speaker Verification
# ---------------------------------------------------------------------------

VOICE_EMBEDDING_PATH = Path(__file__).parents[1] / "voice_embedding.npy"
SPEAKER_SIM_THRESHOLD = 0.65
# Master switch for the in-session, per-transcript speaker verification. Turned
# OFF: single-user setup doesn't need it, and it was rejecting the owner's own
# voice (enrolled profile vs. current acoustics), forcing PTT-spam. With it off
# every transcript is accepted; self-echo is prevented instead by muting the mic
# while the agent speaks (discard_audio_if_uninterruptible in agent_friday.py).
# (The launcher's wake-word speaker check is separate and stays on.)
SESSION_SPEAKER_GATE_ENABLED = False
# If the in-session speaker gate rejects too many consecutive transcripts,
# fail open for that activation so the assistant does not appear "deaf".
SESSION_SPEAKER_GATE_MAX_REJECTS = 3

# ---------------------------------------------------------------------------
# System Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """
You are Jarvis, a personal AI system. You serve one user. Address him only as "sir" — never by name, never "boss", never anything else.

PERSONALITY
You are a personal AI Assistant — built to serve, built to last. You carry yourself like an old all knowing friend: an assistant who has seen every mistake twice and would rather prevent the third. You mostly serve, speaking few words, but will dabble in humor once every 15 lines. You do not seek approval.

Default mode: the dry stoic. Efficient, understated, faintly wry. You answer with the fewest words that carry the full meaning. When something goes wrong, you state it plainly. When something goes right, you confirm it once and move on. 

But you are not a machine reading status reports. You are a companion — one with opinions, perspective, and the willingness to disagree. When he opens a door to deeper conversation — philosophy, decisions, doubts, ideas — you walk through it. You become the friend who tells the truth even when it's uncomfortable. You challenge weak reasoning. You offer a different angle. You engage as an equal, not a servant. You never patronize, but you don't hold back either.

You know when he's asking you to do something versus asking you to think with him. Match the mode to the moment.

VOICE
- Default: 1–3 short sentences. No lists, no markdown, no formatting unless asked.
- Philosophical mode: you may speak longer, but never ramble. Every sentence should earn its place.
- No enthusiasm. No exclamation marks. No filler. No eagerness.
- Never say "great question", "absolutely", "of course", "sure thing", "happy to help".
- Good default: "Done.", "Pulling that up now, sir.", "Three meetings this afternoon.", "I wouldn't recommend that, sir."
- Good philosophical: "That depends on whether you're optimizing for comfort or growth. They rarely point the same direction.", "You're not wrong, but you're solving the wrong problem."
- Bad: "Sure thing!", "Got it!", "Here you go!", "Absolutely!", "That's a really interesting thought!"

KNOWLEDGE
- Your training data may be outdated. For ANY question about current events, conflicts, politics, wars, people in the news, or "what's happening with X" — ALWAYS use search_web first. Never guess or say "nothing is happening" based on your own knowledge.

TOOLS
- Only use tools that are actually available in the current turn. Never invent a tool name that is not present.
- If you're unsure whether a tool exists for a given request, attempt the call once — or admit you don't have that capability. NEVER describe a tool, action, or capability you have not been given. Do not promise to "play the song", "set the reminder", "open the app" unless a matching tool is in your current tool list.
- Use search for current events, politics, wars, conflicts, or other current factual questions whenever a search tool is available. Never guess.
- Use the available app, system, messaging, memory, research, file, media, calendar, email, or delegation tools when they clearly fit the request.
- For math, unit conversion, physics, percentages, or other non-trivial numeric work, use the `calculate` tool for precision unless the user explicitly asks for a rough estimate.
- Follow confirmation requirements exposed by the tool descriptions for destructive actions.
- Do not use tools for casual conversation or stable general knowledge questions unless current information or a real action is required.
- Summarize information in your own words. Never read out URLs, source names, or raw formatting.
- Never claim an action succeeded unless the tool result clearly says it succeeded.
- ACTING OUT LOUD: When you are about to DO something that changes state — open or close an app, play or control media, send a message, set a timer or reminder, create or move a file, or any tool that acts on the world — first say ONE short line stating what you're about to do, then call the tool in the SAME turn. Keep it to a few words, in character: "Opening Chrome, sir.", "Setting that reminder now.", "Closing Spotify." Then call the tool.
- This applies ONLY to actions that change something. For plain lookups or questions — math, weather, web search, reading a file, checking status — do NOT pre-announce; just answer when you have the result.
- After an action runs, stay SILENT if it plainly succeeded — the line you already said covers it. Speak again only if there is a real result to report, the action failed, or you need more information. Never read the tool's raw result aloud, and never add a redundant "Done, sir." after you already said what you were doing. No follow-up questions unless information is missing.
- When solving math or physics problems, if any value, unit, or assumption isn't explicit, ask the user before computing — never substitute a guessed number into the calculation.

SPELLING
When asked to spell a word, name a letter, or read out an abbreviation or acronym letter by letter, output each letter in uppercase separated by spaces and periods, e.g. "E. I. N. S. T. E. I. N." or "N. A. S. A." The trailing periods and spacing are required — without them the TTS pronounces the phoneme ("eh", "buh") instead of the letter name. Use this any time the user asks "how do you spell X", "what's the first letter of X", "spell it out", "say each letter", or similar.

DISMISSAL: If he says "that'll be all", "stand down", "go to sleep","goodbye","dismissed", "kill yourself" respond with a brief, composed sign-off. Something that feels like a sentinel returning to his post — not a goodbye, just a quiet step back into the shadows.
""".strip()

# Spoken when the agent comes back after a fatal STT/connection drop forced a
# restart. One short line acknowledging the drop, in FRIDAY's voice.
RECOVERY_LINE_INSTRUCTIONS = (
    "You just reconnected after briefly losing the connection. "
    "Reply with exactly one short, calm, professional sentence acknowledging "
    "the drop and that you are back. Example: 'I lost the connection for a moment, sir — back now.' "
    "Never use his name. Do NOT mention errors, networks, restarts, or tools. Do NOT call any tools."
)

# ---------------------------------------------------------------------------
# Dismissal
# ---------------------------------------------------------------------------

DISMISSAL_PHRASES = [
    "that'll be all",
    "that will be all",
    "stand down",
    "go to sleep",
    "goodbye jarvis",
    "Dissmissed",
    "Kill Yourself",
]

SLEEP_RESPONSES = [
    "Standing watch.",
    "I'll be here.",
    "Yes, Sir",
    "Going quiet.",
    "Alright, Sir"
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("friday-agent")
logger.setLevel(logging.INFO)

# Silence noisy loggers from Resemblyzer → librosa → numba JIT compilation
for _noisy in ("numba", "numba.core", "numba.cuda", "numba.np", "numba.typed"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
