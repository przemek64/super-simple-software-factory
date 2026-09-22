#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW Simple SDLC — plan, build, test, review, document, committing as it goes.

Usage:
    uv run adws/adw_simple_sdlc.py "<prompt or path/to/prompt.md>" --base <branch> [--config adws/adw_sssf_config/sssf.config.yaml] [--adw-id a1b2c3d4]
    uv run adws/adw_simple_sdlc.py --issue <number> [--config adws/adw_sssf_config/sssf.config.yaml] [--adw-id a1b2c3d4]

Phases: engineer(request) -> planner -> git(commit_plan)
        -> builder -> code(test) [-> builder(fix) -> code(test) ... bounded]
        -> reviewer [-> builder(revise) -> reviewer ... bounded]
        -> code(retest, only if a revision changed code)
        -> git(commit_build) -> code(changes) -> documenter -> git(commit_docs)
        -> code(deliver: push branch and open/reuse PR)

Three commits, three work products, three authors. The plan, the code, and the
write-up each land in their own commit, and each commit message is the words of
the agent that produced it — `commit_message` on PlanOutput describes the spec,
on BuildOutput the code, on DocumentOutput the write-up. No agent's sentence is
ever reused for another agent's diff.

Testing is CODE, not an agent. `bun test` is a command, not a judgement call:
an agent rediscovering it every run costs a million tokens to learn what a
subprocess already knows. Failures travel back to the builder as an envelope,
so the repair loop is unchanged — only the runner became free and repeatable.

Two different questions still get asked, in order. The suite asks "does it
run"; the reviewer asks "is this what was asked for", against `plan.md` — and
neither can answer the other's. A revision that closes a review finding
re-enters the suite, so the tree that gets committed is the tree that was both
tested and approved.

The code commit lands after verification, not straight after the build: fixes
and revisions are part of the same work product, and red code has no business
on the branch. A run that fails verification therefore leaves the plan
committed and the working tree dirty — the spec is a real artifact either way,
and the unfinished code stays where the engineer can see it.

The documenter measures against the commit this run STARTED from, not against
`main`, because by then the run has moved `main` itself. That baseline is
pinned before the first commit phase and printed in the request phase.
"""

import argparse
import sys

from adw_modules import (agents, changes, delivery, gates, git_helper,
                         issue_ingestion, quality, session, utils)
from adw_modules.data_types import (AgentCall, BuildOutput, ChangeCapture,
                                    DocumentOutput, PhaseParams, PlanOutput,
                                    ReviewOutput)

REQUIRED_AGENTS = ["planner", "builder", "reviewer", "documenter"]
MAX_FIX_LOOPS = 3
# Four, not three. The loop breaks at `i == MAX_REVISION_LOOPS` BEFORE revising,
# so N loops buy N reviews but only N-1 repairs: at 3 the final review was
# computed, paid for and discarded unused. Run 577358e2 (issue #5) ended that
# way at 20 of 21 requirements, and its unused review named the remaining defect
# by file and line. A fourth slot turns that last review into a repair.
#
# It is one more attempt, not a fix for a run that is not converging: every
# revision in that run introduced a fresh bug in the same code (6 unmet -> 1 ->
# 1). Still bounded, and an unapproved build is still never committed.
MAX_REVISION_LOOPS = 4

DOCUMENT_NOTES = ("Read diff_path in full before writing. Document only what the "
                  "diff shows, then copy the write-up into app_docs/ as your task "
                  "describes.")


def main(prompt: str | None = None,
         config: str = "adws/adw_sssf_config/sssf.config.yaml",
         adw_id: str | None = None, base: str | None = None,
         issue_number: int | None = None) -> int:
    if (prompt is None) == (issue_number is None):
        raise ValueError("Provide exactly one request source: a prompt or --issue <number>.")
    if issue_number is not None and base is not None:
        raise ValueError(
            f"Issue #{issue_number} supplies its base through its target label; "
            "do not pass --base."
        )

    if issue_number is not None:
        # The gate config and GitHub read deliberately use the code location
        # directly. Resolving the canonical checkout invokes git, which is not
        # allowed until all four issue gates have passed.
        gate_root = session.codebase_dir()
        gate_cfg = agents.load_config(config, repo_root=gate_root)
        issue_request = issue_ingestion.ingest(
            issue_number, repo_root=gate_root,
            settings=gate_cfg.defaults.worktree,
        )
        prompt = issue_request.prompt
        base = issue_request.base_branch
    else:
        base = session.require_phase_a_base(base)

    cfg, repo_root = session.bootstrap(config, REQUIRED_AGENTS)
    if issue_number is None:
        prompt = utils.resolve_prompt(prompt, cwd=repo_root)  # type: ignore[arg-type]
    assert prompt is not None and base is not None
    run = session.ensure(cfg, adw_id, repo_root=repo_root, prompt=prompt, base=base)
    baseline = git_helper.rev("HEAD", cwd=run.work_root)  # before run commits

    def commit(ph, envelope, since: str | None = None) -> None:
        """Commit what the preceding phase produced, in that agent's own words.

        `since` lets a clean tree still count as delivered work when the agent
        committed it itself -- see git_helper.commit_all.
        """
        message = envelope.commit_message or f"sssf({run.adw_id}): {envelope.summary}"
        ph.log(sha=git_helper.commit_all(message, cwd=run.work_root, since=since),
               message=message)

    def record(ph, result) -> None:
        """Log a deterministic block's verdict — the same shape every ADW uses."""
        passed = sum(1 for check in result.checks if check.passed)
        ph.log(passed=result.passed, checks=f"{passed}/{len(result.checks)}",
               artifacts=", ".join(result.artifacts))

    with run.phase(PhaseParams(name="request", kind="engineer", owner=run.engineer,
                               description="Capture the incoming ask")) as ph:
        ph.log(input=prompt, baseline=git_helper.short_sha(baseline, cwd=run.work_root))

    # What was ALREADY failing here, before this run touched anything. Measured
    # on the base commit and cached against it, so the cost is one suite run per
    # base rather than one per run. Without it a repository whose suite is red
    # for its own reasons can never accept any work: every run inherits those
    # failures, the builder is blamed for them, and a correct implementation is
    # rejected. See quality.py for the incident this comes from.
    base_failures = quality.read_baseline(run, baseline)
    with run.phase(PhaseParams(name="baseline_tests", kind="code", owner="quality",
                               description="Measure the suite on the base — a failure "
                                           "already here is not this run's to answer for")) as ph:
        if base_failures is None:
            check = quality.test(run)
            if quality.measurable(check):
                base_failures = quality.ids_from_check(check)
                quality.write_baseline(run, baseline, base_failures, command=check.command)
                ph.log(measured=git_helper.short_sha(baseline, cwd=run.work_root),
                       already_failing=len(base_failures),
                       artifacts=check.output_artifact)
            else:
                # The suite did not finish -- a timeout, a collection error, a
                # crash. It measured nothing, so nothing is cached: caching an
                # empty result here would mark this base "clean" for every run
                # that follows and quietly restore the strict rejection this
                # phase exists to prevent. Fall through unmeasured, which is
                # strict for THIS run only, and say so loudly.
                base_failures = None
                ph.log(unmeasured=git_helper.short_sha(baseline, cwd=run.work_root),
                       exit_code=check.returncode,
                       note="suite did not finish; nothing cached, this run is judged strictly",
                       artifacts=check.output_artifact)
        else:
            ph.log(reused=git_helper.short_sha(baseline, cwd=run.work_root),
                   already_failing=len(base_failures))

    with run.phase(PhaseParams(name="plan", kind="agent", owner="planner",
                               description="Turn the request into an implementable plan")) as ph:
        plan = ph.call(AgentCall(output_type=PlanOutput, prompt=prompt,
                                 gates=[gates.artifacts_exist, gates.files_non_empty]))

    with run.phase(PhaseParams(name="commit_plan", kind="code", owner="git",
                               description="Put the spec on record before any code exists to blur it")) as ph:
        commit(ph, plan)
        plan_head = git_helper.rev("HEAD", cwd=run.work_root)

    with run.phase(PhaseParams(name="build", kind="agent", owner="builder",
                               description="Implement the plan exactly")) as ph:
        build = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt, previous=plan,
                                  gates=[gates.diff_matches_claims]))

    test = None
    for i in range(1, MAX_FIX_LOOPS + 1):
        with run.phase(PhaseParams(name=f"test_{i}", kind="code", owner="quality",
                                   description="Run the suite — a known command, so code runs "
                                               "it and no agent has to rediscover it")) as ph:
            test = quality.run_tests(run)
            record(ph, test)

        check = test.checks[-1]
        new_failures = quality.regressions(check, base_failures)
        if quality.accepts(check, base_failures):
            if not test.passed:
                run.console.note(
                    f"quality test: {len(quality.ids_from_check(check))} failure(s), all "
                    f"already failing on the base — nothing new, accepting")
            break

        with run.phase(PhaseParams(name=f"fix_{i}", kind="agent", owner="builder", retries=1,
                                   description="Repair what this run broke — failures that "
                                               "were already on the base are not in scope")) as ph:
            ph.log(new_failures=len(new_failures),
                   pre_existing=len(quality.ids_from_check(check)) - len(new_failures))
            build = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt,
                                      previous=quality.as_regression_envelope(test, new_failures),
                                      gates=[gates.diff_matches_claims]))

    review = None
    revised = False
    for i in range(1, MAX_REVISION_LOOPS + 1):
        with run.phase(PhaseParams(name=f"review_{i}", kind="agent", owner="reviewer",
                                   description="Confirm the build matches the plan")) as ph:
            review = ph.call(AgentCall(output_type=ReviewOutput, prompt=prompt, previous=build,
                                       gates=[gates.artifacts_exist, gates.verdict_consistent]))

        if review.approved or i == MAX_REVISION_LOOPS:
            break

        with run.phase(PhaseParams(name=f"revise_{i}", kind="agent", owner="builder", retries=1,
                                   description="Close the reviewer's blocking findings")) as ph:
            build = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt, previous=review,
                                      gates=[gates.diff_matches_claims]))
            revised = True

    # A revision edited code after the suite last ran, so the green light is
    # stale. Re-run it rather than commit on a result that predates the change.
    if revised and review is not None and review.approved:
        with run.phase(PhaseParams(name="retest", kind="code", owner="quality",
                                   description="Re-run the suite — the revision changed code "
                                               "after the last green result")) as ph:
            test = quality.run_tests(run)
            record(ph, test)

    # Red tests or a rejected review stop the chain here: the code stays
    # uncommitted and nothing is documented, because there is nothing worth
    # describing yet. The plan commit stands — it is a record of what was asked.
    # "Clean" means this run introduced no failure, not that the repository is
    # spotless. A suite that was red before the run started stays red after it
    # and says nothing about the work; only a test that passed on the base and
    # fails now is this run's to answer for.
    suite_clean = (test is not None
                   and quality.accepts(test.checks[-1], base_failures))
    verified = suite_clean and review is not None and review.approved

    # A green suite with an unapproved review is not the same failure as a
    # broken build, and treating them alike threw away run 577358e2: 14.3M
    # tokens, 20 of 21 requirements met, and the code left uncommitted in a
    # worktree while the next run rebuilt it from nothing. When the suite is
    # green and the reviewer is nearly satisfied, the branch goes out as a
    # DRAFT carrying the open items, so p2/p3 and a person work on real code.
    # The draft is the safety: the factory decider refuses to merge one.
    readiness = None
    if not verified and suite_clean and review is not None:
        readiness = delivery.grade(review)

    if verified:
        with run.phase(PhaseParams(name="commit_build", kind="code", owner="git",
                                   description="Land the code only now: green suite, approved review")) as ph:
            commit(ph, build, since=plan_head)

        with run.phase(PhaseParams(name="changes", kind="code", owner="git",
                                   description="Diff the whole run against its pinned baseline, for the documenter")) as ph:
            changeset = changes.capture(run, ChangeCapture(base=baseline))
            ph.log(base=f"{changeset.base.label} @ {changeset.base.commit[:7]}",
                   reason=changeset.base.reason,
                   files=len(changeset.files) + len(changeset.untracked),
                   lines=f"+{changeset.insertions} -{changeset.deletions}",
                   diff=changeset.diff_path)
            if changeset.empty:
                raise RuntimeError(
                    f"nothing changed since {changeset.base.label} "
                    f"({changeset.base.reason}) — there is nothing to document.")

        with run.phase(PhaseParams(name="document", kind="agent", owner="documenter", retries=1,
                                   description="Write up the completed change")) as ph:
            document = ph.call(AgentCall(output_type=DocumentOutput, prompt=prompt,
                                         previous=changes.as_envelope(
                                             changeset, DOCUMENT_NOTES, cwd=run.work_root),
                                         gates=[gates.artifacts_exist, gates.files_non_empty]))

        with run.phase(PhaseParams(name="commit_docs", kind="code", owner="git",
                                   description="Ship the write-up in its own commit, beside the code it describes")) as ph:
            commit(ph, document)

        with run.phase(PhaseParams(name="deliver", kind="code", owner="git",
                                   description="Push the verified run branch and open or reuse its pull request")) as ph:
            pull_request = delivery.deliver(
                run, verified=True, document=document,
                issue_number=issue_number,
            )
            if pull_request is None:  # verified=True makes this an invariant, not a normal path
                raise RuntimeError("verified delivery returned no pull request")
            ph.log(pr=f"PR #{pull_request.number}", url=pull_request.url,
                   state=pull_request.state,
                   action="created" if pull_request.created else "reused")

    elif readiness is not None and readiness.deliverable:
        with run.phase(PhaseParams(name="deliver_draft", kind="code", owner="git",
                                   description="Push the unapproved but nearly-finished "
                                               "branch as a draft, with the open items")) as ph:
            commit(ph, build)
            pull_request = delivery.deliver(
                run, verified=False, document=None, issue_number=issue_number,
                review=review, readiness=readiness,
            )
            if pull_request is None:
                raise RuntimeError("a deliverable readiness returned no pull request")
            ph.log(pr=f"PR #{pull_request.number}", url=pull_request.url,
                   draft="yes", readiness=readiness.reason,
                   action="created" if pull_request.created else "reused")

    if verified:
        return run.finish(accepted=True, reason="")
    if readiness is not None and readiness.deliverable:
        # Not accepted -- the reviewer did not approve it -- but the work is on
        # a branch and in a draft PR, so say what actually happened.
        return run.finish(
            accepted=False,
            reason=f"review not approved ({readiness.reason}); "
                   f"delivered as a draft for the review stages")
    # Name which half failed. "The suite or the review" sent a person reading
    # the log looking for a red suite that was never this run's doing.
    if not suite_clean and test is not None:
        broke = len(quality.regressions(test.checks[-1], base_failures))
        why = (f"{broke} test(s) that passed on the base now fail"
               if broke else "the suite failed without naming a test")
    elif review is not None and not review.approved:
        why = "the reviewer did not approve"
    else:
        why = "the suite or the review never came back clean"
    return run.finish(accepted=verified, reason=why)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="?", help="inline text or a path to a prompt file")
    parser.add_argument("--issue", type=int, default=None,
                        help="GitHub issue number to use instead of a prompt")
    parser.add_argument("--config", default="adws/adw_sssf_config/sssf.config.yaml")
    parser.add_argument("--adw-id", default=None, help="join or pin an existing session")
    parser.add_argument("--base", default=None,
                        help="remote base branch (required for a free prompt; forbidden with --issue)")
    args = parser.parse_args()
    sys.exit(main(args.prompt, args.config, args.adw_id, args.base, args.issue))
