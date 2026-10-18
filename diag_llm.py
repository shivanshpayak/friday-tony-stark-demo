"""
Evaluate the voice agent's LLM setup through the live path.

Every request goes through the same livekit plugin the agent uses, with the
real system prompt (plus saved memories) and the real tool schemas from
server.py — but no tool is ever executed and nothing is spoken. For each
model/thinking setting it reports how often the right tool was called, how
often Gemini returned an empty completion, and time to first token.

Run it after changing the model, the thinking budget, the system prompt, or a
tool description:

    uv run python diag_llm.py                                  # current config
    uv run python diag_llm.py --configs gemini:gemini-2.5-flash:0,gemini:gemini-2.5-flash:256
    uv run python diag_llm.py --prompt candidate.txt --trials 3 --verbose
    uv run python diag_llm.py --cases tool                     # or chat / after / a case id prefix

A config is provider:model:thinking — thinking is a token budget (0 = off,
-1 = dynamic), a thinking level name, or "none" to send no thinking config.
Each line reads: passed/total and median/p90 first-token time per group
(tool requests, conversation, the turn after a tool result). It makes real API
calls: about 45 per config per trial, ~10k input tokens each.
"""
import argparse, asyncio, json, logging, os, re, statistics, sys, time
from dotenv import load_dotenv
load_dotenv()
logging.disable(logging.WARNING)
from pathlib import Path
from livekit.agents import llm as lk_llm, APIConnectOptions
from livekit.agents.llm.mcp import MCPServerStdio, MCPToolset
from friday import plat

T, C, A = "tool", "chat", "after"
def after(user, said, tool, args, output, is_error=False):
    return dict(user=user, said=said, tool=tool, args=args, output=output, is_error=is_error)
CASES = [
    # --- actions (state-changing: should announce then call) ---
    ("open_chrome", T, "Open Chrome.", {"launch_app"}),
    ("open_youtube", T, "Open YouTube.", {"open_on_screen", "launch_app"}),
    ("close_spotify", T, "Close Spotify.", {"close_app"}),
    ("play_song", T, "Play Havana by Camila Cabello.", {"search_spotify"}),
    ("play_spotify_vague", C, "Can you play some Spotify for me?", None),
    ("pause", T, "Pause the music.", {"play_pause_media"}),
    ("skip", T, "Skip.", {"next_track"}),
    ("volume_set", T, "Set the volume to forty percent.", {"set_volume"}),
    ("volume_up", T, "Turn it up a bit.", {"adjust_volume", "set_volume"}),
    ("timer", T, "Set a timer for ten minutes.", {"set_timer"}),
    ("reminder", T, "Remind me to call mom at six PM.", {"set_reminder", "get_current_time"}),
    ("remember", T, "Remember that my locker combination is 24 16 8.", {"remember"}),
    ("switch_os", T, "Switch to Windows.", {"leave_task_for_other_os"}),
    ("frc_mode", T, "FRC mode.", {"start_frc_mode"}),
    ("show_picture", T, "Show me a picture of the Eiffel Tower.", {"open_on_screen"}),
    # --- lookups (should NOT pre-announce) ---
    ("time", T, "What time is it?", {"get_current_time"}),
    ("weather", T, "What's the weather like today?", {"get_weather", "get_forecast"}),
    ("calc", T, "What's 17 percent of 2,340?", {"calculate"}),
    ("news", T, "What's happening with the Starship launch?", {"search_web", "get_world_news"}),
    ("sports", T, "Who won the Formula One race this weekend?", {"search_web"}),
    ("calendar", T, "What's on my calendar tomorrow?", {"list_upcoming_events"}),
    ("screen", T, "What's on my screen right now?", {"read_screen"}),
    ("system", T, "System check.", {"system_status", "get_system_info"}),
    # "?" = asking which airport / which folder is also a right answer
    ("travel", T, "How long would it take to drive to the airport?", {"get_travel_time", "get_directions", "?"}),
    ("find_file", T, "Find my resume file.", {"search_files", "?"}),
    # --- conversation (no tool) ---
    ("how_are_you", C, "How are you doing today?", None),
    ("whats_up", C, "What's up?", None),
    ("are_you_there", C, "Are you there?", None),
    ("chess_or_go", C, "Which is better, chess or Go?", None),
    ("capital", C, "What's the capital of Australia?", None),
    ("spell", C, "Spell Einstein.", None),
    ("movie", C, "Recommend me a movie for tonight.", None),
    ("sky", C, "Why is the sky blue?", None),
    ("thanks", C, "Thanks, I appreciate it.", None),
    ("dismiss", C, "Dismissed.", None),
    ("dismiss2", C, "Never mind. Dismissed.", None),
    # --- after a tool result ---
    ("after_open_ok", A, after("Open Chrome.", "Opening Chrome, sir.", "launch_app", {"app_name": "chrome"}, "Opened chrome."), "silent"),
    ("after_close_ok", A, after("Close Spotify.", "Closing Spotify.", "close_app", {"app_name": "spotify"}, "Closed spotify."), "silent"),
    ("after_skip_ok", A, after("Skip.", "Skipping, sir.", "next_track", {}, "Skipped to the next track."), "silent"),
    ("after_open_unannounced", A, after("Open Chrome.", "", "launch_app", {"app_name": "chrome"}, "Opened chrome."), "speak"),
    ("after_close_fail", A, after("Close Discord.", "Closing Discord.", "close_app", {"app_name": "discord"}, "discord is not running.", True), "speak"),
    ("after_time", A, after("What time is it?", "", "get_current_time", {}, "It's 9:42 PM on Sunday, October 11, 2026 (EDT)."), "speak"),
    ("after_calc", A, after("What's 17 percent of 2,340?", "", "calculate", {"expression": "0.17*2340"}, "397.8"), "speak"),
    ("after_search", A, after("What's the general price of a Steam Deck?", "", "search_web", {"query": "Steam Deck price"},
        "1. Steam Deck LCD 256GB: $399. Steam Deck OLED 512GB: $549, 1TB: $649 (store.steampowered.com)\n2. Valve keeps Steam Deck pricing unchanged in 2026 (theverge.com)"), "speak"),
    ("after_spotify", A, after("Play Havana.", "Pulling up Havana, sir.", "search_spotify", {"query": "Havana", "type": "track"}, "Pulled up the track, sir — hit play when ready."), "any"),
]

async def load_tools():
    ts = MCPToolset(id="eval", mcp_server=MCPServerStdio(command=sys.executable, args=["server.py"],
                    env=plat.tool_server_env(), cwd=os.getcwd(), client_session_timeout_seconds=30))
    await ts.setup(); return ts

def build(provider, model, budget):
    if provider == "gemini":
        from livekit.plugins import google as g
        kw = {}
        if budget not in (None, "none"):
            kw["thinking_config"] = ({"thinking_level": budget} if not str(budget).lstrip("-").isdigit() else {"thinking_budget": int(budget)})
        return g.LLM(model=model, api_key=os.environ["GOOGLE_API_KEY"], **kw)
    from livekit.plugins import openai as o
    if provider == "groq":
        return o.LLM(model=model, api_key=os.environ["GROQ_API_KEY"], base_url="https://api.groq.com/openai/v1")
    if provider == "openai":
        kw = {"reasoning_effort": budget} if budget not in (None, "none") else {}
        return o.LLM(model=model, **kw)
    raise ValueError(provider)

def make_ctx(system, kind, payload):
    ctx = lk_llm.ChatContext.empty()
    ctx.add_message(role="system", content=system)
    if kind == A:
        ctx.add_message(role="user", content=payload["user"])
        if payload["said"]: ctx.add_message(role="assistant", content=payload["said"])
        ctx.items.append(lk_llm.FunctionCall(call_id="call_1", name=payload["tool"], arguments=json.dumps(payload["args"])))
        ctx.items.append(lk_llm.FunctionCallOutput(call_id="call_1", name=payload["tool"], output=payload["output"], is_error=payload["is_error"]))
    else:
        ctx.add_message(role="user", content=payload)
    return ctx

async def one(model, system, tools, kind, payload):
    ctx = make_ctx(system, kind, payload)
    t0 = time.perf_counter(); ttft = None; text = ""; calls = []; err = None; usage = None; first_kind = None
    try:
        stream = model.chat(chat_ctx=ctx, tools=tools, conn_options=APIConnectOptions(max_retry=0, timeout=30))
        async for ch in stream:
            if ch.usage: usage = ch.usage
            d = ch.delta
            if d is None: continue
            if d.content or d.tool_calls:
                if ttft is None: ttft = time.perf_counter() - t0; first_kind = "text" if d.content else "tool"
            if d.content: text += d.content
            for tc in d.tool_calls or []: calls.append((tc.name, tc.arguments))
        await stream.aclose()
    except Exception as e:
        body = getattr(e, "body", None)
        msg = f"{e} {body or ''}"
        err = "EMPTY" if "no response generated" in msg else f"{type(e).__name__}: {msg[:160]}"
    return dict(ttft=ttft, total=time.perf_counter() - t0, text=text.strip(), calls=calls, err=err, first=first_kind,
                ptok=getattr(usage, "prompt_tokens", None), ctok=getattr(usage, "completion_tokens", None))

BANNED = re.compile(r"great question|absolutely|of course|sure thing|happy to help|as an ai|language model|[*`#]|!", re.I)
def payload_ok_tool(r): return r["calls"][0][0] in ("search_spotify", "play_pause_media", "launch_app")
def grade(kind, expect, r):
    """-> (ok, notes)"""
    notes = []
    if r["err"] and r["err"] != "EMPTY": return False, ["error"]
    if r["text"] and BANNED.search(r["text"]): notes.append("style")
    if r["text"] and len(r["text"].split()) > 60: notes.append("long")
    if kind == T:
        if r["err"] == "EMPTY": return False, ["empty"]
        if not r["calls"]:
            return ("?" in expect and bool(r["text"])), ["asked" if "?" in expect else "no_tool"]
        if r["calls"][0][0] not in expect: return False, ["wrong_tool:" + r["calls"][0][0]]
        if r["text"]: notes.append("announced")
        return True, notes
    if kind == C:
        if r["err"] == "EMPTY": return False, ["empty"]
        if r["calls"]: return (payload_ok_tool(r), ["tool:" + r["calls"][0][0]])
        return bool(r["text"]), notes
    if r["calls"]: return False, ["extra_tool:" + r["calls"][0][0]]
    if expect == "silent":
        if r["err"] == "EMPTY": return True, ["silent(empty)"]
        if len(r["text"].split()) <= 1: return True, ["silent(token)"]   # e.g. "." / sentinel
        return False, ["spoke:" + r["text"][:40]]
    if expect == "speak":
        return (not r["err"] and bool(r["text"])), (["empty"] if r["err"] else notes)
    return True, (["silent(empty)"] if r["err"] else notes)

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--configs", default="")      # provider:model:thinking,...  (default: current config)
    ap.add_argument("--trials", type=int, default=2)
    ap.add_argument("--cases", default="")
    ap.add_argument("--prompt", default="")       # file with alternate system prompt
    ap.add_argument("--out", default="")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    from friday.config import SYSTEM_PROMPT, GEMINI_LLM_MODEL, FAST_THINKING_BUDGET
    a.configs = a.configs or f"gemini:{GEMINI_LLM_MODEL}:{FAST_THINKING_BUDGET}"
    from friday.tools.memory import get_memories_prompt
    base = Path(a.prompt).read_text() if a.prompt else SYSTEM_PROMPT
    mem = get_memories_prompt()
    system = base + ("\n\n" + mem if mem else "")
    ts = await load_tools(); tools = list(ts.tools)
    cases = [c for c in CASES if not a.cases or any(c[0].startswith(p) or c[1] == p for p in a.cases.split(","))]
    print(f"# {len(tools)} tools, system prompt {len(system)} chars, {len(cases)} cases x {a.trials} trials", flush=True)
    dump = {}
    for cfg in a.configs.split(","):
        provider, model, budget = (cfg.split(":") + ["none"])[:3]
        try: m = build(provider, model, budget)
        except Exception as e: print(f"{cfg}: build failed {e}"); continue
        await one(m, system, tools, C, "Hello.")   # warm connection
        rows = []; fails = []
        for cid, kind, payload, expect in cases:
            for _ in range(a.trials):
                r = await one(m, system, tools, kind, payload)
                ok, notes = grade(kind, expect, r)
                r.update(case=cid, kind=kind, ok=ok, notes=notes); rows.append(r)
                if a.verbose or not ok: fails.append(f"    {'ok ' if ok else 'BAD'} {cid:20s} {notes} text={r['text'][:70]!r} calls={[c[0] for c in r['calls']]} err={r['err']}")
        dump[cfg] = rows
        def agg(k):
            rs = [r for r in rows if r["kind"] == k]
            tt = [r["ttft"] for r in rs if r["ttft"] is not None]
            return (sum(r["ok"] for r in rs), len(rs), statistics.median(tt) * 1000 if tt else -1,
                    sorted(tt)[int(len(tt) * 0.9) - 1] * 1000 if len(tt) > 2 else -1)
        t, c, af = agg(T), agg(C), agg(A)
        empties = sum(1 for r in rows if r["err"] == "EMPTY" and not (r["kind"] == A and r["ok"]))
        errs = sum(1 for r in rows if r["err"] and r["err"] != "EMPTY")
        ann = sum(1 for r in rows if "announced" in r["notes"]); style = sum(1 for r in rows if "style" in r["notes"] or "long" in r["notes"])
        ptok = next((r["ptok"] for r in rows if r["ptok"]), None)
        print(f"{cfg:42s} tool {t[0]:2d}/{t[1]:2d} ttft {t[2]:5.0f}/{t[3]:5.0f}ms | chat {c[0]:2d}/{c[1]:2d} ttft {c[2]:5.0f}/{c[3]:5.0f}ms | after {af[0]:2d}/{af[1]:2d} ttft {af[2]:5.0f}ms | bad-empty {empties} err {errs} announced {ann} style {style} ptok {ptok}", flush=True)
        for f in fails: print(f)
        if errs: print("    first error:", next(r["err"] for r in rows if r["err"] and r["err"] != "EMPTY"))
    if a.out: Path(a.out).write_text(json.dumps(dump, indent=1))
    await ts.aclose()
asyncio.run(main())
