"""Small shared helpers. Anything bigger belongs in its own module."""

from __future__ import annotations

import os
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def operator_env() -> dict[str, str]:
    """The engineer's own environment, as their shell would hand it over.

    Agents and quality blocks are meant to see exactly what the operator sees:
    their PATH, their toolchains, their globally installed packages. Copying
    os.environ gets almost all the way there — but ADWs launch under `uv run`,
    which prepends its ephemeral venv's bin to PATH and sets VIRTUAL_ENV. That
    venv holds the ADW's OWN dependencies (pydantic, pyyaml), not the
    operator's, so anything a subprocess resolves through it — `python3`,
    `pip`, every globally pip-installed CLI — silently becomes the wrong one.

    Stripping the venv restores parity: `python3` in an agent's bash is the
    same `python3` the engineer gets in their terminal. The ADW's own imports
    are unaffected; this env is only ever handed to child processes.

    Both layouts have to go: a venv's executables live in `bin` on POSIX and in
    `Scripts` on Windows. Removing only `bin` left the uv venv first on PATH for
    every Windows run, so `python` was the ephemeral ADW environment all along —
    the test block resolved an interpreter holding pydantic and pyyaml and no
    pytest, and the suite failed at import as though the agent had broken it.
    Comparison is normcase'd because PATH entries and the real directory differ
    in case on Windows.

    VIRTUAL_ENV is not the whole story, and believing it was cost a finished run.
    `uv run` prepends TWO directories: the ephemeral venv it names in
    VIRTUAL_ENV, and the cached BASE interpreter it built that venv from, which
    it names nowhere:

        ...\\uv\\cache\\builds-v0\\.tmpXXXX\\Scripts   <- VIRTUAL_ENV, stripped
        ...\\uv\\cache\\archive-v0\\YYYY\\Scripts      <- not, and it survived
        ...\\the-project\\.venv\\Scripts               <- what should have won

    So `python` still resolved inside uv's cache, still had no pytest, and a p3
    run that had fixed all six of its findings with every gate green was failed
    by its own retest phase in 0.065s. Anything under uv's cache belongs to the
    tool and never to the operator, so the whole cache goes.
    """
    env = os.environ.copy()
    venv = env.pop("VIRTUAL_ENV", "")

    venv_dirs = (
        {os.path.normcase(str(Path(venv) / name)) for name in ("bin", "Scripts")}
        if venv else set()
    )

    def is_uv_cache(entry: str) -> bool:
        """Does this PATH entry live inside uv's cache, under any layout?

        Matched on the cache ROOT, not on `builds-v0`/`archive-v0` by name:
        those are uv's private layout, they have been renamed before, and a
        version bump must not quietly restore this bug.
        """
        normalized = os.path.normcase(entry).replace("\\", "/").rstrip("/")
        explicit = os.environ.get("UV_CACHE_DIR")
        if explicit:
            root = os.path.normcase(explicit).replace("\\", "/").rstrip("/")
            if normalized == root or normalized.startswith(root + "/"):
                return True
        return "/uv/cache/" in normalized + "/"

    parts = [
        p for p in env.get("PATH", "").split(os.pathsep)
        if p
        and os.path.normcase(p.rstrip("\\/")) not in venv_dirs
        and not is_uv_cache(p)
    ]
    env["PATH"] = os.pathsep.join(parts)
    return env


def new_id(length: int = 8) -> str:
    return secrets.token_hex(length // 2)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_prompt(arg: str, *, cwd: Path) -> str:
    """Resolve a CLI prompt file from the canonical checkout, else use inline text."""
    try:
        configured = Path(arg).expanduser()
        p = configured if configured.is_absolute() else cwd / configured
        if p.is_file():
            return p.read_text(encoding="utf-8")
    except OSError:
        pass
    return arg


def engineer_name() -> str:
    name = os.environ.get("ENGINEER_NAME", "").strip()
    if name:
        return name
    try:
        out = subprocess.run(["git", "config", "user.name"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except OSError:
        pass
    return os.environ.get("USER", "engineer")
