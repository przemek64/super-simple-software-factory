"""Deterministic lint, typecheck, build, and test blocks.

A known command is not a judgement call. Anything whose invocation you can write
down belongs here as code — it runs in milliseconds, costs nothing, and returns
the same answer every time. Agents are for the parts that need reading and
deciding.

╔══════════════════════════════════════════════════════════════════════════════╗
║  REPLACE THE PLACEHOLDER COMMANDS BELOW.                                     ║
║                                                                              ║
║  Every block ships as an `echo` that exits 0 and announces it is fake. They   ║
║  are placeholders on purpose: a stamped repo has no way to guess your test    ║
║  runner, and a wrong-but-plausible command that silently passes is worse      ║
║  than one that says so out loud.                                             ║
║                                                                              ║
║  For each block you want: swap `_placeholder(...)` for the real argv, e.g.    ║
║      argv=["bun", "test", "apps/web/server.test.ts"]                         ║
║      argv=["uv", "run", "pytest", "-q"]                                      ║
║      argv=["npm", "run", "lint"]                                             ║
║  Delete the blocks you don't need, and drop them from run_quality()'s list.   ║
║                                                                              ║
║  Two rules when you write the real command:                                  ║
║    1. argv LIST, never a shell string — no quoting bugs, no shell injection.  ║
║    2. Call binaries by BARE NAME. These blocks inherit the operator's         ║
║       environment (see utils.operator_env), so `bun`, `uv`, `pytest` resolve  ║
║       exactly as they do in their terminal. Never hard-code an absolute path  ║
║       like /Users/you/.bun/bin/bun — that bakes your machine into the trace.  ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from .data_types import (EventRecord, QualityCheckResult, QualityCheckSpec, QualityResult,
                         VerifyOutput)
from .utils import now_iso, operator_env

# How much of a failing command's output rides back inside the envelope. Enough
# for a builder to act on without opening the artifact; bounded so a runaway
# stack trace can't swamp the next agent's context.
TAIL_CHARS = 4_000


def _placeholder(name: str) -> list[str]:
    """A command that does nothing and admits it. Replace every call to this."""
    return ["echo", f"PLACEHOLDER {name}: edit adws/adw_modules/quality.py and "
                    f"replace this echo with the real {name} command"]


def _check_dir(run, name: str) -> Path:
    seq = run.phases[-1].seq if run.phases else 0
    path = run.context_handoff_dir / "quality" / f"{seq:02d}_{name}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _resolve_argv(argv: list[str], env: dict[str, str]) -> list[str]:
    """Pin argv[0] to the interpreter `env`'s PATH names, not the parent's.

    Windows resolves a bare program name through CreateProcess, which searches
    the CURRENT process's PATH and ignores the `env` handed to subprocess. So
    `operator_env()` could set PATH perfectly and the child would still be
    whatever `python` the ADW itself was launched with -- an ephemeral uv
    environment holding the ADW's dependencies and no pytest. The suite then
    failed at import and the agent was handed the blame for it.

    Resolving here keeps every quality block honest, and leaves a genuinely
    missing binary to fail as before: unresolved names pass through untouched
    and still surface as exit 127 with the real message.
    """
    if not argv:
        return argv
    resolved = shutil.which(argv[0], path=env.get("PATH"))
    return [resolved, *argv[1:]] if resolved else argv


def _as_text(captured: str | bytes | None) -> str:
    """Normalize a captured stream that may be absent, or bytes on a timeout."""
    if captured is None:
        return ""
    if isinstance(captured, bytes):
        return captured.decode("utf-8", errors="replace")
    return captured


def _run(spec: QualityCheckSpec, run) -> QualityCheckResult:
    phase = run.phases[-1]
    output_dir = _check_dir(run, spec.name)
    output_artifact = output_dir / "command.log"
    command = shlex.join(spec.argv)
    env = operator_env()             # the engineer's own shell environment
    argv = _resolve_argv(spec.argv, env)

    run.console.note(f"quality {spec.name}: {command}")
    started_at = now_iso()
    clock = time.monotonic()
    stdout = ""
    stderr = ""
    try:
        completed = subprocess.run(
            argv,
            cwd=spec.cwd or run.work_root,
            env=env,
            capture_output=True,
            text=True,
            # A quality command reports on whatever the repo contains, and this
            # repo contains raw ESC/POS bytes. Decoding its output as the
            # Windows default killed the reader thread on byte 0x90, which
            # surfaces as stdout=None and a TypeError three frames later --
            # never as the test failure the operator was trying to read.
            encoding="utf-8", errors="replace",
            timeout=spec.timeout_seconds,
        )
        returncode = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except subprocess.TimeoutExpired as error:
        returncode = 124
        stdout = _as_text(error.stdout)
        stderr = _as_text(error.stderr) + f"\nTimed out after {spec.timeout_seconds}s."
    except OSError as error:
        # A missing binary lands here as exit 127 with the real message — no
        # pre-flight probe needed, and none wanted.
        returncode = 127
        stderr = str(error)

    duration = time.monotonic() - clock
    output_artifact.write_text(
        f"$ {command}\nexit: {returncode}\nduration_seconds: {duration:.3f}\n"
        f"\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}\n",
        encoding="utf-8", errors="replace",
    )
    passed = returncode == 0
    run.tracer.event(EventRecord(
        adw_id=run.adw_id,
        phase_id=phase.phase_id,
        type="tool_call",
        name=f"quality:{spec.name}",
        payload={
            "area": spec.area,
            "operation": spec.operation,
            "command": command,
            "returncode": returncode,
            "passed": passed,
            "output_artifact": str(output_artifact),
        },
        started_at=started_at,
        ended_at=now_iso(),
    ))
    run.console.note(
        f"quality {spec.name}: {'passed' if passed else 'failed'} "
        f"(exit {returncode}, {duration:.1f}s)"
    )
    return QualityCheckResult(
        name=spec.name,
        area=spec.area,
        operation=spec.operation,
        command=command,
        returncode=returncode,
        passed=passed,
        duration_seconds=duration,
        output_artifact=str(output_artifact),
        output_tail=(stdout + stderr)[-TAIL_CHARS:],
    )


# ── Blocks ────────────────────────────────────────────────────────────────────
# Replace every argv below. See the banner at the top of this file.

def test(run) -> QualityCheckResult:
    """Run the project's test suite. The highest-value block to wire up first."""
    return _run(QualityCheckSpec(
        name="test",
        area="backend",
        operation="build",
        # --ignore=adws: SSSF's own adw_*_test.py scripts match pytest's default
        # `*_test.py` collection pattern and would otherwise be imported as tests.
        #
        # The operator's own interpreter, NOT a `uv run --with ...` env. The
        # stamped default built an ephemeral environment holding only the ADW
        # scripts' own dependencies, which is right for a repo whose tests import
        # nothing else -- this one imports paramiko, serial and cryptography, so
        # collection died with six ModuleNotFoundErrors in 3 seconds, zero tests
        # run. The agent was then handed "no module named serial" as if it were
        # its own regression. `operator_env()` strips the uv venv from PATH, so
        # `python` here is the same interpreter a developer gets in their shell,
        # with the project's real dependencies already installed.
        argv=["python", "-m", "pytest", "-q", "--ignore=adws"],
        timeout_seconds=900,
    ), run)


def lint(run) -> QualityCheckResult:
    return _run(QualityCheckSpec(
        name="lint",
        area="backend",
        operation="lint",
        argv=_placeholder("lint"),        # e.g. ["bun", "x", "oxlint@1.36.0", "src"]
    ), run)


def typecheck(run) -> QualityCheckResult:
    return _run(QualityCheckSpec(
        name="typecheck",
        area="backend",
        operation="typecheck",
        argv=_placeholder("typecheck"),   # e.g. ["bun", "x", "tsc", "--noEmit"]
    ), run)


def build(run) -> QualityCheckResult:
    output_dir = _check_dir(run, "build") / "bundle"
    return _run(QualityCheckSpec(
        name="build",
        area="backend",
        operation="build",
        argv=_placeholder("build"),       # e.g. ["bun", "build", "src/index.ts", "--outdir", str(output_dir)]
    ), run)


def run_tests(run) -> QualityResult:
    """The test suite alone, as a QualityResult — the deterministic test phase.

    This is what replaces a `tester` agent once the command is written down. An
    agent rediscovering the runner on every run costs a fortune to learn what a
    subprocess already knows; the repair loop is unchanged, because a failure
    still reaches the builder through `as_envelope` below.
    """
    check = test(run)
    failures = ([] if check.passed else
                [f"{check.name}: `{check.command}` exited {check.returncode}\n"
                 f"{check.output_tail}".rstrip()])
    return QualityResult(passed=check.passed, checks=[check], failures=failures,
                         artifacts=[check.output_artifact])


# ── Suite baseline (which tests were red before this run touched anything) ───
#
# Acceptance used to demand a green suite outright. On a repository whose suite
# is already red for reasons that predate the run, that is unmeetable: the
# builder is handed failures it did not cause, spends every fix round on code
# it never touched, and the run is rejected however good the work was. Observed
# on labeltool #265 -- 11/11 phases passed, reviewer approved 10 of 10
# requirements, rejected anyway on 35 failures that commit 9119130 had already
# baked into the branch by recording device .def files with zero fields enabled.
#
# So the suite is measured on the base revision FIRST, and the run is judged on
# what it CHANGED: a new failure is a regression and blocks, a pre-existing one
# is a fact about the repository and does not. The measurement is cached per
# base commit, so the extra suite run is paid once per base, not once per run.

BASELINE_FILE = "test_baseline.json"

# pytest's short summary: "FAILED path::Class::test - AssertionError ...".
# Anchored to the line start so a node id quoted inside a traceback is ignored.
_FAILED_ID = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)


def failing_ids(text: str) -> set[str]:
    """The node ids pytest listed as FAILED or ERROR, from its own summary."""
    return {match.group(1) for match in _FAILED_ID.finditer(text or "")}


def ids_from_check(check: QualityCheckResult) -> set[str]:
    """Failing node ids for one check, read from the full log when it survives.

    `output_tail` is a tail: on a long run the summary can be complete there,
    but it is not guaranteed. The artifact holds everything, so it is preferred
    and the tail is the fallback -- never the other way round.
    """
    try:
        text = Path(check.output_artifact).read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = check.output_tail
    return failing_ids(text)


def _baseline_path(run) -> Path:
    return Path(run.data_dir) / BASELINE_FILE


def read_baseline(run, base_sha: str) -> set[str] | None:
    """The stored baseline for this base commit, or None when there is none.

    A malformed or unreadable store is None, not an exception: a missing
    baseline costs one suite run, while a raise here would fail the whole ADW
    on a cache file.
    """
    try:
        store = json.loads(_baseline_path(run).read_text(encoding="utf-8"))
        entry = store[base_sha]
        return set(entry["failing"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None


# pytest's own exit codes. 0 is a clean green run; 1 is "tests failed" and is
# the only non-zero code whose summary can be trusted to list them. Everything
# else -- interrupted (2), internal error (3), usage error (4), nothing
# collected (5), our own timeout (124), missing binary (127) -- means the suite
# did not finish, and a suite that did not finish reports no failures at all.
PYTEST_OK = 0
PYTEST_TESTS_FAILED = 1


def measurable(check: QualityCheckResult) -> bool:
    """Whether this run's result may be stored as a baseline.

    Learned the hard way: the first live baseline hit the 900s timeout, exited
    124, printed no FAILED lines, and was cached as "0 already failing" against
    the base commit -- which would have made every later run on that commit
    strict again, silently and permanently. An unfinished suite is not the
    measurement "nothing is broken"; it is no measurement at all.
    """
    if check.returncode == PYTEST_OK:
        return check.passed
    if check.returncode == PYTEST_TESTS_FAILED:
        # A "tests failed" exit that names nothing did not get far enough to
        # produce a summary either.
        return bool(ids_from_check(check))
    return False


def write_baseline(run, base_sha: str, ids: set[str], *, command: str) -> None:
    path = _baseline_path(run)
    try:
        store = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(store, dict):
            store = {}
    except (OSError, json.JSONDecodeError):
        store = {}
    store[base_sha] = {
        "failing": sorted(ids),
        "command": command,
        "measured_at": now_iso(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=2), encoding="utf-8")


def regressions(check: QualityCheckResult, baseline: set[str] | None) -> set[str]:
    """Failures this run is answerable for: what is failing now and was not before.

    A None baseline means the base was never measured, so nothing can be
    attributed and every failure counts -- the strict behaviour that predates
    baselining, kept as the fail-safe.
    """
    now = ids_from_check(check)
    if baseline is None:
        return now
    return now - baseline


def accepts(check: QualityCheckResult, baseline: set[str] | None) -> bool:
    """True when the suite introduced nothing new, green or not.

    A red suite with an EMPTY failure summary is never accepted: pytest prints
    no FAILED lines for a collection error or a crash, and an empty difference
    there would read as 'changed nothing' when in truth nothing ran.
    """
    if check.passed:
        return True
    if not ids_from_check(check):
        return False
    return not regressions(check, baseline)


def as_regression_envelope(result: QualityResult, new_ids: set[str]) -> VerifyOutput:
    """Hand the builder only what it broke, naming the rest as out of scope.

    Handing over the whole red suite is what sent the builder chasing the
    printer tests: it cannot tell which failures are its own, so it tries all
    of them and burns the fix rounds.
    """
    listed = "\n".join(f"  {node}" for node in sorted(new_ids))
    return VerifyOutput(
        status="fail",
        summary=f"tests: {len(new_ids)} test(s) that passed on the base now fail",
        artifacts=result.artifacts,
        notes_for_next_agent=(
            "Fix ONLY these tests. They passed before this run and fail now, so "
            "this run caused them:\n" + listed + "\n\n"
            "Other failures in the log were already failing on the base revision. "
            "They are not yours, they are not in scope, and changing code to chase "
            "them will be treated as out-of-scope work."
        ),
        passed=False,
        failures=result.failures,
    )


def as_envelope(result: QualityResult, what: str) -> VerifyOutput:
    """Wrap a deterministic result so an agent can be handed it directly.

    Agents hand each other typed envelopes; code blocks return QualityResult.
    This is the adapter, so a failing lint or test run flows back into the
    builder through exactly the same door an agent's report would — the ADW
    script is the only thing that knows the difference.
    """
    return VerifyOutput(
        status="success" if result.passed else "fail",
        summary=(f"{what}: all {len(result.checks)} check(s) passed" if result.passed
                 else f"{what}: {len(result.failures)} of {len(result.checks)} check(s) failed"),
        artifacts=result.artifacts,
        notes_for_next_agent=("" if result.passed else
                              "Fix every failure below. The output is verbatim from the "
                              "command — trust it over any summary."),
        passed=result.passed,
        failures=result.failures,
    )


def run_quality(run) -> QualityResult:
    """Run every block and collect ALL failures — one pass tells you everything.

    Ordering contract for the caller: a failing block does NOT fail the phase.
    The runner did its job; the CODE is what failed. Hand this result to the
    builder and let the bounded repair loop decide the run's fate.
    """
    blocks: list[Callable] = [
        test,
        lint,
        typecheck,
        build,
    ]
    checks = [block(run) for block in blocks]
    # A failure is the command, its exit code, and what it actually printed —
    # everything a builder needs to repair without opening a log or being told
    # what the error "means" by a parser that guessed.
    failures = [
        f"{check.name}: `{check.command}` exited {check.returncode}\n{check.output_tail}".rstrip()
        for check in checks if not check.passed
    ]
    return QualityResult(
        passed=not failures,
        checks=checks,
        failures=failures,
        artifacts=[check.output_artifact for check in checks],
    )
