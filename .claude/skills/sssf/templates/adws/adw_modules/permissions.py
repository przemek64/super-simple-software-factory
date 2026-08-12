"""What an agent may CHANGE, enforced in code after the fact.

`tools:` is a capability list, not a sandbox, and two holes make it
unenforceable on its own:

  * `bash` runs anything. A builder handed bash to run a test suite can also
    run `git checkout adws/` — which is not hypothetical: one did, discarding
    uncommitted changes to the very quality check it was about to be judged by.
  * `write` reaches any path, not just the one report file an agent was given
    it for. A reviewer configured with "no edit, so it cannot quietly fix"
    could still rewrite the code it was reviewing.

So permission is verified the way every other claim in this system is —
after the fact, against the repo itself. Every factory agent participates in a
cross-process repository lock; bounded snapshots retain hashes/metadata in RAM
and only dirty/untracked baseline bytes in temporary on-disk backups (clean
tracked bytes are recoverable by immutable Git blob ID). `enforce()` compares
and fails the phase if the lock-owning agent touched outside its allowlist.

Comparing change-sets, rather than watching for writes, is what catches the
`git checkout` case: a path that was modified before the agent ran and is clean
afterwards has been reverted, and a reversion is a modification. Appearing,
disappearing, and changing all count.

A breach is NOT a gate violation. Gates are for work an agent can be asked to
redo; a breach cannot be corrected by re-prompting, because the write already
happened. It aborts the phase and names every offending path.

Two keys drive it, both in sssf.config.yaml:
    defaults.protected_files   paths no agent may touch unless it names them itself
    agents[].writes      None = unrestricted · [] = read-only · [...] = only these
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .data_types import AgentConfig, SSSFConfig


class PermissionBreach(RuntimeError):
    """An agent modified a path it was not permitted to modify."""


class SnapshotLimitExceeded(PermissionBreach):
    """A guard could not establish a complete baseline within its hard bounds."""


# `enforce()` below only ever sees paths INSIDE the repo — it works off `git
# diff HEAD`, which cannot show a change to a path outside this working tree.
# That is exactly how an agent reached a sibling repo unnoticed: it named an
# absolute path in a `read`/`grep`/`find`/`ls`/`bash` tool call, and nothing
# checked the path was inside the run's allowed roots before the call happened.
_PATH_ARG_KEYS = ("path", "file_path")
_READ_ONLY_TOOLS = {"read", "grep", "find", "ls", "glob"}
# (?<![A-Za-z0-9]) — without it this also matches inside a URL: "http://..."
# has "p:" immediately before "//", which alone satisfies [A-Za-z]:[\\/].
_ABS_PATH_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'<>|;&]*")
_TRAVERSAL_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.])(?:\.\.[\\/])+(?:[^\s\"'<>|;&]+)?"
)
_BASH_READ_ONLY = {
    "cat", "cmp", "diff", "dir", "echo", "find", "grep", "head", "ls",
    "printf", "pwd", "rg", "stat", "tail", "test", "type", "wc", "where", "which",
    # Binary inspection. A task like "read APP_Proprietes.cfg at offset 0x520"
    # cannot be done without these, and the guard used to refuse `od` as
    # "opaque and may write externally" -- it has no way to write at all.
    # `xxd` is absent here on purpose: it is the one hexdumper that writes
    # (`-r`, and a second operand), so it keeps its own operand analysis below.
    "base64", "file", "hexdump", "od", "strings",
    # Digests and pure text/path transforms. None takes an output-file option;
    # anything they emit goes to stdout, and a redirection of that stdout is
    # already resolved and bounds-checked as a destination.
    "cksum", "md5sum", "sha1sum", "sha256sum", "sha512sum",
    "basename", "column", "cut", "dirname", "du", "df", "nl", "realpath",
    "rev", "seq", "tac", "tr", "uname",
}
_XXD_VALUE_OPTIONS = {
    "-c", "-cols", "-g", "-groupsize", "-l", "-len", "-n", "-name",
    "-o", "-offset", "-s", "-seek", "-R", "-color", "--color",
}
_XXD_FLAG_OPTIONS = {
    "-a", "-autoskip", "-b", "-bits", "-C", "-capitalize", "-d", "-decimal",
    "-E", "-EBCDIC", "-e", "-h", "-help", "--help", "-i", "-include",
    "-p", "-ps", "-plain", "-r", "-revert", "-t", "-u", "-upper", "-v",
    "-version", "--version",
}
_GIT_READ_ONLY = {
    "diff", "grep", "log", "ls-files", "ls-tree", "rev-parse", "show",
    "show-ref", "status",
}
_BASH_MUTATORS = {
    "cp", "del", "erase", "install", "mkdir", "move", "mv", "rm", "rmdir",
    "robocopy", "tee", "touch", "truncate",
}
_MUTATOR_SAFE_OPTIONS = {
    "-f", "--force", "-p", "--parents", "-r", "-R", "--recursive", "-v",
    "--verbose", "-rf", "-fr",
}
# Constructs whose expansion HIDES a path from static reading: command and
# process substitution, and Windows variable expansion. Braces are deliberately
# absent -- they are awk/jq program syntax, and refusing them refused the whole
# tool for no safety gain. Under the path-bounded policy (see _analyze_bash) an
# unrecognised COMMAND is no longer a reason to refuse; an unreadable PATH is.
_BASH_DYNAMIC_RE = re.compile(
    r"[`$]|%[^%\s]+%|![^!\s]+!|[<>]\(", re.IGNORECASE
)
# Any absolute path written literally anywhere in a command, including inside a
# quoted interpreter one-liner. This is what keeps `python -c "open('C:/x','w')"`
# bounded now that `python` itself is no longer refused outright.
#
# The lookbehind matters: without it this matches the "/out.txt" inside
# "./out.txt" and the "/fields/" inside an awk program, and refuses both. A
# match must also be absolute by pathlib's rules -- on Windows that means a
# drive letter, so a leading "/" alone is drive-relative, not an escape.
_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w./\\])(?:[A-Za-z]:[\\/]|/)[^\s'\"<>|;&,)]*")
_BASH_GLOB_RE = re.compile(r"[*?\[\]]")
# Commands whose FIRST operand is a program, not a filename. Without this an awk
# script like '/fields/{f=1}' is read as the path C:/fields/{f=1} and refused.
# Only the program is skipped; the files that follow are bounded as usual, so
# `sed -i 's/a/b/' outside.txt` is still caught by its real operand.
_PROGRAM_FIRST_OPERAND = {"awk", "gawk", "mawk", "sed", "jq", "tr"}
# The target alternation must list the fd-duplication forms (&1, &2, &-) before
# the general one: they start with "&", which the general class excludes, so
# without them "2>&1" is not recognised as a redirection at all. It then survives
# into the word list and is read as an operand -- an extra argument to the
# command, or worse, a write target. The `&?\d+` filter further down already
# assumes these get captured here.
_REDIRECTION_RE = re.compile(
    r"(?<![<>])(?:\d*>>?|&>)\s*(\"[^\"]+\"|'[^']+'|&\d+|&-|[^\s;&|]+)"
)

# pi reads this unconditionally before an agent does anything of its own —
# not agent-chosen exploration, so it is exempt by exact-file match (not a
# directory grant). Everything outside this run's worktree and canonical state root stays blocked.
_EXEMPT_FILES = {os.path.normcase(os.path.abspath(os.path.expanduser("~/.claude/CLAUDE.md")))}


def _is_exempt(path_str: str) -> bool:
    try:
        return os.path.normcase(os.path.abspath(path_str.replace("\\", "/"))) in _EXEMPT_FILES
    except (OSError, ValueError):
        return False


# How many times a file may move under the snapshot before the tree is called
# unstable. Small on purpose: this absorbs an incidental writer, not a race
# with an agent actively rewriting what is being baselined.
_SNAPSHOT_READ_ATTEMPTS = 3

_MSYS_DRIVE_RE = re.compile(r"^/([A-Za-z])(/|$)")


def _from_msys_drive(normalized: str) -> str:
    """Translate a Git-Bash drive path (`/c/dev/x`) to its Windows form.

    pi's bash tool runs Git Bash, where `/c/...` IS `C:\\...`. Windows sees a
    leading slash with no drive letter as NOT absolute, so the path was joined
    onto the cwd's drive instead -- turning a read of the granted canonical
    checkout into `C:\\c\\dev\\projects\\...`, a path that exists nowhere and
    therefore lies outside every root. The refusal named that invented path,
    which is why it read as nonsense.
    """
    if os.name != "nt":
        return normalized
    match = _MSYS_DRIVE_RE.match(normalized)
    if not match:
        return normalized
    return f"{match.group(1)}:/{normalized[match.end():]}"


def _named_path(path_str: str, cwd) -> Path:
    """Resolve a tool path lexically from pi's cwd, without following junctions."""
    normalized = path_str.strip().strip("\"'").replace("\\", "/")
    normalized = _from_msys_drive(normalized)
    path = Path(normalized).expanduser()
    if not path.is_absolute():
        path = Path(cwd) / path
    return Path(os.path.normcase(os.path.abspath(str(path))))


def _root_for(path_str: str, cwd, roots) -> Optional[Path]:
    # NOT Path.resolve(): this repo's docs/ can be a Windows junction outside
    # the checkout. The named path is the authority, but ./.. still must be
    # collapsed so a relative escape cannot masquerade as an in-tree path.
    try:
        if _is_exempt(path_str):
            return Path(path_str)
        candidate = _named_path(path_str, cwd)
        for root in roots:
            named_root = _named_path(str(root), cwd)
            if candidate == named_root or named_root in candidate.parents:
                return named_root
    except (OSError, ValueError):
        return None
    return None


def _destination_root(path_str: str, cwd, roots) -> Optional[Path]:
    """Match a write destination after following existing links/junctions."""
    try:
        candidate = _named_path(path_str, cwd).resolve(strict=False)
        for root in roots:
            resolved_root = _named_path(str(root), cwd).resolve(strict=False)
            if candidate == resolved_root or resolved_root in candidate.parents:
                return _named_path(str(root), cwd)
    except (OSError, ValueError):
        pass
    return None


def _bash_paths(command: str) -> list[str]:
    """Extract explicit absolute and ../ paths from a shell command.

    Dynamic or opaque mutating commands are rejected separately; snapshots
    remain defense in depth for the statically confined commands admitted here.
    """
    found = [*(_ABS_PATH_RE.findall(command)), *(_TRAVERSAL_PATH_RE.findall(command))]
    try:
        for token in shlex.split(command, posix=False):
            token = token.strip("\"'")
            if token.startswith(("../", "..\\")) or Path(token).is_absolute():
                found.append(token)
    except ValueError:
        pass
    return list(dict.fromkeys(found))


def _plain_operands(words: list[str]) -> list[str]:
    """Return non-option operands, rejecting option values we cannot interpret."""
    operands: list[str] = []
    options_done = False
    for word in words:
        value = word.strip("\"'")
        if value == "--":
            options_done = True
        elif not options_done and value.startswith("-"):
            continue
        else:
            operands.append(value)
    return operands


def _option_destinations(
    words: list[str], long_option: str, short_option: str | None = None,
) -> tuple[list[str], Optional[str]]:
    """Extract file-valued output options without mistaking them for reads."""
    destinations: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        value: str | None = None
        if word == long_option or (short_option is not None and word == short_option):
            if index + 1 >= len(words):
                return [], f"{word} requires a static destination"
            value = words[index + 1]
            index += 1
        elif word.startswith(long_option + "="):
            value = word.split("=", 1)[1]
        elif short_option is not None and word.startswith(short_option) and word != short_option:
            value = word[len(short_option):]
        if value is not None:
            if not value or value.startswith("-") or _BASH_GLOB_RE.search(value):
                return [], f"{word} requires a static destination"
            destinations.append(value)
        index += 1
    return destinations, None


def _find_destinations(words: list[str]) -> tuple[list[str], Optional[str]]:
    """Find output actions write their following file operand."""
    destinations: list[str] = []
    index = 0
    actions = {"-fprint": 1, "-fprint0": 1, "-fls": 1, "-fprintf": 2}
    while index < len(words):
        action = words[index].lower()
        required = actions.get(action)
        if required is not None:
            if index + required >= len(words):
                return [], f"find {action} requires its output arguments"
            destination = words[index + 1]
            if not destination or _BASH_GLOB_RE.search(destination):
                return [], f"find {action} requires a static destination"
            destinations.append(destination)
            index += required
        index += 1
    return destinations, None


def _xxd_operands(words: list[str]) -> tuple[list[str], Optional[str]]:
    """Parse xxd's options so only its optional infile/outfile remain."""
    operands: list[str] = []
    options_done = False
    index = 0
    while index < len(words):
        word = words[index]
        if options_done:
            operands.append(word)
        elif word == "--":
            options_done = True
        elif not word.startswith("-"):
            options_done = True
            operands.append(word)
        elif word in _XXD_VALUE_OPTIONS:
            if index + 1 >= len(words):
                return [], f"xxd option {word} requires a value"
            index += 1
        elif any(word.startswith(option + "=") for option in _XXD_VALUE_OPTIONS):
            pass
        elif any(word.startswith(option) and word != option
                 for option in {"-c", "-g", "-l", "-n", "-o", "-s", "-R"}):
            pass
        elif word in _XXD_FLAG_OPTIONS:
            pass
        else:
            return [], f"cannot prove effects of xxd option {word!r}"
        index += 1
    return operands, None


_NULL_SINKS = {"/dev/null", "nul", "/dev/stdout", "/dev/stderr"}


def _looks_like_path(word: str) -> bool:
    """True for an operand that plausibly names a file, so it must be bounded.

    Deliberately generous: a bare word like `-q`, `main` or a pytest node id is
    not treated as a path, but anything with a separator, a drive, or a suffix
    is. Over-including is safe -- the worst case is an extra destination inside
    the worktree, which is allowed anyway. Under-including is what lets a write
    escape, so the doubtful cases resolve toward "it is a path".
    """
    if not word or word.startswith("-"):
        return False
    if word in {".", ".."}:
        return True
    return ("/" in word or "\\" in word or Path(word).is_absolute()
            or bool(Path(word).suffix))


def _within_any(candidate: Path, roots) -> bool:
    """True when `candidate` is one of `roots` or sits under one of them."""
    for item in roots:
        if candidate == item or item in candidate.parents:
            return True
    return False


def _is_null_sink(target: str) -> bool:
    """True for a discard/stream target that is not a file on disk.

    ``>/dev/null`` is the ordinary way to silence a command, but resolving it as
    a path yields ``C:/dev/null`` on Windows -- outside the worktree, so the
    write is refused and the run dies on a command that writes nothing at all.
    """
    return target.replace("\\", "/").lower() in _NULL_SINKS


def _shell_segments(command: str) -> list[str]:
    """Split command operators while preserving separators inside quotes.

    A newline is a command separator exactly like ``;``, so a multi-line script
    is analysed one command per line rather than rejected. An unquoted ``#``
    starts a comment that runs to the end of its line and is dropped: it is not
    a command, and leaving it in would make the analyser read ``#`` as an opaque
    executable. Both cases only apply outside quotes, and a backslash before a
    newline is still a line continuation, so the two lines stay one command.
    """
    segments: list[str] = []
    start = 0
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if quote:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        # A comment ends the current command; the rest of the line is not code.
        if char == "#" and (index == start or command[index - 1] in " \t"):
            segments.append(command[start:index])
            while index < len(command) and command[index] not in "\r\n":
                index += 1
            start = index
            continue
        if command.startswith(("&&", "||"), index):
            segments.append(command[start:index])
            index += 2
            start = index
            continue
        # A lone "&" inside a redirection (2>&1, >&2, &>log) is part of the
        # redirection, not a background-job separator. Splitting there cuts the
        # command in half and leaves "1" looking like an opaque executable.
        if char == "&":
            preceding = command[:index].rstrip()
            if command.startswith("&>", index) or preceding.endswith(">"):
                index += 1
                continue
        if char in ";&|\r\n":
            segments.append(command[start:index])
            index += 1
            start = index
            continue
        index += 1
    segments.append(command[start:])
    return segments


def _analyze_bash(
    command: str, work_root, readable_roots=(),
) -> tuple[bool, list[str], list[str], Optional[str]]:
    """Bound a shell command by the PATHS it names, not by whether its command is known.

    The earlier policy tried to prove what each executable did, and refused
    anything it could not classify. That is unwinnable against a general-purpose
    agent: a builder was refused for `od` (cannot write at all), for `pytest`
    with a redirection (the very suite the next phase runs), for an `awk` program
    (braces), and for `cd` into the canonical checkout it is explicitly allowed
    to READ. Five legitimate commands, five dead runs.

    The rule now is the one that actually expresses the requirement: a command
    may do anything it likes as long as every path it names lies inside this
    run's worktree, or inside a root granted for reading. Unrecognised commands
    and interpreters are no longer refused; their path-like operands are simply
    treated as write targets and bounds-checked by the caller. What is still
    refused is anything that HIDES a path -- command substitution, process
    substitution, Windows variable expansion, heredocs -- because a path that
    cannot be read cannot be bounded.

    This leans on the second safety net rather than duplicating it:
    ``snapshot()``/``enforce()`` diff the tree afterwards and roll back writes
    outside the agent's allowlist. The residual risk taken deliberately is a
    computed path built at runtime from non-literal parts; every literal
    absolute path anywhere in the command, including inside a quoted
    interpreter one-liner, is still checked.

    A ``cd`` is admitted when its single literal destination resolves inside
    ``work_root`` or any readable root. Its destination becomes the cwd used to
    resolve later paths in the same shell call.
    """
    if not command.strip():
        return True, [], [], None
    # Newlines are separators, not a threat: _shell_segments splits on them, so
    # every line of a multi-line script is analysed as its own command. Blocking
    # them outright rejected ordinary agent behaviour -- any two-line script or
    # commented command killed the run on its first tool call.
    if _BASH_DYNAMIC_RE.search(command) or "<<" in command:
        return False, [], [], "uses dynamic or opaque shell syntax"

    root = _named_path(str(work_root), work_root).resolve(strict=False)
    allowed_roots = tuple(
        _named_path(str(item), work_root).resolve(strict=False)
        for item in readable_roots)
    # Catch a literal path the segment parser would never see as an operand --
    # inside a quoted `python -c` body, an option value, anywhere. Cheap, and it
    # is what makes dropping the interpreter refusal defensible.
    for literal in _ABSOLUTE_PATH_RE.findall(command):
        if _is_null_sink(literal) or not Path(literal).is_absolute():
            continue
        candidate = Path(literal).resolve(strict=False)
        if not _within_any(candidate, (root, *allowed_roots)):
            return False, [], [], (
                f"names a path outside this run's roots: {literal}")
    effective_cwd = root
    destinations: list[str] = []
    references: list[str] = []
    segments = _shell_segments(command)
    for raw_segment in segments:
        segment = raw_segment.strip()
        if not segment:
            continue
        redirects = _REDIRECTION_RE.findall(segment)
        segment_without_redirects = _REDIRECTION_RE.sub("", segment).strip()
        try:
            words = shlex.split(segment_without_redirects, posix=False)
        except ValueError:
            return False, [], [], "has shell syntax that cannot be parsed safely"
        if not words:
            return False, [], [], "has a redirection without a provable command"
        words = [word.strip("\"'") for word in words]
        executable = Path(words[0]).name.lower()

        if executable == "cd":
            if redirects:
                return False, [], [], "cd cannot use redirection"
            operands = _plain_operands(words[1:])
            if len(operands) != 1 or operands[0] == "-":
                return False, [], [], "cd requires exactly one static destination"
            destination = operands[0]
            if (_BASH_GLOB_RE.search(destination) or destination.startswith("-") or
                    (not Path(destination).is_absolute() and
                     not destination.startswith(("./", ".\\")))):
                return False, [], [], (
                    "cd requires an absolute destination or an explicit ./ relative destination"
                )
            resolved = _named_path(destination, effective_cwd).resolve(strict=False)
            references.append(str(resolved))
            # Reading the canonical checkout is granted, so refusing to cd there
            # contradicted the grant: the builder could read the file but not
            # stand next to it. Any readable root is a legal destination; writes
            # from that cwd are still bounds-checked individually.
            if not _within_any(resolved, (root, *allowed_roots)):
                return False, [], [], (
                    "cd destination is outside this run's worktree and every readable root")
            if not resolved.is_dir():
                return False, [], [], "cd destination is not an existing directory"
            effective_cwd = resolved
            continue

        # Explicit absolute and traversal operands are references even for a
        # read-only command. Resolve traversal from the cwd established by all
        # preceding cd segments, rather than from the original work root.
        for word in words[1:]:
            if word.startswith(("../", "..\\")) or Path(word).is_absolute():
                references.append(str(
                    _named_path(word, effective_cwd).resolve(strict=False)))

        read_only = executable in _BASH_READ_ONLY
        option_destinations: list[str] = []
        if executable == "command":
            if (len(words) != 3 or words[1] not in {"-v", "-V"} or
                    re.fullmatch(r"[A-Za-z0-9_.+-]+", words[2]) is None):
                return False, [], [], "command is allowed only as 'command -v NAME'"
            read_only = True
        if executable == "diff":
            option_destinations, option_error = _option_destinations(
                words[1:], "--output", "-o")
            if option_error:
                return False, [], [], option_error
        if executable == "find":
            option_destinations, option_error = _find_destinations(words[1:])
            if option_error:
                return False, [], [], option_error
            if any(word.lower() in {"-delete", "-exec", "-execdir", "-ok", "-okdir"}
                   for word in words[1:]):
                read_only = False
        if executable == "xxd":
            operands, option_error = _xxd_operands(words[1:])
            if option_error:
                return False, [], [], option_error
            if len(operands) == 1:
                read_only = True
            elif len(operands) == 2:
                read_only = True
                option_destinations = [operands[1]]
            else:
                return False, [], [], "xxd requires exactly one input and at most one output"
        if executable == "git":
            operands = _plain_operands(words[1:])
            operation = operands[0].lower() if operands else ""
            read_only = operation in _GIT_READ_ONLY
            if operation == "diff":
                option_destinations, option_error = _option_destinations(
                    words[2:], "--output")
                if option_error:
                    return False, [], [], option_error
            if operation == "branch":
                read_only = any(word in {"--list", "--show-current"} for word in words[1:])
            elif operation in {"tag", "worktree"}:
                read_only = "--list" in words[1:] or "list" in operands[1:]
        destinations.extend(str(
            _named_path(item, effective_cwd).resolve(strict=False))
            for item in option_destinations)
        if read_only:
            for item in redirects:
                item = item.strip("\"'")
                if re.fullmatch(r"&?\d+", item) or _is_null_sink(item):
                    continue
                destinations.append(str(
                    _named_path(item, effective_cwd).resolve(strict=False)))
            continue

        # Anything not proven read-only is treated as writing. It is NOT refused
        # for being unrecognised -- that was the rule that killed runs on `od`,
        # `pytest` and `awk`. Every path it names becomes a destination and is
        # bounds-checked by the caller, so an unknown command is free to do as it
        # likes inside the worktree and cannot touch anything outside it.
        for item in redirects:
            item = item.strip("\"'")
            if re.fullmatch(r"&?\d+", item) or _is_null_sink(item):
                continue
            destinations.append(str(
                _named_path(item, effective_cwd).resolve(strict=False)))

        operands = _plain_operands(words[1:])
        if executable in _PROGRAM_FIRST_OPERAND and operands:
            operands = operands[1:]
        for item in operands:
            if not _looks_like_path(item):
                continue
            # A glob cannot be resolved to one target, so bound the directory it
            # expands within: `rm build/*.o` is confined by `build/`.
            literal = _BASH_GLOB_RE.split(item)[0] if _BASH_GLOB_RE.search(item) else item
            if not literal:
                continue
            destinations.append(str(
                _named_path(literal, effective_cwd).resolve(strict=False)))

    if destinations:
        return False, destinations, references, None
    return True, [], references, None


def _bash_policy(command: str, work_root=".") -> tuple[bool, list[str], Optional[str]]:
    read_only, destinations, _references, error = _analyze_bash(command, work_root)
    return read_only, destinations, error


def _bash_is_read_only(command: str, work_root=".") -> bool:
    return _bash_policy(command, work_root)[0]


def guard_tool_paths(record: dict, work_root, read_roots=(), write_roots=()) -> Optional[str]:
    """Fail fast on a tool call outside this run's explicitly allowed roots.

    The primary root is normally ``run.work_root`` and the only extra root is
    the canonical checkout for harness/session reads. The worktree parent is
    intentionally never granted, because it contains other runs.

    Called per tool call, before its result is even used, so a scout or
    planner naming a sibling repo in a `read`/`bash` call aborts the run right
    there — instead of quietly recording where that repo lives and a later
    phase treating it as fair game.
    """
    work_root = _named_path(str(work_root), work_root)
    writable_roots = tuple(_named_path(str(root), work_root) for root in write_roots)
    readable_roots = (work_root, *read_roots, *writable_roots)
    tool = str(record.get("tool") or "")
    read_only = tool.lower() in _READ_ONLY_TOOLS
    args = record.get("args") or {}

    candidates = [value for key in _PATH_ARG_KEYS
                  if isinstance((value := args.get(key)), str)]
    destinations: list[str] = []
    command = args.get("command")
    if isinstance(command, str):
        read_only, destinations, references, policy_error = _analyze_bash(
            command, work_root, readable_roots)
        if policy_error:
            return f"{tool} blocked: {policy_error}"
        candidates.extend(references)
        candidates.extend(destinations)

    for value in candidates:
        is_destination = value in destinations
        mutating_path_tool = not read_only and tool.lower() != "bash"
        is_write = is_destination or mutating_path_tool
        # Mutators match only actual write grants. In particular, do not search
        # the broader readable canonical checkout first: it would shadow a
        # nested grant such as this session's exact context_handoff directory.
        roots = (work_root, *writable_roots) if is_write else readable_roots
        matched = (_destination_root(value, work_root, roots)
                   if is_write
                   else _root_for(value, work_root, roots))
        if matched is None:
            readable_match = _root_for(value, work_root, readable_roots)
            if is_write and readable_match is not None:
                return f"{tool} may read but cannot write the canonical checkout: {value}"
            location = "destination is outside this run's worktree" if is_destination \
                else "referenced a path outside this run's roots"
            return f"{tool} {location}: {value}"
    return None


@dataclass(frozen=True)
class FileState:
    """Hash/metadata kept in memory; baseline bytes live only on bounded disk."""

    kind: str
    mode: int
    size: int = 0
    digest: str = ""
    link_target: str | None = None
    backup: Path | None = field(default=None, compare=False)
    git_oid: str | None = field(default=None, compare=False)


class TreeSnapshot(dict[str, FileState]):
    def __init__(self, *args, backup_dir: Path | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.backup_dir = backup_dir

    def close(self) -> None:
        if self.backup_dir is not None:
            shutil.rmtree(self.backup_dir, ignore_errors=True)
            self.backup_dir = None

    def __del__(self) -> None:
        self.close()


class SafetySnapshot(dict[Path, TreeSnapshot]):
    def close(self) -> None:
        for tree in self.values():
            tree.close()


@dataclass
class SnapshotBudget:
    max_files: int
    max_bytes: int
    max_worktrees: int
    max_seconds: float
    started: float = 0.0
    files: int = 0
    bytes: int = 0

    def __post_init__(self) -> None:
        self.started = time.monotonic()

    def consume(self, *, files: int = 0, bytes_: int = 0) -> None:
        self.files += files
        self.bytes += bytes_
        elapsed = time.monotonic() - self.started
        if self.files > self.max_files:
            raise SnapshotLimitExceeded(
                f"permission snapshot file limit exceeded ({self.files}>{self.max_files})")
        if self.bytes > self.max_bytes:
            raise SnapshotLimitExceeded(
                f"permission snapshot byte limit exceeded ({self.bytes}>{self.max_bytes})")
        if elapsed > self.max_seconds:
            raise SnapshotLimitExceeded(
                f"permission snapshot time limit exceeded ({elapsed:.3f}s>{self.max_seconds}s)")


def _budget(run) -> SnapshotBudget:
    limits = run.cfg.defaults.permission_guard
    return SnapshotBudget(limits.max_files, limits.max_bytes,
                          limits.max_worktrees, limits.max_seconds)


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def validate_data_dir(repo_root: Path, configured: str | Path) -> Path:
    """Require runtime state to be disjoint from tracked/source checkout state."""
    canonical = Path(repo_root).resolve()
    configured_path = Path(configured).expanduser()
    data_dir = (configured_path.resolve() if configured_path.is_absolute()
                else (canonical / configured_path).resolve())
    if data_dir == canonical:
        raise ValueError("defaults.data_dir cannot be the canonical checkout")
    if data_dir in canonical.parents:
        raise ValueError("defaults.data_dir cannot contain the canonical checkout")
    if data_dir.exists() and not data_dir.is_dir():
        raise ValueError("defaults.data_dir must be a dedicated directory")
    try:
        relative = data_dir.relative_to(canonical)
    except ValueError:
        # A disjoint external runtime remains valid, but is outside every
        # checkout snapshot and therefore creates no snapshot exclusion.
        return data_dir
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", relative.as_posix()], cwd=canonical,
        capture_output=True,
    )
    if result.returncode != 0:
        raise ValueError(f"cannot validate defaults.data_dir with git: {result.stderr!r}")
    if result.stdout:
        first = result.stdout.split(b"\0", 1)[0].decode(errors="replace")
        raise ValueError(
            f"defaults.data_dir overlaps tracked/source files (for example {first!r})")
    return data_dir


def runtime_exclusions(run) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Exclude a proven runtime root and exact untracked trace DB artifacts.

    Validation deliberately precedes the broad root exclusion. A checkout,
    ancestor, file, or subtree containing any tracked path fails closed rather
    than turning a broad data_dir setting into a source-monitoring bypass.
    """
    canonical = Path(run.repo_root).resolve()
    data_dir = validate_data_dir(canonical, run.data_dir)
    session_dir = Path(getattr(
        run, "session_dir", data_dir / "sessions" / getattr(run, "adw_id", "guard-test")
    )).resolve()
    if data_dir != session_dir and data_dir not in session_dir.parents:
        raise ValueError("run.session_dir is outside the validated defaults.data_dir")
    roots = (data_dir,) if canonical in data_dir.parents else ()
    db = Path(run.observability_db).resolve()
    if canonical == db or canonical in db.parents:
        relative_db = db.relative_to(canonical)
        tracked = subprocess.run(
            ["git", "ls-files", "-z", "--", relative_db.as_posix()], cwd=canonical,
            capture_output=True,
        )
        if tracked.returncode != 0 or tracked.stdout:
            raise ValueError("observability.db must be an untracked runtime artifact")
    exact = tuple(
        path for path in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm"))
        if canonical == path or canonical in path.parents
    )
    return roots, exact


def _guard_lock_path(run) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=run.repo_root, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise PermissionBreach("cannot locate shared git directory for permission guard")
    common = Path(result.stdout.strip()).resolve()
    common.mkdir(parents=True, exist_ok=True)
    return common / "sssf-agent-execution.lock"


@contextmanager
def execution_guard(run):
    """Serialize every factory agent against canonical/sibling monitoring."""
    lock_path = _guard_lock_path(run)
    handle = lock_path.open("a+b")
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    deadline = time.monotonic() + run.cfg.defaults.permission_guard.lock_timeout_seconds
    acquired = False
    try:
        while not acquired:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as error:
                if time.monotonic() >= deadline:
                    raise PermissionBreach(
                        "timed out waiting for the cross-process agent execution guard; "
                        "no agent was started and no rollback was attempted") from error
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _head_blobs(root: Path) -> dict[str, str]:
    """Map files whose exact HEAD blob can be used as an immutable backup."""
    result = subprocess.run(
        ["git", "ls-tree", "-r", "-z", "HEAD"], cwd=root, capture_output=True,
    )
    if result.returncode != 0:
        return {}
    blobs: dict[str, str] = {}
    for record in result.stdout.split(b"\0"):
        if not record or b"\t" not in record:
            continue
        header, raw_path = record.split(b"\t", 1)
        fields = header.split()
        if len(fields) == 3 and fields[1] == b"blob":
            blobs[raw_path.decode(errors="surrogateescape")] = fields[2].decode("ascii")
    return blobs


_REGENERABLE_CACHE_ROOTS = {"node_modules", ".ruff_cache", ".mypy_cache"}


def _is_safe_regenerable_cache_root(relative: str, tracked: set[str]) -> bool:
    """Exclude only conventional cache roots containing no tracked project path."""
    path = Path(relative)
    if path.name not in _REGENERABLE_CACHE_ROOTS:
        return False
    prefix = relative.rstrip("/") + "/"
    return relative not in tracked and not any(item.startswith(prefix) for item in tracked)


def _is_regenerable_cache_artifact(relative: str) -> bool:
    """Recognize only cache files whose names cannot conceal project source.

    Python and pytest cache directories are not excluded wholesale: an
    unexpected ``.py`` or other operator-created file inside one remains
    protected by the snapshot.
    """
    parts = Path(relative).parts
    if "__pycache__" in parts and Path(relative).suffix.casefold() in {".pyc", ".pyo"}:
        return True
    if ".pytest_cache" in parts:
        cache_relative = parts[parts.index(".pytest_cache") + 1:]
        return cache_relative in {
            (".gitignore",), ("CACHEDIR.TAG",), ("README.md",),
            ("v", "cache", "lastfailed"), ("v", "cache", "nodeids"),
            ("v", "cache", "stepwise"),
        }
    return False


_EPHEMERAL_FIXTURE_ROOT = "tests"
_EPHEMERAL_FIXTURE_PREFIX = "_external_cwd_"


def _is_ephemeral_test_fixture(relative: str, tracked: set[str]) -> bool:
    """Recognize the external-cwd suite's scratch fixture in the real checkout.

    ``tests/test_external_cwd_entrypoint.py`` must build its fixture under the
    canonical root -- that is the whole point of the test, since it proves the
    CLI resolves paths from the repository root and not from the launch
    directory. The fixture is created and removed inside the test, so an agent
    phase that runs the suite sees fifteen files appear and vanish through no
    act of its own, and the guard attributes the deletion to the agent.

    The exclusion is deliberately narrow: one fixed parent, one fixed name
    prefix, a hex suffix, and no tracked path underneath. Anything an operator
    put in ``tests/`` stays protected.
    """
    parts = Path(relative).parts
    if len(parts) < 2 or parts[0] != _EPHEMERAL_FIXTURE_ROOT:
        return False
    name = parts[1]
    suffix = name[len(_EPHEMERAL_FIXTURE_PREFIX):]
    if not name.startswith(_EPHEMERAL_FIXTURE_PREFIX) or not suffix:
        return False
    if any(char not in "0123456789abcdefABCDEF" for char in suffix):
        return False
    fixture = f"{_EPHEMERAL_FIXTURE_ROOT}/{name}"
    prefix = fixture + "/"
    return fixture not in tracked and not any(
        item.startswith(prefix) for item in tracked)


_REGENERABLE_CACHE_DIRS = {"__pycache__", ".pytest_cache"}


def _is_regenerable_cache_dir(relative: str) -> bool:
    """Recognize a directory entry that exists only as a build artifact.

    Running the test suite creates and removes these directories incidentally,
    so recording the entry itself turns a bytecode cache into a breach. Only
    the directory entry is dropped — the walk still descends, so any file
    inside that is not itself regenerable stays protected.
    """
    return bool(set(Path(relative).parts) & _REGENERABLE_CACHE_DIRS)


def snapshot_root(root, *, excluded_roots=(), excluded_paths=(),
                  budget: SnapshotBudget | None = None,
                  retain_backup: bool = True) -> TreeSnapshot:
    """Hash all non-runtime entries; optionally spool baseline bytes to disk."""
    root = Path(root).resolve()
    budget = budget or SnapshotBudget(100_000, 1_073_741_824, 32, 120.0)
    excluded = {_path_key(Path(path)) for path in excluded_roots}
    excluded_exact = {_path_key(Path(path)) for path in excluded_paths}
    backup_dir = (Path(tempfile.mkdtemp(prefix="sssf-guard-"))
                  if retain_backup else None)
    states = TreeSnapshot(backup_dir=backup_dir)
    head_blobs = _head_blobs(root)
    tracked_result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True,
    )
    # If Git cannot prove a cache root is disjoint from tracked source, no
    # broad cache exclusion is admitted; the complete tree is scanned instead.
    tracked = ({item.decode(errors="surrogateescape")
                for item in tracked_result.stdout.split(b"\0") if item}
               if tracked_result.returncode == 0 else {"."})

    def visit(directory: Path) -> None:
        budget.consume()
        try:
            entries = list(os.scandir(directory))
        except FileNotFoundError as error:
            raise SnapshotLimitExceeded(
                f"permission snapshot changed while scanning {directory}") from error
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            if (relative == ".git" or relative.startswith(".git/") or
                    _is_regenerable_cache_artifact(relative) or
                    _is_ephemeral_test_fixture(relative, tracked) or
                    _is_safe_regenerable_cache_root(relative, tracked)):
                continue
            key = _path_key(path)
            if key in excluded_exact or any(
                    key == item or key.startswith(item + os.sep) for item in excluded):
                continue
            try:
                metadata = entry.stat(follow_symlinks=False)
                mode = stat.S_IMODE(metadata.st_mode)
                budget.consume(files=1)
                is_junction = bool(getattr(path, "is_junction", lambda: False)())
                if is_junction or entry.is_symlink():
                    target = os.readlink(path)
                    kind = "junction" if is_junction else "symlink"
                    states[relative] = FileState(
                        kind, mode, len(target.encode()),
                        hashlib.sha256(target.encode()).hexdigest(), target)
                elif entry.is_dir(follow_symlinks=False):
                    if not _is_regenerable_cache_dir(relative):
                        states[relative] = FileState("directory", mode)
                    visit(path)
                elif entry.is_file(follow_symlinks=False):
                    # A file that moves under the read yields a torn baseline,
                    # which cannot safely be attributed or restored -- so it is
                    # never blessed. But a file that moved ONCE is usually an
                    # incidental writer (a concurrent run's trace db, a log),
                    # not an unstable tree, and failing the whole snapshot on
                    # first sight of one made any concurrent ADW fatal. Re-read
                    # instead, and fail only if it will not hold still.
                    for attempt in range(_SNAPSHOT_READ_ATTEMPTS):
                        size = metadata.st_size
                        budget.consume(bytes_=size)
                        backup = backup_dir / relative if backup_dir else None
                        if backup:
                            backup.parent.mkdir(parents=True, exist_ok=True)
                        digest = hashlib.sha256()
                        expected_oid = head_blobs.get(relative)
                        git_digest = None
                        if expected_oid:
                            algorithm = "sha256" if len(expected_oid) == 64 else "sha1"
                            git_digest = hashlib.new(algorithm)
                            git_digest.update(f"blob {size}\0".encode())
                        with path.open("rb") as source:
                            destination = backup.open("wb") if backup else None
                            try:
                                while chunk := source.read(1024 * 1024):
                                    digest.update(chunk)
                                    if git_digest:
                                        git_digest.update(chunk)
                                    if destination:
                                        destination.write(chunk)
                                    budget.consume()
                            finally:
                                if destination:
                                    destination.close()
                        final = path.stat(follow_symlinks=False)
                        if (final.st_size == size and
                                final.st_mtime_ns == metadata.st_mtime_ns):
                            break
                        # Re-read against what the file looks like NOW.
                        metadata = final
                    else:
                        raise SnapshotLimitExceeded(
                            f"permission snapshot changed while reading {path}")
                    git_oid = (expected_oid if git_digest and
                               git_digest.hexdigest() == expected_oid else None)
                    if git_oid and backup:
                        backup.unlink()
                        backup = None
                    states[relative] = FileState(
                        "file", mode, size, digest.hexdigest(), backup=backup,
                        git_oid=git_oid)
                else:
                    states[relative] = FileState("other", mode)
            except FileNotFoundError as error:
                raise SnapshotLimitExceeded(
                    f"permission snapshot changed while scanning {path}") from error
    try:
        visit(root)
        return states
    except BaseException:
        states.close()
        raise


def _register_snapshot(run, item) -> None:
    registry = getattr(run, "_permission_snapshots", None)
    if registry is not None:
        registry.append(item)


def snapshot(run) -> TreeSnapshot:
    """Capture the active worktree under the configured shared hard bounds."""
    kwargs = {}
    if _path_key(Path(run.work_root)) == _path_key(Path(run.repo_root)):
        roots, paths = runtime_exclusions(run)
        kwargs = {"excluded_roots": roots, "excluded_paths": paths}
    result = snapshot_root(run.work_root, budget=_budget(run), **kwargs)
    _register_snapshot(run, result)
    return result


def safety_snapshot(run) -> SafetySnapshot:
    """Capture canonical and siblings while the cross-process guard is held."""
    from .worktree import registered_worktrees

    canonical = Path(run.repo_root).resolve()
    own = _path_key(Path(run.work_root))
    roots = {canonical, *(item.path for item in registered_worktrees(repo_root=canonical))}
    budget = _budget(run)
    if len(roots) > budget.max_worktrees:
        raise SnapshotLimitExceeded(
            f"permission snapshot worktree limit exceeded ({len(roots)}>{budget.max_worktrees})")
    excluded_roots, excluded_paths = runtime_exclusions(run)
    snapshots = SafetySnapshot()
    try:
        for root in sorted(roots, key=lambda item: str(item).casefold()):
            if _path_key(root) == own:
                continue
            kwargs = ({"excluded_roots": excluded_roots,
                       "excluded_paths": excluded_paths}
                      if _path_key(root) == _path_key(canonical) else {})
            snapshots[root] = snapshot_root(root, budget=budget, **kwargs)
        _register_snapshot(run, snapshots)
        return snapshots
    except BaseException:
        snapshots.close()
        raise


def changed_paths(before: dict[str, FileState], after: dict[str, FileState]) -> list[str]:
    """Every path whose state differs — appeared, vanished, or was rewritten."""
    return sorted({p for p in set(before) | set(after)
                   if before.get(p) != after.get(p)})


def _glob(pattern: str) -> re.Pattern:
    """Translate a pattern, with `*` stopping at a path separator.

    fnmatch would let `*` cross `/`, which quietly widens every pattern:
    `adws/adw_*.py` would match `adws/adw_data/sessions/x/y.py` as well as the
    ADW scripts it means. `**` is the way to say "cross directories".
    """
    out, i = [], 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("".join(out))


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):                      # directory prefix
        return path == pattern.rstrip("/") or path.startswith(pattern)
    if "*" in pattern or "?" in pattern:
        return _glob(pattern).fullmatch(path) is not None
    return path == pattern


def permitted(path: str, agent: AgentConfig, cfg: SSSFConfig) -> bool:
    """Apply only explicit source-write grants; runtime is outside the worktree."""
    if any(_matches(path, p) for p in (agent.writes or [])):
        return True                      # naming a path is what unlocks a protected one
    if any(_matches(path, p) for p in cfg.defaults.protected_files):
        return False
    return agent.writes is None          # None = unrestricted, [] = no repo writes


def _remove_entry(path: Path) -> None:
    if bool(getattr(path, "is_junction", lambda: False)()):
        path.rmdir()
    elif path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _restore_paths(root: Path, paths: list[str], before: dict[str, FileState],
                   after: dict[str, FileState]) -> dict[str, str]:
    """Restore selected paths byte-for-byte and type-for-type from ``before``."""
    errors: dict[str, list[str]] = {}

    def failed(relative: str, error: OSError) -> None:
        errors.setdefault(relative, []).append(str(error))

    # Remove additions and type substitutions deepest-first. A failure on one
    # path must not prevent independent dirty files from being recovered.
    for relative in sorted(paths, key=lambda item: (item.count("/"), item), reverse=True):
        target = root / relative
        prior, current = before.get(relative), after.get(relative)
        if current is not None and (prior is None or prior.kind != current.kind):
            try:
                _remove_entry(target)
            except OSError as error:
                failed(relative, error)

    # Parents before children; ordinary same-type entries are overwritten.
    baseline = [(relative, before[relative]) for relative in paths if relative in before]
    for relative, prior in sorted(baseline, key=lambda item: item[0].count("/")):
        target = root / relative
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if prior.kind == "directory":
                target.mkdir(exist_ok=True)
            elif prior.kind == "file":
                if prior.git_oid:
                    with target.open("wb") as destination:
                        restored = subprocess.run(
                            ["git", "cat-file", "blob", prior.git_oid], cwd=root,
                            stdout=destination, stderr=subprocess.PIPE,
                        )
                    if restored.returncode != 0:
                        raise OSError(
                            f"git baseline blob unavailable for {relative!r}: "
                            f"{restored.stderr.decode(errors='replace')}")
                elif prior.backup is not None and prior.backup.is_file():
                    shutil.copyfile(prior.backup, target)
                else:
                    raise OSError(f"bounded baseline backup unavailable for {relative!r}")
            elif prior.kind == "symlink":
                if target.is_symlink() or target.exists():
                    _remove_entry(target)
                os.symlink(str(prior.link_target), target,
                           target_is_directory=(target.parent / str(prior.link_target)).is_dir())
            elif prior.kind == "junction" and os.name == "nt":
                if target.exists() or bool(getattr(target, "is_junction", lambda: False)()):
                    _remove_entry(target)
                import _winapi
                _winapi.CreateJunction(str(prior.link_target), str(target))
            else:
                raise OSError(f"cannot recreate special filesystem entry {relative!r}")
        except OSError as error:
            failed(relative, error)

    # Apply modes last so restrictive directory modes cannot block restore.
    for relative, prior in sorted(baseline, key=lambda item: item[0].count("/"), reverse=True):
        if prior.kind in {"symlink", "junction"}:
            continue
        try:
            os.chmod(root / relative, prior.mode)
        except OSError as error:
            failed(relative, error)

    return {
        relative: (f"could not restore ({'; '.join(errors[relative])})"
                   if relative in errors
                   else "restored exactly" if relative in before else "deleted")
        for relative in paths
    }


def _roll_back(run, paths: list[str], before: dict[str, FileState],
               after: dict[str, FileState]) -> dict[str, str]:
    return _restore_paths(Path(run.work_root), paths, before, after)


def enforce_safety(run, before: SafetySnapshot) -> None:
    """Reject and undo attributable writes while the shared guard is held."""
    breaches: list[tuple[Path, str, str]] = []
    canonical = Path(run.repo_root).resolve()
    excluded_roots, excluded_paths = runtime_exclusions(run)
    comparison_budget = _budget(run)
    try:
        for root, prior in before.items():
            kwargs = ({"excluded_roots": excluded_roots,
                       "excluded_paths": excluded_paths}
                      if _path_key(root) == _path_key(canonical) else {})
            # The comparison needs only metadata/hashes, never a second backup.
            after = snapshot_root(root, budget=comparison_budget, retain_backup=False, **kwargs)
            try:
                changed = changed_paths(prior, after)
                outcomes = _restore_paths(root, changed, prior, after) if changed else {}
                breaches.extend((root, path, outcomes[path]) for path in changed)
            finally:
                after.close()
        if breaches:
            detail = "\n".join(
                f"  - {root / path} — {outcome}" for root, path, outcome in breaches
            )
            raise PermissionBreach(
                "agent modified canonical or sibling checkout state:\n" + detail
            )
    finally:
        before.close()


def enforce(run, phase, agent: AgentConfig, before: TreeSnapshot) -> list[str]:
    """Compare the tree against `before`; undo and raise if the agent overstepped.

    Returns the paths it legitimately changed, so the trace records what an
    agent actually touched rather than only what it claimed in its envelope.

    Detection alone would leave the repo holding the unauthorized change while
    reporting a failure, so anything the agent introduced outside its allowlist
    is rolled back before the phase dies. What it cannot undo, it names.
    """
    kwargs = {}
    if _path_key(Path(run.work_root)) == _path_key(Path(run.repo_root)):
        roots, paths = runtime_exclusions(run)
        kwargs = {"excluded_roots": roots, "excluded_paths": paths}
    after = snapshot_root(
        run.work_root, budget=_budget(run), retain_backup=False, **kwargs)
    try:
        touched = changed_paths(before, after)
        breaches = [p for p in touched if not permitted(p, agent, run.cfg)]
        if not breaches:
            return touched

        outcomes = _roll_back(run, breaches, before, after)
        scope = ("read-only" if agent.writes == []
                 else f"limited to {agent.writes}" if agent.writes
                 else f"barred from {run.cfg.defaults.protected_files}")
        detail = "\n".join(f"  - {p} — {outcome}" for p, outcome in outcomes.items())
        raise PermissionBreach(
            f"{agent.name} is {scope} but modified {len(breaches)} path(s):\n{detail}")
    finally:
        after.close()
        before.close()
