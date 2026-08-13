"""Is this pid still the process the trace recorded? — on Windows too.

The factory records a pid per process so a hung run can be found and stopped.
Reading that pid back safely needs two facts: whether it is alive, and whether
it is still running what we recorded (pids are recycled, and killing a recycled
pid kills a stranger's process).

Both were previously read from ``/proc/{pid}/cmdline``, which does not exist on
Windows, so every caller silently learned nothing here. ``os.kill(pid, 0)`` is
not an alternative: on Windows CPython maps ``os.kill`` to ``TerminateProcess``
for any signal that is not a console event, so the liveness probe would kill
the very process it asks about.

This module answers both questions on either platform, in ONE query per call.

It fails SAFE: when the probe itself fails — no PowerShell, a timeout, garbage
output — every pid asked about is reported alive with an unknown command line.
A caller then leaves it alone. Wrongly believing a dead run is alive shows a
stale row; wrongly believing a live run is dead finalizes a run that is still
working, or kills a process that is not ours.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

IS_WINDOWS = os.name == "nt"

# Win32_Process filters are built by hand, so pids are forced to int before they
# reach the query string and can never carry anything else into it.
_CHUNK = 50
_PROBE_TIMEOUT = 30.0


def _windows_live(pids: list[int]) -> dict[int, str]:
    """One CIM query per chunk: {pid: command line} for the pids still running."""
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if not shell:
        return {pid: "" for pid in pids}    # cannot probe: assume alive
    live: dict[int, str] = {}
    for start in range(0, len(pids), _CHUNK):
        chunk = pids[start:start + _CHUNK]
        query = " or ".join(f"ProcessId={pid}" for pid in chunk)
        try:
            done = subprocess.run(
                [shell, "-NoProfile", "-NonInteractive", "-Command",
                 f"Get-CimInstance Win32_Process -Filter \"{query}\""
                 " | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=_PROBE_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError):
            return {pid: "" for pid in pids}
        if done.returncode != 0:
            return {pid: "" for pid in pids}
        payload = (done.stdout or "").strip()
        if not payload:
            continue                        # none of this chunk is running
        try:
            parsed = json.loads(payload)
        except ValueError:
            return {pid: "" for pid in pids}
        rows = parsed if isinstance(parsed, list) else [parsed]
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                pid = int(row.get("ProcessId"))
            except (TypeError, ValueError):
                continue
            live[pid] = str(row.get("CommandLine") or "")
    return live


def _posix_live(pids: list[int]) -> dict[int, str]:
    live: dict[int, str] = {}
    for pid in pids:
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            # No /proc (macOS) or the process is gone; `ps` distinguishes them.
            try:
                done = subprocess.run(
                    ["ps", "-o", "command=", "-p", str(pid)],
                    capture_output=True, text=True, timeout=_PROBE_TIMEOUT)
            except (OSError, subprocess.SubprocessError):
                live[pid] = ""              # cannot probe: assume alive
                continue
            if done.returncode == 0 and done.stdout.strip():
                live[pid] = done.stdout.strip()
            continue
        live[pid] = raw.replace(b"\0", b" ").decode(errors="replace").strip()
    return live


def live_commands(pids) -> dict[int, str]:
    """Map every pid that is running right now to its current command line.

    A pid absent from the result is not running. A pid present with an empty
    string is running (or could not be probed) with an unknown identity, which
    callers must treat as "do not touch".
    """
    wanted = sorted({int(pid) for pid in pids if pid is not None})
    if not wanted:
        return {}
    return _windows_live(wanted) if IS_WINDOWS else _posix_live(wanted)


def is_same_process(recorded_command: str | None, actual_command: str) -> bool:
    """True when a live pid still looks like the process the trace recorded.

    The probe token is the first word of what was recorded — the executable.
    An unknown actual command line (empty) counts as a match, because the
    conservative answer to "is this still ours?" is yes: it stops a caller from
    killing or finalizing something it cannot identify.
    """
    if not actual_command:
        return True
    probe = (recorded_command or "").split()
    if not probe:
        return True
    return Path(probe[0]).name.lower() in actual_command.lower()
