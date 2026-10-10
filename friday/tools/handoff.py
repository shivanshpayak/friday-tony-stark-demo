"""Cross-OS handoff tools — leave a task for the other operating system."""
import logging

from mcp.server.fastmcp import FastMCP

from friday import handoff, plat

logger = logging.getLogger("friday-agent")


def register(mcp: FastMCP):

    @mcp.tool()
    def leave_task_for_other_os(request: str, target_os: str = "",
                                restart_now: bool = False) -> str:
        """Leave a task for JARVIS to carry out on the other operating system
        (this laptop dual-boots Windows and Linux; JARVIS runs on both and
        picks pending tasks up when it starts).

        Use when the user says "next time I'm on Windows, ...", "when I boot
        Linux, ...", or "switch to Windows and ...". `request` is the task,
        phrased the way the user would ask JARVIS for it. `target_os` is
        "windows" or "linux"; leave empty for whichever one this is not.

        Set `restart_now` ONLY when the user asked to switch right now — it
        restarts the computer. In that case first say one short line such as
        "Going to Windows, sir." and nothing after it.
        """
        target = (target_os or handoff.other_os()).strip().lower()
        if target == handoff.current_os() and not restart_now:
            return "That's this system, sir. I can simply do it now."
        if restart_now:
            if target != "windows":
                return "I can only restart into Windows from here, sir."
            blocker = plat.can_restart_into_windows()
            if blocker:
                return f"I can't switch to Windows: {blocker}."
        try:
            handoff.leave_task(request, target)
        except Exception as e:
            logger.warning("leave_task failed: %s", e)
            return "I couldn't save that task, sir. Nothing has been queued."
        if restart_now:
            plat.restart_into_windows()
            return "Handoff queued. Restarting into Windows now."
        return f"Noted for {target.capitalize()}, sir. It'll run when I start up there."
