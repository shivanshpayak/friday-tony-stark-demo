"""
FRC Domain — Tools for building, deploying, and simulating FIRST Robotics Competition code.
"""

import subprocess
import logging
from pathlib import Path

from friday import plat

logger = logging.getLogger("friday-agent")

def _run_gradle(project_path: str, command: str) -> str:
    path = Path(project_path)
    if not path.exists() or not path.is_dir():
        return f"Error: Project path '{project_path}' does not exist or is not a directory."
        
    gradlew_path = path / ("gradlew.bat" if plat.IS_WINDOWS else "gradlew")
    if not gradlew_path.exists():
        return f"Error: Could not find {gradlew_path.name} in '{project_path}'. Are you sure this is the FRC project root?"

    try:
        result = subprocess.run(
            [str(gradlew_path), command],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=120
        )
        
        # Gradle outputs can be very long. We just want the BUILD SUCCESSFUL/FAILED part 
        # and any immediate errors.
        lines = result.stdout.splitlines()
        summary = "\n".join(lines[-20:]) if len(lines) > 20 else result.stdout
        
        if result.returncode == 0:
            return f"BUILD SUCCESSFUL.\n\nSummary:\n{summary}"
        else:
            errors = result.stderr if result.stderr else summary
            return f"BUILD FAILED.\n\nErrors:\n{errors}"
            
    except subprocess.TimeoutExpired:
        return f"Error: The Gradle command '{command}' timed out after 2 minutes."
    except Exception as e:
        return f"Error running Gradle: {e}"


def find_robot_projects(root: Path | None = None) -> list[Path]:
    """WPILib robot projects under the FRC folder, most recently changed first."""
    from friday.config import FRC_PROJECTS_ROOT
    root = root or FRC_PROJECTS_ROOT
    if not root.is_dir():
        return []
    projects = [d for d in root.iterdir()
                if d.is_dir() and (d / ".wpilib" / "wpilib_preferences.json").exists()]
    return sorted(projects, key=lambda d: d.stat().st_mtime, reverse=True)


def pick_robot_project(spoken: str = "", root: Path | None = None) -> Path | None:
    """The project the user means: a spoken name if one matches, else the newest."""
    projects = find_robot_projects(root)
    if not projects:
        return None
    words = [w for w in spoken.lower().replace("-", " ").split() if w]
    if words:
        for project in projects:
            flat = project.name.lower().replace("-", " ").replace("_", " ")
            if all(word in flat for word in words):
                return project
    return projects[0]


def _open_frc_apps(project: str = "") -> str:
    """Windows: open the robot-work apps. The Driver Station brings Elastic up
    with it, so Elastic is only opened directly if the Driver Station isn't
    installed."""
    from friday.tools import apps

    opened, problems = [], []

    def bring_up(label: str, app_name: str) -> bool:
        result = apps.launch_app(app_name)
        ok = result.startswith("Opening")
        (opened if ok else problems).append(label if ok else f"{label} ({result})")
        return ok

    bring_up("robot code", "frc vs code")
    if not bring_up("Driver Station", "frc driver station"):
        bring_up("Elastic", "elastic")
    bring_up("AdvantageScope", "advantagescope")

    parts = []
    if opened:
        parts.append("FRC mode: opened " + ", ".join(opened) + ".")
    if problems:
        parts.append("Couldn't open " + "; ".join(problems) + ".")
    return " ".join(parts) or "Nothing to open for FRC mode."


def _start_frc_mode(project: str = "") -> str:
    """FRC work needs the Driver Station, which only runs on Windows. On
    Windows, open everything. Anywhere else, leave the request for the Windows
    side and reboot into it."""
    if plat.IS_WINDOWS:
        return _open_frc_apps(project)

    from friday import handoff

    blocker = plat.can_restart_into_windows()
    if blocker:
        return f"FRC mode needs Windows, and I can't switch: {blocker}."
    request = "FRC mode" + (f" for the {project} project" if project else "")
    try:
        handoff.leave_task(request, "windows")
    except Exception as e:
        logger.warning("FRC handoff failed: %s", e)
        return ("FRC mode needs Windows, but I couldn't leave myself the note "
                "to continue there, so I'm staying put.")
    plat.restart_into_windows()
    return "Handoff queued. Restarting into Windows now."


_FRC_MODE_DOC = """
Enter FRC mode: set the machine up for robot work (robot code, Driver
Station, dashboard, logs). Use when the user says "FRC mode", "robot
mode", "set up for robotics", "let's work on the robot", or similar.
`project` is optional — a spoken project name.
"""

# Told to the model only where it is true. On Windows it made the pickup after
# a switch announce one ("Going to Windows, sir.") and then say nothing more —
# which is where the confirmation belongs.
_FRC_SWITCH_DOC = """
The Driver Station only runs on Windows. This is not Windows, so this
tool leaves a note for the Windows side and RESTARTS THE COMPUTER into
Windows, where FRC mode then opens by itself. Say exactly "Switching,
sir. I'll see you on the other side." and call this tool in the same
turn; say nothing once it returns.
"""


def register(mcp):

    @mcp.tool(description=_FRC_MODE_DOC + ("" if plat.IS_WINDOWS else _FRC_SWITCH_DOC))
    def start_frc_mode(project: str = "") -> str:
        logger.info("Starting FRC mode (project=%r)", project)
        return _start_frc_mode(project)

    @mcp.tool()
    def build_frc_code(project_path: str) -> str:
        """
        Run the Gradle build command for an FRC robot project.
        Use this when the user asks to compile or check the robot code for errors.
        If you do not know the project path, you MUST ask the user for the absolute path to their FRC code first.
        """
        logger.info(f"Building FRC code in {project_path}")
        return _run_gradle(project_path, "build")

    @mcp.tool()
    def deploy_frc_code(project_path: str) -> str:
        """
        Run the Gradle deploy command to push code to the RoboRIO.
        Use this when the user asks to deploy the robot code.
        If you do not know the project path, you MUST ask the user for the absolute path to their FRC code first.
        """
        logger.info(f"Deploying FRC code from {project_path}")
        return _run_gradle(project_path, "deploy")

    @mcp.tool()
    def start_frc_simulation(project_path: str) -> str:
        """
        Run the Gradle simulateJava command to launch the local physics simulation.
        Use this when the user asks to simulate the robot code.
        If you do not know the project path, you MUST ask the user for the absolute path to their FRC code first.
        """
        logger.info(f"Simulating FRC code in {project_path}")
        # Note: we use subprocess.Popen here because simulation stays open
        path = Path(project_path)
        if not path.exists():
            return f"Error: Project path '{project_path}' does not exist."
            
        gradlew_path = path / ("gradlew.bat" if plat.IS_WINDOWS else "gradlew")
        try:
            plat.open_terminal([str(gradlew_path), "simulateJava"], cwd=str(path))
            return "Successfully launched the FRC simulation in a new window."
        except Exception as e:
            return f"Error launching simulation: {e}"

    @mcp.tool()
    def launch_frc_dashboard(dashboard_name: str) -> str:
        """
        Launch a common FRC utility or dashboard (e.g., 'Driver Station', 'Elastic', 'AdvantageScope', 'Shuffleboard').
        """
        name = dashboard_name.lower()
        if not plat.IS_WINDOWS:
            if "driver" in name or "station" in name:
                return "The FRC Driver Station only runs on Windows, sir."
            # WPILib installs desktop entries (Elastic, AdvantageScope, Glass...).
            from friday.tools.apps_linux import launch_app
            return launch_app(dashboard_name)
        if "driver" in name or "station" in name:
            path = r"C:\Program Files (x86)\FRC Driver Station\DriverStation.exe"
        elif "elastic" in name:
            # Typical path if installed via shortcut, otherwise we fallback
            path = r"C:\Users\Public\Desktop\Elastic.lnk" 
        else:
            return f"I don't have a hardcoded path for the {dashboard_name} dashboard yet."
            
        if not Path(path).exists():
            return f"Error: Could not find the executable for {dashboard_name} at {path}."
            
        try:
            # We use os.startfile for Windows to properly handle .exe and .lnk
            import os
            os.startfile(path)
            return f"Successfully launched {dashboard_name}."
        except Exception as e:
            return f"Failed to launch {dashboard_name}: {e}"

