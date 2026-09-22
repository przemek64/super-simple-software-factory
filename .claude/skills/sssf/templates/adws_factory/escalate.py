"""Reaching a person who is not watching.

Unattended orchestration is only unattended while it is working. The moment the
decider is genuinely stuck, someone has to hear about it on a device, not in a
log file nobody is tailing -- ADR-0002 treats this as part of the design rather
than an add-on.

The transport is the signal-agent daemon already running for other tools
(JSON-RPC `send` over TCP at 127.0.0.1:7583). Reusing it means one daemon to
keep alive instead of two.

Escalation must never raise into the tick. A factory that crashes because it
could not report a problem has turned a recoverable stall into an outage, so
every failure here is returned, not thrown.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

__version__ = "0.1.0"

SIGNAL_HOST = "127.0.0.1"
SIGNAL_PORT = 7583
RECIPIENT = "+353838506908"

# Temporary mute, matching grey-factory's signal_notify.py. `send()` becomes a
# no-op while this file exists beside the module, or while SIGNAL_NOTIFY_MUTE is
# set. Added 2026-09-07 at the operator's request.
#
# A muted send reports delivered=True. That is the uncomfortable choice and it
# is deliberate: `tick.py` re-attempts an escalation it believes never landed,
# so delivered=False would retry on every tick forever. The detail string says
# MUTED in full, and every suppressed message is echoed to stderr, so no reader
# of a log can mistake this for somebody having been told.
MUTE_FILE = Path(__file__).resolve().parent / "signal_notify.MUTED"


# Paging is OFF unless a person turns it on for this process. The mute used to
# be the exception -- an untracked marker file beside this module -- so a fresh
# checkout, a run worktree, or a test process that reached the real `send()`
# paged a muted operator anyway: ~20 messages a minute on 2026-09-22, carrying
# fixture ids (run00001, pid 4321, #4) from a suite nobody thought could send.
# An opt-in cannot be lost by cloning, and a test that forgets to patch `send`
# is silent by construction.
ENABLE_VAR = "SIGNAL_NOTIFY_ENABLE"


def muted() -> str | None:
    """Why sending is muted, or None when it is live."""
    if os.environ.get("SIGNAL_NOTIFY_MUTE"):
        return "SIGNAL_NOTIFY_MUTE is set"
    if MUTE_FILE.exists():
        return f"{MUTE_FILE.name} exists (delete it to restore paging)"
    if not os.environ.get(ENABLE_VAR):
        return f"{ENABLE_VAR} is not set (paging is opt-in)"
    return None


@dataclass(frozen=True)
class EscalationResult:
    delivered: bool
    detail: str


def send(
    message: str,
    *,
    host: str = SIGNAL_HOST,
    port: int = SIGNAL_PORT,
    recipient: str = RECIPIENT,
    timeout: float = 10.0,
) -> EscalationResult:
    """Deliver one message. Never raises."""
    reason = muted()
    if reason is not None:
        print(f"escalate: MUTED ({reason}); NOT sent: {message}", file=sys.stderr)
        # ASCII only: this lands in `stuck.detail` and then the tick log, and a
        # cp1252 console cannot encode an em dash -- that is what crashed
        # `highspeed.py --plan` on a smart quote in a blocked_reason.
        return EscalationResult(True, f"MUTED ({reason}) - nobody was paged")

    request = {
        "jsonrpc": "2.0",
        "method": "send",
        "params": {"recipient": [recipient], "message": message},
        "id": 1,
    }
    payload = (json.dumps(request) + "\n").encode("utf-8")
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(payload)
            sock.settimeout(timeout)
            try:
                sock.recv(4096)  # best-effort ack; absence is not failure
            except socket.timeout:
                pass
    except OSError as error:
        return EscalationResult(False, f"signal-agent unreachable at {host}:{port}: {error}")
    return EscalationResult(True, f"sent to {recipient}")


def escalate_item(repo: str, item: int, reason: str, pr_number: int | None = None) -> EscalationResult:
    """Ask for a person on a specific item."""
    where = f"PR #{pr_number}" if pr_number else f"issue #{item}"
    return send(f"adws_factory {repo} #{item} ({where}) needs you: {reason}")


def escalate_factory_health(
    repo: str,
    signal: str,
    reason: str,
    *,
    item: int | None = None,
    position: str | None = None,
    last_tick: str | None = None,
) -> EscalationResult:
    """Report a factory-health signal from the supervisor.

    Same transport and recipient as `escalate_item`, by design: the operator
    keeps one channel to watch, and a loop-dead page arriving somewhere the
    decider's pages never go is a page nobody sees. The formatter only builds
    the text; `send` remains the sole transport and still never raises.
    """
    where = f", active #{item}" if item is not None else ""
    if item is not None and position:
        where += f" at {position}"
    tick = f", last tick {last_tick}" if last_tick else ""
    return send(f"adws_factory {repo} {signal}: {reason}{where}{tick}")
