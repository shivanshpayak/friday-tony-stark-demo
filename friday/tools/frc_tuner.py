"""
FRC AI Auto-Tuner — Iterative PID tuning via NetworkTables step responses.

Runs up to MAX_ITERATIONS autonomously: run step response → analyze →
adjust PID → repeat. Stops early if the response is good enough.
"""

import threading
import time
import logging
from datetime import datetime
from uuid import uuid4
from pathlib import Path
from friday.tasking.models import TaskRecord
from friday.tasking.store import create_task, save_task

logger = logging.getLogger("friday-agent")

_REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = _REPO_ROOT / "runtime"
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

# Tuning thresholds
MAX_ITERATIONS = 12
OVERSHOOT_OK = 5.0        # % — under this is "good enough"
STEADY_STATE_OK = 2.0     # % of target — under this is "good enough"
SETTLE_WINDOW = 0.3       # last 30% of samples used to check settling

# PID safety bounds
P_MAX = 10.0
I_MAX = 5.0
D_MAX = 5.0


def _run_step_response(inst, target_key: str, actual_key: str, target_val: float, duration: float):
    """Run one step response test. Returns (times, actuals) or None on failure."""
    target_pub = inst.getDoubleTopic(target_key).publish()
    actual_sub = inst.getDoubleTopic(actual_key).subscribe(0.0)

    # Reset by setting target to 0, wait briefly, then step to target
    target_pub.set(0.0)
    time.sleep(0.5)
    target_pub.set(target_val)

    start = time.time()
    times, actuals = [], []

    while (time.time() - start) < duration:
        if not inst.isConnected():
            return None
        t = time.time() - start
        times.append(t)
        actuals.append(actual_sub.get())
        time.sleep(0.02)  # 50Hz

    return times, actuals


def _analyze(actuals, target_val):
    """Analyze a step response. Returns dict with metrics."""
    if not actuals or target_val == 0:
        return None

    peak = max(actuals)
    overshoot = max(0, peak - target_val)
    overshoot_pct = (overshoot / target_val) * 100

    # Steady-state: average of last SETTLE_WINDOW of samples
    settle_start = int(len(actuals) * (1 - SETTLE_WINDOW))
    settled = actuals[settle_start:]
    steady_avg = sum(settled) / len(settled) if settled else actuals[-1]
    steady_err = abs(target_val - steady_avg)
    steady_err_pct = (steady_err / target_val) * 100

    # Rise time: how long to first reach 90% of target
    threshold_90 = target_val * 0.9
    rise_time = None
    for i, v in enumerate(actuals):
        if v >= threshold_90:
            rise_time = i * 0.02  # 50Hz → seconds
            break

    # Is it oscillating? Count zero-crossings around target in settled region
    crossings = 0
    for i in range(1, len(settled)):
        if (settled[i - 1] - target_val) * (settled[i] - target_val) < 0:
            crossings += 1

    return {
        "peak": peak,
        "overshoot_pct": overshoot_pct,
        "steady_err": steady_err,
        "steady_err_pct": steady_err_pct,
        "steady_avg": steady_avg,
        "rise_time": rise_time,
        "oscillating": crossings > 4,
        "good_enough": overshoot_pct < OVERSHOOT_OK and steady_err_pct < STEADY_STATE_OK,
    }


def _adjust_pid(p, i, d, analysis, target_val):
    """Compute new PID values based on step response analysis."""
    overshoot = analysis["overshoot_pct"]
    steady_err_pct = analysis["steady_err_pct"]
    oscillating = analysis["oscillating"]
    rise_time = analysis["rise_time"]
    steady_avg = analysis["steady_avg"]

    new_p, new_i, new_d = p, i, d

    if oscillating:
        # System is unstable — reduce P aggressively, bump D
        new_p *= 0.65
        new_d = min(d + p * 0.15, D_MAX)
        new_i *= 0.5  # reduce I to stop windup
    elif overshoot > 30:
        # Way too much overshoot
        new_p *= 0.7
        new_d = min(d + p * 0.1, D_MAX)
    elif overshoot > 10:
        # Moderate overshoot
        new_p *= 0.85
        new_d = min(d + p * 0.05, D_MAX)
    elif overshoot > OVERSHOOT_OK:
        # Slight overshoot
        new_p *= 0.93
        new_d = min(d + p * 0.02, D_MAX)

    # Steady-state error — need more I (or more P if I is 0 and response is sluggish)
    if steady_err_pct > STEADY_STATE_OK:
        if steady_avg < target_val:
            # Undershoot — system isn't reaching target
            if i == 0 and rise_time is None:
                # Never reached 90% of target and no I term — bump P
                new_p = min(new_p * 1.3, P_MAX)
            else:
                # Add I to close the gap
                increment = p * 0.03 * (steady_err_pct / 10)
                new_i = min(new_i + increment, I_MAX)
        else:
            # Overshoot settling high — reduce I slightly
            new_i *= 0.85

    # If response is too slow (never reached 90% target), increase P
    if rise_time is None and not oscillating:
        new_p = min(new_p * 1.25, P_MAX)

    # Clamp
    new_p = max(0.001, min(new_p, P_MAX))
    new_i = max(0.0, min(new_i, I_MAX))
    new_d = max(0.0, min(new_d, D_MAX))

    return round(new_p, 6), round(new_i, 6), round(new_d, 6)


def _push_values(inst, p_key, p_val, i_key, i_val, d_key, d_val):
    """Push PID values to NetworkTables."""
    inst.getDoubleTopic(p_key).publish().set(p_val)
    if i_key:
        inst.getDoubleTopic(i_key).publish().set(i_val)
    if d_key:
        inst.getDoubleTopic(d_key).publish().set(d_val)
    time.sleep(0.3)  # let values propagate


def _generate_graph(all_runs, target_val, task_id):
    """Generate a comparison graph of all iterations."""
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    fig, ax = plt.subplots(figsize=(10, 6))
    for run in all_runs:
        alpha = 0.3 if run["iteration"] < len(all_runs) - 1 else 1.0
        label = f"Iter {run['iteration']} (P={run['p']:.4f})"
        ax.plot(run["times"], run["actuals"], alpha=alpha, label=label)

    ax.axhline(y=target_val, color="red", linestyle="--", label=f"Target ({target_val})")
    ax.set_title(f"PID Auto-Tune — {len(all_runs)} Iterations")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Value")
    ax.legend(fontsize=7, loc="lower right")
    ax.grid(True, alpha=0.3)

    path = RUNTIME_DIR / f"autotune_{task_id}.png"
    plt.savefig(path, dpi=100)
    plt.close()
    return path


def _auto_tune_loop(task_id: str, ip: str, p_key: str, p_val: float,
                     i_key: str, i_val: float, d_key: str, d_val: float,
                     target_key: str, actual_key: str, target_val: float,
                     duration: float):
    """Main auto-tuning loop. Runs in a background thread."""
    try:
        import ntcore
    except ImportError:
        from friday.tasking.store import load_task
        task = load_task(task_id)
        if task:
            task.status = "failed"
            task.final_summary = "pyntcore is not installed."
            save_task(task)
        return

    inst = ntcore.NetworkTableInstance.create()
    inst.setServer(ip)
    inst.startClient4("FridayAutoTuner")

    # Wait for connection
    deadline = time.time() + 5.0
    while not inst.isConnected() and time.time() < deadline:
        time.sleep(0.1)

    if not inst.isConnected():
        inst.stopClient()
        ntcore.NetworkTableInstance.destroy(inst)
        from friday.tasking.store import load_task
        task = load_task(task_id)
        if task:
            task.status = "failed"
            task.final_summary = f"Could not connect to the RoboRIO at {ip}."
            save_task(task)
        return

    p, i, d = p_val, i_val, d_val
    all_runs = []
    best_run = None
    best_score = float("inf")

    # Update task to running
    from friday.tasking.store import load_task
    task = load_task(task_id)
    if task:
        task.status = "running"
        save_task(task)

    try:
        for iteration in range(1, MAX_ITERATIONS + 1):
            logger.info(f"Auto-tune iteration {iteration}/{MAX_ITERATIONS}: P={p:.4f} I={i:.4f} D={d:.4f}")

            # Push current PID values
            _push_values(inst, p_key, p, i_key, i, d_key, d)

            # Run step response
            result = _run_step_response(inst, target_key, actual_key, target_val, duration)
            if result is None:
                logger.error("Connection lost during iteration %d", iteration)
                break

            times, actuals = result

            # Check for garbage data
            if all(v == 0.0 for v in actuals):
                logger.warning("All zeros on iteration %d — sensor key may be wrong", iteration)
                break

            # Analyze
            analysis = _analyze(actuals, target_val)
            if analysis is None:
                break

            # Track this run
            run_data = {
                "iteration": iteration,
                "p": p, "i": i, "d": d,
                "times": times,
                "actuals": actuals,
                "analysis": analysis,
            }
            all_runs.append(run_data)

            # Score: weighted combination of overshoot and steady-state error
            score = analysis["overshoot_pct"] * 1.5 + analysis["steady_err_pct"]
            if score < best_score:
                best_score = score
                best_run = run_data

            logger.info(
                f"  → overshoot={analysis['overshoot_pct']:.1f}%  "
                f"steady_err={analysis['steady_err_pct']:.1f}%  "
                f"score={score:.1f}  good={analysis['good_enough']}"
            )

            # Check if we're done
            if analysis["good_enough"]:
                logger.info("Auto-tune converged at iteration %d!", iteration)
                break

            # Adjust PID for next iteration
            p, i, d = _adjust_pid(p, i, d, analysis, target_val)

            # Brief pause between iterations to let mechanism settle
            time.sleep(0.5)

    finally:
        inst.stopClient()
        ntcore.NetworkTableInstance.destroy(inst)

    # Generate comparison graph
    graph_path = _generate_graph(all_runs, target_val, task_id) if all_runs else None

    # Build summary
    if not all_runs:
        summary = "Auto-tune failed — no valid data was collected. Check the NT keys and robot connection."
    elif best_run:
        ba = best_run["analysis"]
        converged = ba["good_enough"]
        iters = len(all_runs)

        if converged:
            summary = (
                f"PID auto-tune complete — converged in {iters} iteration{'s' if iters > 1 else ''}. "
                f"Final values: kP={best_run['p']:.4f}, kI={best_run['i']:.4f}, kD={best_run['d']:.4f}. "
                f"Overshoot: {ba['overshoot_pct']:.1f}%, steady-state error: {ba['steady_err_pct']:.1f}%."
            )
        else:
            summary = (
                f"Ran {iters} iterations but couldn't fully converge. "
                f"Best result: kP={best_run['p']:.4f}, kI={best_run['i']:.4f}, kD={best_run['d']:.4f} "
                f"(overshoot {ba['overshoot_pct']:.1f}%, steady-state error {ba['steady_err_pct']:.1f}%). "
                f"These values are loaded on the robot — further manual tuning is up to you, sir."
            )

        if graph_path:
            summary += f" Graph saved to {graph_path}."

        # Make sure best values are loaded on the robot
        if best_run != all_runs[-1]:
            # Re-push the best values if the last iteration wasn't the best
            try:
                inst2 = ntcore.NetworkTableInstance.create()
                inst2.setServer(ip)
                inst2.startClient4("FridayPIDFinal")
                deadline = time.time() + 3.0
                while not inst2.isConnected() and time.time() < deadline:
                    time.sleep(0.1)
                if inst2.isConnected():
                    _push_values(inst2, p_key, best_run["p"],
                                 i_key, best_run["i"], d_key, best_run["d"])
                inst2.stopClient()
                ntcore.NetworkTableInstance.destroy(inst2)
            except Exception as e:
                logger.warning("Could not re-push best PID values: %s", e)
    else:
        summary = "Auto-tune produced no results."

    task = load_task(task_id)
    if task:
        task.status = "completed"
        task.final_summary = summary
        save_task(task)


def register(mcp):
    @mcp.tool()
    def auto_tune_pid(
        robot_ip: str,
        p_key: str,
        p_val: float,
        target_key: str,
        actual_key: str,
        target_val: float,
        i_key: str = "",
        i_val: float = 0.0,
        d_key: str = "",
        d_val: float = 0.0,
        duration: float = 3.0,
    ) -> str:
        """
        Automatically tune a PID loop via NetworkTables step responses.
        Runs up to 12 iterations: sets PID → runs step response → analyzes
        overshoot and steady-state error → adjusts values → repeats.
        Stops early when the response is good enough.

        This is a background task — JARVIS will report results when done.

        Args:
            robot_ip: RoboRIO IP (e.g. '10.94.77.2').
            p_key: NetworkTables key for kP (e.g. '/SmartDashboard/Shooter/kP').
            p_val: Starting kP value.
            target_key: NT key for the target setpoint.
            actual_key: NT key for the actual sensor reading.
            target_val: The setpoint value for the step response test.
            i_key: (Optional) NT key for kI.
            i_val: (Optional) Starting kI value.
            d_key: (Optional) NT key for kD.
            d_val: (Optional) Starting kD value.
            duration: Seconds per test (default 3.0). Increase for heavy mechanisms.
        """
        # Safety check on starting values
        if p_val > P_MAX or p_val < 0:
            return f"Starting kP={p_val} is outside safe bounds (0 to {P_MAX})."
        if i_val > I_MAX or i_val < 0:
            return f"Starting kI={i_val} is outside safe bounds (0 to {I_MAX})."
        if d_val > D_MAX or d_val < 0:
            return f"Starting kD={d_val} is outside safe bounds (0 to {D_MAX})."

        task_id = f"autotune_{datetime.now().strftime('%Y%m%d')}_{uuid4().hex[:6]}"
        task = TaskRecord(
            task_id=task_id,
            goal=f"Auto-tune PID for {target_key} (target={target_val})",
            status="pending",
            mode="planner",
            source="voice",
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
            steps=[]
        )
        create_task(task)

        thread = threading.Thread(
            target=_auto_tune_loop,
            args=(task_id, ip, p_key, p_val, i_key, i_val, d_key, d_val,
                  target_key, actual_key, target_val, duration),
            daemon=True,
        )
        thread.start()

        est_time = int(MAX_ITERATIONS * (duration + 1.5))
        return (
            f"Started auto-tuning in the background. I'll run up to {MAX_ITERATIONS} iterations, "
            f"about {est_time} seconds max. I'll stop early if it converges. "
            f"Starting at kP={p_val}, kI={i_val}, kD={d_val}."
        )

    @mcp.tool()
    def push_pid_values(robot_ip: str, p_key: str, p_val: float, i_key: str = "", i_val: float = 0.0, d_key: str = "", d_val: float = 0.0) -> str:
        """
        Manually push PID values to NetworkTables. Use when the user
        explicitly says what values to set — not for auto-tuning.

        Args:
            robot_ip: The IP address of the RoboRIO.
            p_key: NetworkTables key for kP.
            p_val: New kP value.
            i_key: (Optional) NetworkTables key for kI.
            i_val: (Optional) New kI value.
            d_key: (Optional) NetworkTables key for kD.
            d_val: (Optional) New kD value.
        """
        if p_val > P_MAX or p_val < 0:
            return f"SAFETY ERROR: kP={p_val} is outside bounds (0 to {P_MAX})."
        if i_val > I_MAX or i_val < 0:
            return f"SAFETY ERROR: kI={i_val} is outside bounds (0 to {I_MAX})."
        if d_val > D_MAX or d_val < 0:
            return f"SAFETY ERROR: kD={d_val} is outside bounds (0 to {D_MAX})."

        try:
            import ntcore
        except ImportError:
            return "Error: pyntcore not installed."

        inst = ntcore.NetworkTableInstance.create()
        inst.setServer(robot_ip)
        inst.startClient4("FridayPIDPush")

        deadline = time.time() + 3.0
        while not inst.isConnected() and time.time() < deadline:
            time.sleep(0.1)

        if not inst.isConnected():
            ntcore.NetworkTableInstance.destroy(inst)
            return f"Could not connect to the RoboRIO at {robot_ip}."

        inst.getDoubleTopic(p_key).publish().set(p_val)
        if i_key:
            inst.getDoubleTopic(i_key).publish().set(i_val)
        if d_key:
            inst.getDoubleTopic(d_key).publish().set(d_val)

        time.sleep(0.3)
        inst.stopClient()
        ntcore.NetworkTableInstance.destroy(inst)
        return f"Pushed PID values: kP={p_val}, kI={i_val}, kD={d_val}."
