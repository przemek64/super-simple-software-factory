#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW Two-Axis PR Review (M3) — Standards and Spec, judged separately.

Usage:
    uv run adws/adw_pr_review_2axis_m3.py --pr 90
    uv run adws/adw_pr_review_2axis_m3.py --pr 90 --no-post --keep-worktree

Phases: engineer(request) -> git(sources) -> standards_axis -> spec_axis -> git(publish)

Both axes are MiniMax-M3. Cheap, and each axis is a single whole-diff call — so
keep the PR small: M3 times out on any large single call, which is why the
adversarial ADW splits its work per file and this one does not.

Where the adversarial review (adw_pr_review_m3m3.py) asks "is this code correct",
this one asks two different questions the other never asks: is it written the way
this repo writes code, and is it the thing the issue asked for. Run both when a
PR matters; they do not overlap.

The codex variant (adw_pr_review_2axis_codex.py) puts gpt-5.4 on both axes — a
different mind, and no single-call timeout.
"""

import argparse
import sys

from adw_modules.two_axis import review_pr_two_axis

STANDARDS = "standards_axis_m3"
SPEC = "spec_axis_m3"


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
