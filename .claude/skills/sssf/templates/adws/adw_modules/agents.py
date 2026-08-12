"""Config loading/validation and agent execution.

Every ADW validates its agents before running (fail fast, nothing spawns
against a half-valid config). Every agent call parses against a concrete
output type; parse failures and gate violations re-prompt the SAME session
with a correction — context intact, bounded retries. Agent proposes, code
disposes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import yaml

from . import agent_pi, permissions, prompts
from .data_types import (AgentCall, AgentConfig, EnvelopeBase, EventRecord,
                         GateCheck, GateReport, Phase, PiRequest, SSSFConfig,
                         UsageBreakdown)
from .utils import new_id

JSON_FIX_ATTEMPTS = 2      # continue-with-correction attempts for malformed JSON


class GateFailure(RuntimeError):
    pass


# ── config ───────────────────────────────────────────────────────────────────

def _rooted_path(repo_root: Path, configured: str | Path) -> Path:
    path = Path(configured).expanduser()
    return path.resolve() if path.is_absolute() else (repo_root / path).resolve()


def load_config(path: str = "adws/adw_sssf_config/sssf.config.yaml", *,
                repo_root: Path) -> SSSFConfig:
    config_path = _rooted_path(repo_root, path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    defaults = raw.get("defaults", {}) or {}
    for agent in raw.get("agents", []) or []:
        for key in ("coding_agent", "model", "thinking", "color", "tools", "writes"):
            if key in defaults:
                agent.setdefault(key, defaults[key])
        agent.setdefault("harness_engineering", defaults.get("harness_engineering", []))
    cfg = SSSFConfig(**raw)
    permissions.validate_data_dir(repo_root, cfg.defaults.data_dir)
    # Pi later changes cwd to run.work_root. Canonical harness paths stay
    # absolute so config validation and execution still read the same files.
    for agent in cfg.agents:
        agent.prompt_engineering.system = str(
            _rooted_path(repo_root, agent.prompt_engineering.system))
        agent.prompt_engineering.user = str(
            _rooted_path(repo_root, agent.prompt_engineering.user))
        agent.harness_engineering = [
            str(_rooted_path(repo_root, extension))
            for extension in agent.harness_engineering
        ]
    return cfg


def resolve(cfg: SSSFConfig, name: str) -> AgentConfig:
    for agent in cfg.agents:
        if agent.name == name:
            return agent
    raise SystemExit(f"agent {name!r} is not defined in the config — "
                     f"available: {[a.name for a in cfg.agents]}")


def validate(cfg: SSSFConfig, required: list[str]) -> None:
    """Fail fast: every required name must resolve to a usable agent."""
    problems = []
    for name in required:
        try:
            agent = resolve(cfg, name)
        except SystemExit as e:
            problems.append(str(e))
            continue
        if agent.coding_agent != "pi":
            problems.append(f"agent {name!r}: coding_agent {agent.coding_agent!r} "
                            f"is not implemented in v1 (pi only)")
        for label, ref in (("system", agent.prompt_engineering.system),
                           ("user", agent.prompt_engineering.user)):
            if not Path(ref).is_file():
                problems.append(f"agent {name!r}: {label} prompt not found: {ref}")
        try:
            agent_pi.resolve_model(agent.model)
        except ValueError as e:
            problems.append(f"agent {name!r}: {e}")
    if problems:
        raise SystemExit("config validation failed:\n- " + "\n- ".join(problems))


# ── execution ────────────────────────────────────────────────────────────────

def execute(run, phase: Phase, call: AgentCall) -> EnvelopeBase:
    """Run one agent while holding the repository-wide attribution boundary."""
    with permissions.execution_guard(run):
        snapshots = []
        run._permission_snapshots = snapshots
        try:
            return _execute_guarded(run, phase, call)
        finally:
            for item in snapshots:
                item.close()
            del run._permission_snapshots


# A refused tool call is a correction, not a fatal error. Bounded so an agent
# that keeps probing the boundary still fails the run rather than looping. Four,
# not two: a builder legitimately meets several distinct refusals in one phase
# (an interpreter, then a heredoc, then an unlisted reader), and two corrections
# ran out mid-task on work that was otherwise going fine.
_MAX_REFUSAL_CORRECTIONS = 4


def _execute_guarded(run, phase: Phase, call: AgentCall) -> EnvelopeBase:
    """One agent call: render prompts -> pi run -> typed parse -> gates -> envelope."""
    agent = resolve(run.cfg, phase.params.owner)
    agent_dir = run.session_dir / agent.name
    agent_dir.mkdir(parents=True, exist_ok=True)

    variables = {
        "prompt": call.prompt,
        "previous_envelope": call.previous.model_dump_json(indent=2) if call.previous else "(none)",
        "context_handoff_dir": str(run.context_handoff_dir),
        # This is also Pi's cwd below. Rendering the exact checkout path keeps
        # agents from falling back to a canonical path remembered by a reused
        # model session.
        "work_root": str(run.work_root),
    }
    system_text = prompts.render(agent.prompt_engineering.system, variables)
    user_text = prompts.render(agent.prompt_engineering.user, variables)
    prompts.save(agent_dir / "prompts", "system.md", system_text)
    prompts.save(agent_dir / "prompts", "user.md", user_text)

    session_id = _agent_session_id(run, agent)
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="agent_start", name=agent.name,
                                 payload={"model": agent.model, "thinking": agent.thinking,
                                          "color": agent.color,
                                          "session_id": session_id,
                                          "coding_agent": agent.coding_agent,
                                          "purpose": agent.purpose,
                                          "tools": agent.tools,  # None = all tools
                                          "harness_engineering": agent.harness_engineering}))
    run.console.agent_started(agent.name, agent.model, session_id)

    # Parse retries and gate corrections re-enter the SAME pi session, so the
    # last send is the one whose context occupancy is current — while spend is
    # the opposite: every send costs, so usage accumulates across all of them.
    latest: agent_pi.PiResult | None = None
    spent = UsageBreakdown()
    def send(prompt_text: str) -> agent_pi.PiResult:
        nonlocal latest
        request = PiRequest(
            prompt=prompt_text,
            system_prompt=system_text,
            model=agent.model,
            thinking=agent.thinking,
            session_id=session_id,
            # Session artifacts stay canonical while pi edits the isolated work root.
            session_dir=str((agent_dir / "pi_sessions").resolve()),
            raw_output_path=str((agent_dir / "raw_output.jsonl").resolve()),
            tools=agent.tools,
            extensions=agent.harness_engineering,
            cwd=str(run.work_root),
        )
        result = agent_pi.run(
            request,
            on_event=_event_forwarder(run, phase, agent.name),
            on_spawn=lambda pid: run.tracer.process_start(
                run.adw_id, "agent", agent.name, pid,
                f"{agent.coding_agent} {agent.name} {agent.model}"),
            on_exit=lambda pid: run.tracer.process_end(run.adw_id, pid))
        run.add_usage(result.tokens, result.cost)
        spent.merge(result.usage)
        latest = result
        return result

    def send_correcting_refusals(prompt_text: str) -> agent_pi.PiResult:
        """Send, and treat a refused tool call as a correction, not a run failure.

        The guard can only stop a pi tool call by aborting the process, so a
        single refused probe used to end the whole run — the planner spent 88s
        of real work and lost all of it because it ran `command -v rtk`. The
        agent never learned it was refused, so a rerun made the same call.

        A refusal is the same shape of problem as a bad JSON parse or a failed
        claim gate, and gets the same treatment: re-enter the SAME pi session
        with the reason, so the agent keeps its context and picks another route.
        Exhausting the corrections re-raises, so a determined escape still fails
        the run.
        """
        text = prompt_text
        for attempt in range(1, _MAX_REFUSAL_CORRECTIONS + 2):
            try:
                return send(text)
            except permissions.PermissionBreach as breach:
                if attempt > _MAX_REFUSAL_CORRECTIONS:
                    raise
                run.tracer.event(EventRecord(
                    adw_id=run.adw_id, phase_id=phase.phase_id,
                    type="error", name="permission_breach_corrected",
                    payload={"agent": agent.name, "attempt": attempt,
                             "reason": str(breach)}))
                run.console.retry(agent.name, attempt, _MAX_REFUSAL_CORRECTIONS,
                                  f"tool call refused: {breach}")
                text = (
                    f"Your last tool call was refused by the path guard and did "
                    f"not run: {breach}\n\n"
                    f"This is a hard boundary, not a hint — retrying the same "
                    f"call fails the same way. Work inside {run.work_root}; you "
                    f"may read the canonical checkout at {run.repo_root}.\n\n"
                    f"The guard only allows shell commands whose effects it can "
                    f"prove. Interpreters (python, bash, sh, perl) and heredocs "
                    f"are always refused because their writes cannot be proven — "
                    f"use the write and edit tools for file changes instead of "
                    f"shelling out. To inspect binary data use od, hexdump, xxd, "
                    f"strings, file or base64. Prefer the read, grep, find and ls "
                    f"tools over shell equivalents.\n\n"
                    f"Pick a different approach and continue the task from where "
                    f"you stopped."
                )
        raise AssertionError("unreachable")  # pragma: no cover

    # What the tree looked like before this agent got its hands on it. Every
    # send in this phase — first prompt, JSON retries, gate corrections — is
    # measured against this one baseline.
    tree_before = permissions.snapshot(run)

    try:
        safety_before = permissions.safety_snapshot(run)
    except BaseException:
        tree_before.close()
        raise
    try:
        result = send_correcting_refusals(user_text)
        envelope, attempt = _parse_with_retries(
            run, phase, call, result, send_correcting_refusals)

        # claim gates — violations flow back into the SAME session as corrections
        for gate_attempt in range(1, max(1, phase.params.retries + 1) + 1):
            violations = []
            for gate in call.gates:
                report = _as_report(gate(envelope, run))
                found = report.violations
                run.tracer.gate_row(phase, gate.__name__, report, gate_attempt)
                run.tracer.event(EventRecord(
                    adw_id=run.adw_id, phase_id=phase.phase_id,
                    type="gate_fail" if found else "gate_pass", name=gate.__name__,
                    payload={"attempt": gate_attempt, "violations": found,
                             "checks": [c.model_dump() for c in report.checks]}))
                run.console.gate_result(gate.__name__, report)
                violations.extend(found)
            if not violations:
                break
            if gate_attempt > phase.params.retries:
                raise GateFailure(f"{agent.name} failed gates after {gate_attempt} attempt(s):\n- "
                                  + "\n- ".join(violations))
            phase.attempt = gate_attempt
            run.console.retry(agent.name, gate_attempt, phase.params.retries,
                              f"{len(violations)} gate violation(s)")
            correction = ("Your previous response failed validation:\n- "
                          + "\n- ".join(violations)
                          + "\n\nFix these problems, then re-emit ONLY your Report JSON.")
            result = send(correction)
            envelope, attempt = _parse_with_retries(run, phase, call, result, send)

        _persist_envelope(run, phase, agent.name, call, envelope, attempt, valid=True)
        run.console.envelope_summary(envelope)
        context = latest or result
        run.tracer.agent_session_row(run.adw_id, agent, session_id,
                                     context_tokens=context.context_tokens,
                                     context_window=context.context_window)
        run.save_agent_map(agent.name, {"session_id": session_id, "model": agent.model,
                                        "coding_agent": agent.coding_agent})
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                     type="handoff", name=agent.name,
                                     payload={"artifacts": envelope.artifacts,
                                              "summary": envelope.summary}))
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                     type="agent_end", name=agent.name,
                                     # Phase totals, not the last send's: a retried
                                     # phase paid for every attempt.
                                     tokens=spent.total_tokens,
                                     payload={"cost": spent.total_cost,
                                              "usage": spent.model_dump(),
                                              "context_tokens": context.context_tokens,
                                              "context_window": context.context_window}))
        run.console.agent_finished(agent.name, spent.total_tokens, spent.total_cost)
        if envelope.status != "success":
            raise RuntimeError(f"{agent.name} reported status={envelope.status!r}: {envelope.summary}")
        return envelope
    finally:
        _enforce_agent_boundaries(run, phase, agent, tree_before, safety_before)


# ── internals ────────────────────────────────────────────────────────────────

def _enforce_agent_boundaries(run, phase: Phase, agent: AgentConfig,
                              tree_before, safety_before) -> None:
    """Run both restorers; a body failure remains chained to any guard failure."""
    errors: list[Exception] = []
    touched: list[str] = []
    try:
        touched = permissions.enforce(run, phase, agent, tree_before)
    except Exception as error:
        errors.append(error)
    try:
        permissions.enforce_safety(run, safety_before)
    except Exception as error:
        errors.append(error)

    if errors:
        breach = errors[0] if len(errors) == 1 else ExceptionGroup(
            "multiple permission boundary checks failed", errors)
        try:
            run.tracer.event(EventRecord(
                adw_id=run.adw_id, phase_id=phase.phase_id,
                type="error", name="permission_breach",
                payload={"agent": agent.name, "error": str(breach),
                         "writes": agent.writes,
                         "protected_files": run.cfg.defaults.protected_files}))
        except Exception:
            # Observability must never prevent the second restore or replace the
            # security failure that explains what was changed.
            pass
        raise breach
    if touched:
        run.tracer.event(EventRecord(
            adw_id=run.adw_id, phase_id=phase.phase_id,
            type="log", name="paths_touched",
            payload={"agent": agent.name, "paths": touched}))


def _as_report(result) -> GateReport:
    """Accept a GateReport, or a legacy gate that returned a violations list."""
    if isinstance(result, GateReport):
        return result
    return GateReport(checks=[GateCheck(item=str(v), ok=False) for v in (result or [])])


def _agent_session_id(run, agent: AgentConfig) -> str:
    entry = run.agent_map.get(agent.name)
    if entry and entry.get("model") == agent.model:
        return entry["session_id"]           # rejoin the existing context window
    return f"sssf-{run.adw_id}-{agent.name}-{new_id(4)}"


def _event_forwarder(run, phase: Phase, agent_name: str):
    """One tool_call event per real tool call, with its exact args and result."""
    tracker = agent_pi.ToolCallTracker()

    def guard(record: dict) -> None:
        # Read tools may inspect canonical harness/session files. Mutating tools
        # are confined to this run's worktree; the parent and sibling trees are
        # never grants.
        context_handoff_dir = getattr(
            run,
            "context_handoff_dir",
            Path(run.data_dir) / "sessions" / run.adw_id / "context_handoff",
        )
        violation = permissions.guard_tool_paths(
            record,
            run.work_root,
            (str(run.repo_root),),
            (str(context_handoff_dir),),
        )
        if violation:
            raise permissions.PermissionBreach(f"{agent_name}: {violation}")

    def forward(event: dict) -> None:
        # Preflight at announcement/start, before waiting for tool completion.
        # The completed record is still guarded below and is the only one traced.
        if event.get("type") == "message_end":
            for block in event.get("message", {}).get("content", []) or []:
                if isinstance(block, dict) and block.get("type") == "toolCall":
                    guard({"tool": block.get("name"),
                           "args": block.get("arguments") or {}})
        elif event.get("type") == "tool_execution_start":
            guard({"tool": event.get("toolName"), "args": event.get("args") or {}})

        record = tracker.observe(event)
        if record is None:
            return
        guard(record)
        # The call's span rides the columns; duration_ms stays in the payload as
        # pi's own authoritative number.
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                     type="tool_call", name=record.pop("label"),
                                     started_at=record.pop("started_at", None),
                                     ended_at=record.pop("ended_at", None),
                                     payload={**record, "agent": agent_name}))
    return forward


def _extract_json(text: str) -> dict:
    candidate = text
    if "```" in text:
        for block in text.split("```")[1::2]:
            block = block.removeprefix("json").strip()
            if block.startswith("{"):
                candidate = block
                break
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in the response")
    return json.loads(candidate[start:end + 1])


def _parse_with_retries(run, phase: Phase, call: AgentCall, result, send):
    """Parse the final response against the declared output type; on failure,
    continue the SAME session with a correction (bounded)."""
    for attempt in range(1, JSON_FIX_ATTEMPTS + 2):
        try:
            payload = _extract_json(result.text)
            return call.output_type.model_validate(payload), attempt
        except Exception as error:
            _persist_envelope(run, phase, phase.params.owner, call, None, attempt,
                              valid=False, raw=result.text)
            if attempt > JSON_FIX_ATTEMPTS:
                raise RuntimeError(
                    f"{phase.params.owner} never produced valid "
                    f"{call.output_type.__name__} JSON: {error}") from error
            run.console.retry(phase.params.owner, attempt, JSON_FIX_ATTEMPTS,
                              f"invalid {call.output_type.__name__} JSON: {error}")
            fields = ", ".join(call.output_type.model_fields.keys())
            result = send(
                f"Your response was not valid JSON for the required structure "
                f"({error}). Respond again with ONLY a JSON object with these "
                f"fields: {fields}. No prose, no code fences.")


def _persist_envelope(run, phase: Phase, agent_name: str, call: AgentCall,
                      envelope: Optional[EnvelopeBase], attempt: int,
                      valid: bool, raw: str = "") -> None:
    payload_json = envelope.model_dump_json(indent=2) if envelope else json.dumps({"raw": raw[-2000:]})
    run.tracer.envelope_row(phase, agent_name, call.output_type.__name__,
                            payload_json, valid, attempt)
    if envelope:
        record = {"agent_name": agent_name, "purpose": resolve(run.cfg, agent_name).purpose,
                  "output_type": call.output_type.__name__, "attempt": attempt,
                  **envelope.model_dump()}
        (run.session_dir / agent_name / "envelope.json").write_text(json.dumps(record, indent=2))
