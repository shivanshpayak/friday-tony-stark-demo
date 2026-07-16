# Acting Out Loud — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make FRIDAY say one short line about what he's about to do *before* running a state-changing tool, then act, then stay silent on plain success.

**Architecture:** Prompt-only change. LiveKit already speaks text that precedes a tool call in the assistant's generation stream, runs the tool, then does a follow-up generation (`max_tool_steps=3` permits it). So we only edit `SYSTEM_PROMPT` to instruct the model to emit a short pre-line before action tools and to stay silent on success. Tools in the MCP subprocess are untouched. Verified by live voice test, not unit tests (this is a wording/behavior change, and the repo has no configured test runner).

**Tech Stack:** Python, LiveKit Agents SDK, Gemini 2.5 Flash (LLM), Google TTS (Charon). Spec: [docs/superpowers/specs/2026-07-12-acting-out-loud-design.md](../specs/2026-07-12-acting-out-loud-design.md).

**Note on git:** Per the repo owner's standing preference ("I'll handle git myself, don't ever worry about git"), this plan contains **no commit steps**. Leave all changes uncommitted in the working tree for the owner to commit. Do not run `git add`/`commit`/`branch`/`reset`.

---

### Task 1: Replace the post-action confirm rule with the "Acting out loud" rule

**Files:**
- Modify: `friday/config.py` (the `SYSTEM_PROMPT` string, `TOOLS` section — currently line 179)

- [ ] **Step 1: Read the current TOOLS section**

Open [friday/config.py](../../../friday/config.py) and locate this exact line inside the `TOOLS` bullet list of `SYSTEM_PROMPT` (currently line 179):

```
- After completing an action, confirm in one short line. No follow-up questions unless information is missing.
```

- [ ] **Step 2: Replace that single line with the ACTING OUT LOUD block**

Replace the exact line above with the following (multi-line) replacement. Keep it inside the `TOOLS` bullet list, same indentation style as the surrounding bullets:

```
- ACTING OUT LOUD: When you are about to DO something that changes state — open or close an app, play or control media, send a message, set a timer or reminder, create or move a file, or any tool that acts on the world — first say ONE short line stating what you're about to do, then call the tool in the SAME turn. Keep it to a few words, in character: "Opening Chrome, sir.", "Setting that reminder now.", "Closing Spotify." Then call the tool.
- This applies ONLY to actions that change something. For plain lookups or questions — math, weather, web search, reading a file, checking status — do NOT pre-announce; just answer when you have the result.
- After an action runs, stay SILENT if it plainly succeeded — the line you already said covers it. Speak again only if there is a real result to report, the action failed, or you need more information. Never read the tool's raw result aloud, and never add a redundant "Done, sir." after you already said what you were doing. No follow-up questions unless information is missing.
```

Rationale: the old line drove a *post*-action confirm on every action, which now conflicts with "silence on plain success." The final bullet preserves the original "no follow-up questions unless information is missing" intent.

- [ ] **Step 3: Sanity-check the string still parses and reads correctly**

Run:

```bash
python -c "from friday.config import SYSTEM_PROMPT; assert 'ACTING OUT LOUD' in SYSTEM_PROMPT; assert 'After completing an action, confirm in one short line' not in SYSTEM_PROMPT; print('prompt OK, len=', len(SYSTEM_PROMPT))"
```

Expected: prints `prompt OK, len= <some number>` with no assertion error and no import/syntax error. (This confirms the new block is present and the replaced sentence is gone.)

---

### Task 2: Document the behavior in ARCHITECTURE.md

**Files:**
- Modify: `ARCHITECTURE.md` (§4 "Audio pipeline (mic → response)", after the "Echo guard" note near line 133)

- [ ] **Step 1: Add a "Speak-before-act" note under §4**

In [ARCHITECTURE.md](../../../ARCHITECTURE.md), immediately after the **Echo guard** paragraph in §4 (the one ending "...so FRIDAY never transcribes its own voice.", ~line 133), add:

```markdown
**Speak-before-act:** for state-changing tools (open/close app, media, messaging,
reminders, files), the LLM emits a short spoken pre-line ("Opening Chrome, sir.")
*before* the `tool call` edge above, then acts, then stays silent on plain success.
This is prompt-driven (`SYSTEM_PROMPT` "ACTING OUT LOUD" rule in
[`friday/config.py`](friday/config.py)) — LiveKit speaks text that precedes a tool
call in the stream. Pure lookups (math, weather, search) get no pre-line.
```

- [ ] **Step 2: Verify the doc still renders**

Confirm the added block sits inside §4, the surrounding Mermaid diagram and headings are intact, and no other section was disturbed. (Visual check — no command.)

---

### Task 3: Live voice verification

**Files:** none (manual runtime verification, per the spec's Verification section)

- [ ] **Step 1: Launch console mode**

Run:

```bash
uv run friday_voice
```

Wait for the ready line ("...What can I do for you, sir?" or similar).

- [ ] **Step 2: Verify pre-announce + silence on success (action tool)**

Say/type: `open Chrome`

Expected sequence:
1. FRIDAY speaks a pre-line first, e.g. "Opening Chrome, sir."
2. Chrome launches.
3. **No** trailing "Done, sir." on success (silence after the action).

- [ ] **Step 3: Verify failure IS reported (action tool)**

With Spotify NOT running, say/type: `close Spotify`

Expected: a pre-line ("Closing Spotify.") followed by a spoken report that it wasn't running / couldn't be closed (failure counts as "something to report").

- [ ] **Step 4: Verify NO pre-announce for lookups**

Say/type: `what's seventeen times twenty-three` and `what's the weather`

Expected: no pre-line — FRIDAY just answers with the result. (If he pre-announces a lookup, the "ONLY for actions" clause needs strengthening.)

- [ ] **Step 5: Verify a multi-action turn**

Say/type: `close Spotify and open Chrome`

Expected: a single covering pre-line before the actions (not a separate line per tool), both actions run, silence on success.

- [ ] **Step 6: Record the outcome**

If Steps 2–5 all pass → feature is done; hand back to the owner (they handle git).

If Gemini **frequently** skips the pre-line (Step 2 fails often across retries), that is the trigger — documented in the spec — to escalate to Approach C (a deterministic fallback in `llm_node` that injects a canned ack only when an action tool call arrives with no preceding text). Do **not** build Approach C unless this step shows it's needed; report back first.

---

## Self-Review

- **Spec coverage:** Behavior contract (pre-announce actions / lookups untouched / silence-on-success) → Task 1. Mechanism = prompt-only → Task 1. "Unaffected paths" (`tool_choice="none"`, task/scheduler acks) → not modified by any task (correct — they must stay as-is). ARCHITECTURE.md update → Task 2. Verification (open Chrome, close-not-running, weather/math, multi-action) → Task 3. Approach-C escalation trigger → Task 3 Step 6. All spec sections covered.
- **Placeholder scan:** No TBD/TODO; the one exact line to replace and its full replacement text are both given verbatim; the ARCHITECTURE.md insertion text is given verbatim; every verification step has concrete inputs and expected outputs. No unresolved placeholders.
- **Type consistency:** No new types/functions introduced. The only identifier referenced is the existing `SYSTEM_PROMPT` in `friday/config.py`, used consistently.
