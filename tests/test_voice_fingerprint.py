"""One speaker fingerprint per OS.

The dual-boot installs share the repo through git. With a single
voice_embedding.npy, enrolling on Linux replaced the Windows fingerprint on the
next pull (the two differ a lot — cosine 0.74 between them). Each OS now reads
and writes its own file.
"""
from types import SimpleNamespace

from friday import plat


def test_each_os_has_its_own_fingerprint_file(monkeypatch):
    monkeypatch.setattr(plat, "IS_WINDOWS", True)
    windows = plat.voice_embedding_path()
    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    linux = plat.voice_embedding_path()

    assert windows.name == "voice_embedding.windows.npy"
    assert linux.name == "voice_embedding.linux.npy"
    assert windows.parent == linux.parent   # both at the repo root


def test_wake_gate_session_gate_and_enrollment_agree_on_the_file():
    import enroll_voice
    import friday_launcher
    from friday import config

    expected = plat.voice_embedding_path()
    assert friday_launcher.VOICE_EMBEDDING_PATH == expected   # wake-word gate
    assert config.VOICE_EMBEDDING_PATH == expected            # in-session gate
    assert enroll_voice.OUTPUT_PATH == expected               # what enrolling writes
    # The backup is per-OS too, so it can't clobber the other side's either.
    assert enroll_voice.BACKUP_PATH.name == expected.stem + ".previous.npy"


def test_windows_enrollment_finds_the_launcher_and_nothing_else(monkeypatch):
    """Windows has no PID file, so enrollment looks the launcher up by command
    line — it must match the launcher's own processes and no bystanders."""
    import enroll_voice
    import psutil

    def proc(pid, name, *cmdline):
        # psutil reports None for a command line it isn't allowed to read.
        return SimpleNamespace(
            info={"pid": pid, "name": name, "cmdline": list(cmdline) or None})

    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: [
        proc(10, "python.exe", r"C:\repo\.venv\Scripts\python.exe",
             r"C:\repo\.venv\Scripts\friday_start.exe"),
        proc(11, "pythonw.exe", "pythonw.exe", "-m", "uv", "run", "friday_start"),
        proc(12, "python.exe", "python.exe", r"C:\repo\friday_launcher.py"),
        proc(20, "python.exe", "python.exe", r"C:\repo\agent_friday.py", "console"),
        proc(21, "python.exe", "python.exe", r"C:\repo\enroll_voice.py"),
        proc(22, "notepad.exe", "notepad.exe", r"C:\repo\friday_launcher.py"),
        proc(23, "System"),
    ])
    assert enroll_voice._windows_launcher_pids() == [10, 11, 12]
