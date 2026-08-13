#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW PR Review (M3.M3) — two passes of the SAME model, arguing with each other.

Usage:
    uv run adws/adw_pr_review_m3m3.py --pr 87
    uv run adws/adw_pr_review_m3m3.py --pr 87 --no-post --keep-worktree

Phases: engineer(request) -> git(scope) -> reviewer x N files
        -> code(coverage) -> synthesizer -> git(post)
        -> auditor x N files -> code(coverage) -> synthesizer(verdict) -> git(publish)

Both seats are MiniMax-M3, exactly like the Archon workflow this is ported from
(`archonx-fix-github-issue-p3a-4m3m3k-scoped`). Two instances of one model with
no shared context are still two minds: the audit pass reads pass 1's claims as a
target, not as its own memory, and M3 is cheap enough to spend twice per file.

The mixed-seat variant (adw_pr_review_mixed.py) pays for a genuinely different
model on the audit seat. Run both on the same PR when a split verdict matters.

This ADW never edits the code it reviews — see adw_modules/pr_review.py.
"""

import argparse
import sys

from adw_modules.pr_review import review_pr

REVIEWER = "file_reviewer_m3"
AUDITOR = "auditor_m3"
SYNTHESIZER = "review_synthesizer"


def main(pr: int, config: str = "adws/adw_sssf_config/sssf.config.yaml",
         adw_id: str | None = None, repo: str | None = None,
         post: bool = True, keep_worktree: bool = False) -> int:
    return review_pr(pr=pr, reviewer=REVIEWER, auditor=AUDITOR,
                     synthesizer=SYNTHESIZER, config=config, adw_id=adw_id,
                     repo=repo, post=post, keep_worktree=keep_worktree)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", type=int, required=True, help="pull request number to review")
    parser.add_argument("--config", default="adws/adw_sssf_config/sssf.config.yaml")
    parser.add_argument("--adw-id", default=None, help="join or pin an existing session")
    parser.add_argument("--repo", default=None, help="owner/name (default: this repo)")
    parser.add_argument("--no-post", action="store_true",
                        help="write the review to artifacts only; post nothing to GitHub")
    parser.add_argument("--keep-worktree", action="store_true",
                        help="leave the PR head checkout on disk after a clean run")
    args = parser.parse_args()
    sys.exit(main(args.pr, args.config, args.adw_id, args.repo,
                  not args.no_post, args.keep_worktree))
