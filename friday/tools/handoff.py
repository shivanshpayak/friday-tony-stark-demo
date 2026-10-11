"""Cross-OS handoff tools — leave a task for the other operating system."""
import logging

from mcp.server.fastmcp import FastMCP

from friday import handoff, plat

logger = logging.getLogger("friday-agent")


def _leave_task_for_other_os(request: str = "", target_os: str = "",
                             restart_now: bool = False) -> str:
    request = request.strip()
    target = (target_os or handoff.other_os()).strip().lower()
    if target == handoff.current_os():
        return "That's this system, sir. I can simply do it now."
    if not request and not restart_now:
        return "There's nothing to leave for it, sir."
    if restart_now:
        blocker = plat.can_restart_into(target)
        if blocker:
            return f"I can't switch to {target.capitalize()}: {blocker}."
    if request:
        try:
            handoff.leave_task(request, target)
        except Exception as e:
            logger.warning("leave_task failed: %s", e)
            return "I couldn't save that task, sir. Nothing has been queued."
    if restart_now:
        plat.restart_into(target)
        queued = "Handoff queued. " if request else ""
        return f"{queued}Restarting into {target.capitalize()} now."
    return f"Noted for {target.capitalize()}, sir. It'll run when I start up there."


def register(mcp: FastMCP):

    @mcp.tool()
    def leave_task_for_other_os(request: str = "", target_os: str = "",
                                restart_now: bool = False) -> str:
        """Leave a task for JARVIS to carry out on the other operating system
        (this laptop dual-boots Windows and Linux; JARVIS runs on both and
        picks pending tasks up when it starts), and/or switch to it.

        Use when the user says "next time I'm on Windows, ...", "when I boot
        Linux, ...", "switch to Windows and ...", or just "switch to Linux".
        `request` is the task, phrased the way the user would ask JARVIS for
        it; leave it empty when the user only wants to switch. `target_os` is
        "windows" or "linux"; leave empty for whichever one this is not.

        Set `restart_now` ONLY when the user asked to switch right now — it
        restarts the computer. Saying you will switch does nothing: the restart
        happens only when you CALL this tool. Say exactly "Switching, sir.
        I'll see you on the other side." and call this tool in the same turn;
        say nothing once it returns.
        """
        return _leave_task_for_other_os(request, target_os, restart_now)
