#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW Simple SDLC (resume) — the same chain, restarted where it stopped.

Usage:
    uv run adws/adw_simple_sdlc_resume.py --adw-id a1b2c3d4 --issue 2
    uv run adws/adw_simple_sdlc_resume.py --adw-id a1b2c3d4 "<prompt>" --base main

Identical to adw_simple_sdlc.py in every phase, output and commit. The one
difference: it requires an existing --adw-id, and skips the run's leading
phases that already succeeded, restarting at the first that did not.

Why a resume exists. A run that dies at `build` has already paid for `plan` —
a full planner pass, real money, real minutes — and has already committed that
plan to its branch. Re-running the whole ADW spends it again, and `commit_plan`
then fails outright with "nothing to commit" because the identical spec is
already on the branch. The work is not lost, only unreachable: the phases are
in the tracer db and so are their envelopes.

Where it stops skipping. At the FIRST phase that is not `success`, even if a
later one succeeded. A phase that ran after a failure was computed against a
tree that has since been repaired, so its green result is stale — reusing it
would hand this run a review of code that no longer exists.

What a skipped phase still provides. Its envelope, rehydrated from the db, so
the phase that consumed it is unaffected: the builder receives the same
PlanOutput the planner produced. A phase marked success whose envelope is
missing is an error, not a shrug — see adw_modules/resume.py.

The worktree, the branch and each agent's pi session are all keyed by adw_id
and come back on their own, so a resumed builder continues with the context it
had when it stopped.
"""

import argparse
import sys

from adw_modules import (agents, changes, delivery, gates, git_helper,
                         issue_ingestion, quality, resume, session, utils)
from adw_modules.data_types import (AgentCall, BuildOutput, ChangeCapture,
                                    DocumentOutput, PhaseParams, PlanOutput,
                                    ReviewOutput)

REQUIRED_AGENTS = ["planner", "builder", "reviewer", "documenter"]
MAX_FIX_LOOPS = 3
# Keep in step with adw_simple_sdlc.py — this ADW is identical in every phase.
MAX_REVISION_LOOPS = 3

DOCUMENT_NOTES = ("Read diff_path in full before writing. Document only what the "
                  "diff shows, then copy the write-up into app_docs/ as your task "
                  "describes.")


def main(prompt: str | None = None,
         config: str = "adws/adw_sssf_config/sssf.config.yaml",
         adw_id: str | None = None, base: str | None = None,
         issue_number: int | None = None) -> int:
    if not adw_id:
        raise ValueError(
            "--adw-id is required: a resume continues a specific run. Use "
            "adw_simple_sdlc.py to start a new one.")
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

    # Read the prior run BEFORE any phase opens, so the skip set is the history
    # as it stood, not as this process is about to rewrite it.
    db_path = session.state_path(repo_root, cfg.observability.db)
    already_done = resume.completed_prefix(db_path, adw_id)
    if not already_done:
        raise resume.ResumeError(
            f"run {adw_id!r} has no successful leading phase to skip -- nothing "
            f"to resume. Start it with adw_simple_sdlc.py.")
    run.console.note(
        f"resuming {adw_id}: skipping {len(already_done)} completed "
        f"phase(s) -- {', '.join(already_done)}")

    def done(phase_name: str) -> bool:
        """True when this phase already succeeded in the run being resumed."""
        return phase_name in already_done

    # The baseline is what the ORIGINAL run measured against. On a resume the
    # branch already carries that run's commits, so pinning HEAD now would make
    # the documenter's diff exclude the plan commit this run is continuing.
    baseline = git_helper.rev("HEAD", cwd=run.work_root)

    def commit(ph, envelope) -> None:
        """Commit what the preceding phase produced, in that agent's own words."""
        message = envelope.commit_message or f"sssf({run.adw_id}): {envelope.summary}"
        ph.log(sha=git_helper.commit_all(message, cwd=run.work_root), message=message)

    def record(ph, result) -> None:
        """Log a deterministic block's verdict — the same shape every ADW uses."""
        passed = sum(1 for check in result.checks if check.passed)
        ph.log(passed=result.passed, checks=f"{passed}/{len(result.checks)}",
               artifacts=", ".join(result.artifacts))

    if not done("request"):
        with run.phase(PhaseParams(name="request", kind="engineer", owner=run.engineer,
                                   description="Capture the incoming ask")) as ph:
            ph.log(input=prompt, baseline=git_helper.short_sha(baseline, cwd=run.work_root))

    if done("plan"):
        # The planner is the expensive phase and the reason this ADW exists.
        plan = resume.rehydrate(db_path, adw_id, "plan", PlanOutput)
    else:
        with run.phase(PhaseParams(name="plan", kind="agent", owner="planner",
                                   description="Turn the request into an implementable plan")) as ph:
            plan = ph.call(AgentCall(output_type=PlanOutput, prompt=prompt,
                                     gates=[gates.artifacts_exist, gates.files_non_empty]))

    # Skipping this is not an optimisation but a correctness requirement: the
    # spec is already committed on the branch, and commit_all raises "nothing to
    # commit" on an unchanged tree.
    if not done("commit_plan"):
        with run.phase(PhaseParams(name="commit_plan", kind="code", owner="git",
                                   description="Put the spec on record before any code exists to blur it")) as ph:
            commit(ph, plan)

    if done("build"):
        build = resume.rehydrate(db_path, adw_id, "build", BuildOutput)
    else:
        with run.phase(PhaseParams(name="build", kind="agent", owner="builder",
                                   description="Implement the plan exactly")) as ph:
            build = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt, previous=plan,
                                      gates=[gates.diff_matches_claims]))

    # Resume stops here. The loop phases (test_i / fix_i / review_i) always
    # re-run, even if the prior run's rows say they passed. They are cheap
    # relative to an agent phase, and their verdicts are only meaningful against
    # the tree as it stands now -- a green suite recorded before the build was
    # repaired says nothing about the build that exists today.
    test = None
    for i in range(1, MAX_FIX_LOOPS + 1):
        with run.phase(PhaseParams(name=f"test_{i}", kind="code", owner="quality",
                                   description="Run the suite — a known command, so code runs "
                                               "it and no agent has to rediscover it")) as ph:
            test = quality.run_tests(run)
            record(ph, test)

        if test.passed:
            break

        with run.phase(PhaseParams(name=f"fix_{i}", kind="agent", owner="builder", retries=1,
                                   description="Repair what the suite reported, from its "
                                               "verbatim output")) as ph:
            build = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt,
                                      previous=quality.as_envelope(test, "tests"),
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
    verified = (test is not None and test.passed
                and review is not None and review.approved)
    if verified:
        # Same reason commit_plan is guarded: the code is already on the branch
        # from the run being resumed, and commit_all raises "nothing to commit"
        # on an unchanged tree.
        if not done("commit_build"):
            with run.phase(PhaseParams(name="commit_build", kind="code", owner="git",
                                       description="Land the code only now: green suite, approved review")) as ph:
                commit(ph, build)

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

    return run.finish(accepted=verified,
                      reason="the suite or the review never came back clean")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="?", help="inline text or a path to a prompt file")
    parser.add_argument("--issue", type=int, default=None,
                        help="GitHub issue number to use instead of a prompt")
    parser.add_argument("--config", default="adws/adw_sssf_config/sssf.config.yaml")
    parser.add_argument("--adw-id", required=True,
                        help="the run to resume (required — this ADW never starts a new one)")
    parser.add_argument("--base", default=None,
                        help="remote base branch (required for a free prompt; forbidden with --issue)")
    args = parser.parse_args()
    sys.exit(main(args.prompt, args.config, args.adw_id, args.base, args.issue))
