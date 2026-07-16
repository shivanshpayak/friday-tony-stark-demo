# Acting Out Loud — Design

**Date:** 2026-07-12
**Status:** Approved, ready for implementation plan

## Problem

Today, when FRIDAY runs an action tool (e.g. "open Chrome"), the LLM emits only
the tool call — no spoken text. LiveKit runs the tool silently, the tool returns
a short string, and a follow-up generation speaks the confirmation. The result is
a silent gap while the action runs, then a single line after. The user wants
FRIDAY to **say what he's about to do, do it, then speak after only if warranted** —
the classic JARVIS cadence, and a better fit for the latency-first "feels instant"
design goal (the preamble fills the dead air during execution).

## Behavior Contract

1. **Pre-announce state-changing actions.** When about to run a tool that changes
   the world — open/close an app, play or control media, send a message, set a
   timer or reminder, create or move a file, or any other tool that acts on the
   world — FRIDAY first speaks one short, in-character line stating what he's about
   to do, then calls the tool in the same turn.
   - Examples: "Opening Chrome, sir.", "Setting that reminder now.", "Closing Spotify."

2. **Lookups stay as-is.** Pure lookups and questions — math, weather, web search,
   reading a file, checking status — get **no** preamble. FRIDAY just answers when
   the result is in. Pre-announcing a lookup would be noise.

3. **Close only if there's something to report.** After the action runs:
   - Plain success → stay silent. The pre-line already covered it.
   - A real result to report, a failure, or missing information → speak.
   - Never read the tool's raw result aloud; never tack on a redundant "Done, sir."
     after already saying what was being done.

## Mechanism

This is implemented **entirely as a `SYSTEM_PROMPT` change** in
`friday/config.py`. No new code paths, no new modules.

**Why prompt-only works:** LiveKit's voice pipeline already synthesizes any text
that precedes a tool call in the assistant's generation stream, then executes the
tool, then runs a follow-up generation (the current `llm_node` even prints
`"SPEAKING"` when the first content chunk arrives). `max_tool_steps=3` already
permits the follow-up generation. So getting "speak → act → maybe speak" only
requires instructing the model to emit a short spoken line *before* the tool call,
and to stay silent on plain success afterward.

**No latency cost:** the preamble *is* the first tokens streamed, spoken while the
tool executes — the action is not delayed waiting for TTS to finish. This is the
idiomatic LiveKit "say something before a tool call" pattern.

**Tools are untouched.** Action tools live in the MCP subprocess (`server.py` +
`friday/tools/`) and have no access to the voice session, so the spoken line
cannot and does not come from the tool. It comes from the model's own output.

### Prompt change (concrete)

In the `TOOLS` section of `SYSTEM_PROMPT`, replace the line:

> After completing an action, confirm in one short line. No follow-up questions
> unless information is missing.

with an "ACTING OUT LOUD" block along these lines (final wording tuned during
implementation):

> **ACTING OUT LOUD** — When you're about to *do* something that changes state
> (open or close an app, play or control media, send a message, set a timer or
> reminder, create or move a file, or any tool that acts on the world), first say
> one short line stating what you're about to do, then call the tool in the same
> turn — e.g. "Opening Chrome, sir.", "Setting that reminder now.", "Closing
> Spotify." This applies **only** to actions that change something. For plain
> lookups or questions (math, weather, web search, reading a file, checking
> status) do **not** pre-announce — just answer when you have the result. After
> the action runs, stay silent if it plainly succeeded — the line you already said
> covers it. Speak again only if there's a real result to report, it failed, or
> you need more information. Never read the tool's raw result aloud, and never add
> a redundant "Done, sir." after you already said what you were doing.

The existing `VOICE` examples ("Pulling that up now, sir.") already match this
cadence and stay as-is.

## Unaffected by Design

These paths use `tool_choice="none"` (no tools → no preamble) or are separate
`generate_reply` paths, and are intentionally not changed:

- Greeting / ready line (`_READY_LINE_INSTRUCTIONS`)
- Recovery line (`RECOVERY_LINE_INSTRUCTIONS`)
- Dismissal sign-off
- Background-task completion acks (`_on_task_finished`)
- Scheduler fire acks (`_on_scheduled_fire`)

Multi-tool turns (e.g. "close Spotify and open Chrome") get a single covering
preamble before the batch rather than one line per tool.

## Error Handling / Graceful Degradation

The only real risk is model compliance (Gemini 2.5 Flash), and both failure modes
degrade gracefully:

- **Model skips the preamble on some turn** → that turn behaves like today (silent
  action, then a confirmation). No breakage.
- **Model still says "Done, sir." on success** → slightly more verbose than the
  ideal, but harmless.

If preamble-skipping proves *frequent* during testing, that is the trigger to
escalate to the previously-considered Approach C (a deterministic fallback in
`llm_node` that injects a canned ack only when an action tool call arrives with no
preceding text). Approach C is out of scope for this change unless testing shows
it is needed.

## Verification

Live voice test via `uv run friday_voice` (console mode):

- "open Chrome" → "Opening Chrome, sir." → Chrome opens → **silence** (no close).
- "close Spotify" when it isn't running → preamble, then a spoken failure/"wasn't
  running" report.
- "what's the weather" / "what's 17 times 23" → **no** preamble; answer only.
- A multi-action request → a single covering preamble before the actions run.

Also update the `ARCHITECTURE.md` Feature Map / reply-path notes to document the
new speak-before-act behavior, per the repo rule that `ARCHITECTURE.md` stays
current when reply-path behavior changes.

## Scope

- Edit: `friday/config.py` (`SYSTEM_PROMPT`).
- Doc: `ARCHITECTURE.md` update.
- No new code, no new modules, no tool changes.
