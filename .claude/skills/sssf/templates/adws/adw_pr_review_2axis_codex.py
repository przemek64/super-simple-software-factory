#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW Two-Axis PR Review (codex) — Standards and Spec on gpt-5.4.

Usage:
    uv run adws/adw_pr_review_2axis_codex.py --pr 90
    uv run adws/adw_pr_review_2axis_codex.py --pr 90 --no-post --keep-worktree

Identical flow to adw_pr_review_2axis_m3.py; only the two axis seats change.

WHY BOTH VARIANTS EXIST: they are built to be run head to head on the same PR.
gpt-5.4 is the stronger cross-mind and carries no single-call timeout, so it
handles a diff that would time M3 out — and it costs accordingly. M3 is the one
to reach for by default; this is the one for the PR you cannot afford to get
wrong, or for a second opinion when the M3 run says the diff is clean.
"""

import argparse
import sys

from adw_modules.two_axis import review_pr_two_axis

STANDARDS = "standards_axis_codex"
SPEC = "spec_axis_codex"


def main(pr: int, config: str = "adws/adw_sssf_config/sssf.config.yaml",
         adw_id: str | None = None, repo: str | None = None,
         post: bool = True, keep_worktree: bool = False) -> int:
    return review_pr_two_axis(pr=pr, standards_agent=STANDARDS, spec_agent=SPEC,
                              config=config, adw_id=adw_id, repo=repo,
                              post=post, keep_worktree=keep_worktree)


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
