"""Best-effort Windows integration for runs started from WSL.

Both helpers silently do nothing when ``powershell.exe`` / ``explorer.exe`` are
unavailable (any non-WSL machine), so callers never need to special-case them.
"""
from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Iterator
from pathlib import Path

# ES_CONTINUOUS | ES_SYSTEM_REQUIRED (0x80000001), written in decimal because
# PowerShell parses the hex literal as a negative Int32. The state lasts as long
# as this PowerShell process: it blocks until its stdin closes, which also
# happens if WSL itself dies, so it can never keep the PC awake forever.
KEEP_AWAKE_SCRIPT = (
    "Add-Type -Namespace Sped -Name Power -MemberDefinition "
    "'[DllImport(\"kernel32.dll\")] public static extern uint SetThreadExecutionState(uint flags);'; "
    "[void][Sped.Power]::SetThreadExecutionState([uint32]2147483649); "
    "[void][Console]::In.ReadToEnd()"
)


@contextlib.contextmanager
def keep_awake() -> Iterator[bool]:
    """Prevent Windows from sleeping while the block runs; yield whether it worked."""
    try:
        process = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", KEEP_AWAKE_SCRIPT],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        yield False
        return
    try:
        yield True
    finally:
        with contextlib.suppress(OSError):
            process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def open_folder(path: Path) -> bool:
    """Show a folder in Windows Explorer; return whether it could be launched."""
    try:
        windows_path = subprocess.run(
            ["wslpath", "-w", str(path)], capture_output=True, text=True, check=True,
        ).stdout.strip()
        # explorer.exe reports exit status 1 even on success, so ignore it.
        subprocess.run(["explorer.exe", windows_path], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True
