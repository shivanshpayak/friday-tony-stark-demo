"""Windows -> Linux: the reboot JARVIS triggers, and the installer behind the
"Restart into Linux" shortcut."""
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import install_boot_switch as installer
from friday import plat


# --- plat: can we switch, and switching ---------------------------------------

def _schtasks_knowing(*registered):
    """A fake `schtasks /query` that only finds the task names given."""
    def run(argv, **kwargs):
        assert argv[:3] == ["schtasks", "/query", "/tn"], argv
        return subprocess.CompletedProcess(argv, 0 if argv[3] in registered else 1)
    return run


def test_linux_switch_is_available_once_its_task_is_registered(monkeypatch):
    monkeypatch.setattr(plat, "IS_WINDOWS", True)
    monkeypatch.setattr(subprocess, "run", _schtasks_knowing(plat.BOOT_LINUX_TASK))
    assert plat.can_restart_into_linux() is None


def test_linux_switch_is_refused_until_the_installer_has_run(monkeypatch):
    monkeypatch.setattr(plat, "IS_WINDOWS", True)
    monkeypatch.setattr(subprocess, "run", _schtasks_knowing("JARVIS Voice Assistant"))
    assert "isn't installed" in plat.can_restart_into_linux()


def test_linux_cannot_restart_into_linux(monkeypatch):
    def no_commands(*a, **k):
        raise AssertionError("nothing should be run to answer this on Linux")

    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    monkeypatch.setattr(subprocess, "run", no_commands)
    assert plat.can_restart_into_linux() == "already on Linux"


def test_restart_into_linux_runs_the_registered_task_after_the_delay(monkeypatch):
    """The task sets the one-time boot before restarting; a plain `shutdown`
    here would reboot into whatever the firmware happens to prefer."""
    later = []
    monkeypatch.setattr(plat, "_run_later", lambda delay, argv: later.append((delay, argv)))
    plat.restart_into_linux(delay=3)
    assert later == [(3, ["schtasks", "/run", "/tn", plat.BOOT_LINUX_TASK])]


def test_run_later_runs_the_command_in_a_process_of_its_own(tmp_path):
    marker = tmp_path / "ran.txt"
    plat._run_later(0.2, [sys.executable, "-c",
                          "import sys; open(sys.argv[1], 'w').write('ran')", str(marker)])
    assert not marker.exists()               # it waits first
    deadline = time.monotonic() + 15
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert marker.read_text() == "ran"


# --- the installer behind the "Restart into Linux" shortcut --------------------

windows_only = pytest.mark.skipif(sys.platform != "win32",
                                  reason="the Windows side of the switch")

# `bcdedit /enum firmware` on a dual-boot laptop.
LINUX_ENTRY = "{5d4c2b2e-6a3c-11ec-9a6c-806e6f6e6963}"
FIRMWARE = r"""
Firmware Boot Manager
---------------------
identifier              {fwbootmgr}
displayorder            {bootmgr}
                        {5d4c2b2f-6a3c-11ec-9a6c-806e6f6e6963}
                        {5d4c2b2e-6a3c-11ec-9a6c-806e6f6e6963}
                        {5d4c2b30-6a3c-11ec-9a6c-806e6f6e6963}
timeout                 0

Windows Boot Manager
--------------------
identifier              {bootmgr}
device                  partition=\Device\HarddiskVolume1
path                    \EFI\Microsoft\Boot\bootmgfw.efi
description             Windows Boot Manager
locale                  en-US
inherit                 {globalsettings}
default                 {current}
resumeobject            {5d4c2b2a-6a3c-11ec-9a6c-806e6f6e6963}
displayorder            {current}
toolsdisplayorder       {memdiag}
timeout                 30

Firmware Application (101fffff)
-------------------------------
identifier              {5d4c2b2f-6a3c-11ec-9a6c-806e6f6e6963}
description             UEFI: PXE IPv4 Realtek PCIe GBE Family Controller

Firmware Application (101fffff)
-------------------------------
identifier              {5d4c2b2e-6a3c-11ec-9a6c-806e6f6e6963}
device                  partition=\Device\HarddiskVolume1
path                    \EFI\ubuntu\shimx64.efi
description             ubuntu

Firmware Application (101fffff)
-------------------------------
identifier              {5d4c2b30-6a3c-11ec-9a6c-806e6f6e6963}
device                  partition=\Device\HarddiskVolume1
path                    \EFI\Boot\bootx64.efi
description             UEFI OS
"""
LINUX_LOADER = r"\EFI\ubuntu\shimx64.efi"


def test_the_linux_boot_entry_is_found_among_the_firmware_entries():
    assert installer.linux_loaders(FIRMWARE) == [("ubuntu", LINUX_LOADER)]


def test_firmware_written_upper_case_paths_are_still_linux():
    listing = FIRMWARE.replace(LINUX_LOADER, r"\EFI\FEDORA\SHIMX64.EFI")
    assert installer.linux_loaders(listing) == [("ubuntu", r"\EFI\FEDORA\SHIMX64.EFI")]


def test_a_vendor_tool_is_not_mistaken_for_linux():
    listing = FIRMWARE.replace(LINUX_LOADER, r"\EFI\HP\BIOSUpdate\HpBiosUpdate.efi")
    assert installer.linux_loaders(listing) == []


def _click(tmp_path, firmware, set_exit=0):
    """Run what the task runs, against stand-ins for bcdedit and shutdown.

    Returns (what the two were asked to do, the reason given for failing).
    """
    (tmp_path / "firmware.txt").write_text(firmware, newline="\r\n")
    (tmp_path / "bcdedit.cmd").write_text(
        "@echo off\n"
        'if /i "%~1"=="/enum" (type "%~dp0firmware.txt" & exit /b 0)\n'
        '>>"%~dp0asked.log" echo bcdedit %*\n'
        f"exit /b {set_exit}\n", newline="\r\n")
    (tmp_path / "shutdown.cmd").write_text(
        '@>>"%~dp0asked.log" echo shutdown %*\n', newline="\r\n")
    why = tmp_path / "why.txt"
    script = installer.switch_script(
        LINUX_LOADER, str(tmp_path / "bcdedit.cmd"), str(tmp_path / "shutdown.cmd"))
    script += f"\nSet-Content -LiteralPath '{why}' -Value $why\n"
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                    "-EncodedCommand", installer.encoded(script)],
                   timeout=120, creationflags=subprocess.CREATE_NO_WINDOW)
    asked = tmp_path / "asked.log"
    return ([line.strip() for line in asked.read_text().splitlines()] if asked.exists() else [],
            why.read_text().strip() if why.exists() else "")


@windows_only
def test_a_click_makes_linux_the_next_boot_then_restarts_without_forcing(tmp_path):
    asked, _ = _click(tmp_path, FIRMWARE)
    assert asked == [f"bcdedit /set {{fwbootmgr}} bootsequence {LINUX_ENTRY}",
                     "shutdown /r /t 0"]


@windows_only
def test_no_restart_if_the_firmware_refuses_the_one_time_boot(tmp_path):
    """Restarting anyway would just land back in Windows."""
    asked, why = _click(tmp_path, FIRMWARE, set_exit=1)
    assert asked == [f"bcdedit /set {{fwbootmgr}} bootsequence {LINUX_ENTRY}"]
    assert why


@windows_only
def test_no_restart_if_the_linux_boot_entry_has_gone(tmp_path):
    asked, why = _click(tmp_path, FIRMWARE.replace(LINUX_LOADER, r"\EFI\Boot\grubx64.efi"))
    assert asked == []
    assert why


@windows_only
def test_the_task_runs_elevated_and_also_on_battery():
    """Task Scheduler's default is to refuse, silently, to start a task on
    battery — on a laptop the button would then do nothing half the time."""
    import win32com.client

    scheduler = win32com.client.Dispatch("Schedule.Service")
    scheduler.Connect()
    task = scheduler.NewTask(0)
    task.XmlText = installer.task_xml(LINUX_LOADER)   # Task Scheduler parses it
    assert task.Settings.DisallowStartIfOnBatteries is False
    assert task.Settings.StopIfGoingOnBatteries is False
    assert task.Principal.RunLevel == 1               # TASK_RUNLEVEL_HIGHEST


@windows_only
def test_the_shortcut_runs_the_task_jarvis_runs(tmp_path):
    import win32com.client

    (shortcut,) = installer.create_shortcuts([tmp_path])
    link = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(shortcut))
    assert link.TargetPath.lower().endswith(r"\schtasks.exe")
    # The task name has spaces: it must still arrive as ONE argument.
    assert shlex.split(link.Arguments) == ["/run", "/tn", plat.BOOT_LINUX_TASK]


# --- Linux -> Windows: the helper behind plat.restart_into_windows --------------

HELPER = Path(installer.__file__).parent / "boot-switch" / "boot-windows-next"
needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")

# `efibootmgr` on the dual-boot laptop, booted into Linux.
EFIBOOTMGR = r"""BootCurrent: 0001
Timeout: 0 seconds
BootOrder: 0001,0000,0002
Boot0001* ubuntu	HD(1,GPT,9c9e0f7a-0d5c-4a53-8f4c-1c7e5c1b2a10,0x800,0x82000)/File(\EFI\ubuntu\shimx64.efi)
Boot0000* Windows Boot Manager	HD(1,GPT,9c9e0f7a-0d5c-4a53-8f4c-1c7e5c1b2a10,0x800,0x82000)/File(\EFI\Microsoft\Boot\bootmgfw.efi)57494e444f5753000100
Boot0002* UEFI: PXE IPv4 Realtek PCIe GBE Family Controller
"""


def _run_helper(tmp_path, listing):
    """Run the helper against a stand-in efibootmgr.

    Returns (its exit code, what efibootmgr was asked to change).
    """
    (tmp_path / "listing.txt").write_text(listing, newline="\n")
    (tmp_path / "efibootmgr").write_text(
        "#!/bin/sh\n"
        'here=$(dirname "$0")\n'
        'if [ $# -eq 0 ]; then cat "$here/listing.txt"; exit 0; fi\n'
        'echo "$*" >> "$here/asked.log"\n', newline="\n")
    (tmp_path / "efibootmgr").chmod(0o755)
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"])
    done = subprocess.run(["sh", str(HELPER)], env=env, capture_output=True, text=True)
    asked = tmp_path / "asked.log"
    return done.returncode, asked.read_text().splitlines() if asked.exists() else []


@needs_sh
def test_linux_helper_makes_the_firmware_start_windows_next(tmp_path):
    """Through the firmware, not GRUB: a Windows that GRUB starts fails
    BitLocker's TPM check and asks for the recovery key."""
    assert _run_helper(tmp_path, EFIBOOTMGR) == (0, ["--quiet --bootnext 0000"])


@needs_sh
def test_linux_helper_reads_hexadecimal_and_inactive_entry_numbers(tmp_path):
    listing = EFIBOOTMGR.replace("Boot0000* Windows", "Boot001A  Windows")
    assert _run_helper(tmp_path, listing) == (0, ["--quiet --bootnext 001A"])


@needs_sh
def test_linux_helper_fails_rather_than_guess_when_windows_has_no_entry(tmp_path):
    code, asked = _run_helper(tmp_path, EFIBOOTMGR.replace("Windows Boot Manager", "debian"))
    assert code != 0
    assert asked == []


def test_installing_on_linux_puts_the_repo_helper_where_jarvis_runs_it(monkeypatch):
    """sudoers lets JARVIS run exactly one path as root; a helper installed
    anywhere else is never the one that runs."""
    ran = []

    def run(argv, **kwargs):
        ran.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", run)
    assert installer.install_linux_helper() == 0
    (argv,) = ran
    assert argv[0] == "sudo"
    assert argv[-2:] == [str(HELPER), plat._BOOT_WINDOWS_HELPER]
