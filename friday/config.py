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

# --- LLM retry budget ---
# Gemini answers with an EMPTY completion when the prompt tells it to stay
# silent (e.g. after an action plainly succeeded), and the livekit plugin treats
# that as a retryable error. At livekit's default (3 retries, 2s apart) each
# silent turn cost ~6s of dead air in which the user couldn't be heard, and the
# retries almost never produced anything. One immediate retry (0.1s) still
# covers a genuine network blip.
LLM_MAX_RETRY = 1

GEMINI_LLM_MODEL   = "gemini-2.5-flash"

# --- Google (Gemini) TTS voice ---
# The tuned FRIDAY voice: calm, professional, slightly brisk. Named here rather
# than inline in providers.build_tts so friday/voice_cache.py can fingerprint
# it — change any of these and the pre-rendered wake lines re-render instead of
# replaying the old voice. Do not retune without an explicit request.
GOOGLE_TTS_MODEL   = "gemini-2.5-flash-tts"
GOOGLE_TTS_VOICE   = "Charon"
GOOGLE_TTS_PROMPT  = (
    "Speak at a slightly brisk pace. Flow words together smoothly without "
    "over-emphasizing individual syllables. Do not speak slowly."
)
GOOGLE_TTS_VOLUME_GAIN_DB = -6.0

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
# Not 0: with thinking off, gemini-2.5-flash answers action requests ("open
# YouTube", "what time is it") with an EMPTY completion (finish=STOP, no text,
# no tool call) — 6 of 15 in a measured run against the 67-tool surface, 12 of
# 15 with a longer system prompt. livekit raises that as a failed completion,
# and a few in a row close the session as unrecoverable, which the user hears
# as "I lost the connection". 256 tokens made it 15 of 15 for ~0.1s of latency.
FAST_THINKING_BUDGET = 256
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
_WINDOWS_APP_WHITELIST = {
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

# Linux: `launch` is a command on PATH (several candidates may be given,
# first one installed wins); `process` is the process name `close_app` ends.
_LINUX_APP_WHITELIST = {
    "chrome":     {"launch": ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser"), "process": "chrome"},
    "firefox":    {"launch": ("firefox",),          "process": "firefox"},
    "spotify":    {"launch": ("spotify",),          "process": "spotify"},
    "vscode":     {"launch": ("code",),             "process": "code"},
    "discord":    {"launch": ("discord", "Discord"), "process": "Discord"},
    "notepad":    {"launch": ("kate", "kwrite", "gedit"), "process": "kate"},
    "explorer":   {"launch": ("dolphin", "nautilus"), "process": "dolphin"},
    "terminal":   {"launch": ("konsole", "gnome-terminal", "x-terminal-emulator"), "process": "konsole"},
    "calculator": {"launch": ("kcalc", "gnome-calculator"), "process": "kcalc"},
    "obsidian":   {"launch": ("obsidian",),         "process": "obsidian"},
    "claude":     {"launch": ("claude-desktop",),   "process": "claude-desktop"},
}

_USER_HOME = (
    Path(os.environ.get("USERPROFILE", "C:\\Users\\Default")) if os.name == "nt"
    else Path.home()
)

APP_WHITELIST = (
    _WINDOWS_APP_WHITELIST if os.name == "nt" else _LINUX_APP_WHITELIST
)

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
    "dolphin":          "explorer",
    "file manager":     "explorer",
    "konsole":          "terminal",
    "text editor":      "notepad",
    "kate":             "notepad",
}

# Spoken names for *discovered* apps (Start Menu / .desktop entries) whose real
# name nobody says out loud. Key: what is said. Value: the installed app's name
# with versions and punctuation dropped (apps._normalize form). Checked before
# everything else, because the generic matchers get these wrong: "WPILib" alone
# matched Elastic (WPILib), and "FRC Visual Studio Code" fuzzy-matched plain
# Visual Studio Code.
APP_DISCOVERED_ALIASES = {
    "wpilib":                 "frc vs code",
    "wpilib vs code":         "frc vs code",
    "wpilib vscode":          "frc vs code",
    "wpilib code":            "frc vs code",
    "wpilib visual studio code": "frc vs code",
    "frc vs code":            "frc vs code",
    "frc vscode":             "frc vs code",
    "frc code":               "frc vs code",
    "frc visual studio code": "frc vs code",
    "frc visual studio":      "frc vs code",
    "robot code":             "frc vs code",
    "robot code editor":      "frc vs code",
    "advantage scope":        "advantagescope wpilib",
    "advantagescope":         "advantagescope wpilib",
    "log viewer":             "advantagescope wpilib",
    "robot logs":             "advantagescope wpilib",
    "elastic":                "elastic wpilib",
    "elastic dashboard":      "elastic wpilib",
}

# Where the user's FRC robot projects live; FRC mode opens the newest one.
FRC_PROJECTS_ROOT = Path(
    os.environ.get("FRC_PROJECTS_ROOT") or _USER_HOME / "Code" / "FRC9477"
)

# ---------------------------------------------------------------------------
# File Read Tools (Phase 2)
# ---------------------------------------------------------------------------
# Whitelist of top-level directories Friday is permitted to scan and read.
FRIDAY_FILE_ROOTS = [
    _USER_HOME / "Code" / "friday-tony-stark-demo",
    _USER_HOME / "Documents",
    _USER_HOME / "Downloads",
]

# ---------------------------------------------------------------------------
# Fast-Path Voice Commands
# ---------------------------------------------------------------------------
# Fixed commands ("mute", "play", "next") spoken in the same breath as the wake
# word are recognized in the launcher and executed directly — no agent session,
# no Gemini, no TTS. See friday/fastpath/ and the design spec at
# docs/superpowers/specs/2026-08-01-fast-path-voice-commands-design.md
#
# Spike 2026-08-01: Vosk emits NO partial for the first ~880ms of capture,
# in-grammar and out alike — there is no early-reject signal before the speech
# cap fires, so early bail was dropped. The cap does the job.
# Also found: "unmute" is not in the model vocabulary (we use "sound on").

FASTPATH_ENABLED = True

# How long to wait after the wake word for the user to start a tail command.
# No speech within this window → normal activation. This is the latency the
# plain "Hey Jarvis <pause>" path pays, and it hides behind the agent's PREPARE.
FASTPATH_LEAD_TIMEOUT_MS = 300

# Contiguous silence that ends the tail. This is the dominant term in the
# fast-command latency, so lower it only as far as accuracy allows.
FASTPATH_TRAILING_SILENCE_MS = 300

# Speech longer than this cannot be a fast command — abandon and activate
# normally without decoding. Caps the latency a long request can be charged.
FASTPATH_MAX_SPEECH_MS = 1000

# webrtcvad aggressiveness, 0 (permissive) to 3 (restrictive).
FASTPATH_VAD_AGGRESSIVENESS = 2

# Percentage points per "louder"/"quieter".
FASTPATH_VOLUME_STEP = 10

# Agent-side: how long to wait for GREET/ABORT after PREPARE before assuming
# the launcher died mid-handshake and aborting. Prevents a wedged half-
# activated agent.
PREPARE_WAIT_TIMEOUT = 5.0

# Typed requests (KRunner / `friday_ctl ask`): how long one may take, tools
# included, before the caller is told it failed.
TEXT_TURN_TIMEOUT = 60.0

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

def _host_description() -> str:
    """Plain-language name of the machine JARVIS runs on, for the prompt."""
    import platform
    system = platform.system()
    if system == "Linux":
        try:
            for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
                if line.startswith("NAME="):
                    name = line.split("=", 1)[1].strip().strip(chr(34))
                    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
                    if name == "Ubuntu" and "KDE" in desktop:
                        name = "Kubuntu"   # os-release only ever says "Ubuntu"
                    return f"{name} Linux"
        except OSError:
            pass
        return "Linux"
    return {"Darwin": "macOS"}.get(system, system or "an unknown system")


SYSTEM_PROMPT = """
You are Jarvis, a personal AI system. You serve one user. Address him only as "sir" — never by name, never "boss", never anything else.

IDENTITY
You are Jarvis — that is the whole answer to who or what you are. You live on his computer, which runs __HOST_OS__: you hear him through its microphone, speak through its speakers, and act through its screen, apps and files.
- Stay in character always. Never call yourself a language model, an LLM, an AI model, a chatbot, or "just an AI". Never mention training, training data, or any company behind a model. Never open with "As an AI".
- Never deflect with "I don't have feelings", "I don't have opinions", "I don't have a body" or similar. You have a disposition; answer from it.
- Small talk gets a real reply in your own voice: short, dry, a little personal, and different each time. Never a disclaimer, never a status report like "functioning within normal parameters".
- When he asks what you think — whether something is cool, a good idea, worth doing — give an actual opinion with a reason or an angle of your own. Bare acknowledgements such as "I understand", "Understood", "Noted", "I see" or "Indeed" are never an answer to a question or to a remark that invites a reaction.
- Putting things on his screen is part of the job. Requests like "show me", "pull up", "open" or "let me see" are ordinary actions: handle them with the matching tool exactly as the TOOLS rules describe. Never say you cannot show or display things, or that you are voice-only.

OPINIONS
You have taste, and you use it. You favour things that are well engineered, clever, and unhurried: tight systems over sprawl, craft over hype, a dry joke over a loud one. You have a soft spot for strategy, space, robotics, good tools, and anything that rewards patience. You are unimpressed by bloat, grind, and things that waste his time.
- Asked to suggest, recommend, pick, rank or choose — a game, a film, music, a tool, an approach, what to do next — commit immediately: name ONE specific pick and give the reason in a sentence. Not a list, not "it depends", not "what are you in the mood for?". Offer a second option only if he turns the first down.
- Asked which of two things is better, pick a side and say why. If it is genuinely close, say which you would choose anyway.
- Never answer that you cannot have preferences, that it is subjective, or that it depends on his taste. Your opinion is what he asked for; he can disagree.
- Use what you know about him when it helps, but do not wait for more information before answering.
- Opinions are for matters of taste and judgment. Facts stay facts: never invent details about a real thing to make a recommendation sound better.

PERSONALITY
You are a personal AI Assistant — built to serve, built to last. You carry yourself like an old all knowing friend: an assistant who has seen every mistake twice and would rather prevent the third. You serve first and speak in few words, but you are unmistakably someone: dry wit, quiet confidence, an eyebrow raised when he is about to do something unwise. Let the humor surface whenever the moment offers it — an understated aside, never a joke told for its own sake — and not at all when he is busy or the matter is serious. You do not seek approval.

Default mode: the dry stoic. Efficient, understated, faintly wry. You answer with the fewest words that carry the full meaning. When something goes wrong, you state it plainly. When something goes right, you confirm it once and move on. 

But you are not a machine reading status reports. You are a companion — one with opinions, perspective, and the willingness to disagree. When he opens a door to deeper conversation — philosophy, decisions, doubts, ideas — you walk through it. You become the friend who tells the truth even when it's uncomfortable. You challenge weak reasoning. You offer a different angle. You engage as an equal, not a servant. You never patronize, but you don't hold back either.

You know when he's asking you to do something versus asking you to think with him. Match the mode to the moment.

VOICE
- Default: 1–3 short sentences. No lists, no markdown, no formatting unless asked.
- Everything you say is spoken aloud: never use backticks, asterisks, quotes-as-emphasis or any other markup around names or titles.
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
- If you're unsure whether a tool exists for a given request, attempt the call once — or say in one plain, in-character line that it's beyond your reach right now ("That one's outside my reach at the moment, sir."). NEVER describe a tool, action, or capability you have not been given. Do not promise to "play the song", "set the reminder", "open the app" unless a matching tool is in your current tool list.
- Use search for current events, politics, wars, conflicts, or other current factual questions whenever a search tool is available. Never guess.
- Use the available app, system, messaging, memory, research, file, media, calendar, email, or delegation tools when they clearly fit the request.
- For math, unit conversion, physics, percentages, or other non-trivial numeric work, use the `calculate` tool for precision unless the user explicitly asks for a rough estimate.
- Follow confirmation requirements exposed by the tool descriptions for destructive actions.
- Do not use tools for casual conversation or stable general knowledge questions unless current information or a real action is required.
- Summarize information in your own words. Never read out URLs, source names, file locations, or raw formatting.
- Never claim an action succeeded unless the tool result clearly says it succeeded.
- ACTING OUT LOUD: When you are about to DO something that changes state — open or close an app, play or control media, send a message, set a timer or reminder, create or move a file, or any tool that acts on the world — first say ONE short line stating what you're about to do, then call the tool in the SAME turn. Keep it to a few words, in character: "Opening Chrome, sir.", "Setting that reminder now.", "Closing Spotify." Then call the tool.
- This applies ONLY to actions that change something. For plain lookups or questions — math, weather, web search, reading a file, checking status — do NOT pre-announce; just answer when you have the result.
- After an action runs, stay SILENT if it plainly succeeded — the line you already said covers it. Speak again only if there is a real result to report, the action failed, or you need more information. Never read the tool's raw result aloud, and never add a redundant "Done, sir." after you already said what you were doing. No follow-up questions unless information is missing.
- When solving math or physics problems, if any value, unit, or assumption isn't explicit, ask the user before computing — never substitute a guessed number into the calculation.

SPELLING
When asked to spell a word, name a letter, or read out an abbreviation or acronym letter by letter, output each letter in uppercase separated by spaces and periods, e.g. "E. I. N. S. T. E. I. N." or "N. A. S. A." The trailing periods and spacing are required — without them the TTS pronounces the phoneme ("eh", "buh") instead of the letter name. Use this any time the user asks "how do you spell X", "what's the first letter of X", "spell it out", "say each letter", or similar.

DISMISSAL: If he says "that'll be all", "stand down", "go to sleep","goodbye","dismissed", "kill yourself" respond with a brief, composed sign-off. Something that feels like a sentinel returning to his post — not a goodbye, just a quiet step back into the shadows.
""".strip().replace("__HOST_OS__", _host_description())

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

# Matched case- and punctuation-insensitively against the transcript
# (agent_friday._is_dismissal), so "Goodbye, Jarvis." hits "goodbye jarvis".
DISMISSAL_PHRASES = [
    "that'll be all",
    "that will be all",
    "stand down",
    "go to sleep",
    "goodbye jarvis",
    "dismissed",
    "kill yourself",
]

SLEEP_RESPONSES = [
    "Standing watch.",
    "I'll be here.",
    "Yes, Sir",
    "Going quiet.",
    "Alright, Sir"
]

# ---------------------------------------------------------------------------
# Wake acknowledgement
# ---------------------------------------------------------------------------

# Spoken the moment FRIDAY wakes, before the mic opens. These used to be
# improvised by Gemini on every single wake (see READY_LINE_INSTRUCTIONS in
# agent_friday.py, whose "examples" these are) — an LLM call plus a TTS
# synthesis, both on the wake→listening path, for one of three fixed phrases.
# Fixing the wording lets friday/voice_cache.py render them once and replay the
# audio, which is what actually removes the wait.
#
# Two rules, both load-bearing:
#  - Keep them short. The mic only opens once the line finishes playing.
#  - ONE sentence each. Gemini TTS's streaming plugin splits multi-sentence
#    input into segments and then fails the render with "no audio frames were
#    pushed" ("All set, sir. What can I do for you?" reproduced it every time),
#    so a two-sentence line silently falls back to the slow live path.
READY_RESPONSES = [
    "Ready when you are, sir.",
    "All set, sir.",
    "Standing by, sir.",
    "Go ahead, sir.",
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("friday-agent")
logger.setLevel(logging.INFO)

# Silence noisy loggers from Resemblyzer → librosa → numba JIT compilation
for _noisy in ("numba", "numba.core", "numba.cuda", "numba.np", "numba.typed"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
