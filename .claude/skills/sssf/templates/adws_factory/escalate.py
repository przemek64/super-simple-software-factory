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
import socket
from dataclasses import dataclass

__version__ = "0.1.0"

SIGNAL_HOST = "127.0.0.1"
SIGNAL_PORT = 7583
RECIPIENT = "+353838506908"


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
