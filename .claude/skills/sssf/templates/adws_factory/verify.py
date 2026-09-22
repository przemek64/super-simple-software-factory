"""Ruling on one blocking finding by reading the code it is about.

ADR-0002 gives the decider authority to merge when a blocking finding "does not
survive that check". Until this module existed, the check did not exist either:
a blocking finding escalated to a person, because `decider.py` reads review
prose and nothing read the code the prose is about.

Measured on PR #99, parked at 00:58 on 2026-08-21 on three Major findings that
were all checked by hand afterwards. Two were already fixed in the file at the
judged head; the third was accurate about the code but demanded an edit to
`tty-bridge.service`, which Decision 20 leaves untouched. None justified the
park. The external triager on the same pull request recorded refusal rates of
15 of 18 and then 21 of 21. A false blocking finding is the normal case here,
and every one of them costs a human interruption.

Three rules shape everything below.

*Judge the code, not the prose.* The defect this module repairs is that the
decider only ever read review text. So a ruling requires the file at the judged
revision, read from git. A finding whose file cannot be read is not ruled on.

*Fail closed, in every direction.* Cannot find the cited path, cannot read the
blob, the agent errors, times out, or answers something this module cannot
parse -- the finding SURVIVES and goes to a person. An unnecessary escalation
costs one interruption. A merge past a real blocker costs the thing the whole
design exists to protect. "Cannot confirm" must never read as "confirmed
false", which is the same rule `_reviewer_retracted` follows in `decider.py`.

*Never touch the branch.* This module dismisses findings. It does not rewind,
reset, revert, or edit anything -- ADR-0002 records an automated gate that
enacted its own rewind and destroyed a sound pull request on five false
findings. The only side effect here is a comment recording what was ruled.

Deliberately NOT reading the triager's ledger (option O1, considered and
deferred). `adws/adw_runtime/coderabbit_processed.json` stores a coarse outcome
per review -- "all rejected", "fixed" -- with no per-finding rulings and no
reasons, so there is nothing there to feed an agent. Dropping every finding
from a review the triager rejected wholesale would also let a lower gate's
judgement suppress the decider's escalation, which ADR-0002 forbids in as many
words: "Automated gates below the decider may never discard work on a severity
judgement -- they hold, and the decider rules." The triager's refusal is
evidence, not a verdict. This module verifies from scratch.
"""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

__version__ = "0.1.0"

# The judgement is small and bounded -- one finding, one or two files -- but it
# is the call that decides whether an unattended merge happens, so it runs on a
# reviewer-grade seat rather than the cheapest one available. Overridable from
# factory.yaml; whatever it names is credit-gated like any other metered work.
DEFAULT_MODEL = "openai-codex/gpt-5.6-terra"

# A file's worth of context, not a repository's. Beyond this the agent is being
# asked to skim rather than to read, and a skimmed ruling is worse than an
# escalation. A finding whose file exceeds it is escalated unread.
#
# 120_000 was below the size of the file most findings are about. labeltool's
# labeltool55.py was 225_349 chars before this week's work and 231_037 at PR
# #259's head -- 1.9x the cap, and growing every issue. So every spec-axis
# finding against it escalated unread and parked the item, which is what held
# issue #257 with two HIGH findings nobody had checked. 300_000 clears it with
# room for roughly a year of that growth.
MAX_BLOB_CHARS = 300_000

# One check, bounded. `docs/verifying-claims.md`: "If a check needs a full run,
# an environment, or more than about ten minutes, it is not a check -- escalate
# instead."
AGENT_TIMEOUT_SECONDS = 600


class AgentError(RuntimeError):
    """The verification agent could not produce a ruling. Infrastructure, never work."""


@dataclass(frozen=True)
class Ruling:
    """What this module concluded about one finding, and what it read to get there.

    `survives` is the only field the decider acts on. The rest exists so a
    person auditing an unattended merge can see the reasoning, which is what
    makes the agent correctable when it is wrong.
    """

    survives: bool
    reason: str
    # Paths actually read at the judged revision. Empty on every fail-closed
    # path, which is how the record distinguishes "checked and stands" from
    # "could not check".
    read: list[str] = field(default_factory=list)
    # True when `survives` is the fail-closed default rather than a judgement.
    # Recorded separately because the two are the same action and very
    # different facts, and a record that cannot tell them apart is the
    # "two states, one signal" defect docs/verifying-claims.md warns about.
    unverified: bool = False

    @property
    def outcome(self) -> str:
        if self.unverified:
            return "unverified"
        return "survives" if self.survives else "dismissed"


class Agent(Protocol):
    """One bounded model call: a prompt in, its text out.

    Deliberately this narrow. The agent does not get tools, a worktree, or the
    ability to write anything -- this module hands it the code to judge and
    takes back a verdict. That keeps the whole path testable with a plain
    function and keeps a judgement seat from being able to touch the branch.
    """

    def __call__(self, prompt: str, system_prompt: str, model: str) -> str: ...


# --------------------------------------------------------------------------
# What the finding is about
# --------------------------------------------------------------------------

# The in-house two-axis review tags every finding with the axis it came from.
# A `spec` finding is not a claim about the code in isolation -- it claims the
# code disagrees with the ISSUE, so it cannot be ruled on from the file alone
# and needs the issue passed alongside it.
_SPEC_AXIS = re.compile(r"\*\*axis\*\*:?\s*:?\s*spec\b", re.IGNORECASE)


def is_spec_axis(excerpt: str) -> bool:
    """Does this finding compare the code against the issue rather than to itself?"""
    return bool(_SPEC_AXIS.search(excerpt or ""))


# `collect_findings` builds an inline comment's source as `author@path:line`
# (ReviewComment.location), which is the reviewer's own anchor and far better
# evidence than anything recoverable from prose. Review-BODY findings carry
# `author#review_id` instead and have no anchor, so those fall back to the
# excerpt.
_SOURCE_LOCATION = re.compile(r"@(?P<path>[^\s@]+?)(?::(?P<line>\d+))?$")

# A path inside the excerpt: at least one directory separator or a known source
# extension, so ordinary prose does not read as a filename. Backticks are
# stripped by the caller before this runs.
_PATH_IN_TEXT = re.compile(
    r"(?<![\w/.-])"
    r"((?:[\w.-]+/)*[\w.-]+\.(?:py|sh|service|yaml|yml|json|toml|md|ts|tsx|js|cfg|ini|txt))"
    r"(?![\w/])"
)


def cited_paths(source: str, excerpt: str, limit: int = 3) -> list[str]:
    """Which files this finding is about, best evidence first.

    The reviewer's own anchor wins when there is one. Everything after it comes
    out of the prose, which is guesswork -- so the caller must treat a path
    that does not resolve at the judged revision as a reason to escalate, not
    as a reason to rule.
    """
    paths: list[str] = []

    anchored = _SOURCE_LOCATION.search(source or "")
    if anchored:
        paths.append(anchored.group("path"))

    for match in _PATH_IN_TEXT.finditer((excerpt or "").replace("`", " ")):
        candidate = match.group(1)
        if candidate not in paths:
            paths.append(candidate)

    return paths[:limit]


def cited_line(source: str) -> int | None:
    """The line the reviewer anchored on, when it anchored on one."""
    anchored = _SOURCE_LOCATION.search(source or "")
    if not anchored or not anchored.group("line"):
        return None
    return int(anchored.group("line"))


def read_at_revision(root: Path, revision: str, path: str) -> str | None:
    """The file as it stands at the judged commit, or None.

    `git show rev:path`, not the working tree. The working tree is whatever the
    factory happens to have checked out, which on a machine running other work
    is not the branch under judgement -- docs/verifying-claims.md section F,
    "a review run against a local main that was eight commits stale, so every
    finding it produced was about already-merged code".

    None on every failure: a path that does not exist at that revision, a blob
    too large to read properly, a git that errors. The caller escalates.
    """
    if not revision or not path:
        return None

    environment = dict(os.environ)
    # Under Git Bash on Windows, MSYS rewrites the `rev:path` argument into a
    # Windows path and git then reports the object as missing.
    environment["MSYS_NO_PATHCONV"] = "1"

    try:
        completed = subprocess.run(
            ["git", "show", f"{revision}:{path}"],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if completed.returncode != 0:
        return None

    blob = completed.stdout
    if not blob.strip() or len(blob) > MAX_BLOB_CHARS:
        # An empty read is indistinguishable from a successful read of nothing,
        # and an oversized one would be skimmed rather than read. Both escalate.
        return None
    return blob


# --------------------------------------------------------------------------
# Asking
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You rule on whether one code-review finding is true of the code in front of you.

You are the last check before an unattended merge. A person is woken only for
findings you say survive, so a wrong dismissal merges a real defect and a wrong
survival costs one interruption. The costs are not symmetric. When you cannot
tell, the finding survives.

Method, from docs/verifying-claims.md:

1. Name the load-bearing fact -- the thing that, if false, collapses the claim.
2. Check that fact in the file you were given. Not from memory, not from the
   finding's own prose, which is the thing being tested.
3. Only then rule.

A finding does NOT survive when:
- the code it objects to is not there, or already does what it asks;
- it cites a path, symbol, or line that does not exist in what you were given;
- it is reviewing a frozen snapshot or archived copy as if it were live code;
- it asks for a change to a file outside what it was given, and the file it was
  given is already correct.

When an issue is given below the code, the finding claims the code disagrees
with that issue. Rule on whether the code matches the ISSUE AS WRITTEN. The
issue is the authority and the finding is not: a finding that misquotes the
issue, or that objects to something the issue permits, does not survive. Read
the issue for what it actually says, including any correction it carries.

A finding DOES survive when the defect it names is present in the code, or when
you cannot establish either way from what you were given. Detail and confidence
in the finding's wording are not evidence. A long, well-argued finding built on
one false fact does not survive.

Answer in exactly this shape, two lines, nothing else:

VERDICT: SURVIVES
REASON: <one line, naming the load-bearing fact and what you found>

or

VERDICT: DOES_NOT_SURVIVE
REASON: <one line, naming the load-bearing fact and what you found>
"""

_VERDICT_LINE = re.compile(r"^\s*VERDICT:\s*(SURVIVES|DOES_NOT_SURVIVE)\s*$",
                           re.IGNORECASE | re.MULTILINE)
_REASON_LINE = re.compile(r"^\s*REASON:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def build_prompt(finding_text: str, revision: str, blobs: dict[str, str],
                 line: int | None = None, spec: str | None = None) -> str:
    """The finding, then the code, with the judged revision stated.

    `spec` is the issue body, passed only for a spec-axis finding, where the
    issue is the thing the code is being compared against.
    """
    anchor = f"\nThe reviewer anchored this on line {line}.\n" if line else ""
    files = "\n\n".join(
        f"--- {path} @ {revision[:8]} ---\n{content}" for path, content in blobs.items()
    )
    specification = (
        "\n\n--- the issue this pull request was built from, which is the "
        "specification the finding appeals to ---\n" + spec
        if spec else ""
    )
    return (
        "A code reviewer raised this finding and marked it blocking:\n\n"
        f"{finding_text}\n"
        f"{anchor}"
        f"\nHere is the code at the commit being judged, {revision[:8]}. "
        "This is the whole of what you have; do not assume anything about files "
        "you were not given.\n\n"
        f"{files}"
        f"{specification}\n\n"
        "Rule on the finding."
    )


def parse_ruling(answer: str) -> tuple[bool, str]:
    """Read the agent's verdict, or raise.

    Strict on purpose. An answer this cannot parse is an answer this does not
    understand, and guessing at one is how a dismissal gets manufactured out of
    a model that was hedging. `AgentError` here reaches the caller's fail-closed
    path exactly like a timeout does.
    """
    verdict = _VERDICT_LINE.search(answer or "")
    if verdict is None:
        raise AgentError(f"no VERDICT line in the answer: {(answer or '')[:200]!r}")

    reason_match = _REASON_LINE.search(answer)
    reason = reason_match.group(1)[:400] if reason_match else "(no reason given)"
    return verdict.group(1).upper() == "SURVIVES", reason


def pi_agent(prompt: str, system_prompt: str, model: str) -> str:
    """The default agent: one non-interactive `pi` turn, no tools.

    Imported lazily. `adws_factory` does not otherwise depend on `adws`, and a
    module-level import would make the whole factory unloadable whenever the
    workflow package is mid-edit -- which, on a machine that edits it, is often.
    """
    try:
        from adws.adw_modules import agent_pi
        from adws.adw_modules.data_types import PiRequest
    except ImportError as error:  # pragma: no cover - environment-dependent
        raise AgentError(f"cannot load the pi agent: {error}") from error

    session = uuid.uuid4().hex[:8]
    workspace = Path("adws/adw_runtime/verify") / session
    workspace.mkdir(parents=True, exist_ok=True)

    request = PiRequest(
        prompt=prompt,
        system_prompt=system_prompt,
        model=model,
        thinking="medium",
        session_id=session,
        session_dir=str(workspace),
        raw_output_path=str(workspace / "raw.jsonl"),
        # No tools. The agent judges what it was handed and cannot reach the
        # branch, the network, or anything else.
        tools=[],
    )

    try:
        result = agent_pi.run(request)
    except Exception as error:  # noqa: BLE001 - any failure is one fail-closed path
        raise AgentError(f"the verification agent failed: {error}") from error

    if result.returncode != 0:
        raise AgentError(f"the verification agent exited {result.returncode}")
    return result.text


# --------------------------------------------------------------------------
# The ruling
# --------------------------------------------------------------------------

def verify_finding(
    finding_source: str,
    finding_excerpt: str,
    revision: str,
    root: Path,
    agent: Agent | None = None,
    model: str = DEFAULT_MODEL,
    reader: Callable[[Path, str, str], str | None] = read_at_revision,
    spec: str | None = None,
) -> Ruling:
    """Rule on one finding against the code at `revision`.

    `spec` is the issue body. Required for a spec-axis finding and ignored
    otherwise: such a finding claims the code disagrees with the issue, and
    ruling on it without the issue is ruling on half the comparison.

    Every exit that is not a judgement returns `survives=True, unverified=True`.
    """
    if is_spec_axis(finding_excerpt) and not (spec or "").strip():
        # Fail closed rather than guess. Without the issue the agent can only
        # confirm what the code says, which is never the question a spec
        # finding asks.
        return Ruling(
            True,
            "a spec-axis finding cannot be ruled on without the issue it cites",
            unverified=True,
        )

    paths = cited_paths(finding_source, finding_excerpt)
    if not paths:
        return Ruling(True, "the finding names no file that could be checked",
                      unverified=True)

    blobs: dict[str, str] = {}
    missing: list[str] = []
    for path in paths:
        blob = reader(root, revision, path)
        if blob is None:
            missing.append(path)
            continue
        blobs[path] = blob

    if not blobs:
        # Note what this is NOT: "the path does not exist, so the finding is
        # false". A reviewer citing a path that is not there is usually wrong,
        # and ruling it wrong from here would be ruling on the prose again --
        # the exact defect this module exists to remove. The agent rules only
        # on code it was given; with no code, nobody rules.
        return Ruling(
            True,
            f"could not read {', '.join(missing)} at {revision[:8]}",
            unverified=True,
        )

    prompt = build_prompt(finding_excerpt, revision, blobs,
                          line=cited_line(finding_source),
                          spec=spec if is_spec_axis(finding_excerpt) else None)
    ask = agent or pi_agent

    try:
        answer = ask(prompt, SYSTEM_PROMPT, model)
        survives, reason = parse_ruling(answer)
    except AgentError as error:
        return Ruling(True, f"could not be ruled on: {error}", unverified=True)
    except Exception as error:  # noqa: BLE001 - an unruled finding must escalate
        return Ruling(True, f"could not be ruled on: {error}", unverified=True)

    if missing:
        reason += f" (unread: {', '.join(missing)})"
    return Ruling(survives, reason, read=sorted(blobs))
