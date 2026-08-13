#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW PR Review (mixed seats) — M3 reviews, a codex model audits it.

Usage:
    uv run adws/adw_pr_review_mixed.py --pr 87
    uv run adws/adw_pr_review_mixed.py --pr 87 --no-post --keep-worktree

Same two-pass flow as adw_pr_review_m3m3.py; only the audit seat changes.

WHY A SECOND VARIANT: two instances of one model share its blind spots — the
class of bug M3 does not look for is the class neither pass finds. A different
family on the audit seat disagrees for structural reasons, not sampling ones,
and that is what catches the miss. It costs more per file, so this is the
variant for the PRs that matter, not the default.

Read the two runs side by side: where the seats agree, the finding is solid;
where they split, that is the finding worth a human's attention.
"""

import argparse
import sys

from adw_modules.pr_review import review_pr

REVIEWER = "file_reviewer_m3"
AUDITOR = "auditor_codex"
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
