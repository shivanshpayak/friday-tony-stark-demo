"""
Install/uninstall the "Restart into Linux" button (the Windows side of the
dual boot; Linux's counterpart is /usr/local/sbin/boot-windows-next).

Windows cannot steer GRUB, but it can ask the firmware to boot the Linux entry
next, once, and then restart. That takes administrator rights, so `install`
registers a Task Scheduler task that does exactly that and runs elevated. The
Desktop / Start-menu shortcut and JARVIS ("switch to Linux") both just run the
task — no UAC prompt after the one at install time.

Usage:
    python install_boot_switch.py install [--loader \\EFI\\<distro>\\<file>.efi]
    python install_boot_switch.py uninstall
"""

import argparse
import base64
import ctypes
import getpass
import os
import re
import subprocess
import sys
import tempfile
import traceback
from contextlib import redirect_stdout
from pathlib import Path
from xml.sax.saxutils import escape

from friday.plat import _BOOT_WINDOWS_HELPER, BOOT_LINUX_TASK, BOOT_LINUX_VOICE_TASK

SHORTCUT_NAME = "Restart into Linux.lnk"

# Boot loaders Linux installs: \EFI\<distro>\shimx64.efi, grubx64.efi, ...
_LINUX_LOADER = re.compile(
    r"\\EFI\\(?:Linux\\[^\\\r\n]+|[^\\\r\n]+\\(?:shim|grub|systemd-boot|refind)[^\\\r\n]*)\.efi",
    re.IGNORECASE)


def linux_loaders(firmware_listing: str) -> list[tuple[str, str]]:
    """(description, loader path) of each entry in `bcdedit /enum firmware`
    output that boots Linux."""
    found = []
    for entry in re.split(r"(?:\r?\n){2,}", firmware_listing):
        loader = _LINUX_LOADER.search(entry)
        if loader and loader.group(0).lower() not in [path.lower() for _, path in found]:
            description = re.search(r"^description\s+(.+)$", entry,
                                    re.MULTILINE | re.IGNORECASE)
            found.append((description.group(1).strip() if description else "",
                          loader.group(0)))
    return found


# --- what the task runs --------------------------------------------------------

# The entry is looked up by its loader path on every click rather than stored
# by id at install time: Windows hands the entry a new id whenever the firmware
# or the Linux boot loader re-creates it.
_SWITCH = r"""
$id = $null; $entry = ''
foreach ($line in @(& @BCDEDIT@ /enum firmware) + '') {
    if ("$line".Trim()) { $entry += "$line`n"; continue }
    if ($entry.ToLower().Contains(@LOADER@) -and $entry -match '\{[0-9a-fA-F-]{36}\}') {
        $id = $Matches[0]; break
    }
    $entry = ''
}
$why = 'the Linux boot entry was not found'
if ($id) {
    & @BCDEDIT@ /set '{fwbootmgr}' bootsequence $id | Out-Null
    # /t 0 does not force: apps with unsaved work still get to ask first.
    if ($LASTEXITCODE -eq 0) { @PASS@& @SHUTDOWN@ /r /t 0; exit 0 }
    $why = 'the firmware refused the one-time boot'
}
"""

# The voice task only: leave Linux a one-time pass to log in without the
# password (boot-switch/jarvis-autologin-once takes it away at boot). It goes on
# the EFI system partition, the one partition both systems can read. Failing to
# leave it must never stop the switch — Linux then just asks for the password.
_LEAVE_PASS = r"""
        try {
            $esp = @ESP@
            if ($esp) {
                [IO.Directory]::CreateDirectory("$esp\EFI\jarvis") | Out-Null
                [IO.File]::WriteAllText("$esp\EFI\jarvis\autologin-once", 'voice')
            }
        } catch { }
        """

# The EFI system partition has no drive letter; reach it by its volume path.
ESP_ROOT = ("((Get-Partition | Where-Object { $_.GptType -eq "
            "'{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}' } | Select-Object -First 1 "
            "| Get-Volume).Path).TrimEnd('\\')")

_TELL_THE_USER = r"""
(New-Object -ComObject WScript.Shell).Popup(
    "Could not switch to Linux: $why.`n`nRun 'python install_boot_switch.py install' again.",
    0, 'Restart into Linux', 48) | Out-Null
exit 1
"""


def _ps(text: str) -> str:
    """`text` as a PowerShell string literal."""
    return "'" + text.replace("'", "''") + "'"


def switch_script(loader: str, bcdedit: str, shutdown: str,
                  pass_root: str | None = None) -> str:
    """PowerShell that makes `loader`'s firmware entry the next boot only, then
    restarts — but only if that worked. Otherwise it falls through with the
    reason in $why.

    `pass_root` is a PowerShell expression for the EFI system partition's root
    (ESP_ROOT): given, the script also leaves the autologin pass there, after
    the one-time boot is set and before the restart. None leaves no pass."""
    leave = _LEAVE_PASS.replace("@ESP@", pass_root) if pass_root else ""
    return (_SWITCH.replace("@PASS@", leave)
                   .replace("@BCDEDIT@", _ps(bcdedit))
                   .replace("@SHUTDOWN@", _ps(shutdown))
                   .replace("@LOADER@", _ps(loader.lower())))


def encoded(script: str) -> str:
    """`script` the way PowerShell's -EncodedCommand takes it."""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


# --- the task ------------------------------------------------------------------

def _system32() -> Path:
    # Asked of Windows rather than read from %SystemRoot%: a task that runs
    # elevated must not take its program paths from an environment variable
    # the user's own (unelevated) processes can change.
    buffer = ctypes.create_unicode_buffer(260)
    ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    return Path(buffer.value)


_TASK_XML = """\
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Makes Linux the next boot only, then restarts. Run by the "Restart into Linux" shortcut and by JARVIS. Installed by install_boot_switch.py.</Description>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowStartOnDemand>true</AllowStartOnDemand>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def task_xml(loader: str, leave_pass: bool = False) -> str:
    """The task: on demand only, elevated, for the user installing it. Its
    whole action is in the definition — it runs no file the user can edit.
    `leave_pass` makes it the voice task (see plat.BOOT_LINUX_VOICE_TASK)."""
    system32 = _system32()
    user = subprocess.run([str(system32 / "whoami.exe")], capture_output=True,
                          text=True, check=True).stdout.strip()
    script = switch_script(loader, str(system32 / "bcdedit.exe"),
                           str(system32 / "shutdown.exe"),
                           ESP_ROOT if leave_pass else None) + _TELL_THE_USER
    return _TASK_XML.format(
        user=escape(user),
        command=escape(str(system32 / "WindowsPowerShell" / "v1.0" / "powershell.exe")),
        arguments="-NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand "
                  + encoded(script))


# --- the shortcut --------------------------------------------------------------

def create_shortcuts(folders) -> list[Path]:
    """Put a "Restart into Linux" shortcut in each folder. It runs the task."""
    import win32com.client

    shell = win32com.client.Dispatch("WScript.Shell")
    system32 = _system32()
    made = []
    for folder in folders:
        link = shell.CreateShortcut(str(Path(folder) / SHORTCUT_NAME))
        link.TargetPath = str(system32 / "schtasks.exe")
        link.Arguments = f'/run /tn "{BOOT_LINUX_TASK}"'
        link.Description = "Restart this laptop into Linux"
        link.IconLocation = f"{system32 / 'shell32.dll'},238"
        link.WindowStyle = 7        # minimised: schtasks is a console program
        link.Save()
        made.append(Path(link.FullName))
    return made


def _shortcut_folders() -> list[Path]:
    """The user's Desktop and Start menu, wherever Windows keeps them."""
    import win32com.client

    folders = win32com.client.Dispatch("WScript.Shell").SpecialFolders
    return [Path(folders("Desktop")), Path(folders("Programs"))]


# --- the Linux side ------------------------------------------------------------

AUTOLOGIN_HELPER = "/usr/local/sbin/jarvis-autologin-once"
AUTOLOGIN_UNIT = "jarvis-autologin-once.service"
AUTOLOGIN_CONF = "/etc/sddm.conf.d/90-jarvis-autologin.conf"
_SOURCES = Path(__file__).resolve().parent / "boot-switch"


def autologin_unit(user: str) -> str:
    """The systemd unit that runs the autologin helper before the login screen."""
    return f"""[Unit]
Description=JARVIS: log in without the password once, after a voice switch from Windows
RequiresMountsFor=/boot/efi
Before=display-manager.service

[Service]
Type=oneshot
ExecStart={AUTOLOGIN_HELPER} {user}

[Install]
WantedBy=graphical.target
"""


def _sudo_install(source: Path, target: str, mode: str = "755") -> int:
    return subprocess.run(["sudo", "install", "--backup", "-o", "root", "-g", "root",
                           "-m", mode, str(source), target]).returncode


def install_linux_helper() -> int:
    """Put the two root helpers in place (asks for sudo).

    boot-windows-next is what JARVIS runs to switch to Windows; the sudoers
    rule that lets it run without a password is already in place and names
    this same path. jarvis-autologin-once runs at boot, before the login
    screen, and logs this user in once if the Windows side left a pass.
    """
    code = _sudo_install(_SOURCES / "boot-windows-next", _BOOT_WINDOWS_HELPER)
    if code != 0:
        print("Nothing was installed.")
        return code
    print(f"Installed {_BOOT_WINDOWS_HELPER}.")

    user = os.environ.get("SUDO_USER") or getpass.getuser()
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", user) or user == "root":
        print(f"Not setting up the one-time login: unexpected user name {user!r}.")
        return 1
    folder = Path(tempfile.mkdtemp(prefix="jarvis-boot-switch-"))
    unit = folder / AUTOLOGIN_UNIT
    try:
        unit.write_text(autologin_unit(user), encoding="utf-8")
        code = (_sudo_install(_SOURCES / "jarvis-autologin-once", AUTOLOGIN_HELPER)
                or _sudo_install(unit, f"/etc/systemd/system/{AUTOLOGIN_UNIT}", "644")
                or subprocess.run(["sudo", "systemctl", "daemon-reload"]).returncode
                or subprocess.run(["sudo", "systemctl", "enable", AUTOLOGIN_UNIT]).returncode)
    finally:
        unit.unlink(missing_ok=True)
        folder.rmdir()
    print(f"One-time login after a voice switch from Windows: on, for {user}."
          if code == 0 else "The one-time login was NOT set up.")
    return code


def uninstall_linux_autologin() -> int:
    """Turn the one-time login off again. boot-windows-next is left in place."""
    subprocess.run(["sudo", "systemctl", "disable", AUTOLOGIN_UNIT])
    code = subprocess.run(["sudo", "rm", "-f", f"/etc/systemd/system/{AUTOLOGIN_UNIT}",
                           AUTOLOGIN_HELPER, AUTOLOGIN_CONF]).returncode
    subprocess.run(["sudo", "systemctl", "daemon-reload"])
    print(f"One-time login removed. {_BOOT_WINDOWS_HELPER} is left in place."
          if code == 0 else "Could not remove the one-time login.")
    return code


# --- install / uninstall -------------------------------------------------------

def _choose_loader(asked_for: str | None) -> str | None:
    """The Linux loader to boot, checked against the firmware's entries."""
    listing = subprocess.run([str(_system32() / "bcdedit.exe"), "/enum", "firmware"],
                             capture_output=True, text=True).stdout
    if asked_for:
        if asked_for.lower() in listing.lower():
            return asked_for
        print(f"No firmware boot entry uses {asked_for}. The entries are:\n{listing}")
        return None
    found = linux_loaders(listing)
    if len(found) == 1:
        description, loader = found[0]
        print(f"Linux boot entry: {description} ({loader})")
        return loader
    if found:
        print("There is more than one Linux boot entry:")
        for description, loader in found:
            print(f"  {description}: {loader}")
    else:
        print(f"Could not tell which firmware boot entry is Linux:\n{listing}")
    print("Say which one to boot:\n"
          r"  python install_boot_switch.py install --loader \EFI\<distro>\<file>.efi")
    return None


def _as_admin(args) -> int:
    """The half that needs administrator rights: the task itself."""
    schtasks = str(_system32() / "schtasks.exe")
    if args.command == "uninstall":
        done = subprocess.run([schtasks, "/delete", "/tn", BOOT_LINUX_TASK, "/f"],
                              capture_output=True, text=True)
        print((done.stdout or done.stderr).strip())
        subprocess.run([schtasks, "/delete", "/tn", BOOT_LINUX_VOICE_TASK, "/f"],
                       capture_output=True, text=True)
        return done.returncode
    loader = _choose_loader(args.loader)
    if loader is None:
        return 1
    definition = Path(tempfile.mkdtemp(prefix="jarvis-boot-switch-")) / "task.xml"
    try:
        # The plain task (the shortcut), then the voice task (JARVIS only).
        for name, leave_pass in ((BOOT_LINUX_TASK, False),
                                 (BOOT_LINUX_VOICE_TASK, True)):
            definition.write_text(task_xml(loader, leave_pass), encoding="utf-16")
            done = subprocess.run([schtasks, "/create", "/tn", name,
                                   "/xml", str(definition), "/f"],
                                  capture_output=True, text=True)
            print((done.stdout or done.stderr).strip())
            if done.returncode != 0:
                break
    finally:
        definition.unlink(missing_ok=True)
        definition.parent.rmdir()
    return done.returncode


def _elevated(argv: list[str]) -> int:
    """Run this script's admin half elevated — one UAC prompt. Its exit code."""
    handle, log = tempfile.mkstemp(prefix="jarvis-boot-switch-", suffix=".log")
    os.close(handle)
    arguments = subprocess.list2cmdline(
        [str(Path(__file__).resolve()), *argv, "--elevated", log])
    script = (f"try {{ $p = Start-Process -FilePath {_ps(sys.executable)}"
              f" -ArgumentList {_ps(arguments)} -Verb RunAs -WindowStyle Hidden"
              " -Wait -PassThru -ErrorAction Stop } catch { exit 1223 }\n"
              "exit $p.ExitCode")
    code = subprocess.run(["powershell", "-NoProfile", "-EncodedCommand",
                           encoded(script)]).returncode
    print(Path(log).read_text(encoding="utf-8"), end="")
    os.unlink(log)
    if code == 1223:        # ERROR_CANCELLED
        print("Administrator approval was declined.")
    return code


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Install or remove the "Restart into Linux" button.')
    parser.add_argument("command", choices=("install", "uninstall"))
    parser.add_argument("--loader", metavar="PATH",
                        help=r"the Linux boot loader, e.g. \EFI\ubuntu\shimx64.efi "
                             "(only needed when it cannot be worked out)")
    parser.add_argument("--elevated", metavar="LOG", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if sys.platform != "win32":
        if args.command == "uninstall":
            return uninstall_linux_autologin()
        return install_linux_helper()

    if args.elevated:       # we are the elevated copy: report through the log
        with open(args.elevated, "w", encoding="utf-8") as log, redirect_stdout(log):
            try:
                return _as_admin(args)
            except Exception:
                traceback.print_exc(file=log)
                return 1

    is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    code = _as_admin(args) if is_admin else _elevated(sys.argv[1:])
    if args.command == "uninstall":
        for folder in _shortcut_folders():
            (folder / SHORTCUT_NAME).unlink(missing_ok=True)
        print("Removed the Restart into Linux shortcuts.")
    elif code == 0:
        for shortcut in create_shortcuts(_shortcut_folders()):
            print(f"Shortcut: {shortcut}")
        print('Installed. Double-click "Restart into Linux", or tell JARVIS '
              '"switch to Linux".')
    else:
        print("Nothing was installed.")
    return code


if __name__ == "__main__":
    sys.exit(main())
