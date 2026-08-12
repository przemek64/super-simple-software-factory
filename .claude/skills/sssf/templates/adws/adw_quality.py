#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW Quality — lint, typecheck, and build the project.

Usage:
    uv run adws/adw_quality.py "<reason for the quality run>" --base <branch> [--config adws/adw_sssf_config/sssf.config.yaml] [--adw-id a1b2c3d4]

Phases: engineer(request) -> code(quality)
"""

import argparse
import sys

from adw_modules import quality, session, utils
from adw_modules.data_types import PhaseParams

REQUIRED_AGENTS: list[str] = []


def main(prompt: str, config: str = "adws/adw_sssf_config/sssf.config.yaml",
         adw_id: str | None = None, base: str | None = None) -> int:
    base = session.require_phase_a_base(base)
    cfg, repo_root = session.bootstrap(config, REQUIRED_AGENTS)
    prompt = utils.resolve_prompt(prompt, cwd=repo_root)
    run = session.ensure(cfg, adw_id, repo_root=repo_root, prompt=prompt, base=base)

    with run.phase(PhaseParams(name="request", kind="engineer", owner=run.engineer,
                               description="Capture why quality verification was requested")) as ph:
        ph.log(input=prompt)

    with run.phase(PhaseParams(name="quality", kind="code", owner="quality",
                               description="Run the deterministic quality blocks")) as ph:
        result = quality.run_quality(run)
        passed = sum(1 for check in result.checks if check.passed)
        ph.log(passed=result.passed, checks=f"{passed}/{len(result.checks)}",
               artifacts=", ".join(result.artifacts))
        if not result.passed:
            raise RuntimeError("quality failed: " + "; ".join(result.failures))

    return run.finish()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", help="inline text or a path to a prompt file")
    parser.add_argument("--config", default="adws/adw_sssf_config/sssf.config.yaml")
    parser.add_argument("--adw-id", default=None, help="join or pin an existing session")
    parser.add_argument("--base", required=True, help="required remote base branch for the isolated run")
    args = parser.parse_args()
    sys.exit(main(args.prompt, args.config, args.adw_id, args.base))
