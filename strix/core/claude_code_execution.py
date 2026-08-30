"""Claude Code agent engine: drives one agent's turn loop via the real
``claude`` CLI (through ``claude-agent-sdk``), for STRIX_LLM=claude-code/<model>.

Every tool call against the scan target still executes through Strix's existing
implementations (bridged in via ``strix.agents.claude_code_tools``, including
the shell/filesystem tools the agents SDK's sandbox capabilities expose); this
module is responsible for the turn loop, lifecycle recovery, cost/turn
accounting, and translating the SDK's own error signals into the exceptions
``strix.core.runner`` and ``strix.core.agents.AgentCoordinator`` already know
how to handle.

Native tool scoping (what is actually enforced):

- Claude Code's own ``Bash``/``Read``/``Write``/``WebSearch`` stay available as
  auxiliary research aids, but they must never reach the scan target. That is
  enforced by ``ClaudeAgentOptions.sandbox`` (the CLI's real sandbox: bash runs
  isolated with an empty network allow-list, and cannot opt out), not by ``cwd``
  -- ``cwd`` is only a starting directory and jails nothing on its own.
- ``tools=`` restricts which built-ins exist at all (``allowed_tools`` only
  pre-approves them), and ``Bash`` is offered only on platforms whose CLI
  sandbox can contain it (macOS/Linux); elsewhere there is nothing to contain a
  native shell, so it is left out.
- A ``PreToolUse`` hook additionally denies native file reads/writes outside the
  agent's scratch directory. Belt-and-suspenders on top of the SDK sandbox, and
  the layer that still applies where the OS sandbox does not.
- ``WebSearch`` is unaffected by the network denial: it is served by Anthropic's
  API backend, not by a host network call from a sandboxed command.
- The session is isolated from the operator's own Claude Code configuration
  (``setting_sources=[]``, ``strict_mcp_config=True``): their hooks, permission
  rules and MCP servers must not follow a scan agent into a run.

Verified against the real CLI (0.2.148 on Windows): the session starts with only
``Read``/``Write``/``WebSearch`` plus the bridged ``mcp__strix__*`` tools, with
the ``strix`` MCP server as the only connection and ``cwd`` on the scratch
directory. The CLI reports the bash sandbox as unavailable on Windows, which is
why ``Bash`` is not offered there at all.

Parity with the default engine: lifecycle recovery (a turn that ends without
``finish_scan``/``agent_finish``/``respond_to_user``/``wait_for_agents`` is
nudged back into a tool call, bounded by the same recovery limit), interactive
parking/resuming on messages, mid-turn interruption, the interactive budget
pause, cost and turn accounting, and the multi-agent graph all behave the same
way here -- they are driven from the same coordinator and the same helpers in
``strix.core.execution``.

Known differences, none of which change a scan's outcome:

- Wind-down directives at the turn-budget warn bands are logged rather than
  injected into the conversation: the CLI owns the turn once a query starts, so
  there is no equivalent of the default engine's per-LLM-call hook.
- Context compaction, image budgeting and transient-error turn replay are the
  CLI's own responsibility on this engine, not Strix's.
- ``strix --resume`` restores the run's Strix state (agents, findings, queued
  messages) but starts a fresh CLI conversation; the default engine replays its
  SDK session instead. The fresh conversation opens with an explicit resume
  prompt (``_first_prompt``) carrying the agent's registered task and its queued
  messages, so it knows what it is resuming -- but the turn-by-turn history of
  the interrupted run is genuinely gone, and the agent has to rebuild context
  from the scan's persisted state (notes, todos, coverage, findings).

Quota-exceeded detection has two layers:

- Primary: a structured ``RateLimitEvent`` with ``rate_limit_info.status ==
  "rejected"`` -- the CLI's own authoritative signal, confirmed against the
  installed ``claude_agent_sdk`` package (see ``_internal/message_parser.py``,
  which builds it straight from the CLI's ``rate_limit_event`` JSON message).
  ``resets_at`` is assumed to be a Unix timestamp in seconds, consistent
  with the SDK docstring ("Unix timestamp when the rate limit window
  resets", no "ms" qualifier) and a ``resetsAtSeconds`` string spotted in
  the bundled CLI binary next to its "usage limit resets" copy.
- Fallback: free-text detection (``classify_quota_error``) against the final
  ``ResultMessage.result``, in case a CLI version ever terminates without
  emitting a ``RateLimitEvent`` first.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import gettempdir
from typing import TYPE_CHECKING, Any

from agents.exceptions import MaxTurnsExceeded
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    RateLimitEvent,
    SandboxSettings,
)

from strix.agents.claude_code_tools import bridged_tool_names, build_mcp_server
from strix.config.claude_code import SubscriptionQuotaExceededError, classify_quota_error
from strix.core.execution import (
    # Shared with the default engine on purpose: both engines nudge a
    # lifecycle-less turn with the same wording, bound it by the same recovery
    # limits, and settle an unrecoverable agent the same way.
    _INTERACTIVE_TOOL_RECOVERY_LIMIT,
    _MAX_IDLE_AUTO_RESUMES,
    _agent_status,
    _exhausted_recovery,
    _notify_parent_on_stall,
    _plain_waiting_timeout,
    _reserve_notice,
    notify_parent_on_terminal,
    tool_required_message,
)
from strix.core.hooks import (
    # Reused intentionally from strix.core.hooks despite the leading
    # underscore: both engines share one set of wind-down bands/directives
    # and the same sub-agent budget reserve fraction, so behavior (and any
    # future tuning) stays identical regardless of which engine is driving
    # the agent. See strix/core/hooks.py.
    _ROOT_DIRECTIVES,
    _SUBAGENT_BUDGET_RESERVE,
    _SUBAGENT_DIRECTIVES,
    _TURN_WARN_BANDS,
    BudgetExceededError,
    BudgetPausedError,
    SubagentBudgetReservedError,
    _crossed_stage,
)
from strix.report.state import get_global_report_state


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agents.memory import Session
    from agents.tool import Tool

    from strix.core.agents import AgentCoordinator, Status

logger = logging.getLogger(__name__)

_MCP_SERVER_NAME = "strix"
_MCP_TOOL_PREFIX = f"mcp__{_MCP_SERVER_NAME}__"
_SCRATCH_DIR_NAME = "strix-claude-code-scratch"

# Strix's prompts (and its lifecycle nudge) name tools bare -- `finish_scan`,
# `exec_command`, ... -- but an in-process MCP server can only expose them as
# `mcp__strix__<tool>`. Without this note the agent calls the name it was told
# and gets "No such tool available" (observed against the real CLI).
_TOOL_NAMING_NOTE = f"""

# Tool names on this engine

Every Strix tool reaches you through an in-process MCP server, so its real name
is `{_MCP_TOOL_PREFIX}<tool>` -- for example `{_MCP_TOOL_PREFIX}finish_scan`,
`{_MCP_TOOL_PREFIX}exec_command`, `{_MCP_TOOL_PREFIX}create_agent`. Wherever
these instructions name a tool without that prefix, add it: the unprefixed name
does not exist and calling it fails.
"""

# Whose JSON result stands in for the CLI's closing prose as ``final_output``,
# so ``strix.core.runner``'s completion check sees the same shape the default
# engine's ``_finish_tool_use_behavior`` forces.
_LIFECYCLE_FINISH_TOOLS = frozenset({"finish_scan", "agent_finish"})

# Sent to a root/sub-agent whose ``initial_input`` is empty -- ``strix --resume``,
# ``--auto-resume`` and sub-agent respawn all pass ``[]``, because the default
# engine replays its persisted SDK session instead. This engine starts a fresh
# CLI conversation, so it says what is being resumed rather than sending nothing.
_RESUME_PROMPT = (
    "You are resuming an interrupted Strix scan. This is a fresh conversation: none of "
    "your previous turn-by-turn history is available here, only the scan's persisted "
    "state. Before doing anything else, re-establish context from that state with your "
    "own tools (your notes, todos, coverage, recorded findings, and the agent graph), "
    "then continue from where the scan left off."
)

# Claude Code's own tools, kept as auxiliary research aids only.
_NATIVE_TOOLS: tuple[str, ...] = ("Read", "Write", "WebSearch")
# Platforms whose `claude` CLI can actually sandbox a bash command.
_SANDBOXED_BASH_PLATFORMS = ("darwin", "linux")
_NATIVE_PATH_ARGUMENTS = ("file_path", "path", "notebook_path")


@dataclass
class ClaudeCodeRunResult:
    """Duck-type match for the ``.final_output`` read on ``RunResultBase``
    in ``strix/core/runner.py`` -- the rest of ``run_strix_scan`` needs
    nothing else from this object."""

    final_output: str | None


def _open_client(*, options: ClaudeAgentOptions) -> ClaudeSDKClient:
    """Seam for tests: patch this to avoid spawning a real ``claude`` process."""
    return ClaudeSDKClient(options=options)


def _identity(text: str) -> str:
    return text


def _tool_name_rewriter(tools: Sequence[Tool]) -> Callable[[str], str]:
    """Rewrite bare Strix tool names in outgoing text to their bridged form.

    Strix's shared system prompt, its lifecycle nudge and its system mailbox
    notices all name tools bare (``finish_scan``, ``respond_to_user``, ...).
    That is correct for the default engine and wrong here, where a Strix tool
    only exists as ``mcp__strix__<tool>`` and the bare name answers "No such
    tool available". Rewriting on this engine's send path keeps the shared
    prompt-building code -- used by both engines -- untouched.

    ponytail: only names containing an underscore are rewritten. A single-word
    tool name (``think``, ``notes``) is also an ordinary English word, and
    prefixing every prose occurrence of it would corrupt the prompt; those stay
    covered by ``_TOOL_NAMING_NOTE``'s blanket instruction.
    """
    names = sorted(
        {name for tool in tools if "_" in (name := str(getattr(tool, "name", "") or ""))},
        key=len,
        reverse=True,
    )
    if not names:
        return _identity
    # No lookbehind needed: `mcp__strix__finish_scan` has no word boundary
    # before `finish_scan`, so an already-prefixed name cannot match.
    pattern = re.compile(rf"\b({'|'.join(re.escape(n) for n in names)})\b")
    return lambda text: pattern.sub(lambda m: _MCP_TOOL_PREFIX + m.group(1), text)


def _prompt_text(initial_input: Any) -> str:
    """Whatever ``run_agent_loop`` was handed, as CLI prompt text.

    A fresh run passes the task string; a spawned sub-agent passes
    ``child_initial_input``'s ``[{"role": "user", "content": ...}]`` list; a
    resume or respawn passes ``[]``.
    """
    if isinstance(initial_input, list | tuple):
        return _pending_prompt(initial_input)
    return str(initial_input or "").strip()


def _wind_down_directive(is_root: bool, stage: int) -> str:
    directives = _ROOT_DIRECTIVES if is_root else _SUBAGENT_DIRECTIVES
    return directives[stage]


def _quota_error_from_rate_limit(event: RateLimitEvent) -> SubscriptionQuotaExceededError | None:
    info = event.rate_limit_info
    if info.status != "rejected":
        return None
    reset_at = (
        datetime.fromtimestamp(info.resets_at, tz=UTC) if info.resets_at is not None else None
    )
    label = info.rate_limit_type or "usage"
    return SubscriptionQuotaExceededError(f"Claude Code {label} rate limit rejected", reset_at)


def _scratch_cwd(agent_id: str) -> Path:
    path = Path(gettempdir()) / _SCRATCH_DIR_NAME / agent_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _clear_scratch_cwd(agent_id: str) -> None:
    """Drop the agent's native-tool scratch directory when its session ends."""
    shutil.rmtree(Path(gettempdir()) / _SCRATCH_DIR_NAME / agent_id, ignore_errors=True)


def _native_tools() -> list[str]:
    if sys.platform.startswith(_SANDBOXED_BASH_PLATFORMS):
        return ["Bash", *_NATIVE_TOOLS]
    return list(_NATIVE_TOOLS)


def _sandbox_settings() -> SandboxSettings:
    """``ClaudeAgentOptions.sandbox`` value: native commands get no network.

    Shaped against the installed ``claude_agent_sdk``'s ``SandboxSettings``
    TypedDict. An empty ``allowedDomains`` leaves a sandboxed command with
    nothing it may reach, and ``allowUnsandboxedCommands: False`` means a
    command cannot opt out of the sandbox -- so the scan target is reachable
    only through Strix's own (proxied, logged, scope-checked) tools.
    """
    return {
        "enabled": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "network": {"allowedDomains": [], "allowAllUnixSockets": False},
    }


def _is_within(root: Path, candidate: str) -> bool:
    try:
        path = Path(candidate)
        resolved = (path if path.is_absolute() else root / path).resolve()
    except (OSError, ValueError):
        return False
    return resolved == root or resolved.is_relative_to(root)


def _scratch_path_guard(scratch: Path) -> Callable[..., Any]:
    """A ``PreToolUse`` hook denying native file access outside ``scratch``.

    Unlike ``can_use_tool``, a ``PreToolUse`` hook runs for every tool call,
    including ones already pre-approved through ``allowed_tools``.
    """
    root = scratch.resolve()

    async def guard(
        hook_input: dict[str, Any], _tool_use_id: str | None, _context: Any
    ) -> dict[str, Any]:
        tool_input = hook_input.get("tool_input") or {}
        for key in _NATIVE_PATH_ARGUMENTS:
            value = tool_input.get(key)
            if not isinstance(value, str) or not value or _is_within(root, value):
                continue
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"Native file access is limited to this agent's scratch directory "
                        f"({root}). Reach the scan target through Strix's own sandbox tools "
                        f"(exec_command, apply_patch, view_image) instead."
                    ),
                }
            }
        return {}

    return guard


def _build_options(
    *,
    tools: Sequence[Tool],
    instructions: str,
    model_slug: str,
    max_turns: int,
    agent_id: str,
    context: dict[str, Any],
    lifecycle_output: dict[str, str] | None = None,
) -> ClaudeAgentOptions:
    def _record(name: str, text: str) -> None:
        if lifecycle_output is not None and name in _LIFECYCLE_FINISH_TOOLS:
            lifecycle_output["last"] = text

    server = build_mcp_server(tools, context=context, name=_MCP_SERVER_NAME, on_result=_record)
    native = _native_tools()
    scratch = _scratch_cwd(agent_id)
    return ClaudeAgentOptions(
        system_prompt=_tool_name_rewriter(tools)(instructions) + _TOOL_NAMING_NOTE,
        model=model_slug,
        mcp_servers={_MCP_SERVER_NAME: server},
        # Isolation: without these the CLI loads the operator's own
        # ~/.claude settings -- their hooks, their permission rules (which can
        # widen what this agent may do) and their MCP servers, which a scan
        # agent must not be able to reach. Confirmed against the real CLI: an
        # unisolated session offered this machine's unrelated MCP tools.
        setting_sources=[],
        strict_mcp_config=True,
        tools=native,
        allowed_tools=[*bridged_tool_names(tools, server_name=_MCP_SERVER_NAME), *native],
        max_turns=max_turns,
        cwd=str(scratch),
        sandbox=_sandbox_settings(),
        hooks={
            "PreToolUse": [
                HookMatcher(matcher="Read|Write", hooks=[_scratch_path_guard(scratch)]),
            ]
        },
    )


class _InterruptHandle:
    """Maps the coordinator's stream cancellation onto the SDK's ``interrupt()``.

    ``AgentCoordinator.send`` cancels an interactive agent's attached run stream
    so a user message lands mid-turn. The Claude Code engine has no
    agents-SDK stream, so this stands in for one and interrupts the CLI turn
    instead; the loop then picks the queued message up on its next pass.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self._interrupt_task: asyncio.Task[None] | None = None

    def cancel(self, mode: str = "immediate") -> None:
        # "after_turn" is a request to stop once the turn ends, which the
        # loop's own status checks already honor.
        if mode != "immediate":
            return
        with contextlib.suppress(RuntimeError):
            # Held on the instance so the task is not garbage-collected mid-flight.
            self._interrupt_task = asyncio.get_running_loop().create_task(self._interrupt())

    async def _interrupt(self) -> None:
        try:
            await self._client.interrupt()
        except Exception:  # noqa: BLE001 - interrupting is best-effort; a failure just
            # means the queued message lands after the current turn instead of during it.
            logger.debug("interrupting the in-flight Claude Code turn failed", exc_info=True)


async def _check_already_stopped(
    *, coordinator: AgentCoordinator, agent_id: str, is_root: bool
) -> None:
    if coordinator.budget_stopped:
        await coordinator.set_status(agent_id, "stopped")
        raise BudgetExceededError("scan budget reached")
    if coordinator.reserve_stopped and not is_root:
        await coordinator.set_status(agent_id, "stopped")
        raise SubagentBudgetReservedError("scan reached the sub-agent budget reserve")


async def _result_text(
    message: object, *, coordinator: AgentCoordinator, agent_id: str
) -> str | None:
    """A message's ``result`` text, or None if it carries none.

    Duck-typed: only ``ResultMessage`` has ``result``/``is_error``. Raises the
    free-text quota fallback when a terminal error result reads like a
    usage-limit message.
    """
    result_text = getattr(message, "result", None)
    if not isinstance(result_text, str):
        return None
    if bool(getattr(message, "is_error", False)):
        quota_error = classify_quota_error(result_text)
        if quota_error is not None:
            await coordinator.set_status(agent_id, "stopped")
            raise quota_error
    return result_text


def _log_turn_stage(
    message: object, *, turns_used: int, max_turns: int, is_root: bool, agent_id: str
) -> int:
    reported = getattr(message, "num_turns", None)
    # Monotonic: the CLI's num_turns may be per-response or session-cumulative,
    # and taking the max is right either way (it never double-counts).
    if isinstance(reported, int):
        turns_used = max(turns_used, reported)
    stage = _crossed_stage(turns_used / max_turns, _TURN_WARN_BANDS) if max_turns else None
    if stage is not None:
        logger.info(
            "agent %s turn budget stage %d: %s",
            agent_id,
            stage,
            _wind_down_directive(is_root, stage),
        )
    return turns_used


async def _apply_cost(
    message: object,
    *,
    coordinator: AgentCoordinator,
    agent_id: str,
    is_root: bool,
    interactive: bool,
    max_budget_usd: float | None,
    budget_hooks: Any = None,
) -> None:
    """Record the turn's cost and enforce the scan budget.

    Mirrors ``strix.core.hooks.ReportUsageHooks`` (which this engine never
    runs): an interactive scan pauses for the user instead of stopping, and
    only an autonomous one holds back the sub-agent reserve. The ceiling is
    read from the hooks object when there is one, so a budget the user extends
    mid-run is honored here too.
    """
    cost = getattr(message, "total_cost_usd", None)
    if not isinstance(cost, int | float):
        return
    report_state = get_global_report_state()
    if report_state is None:
        return
    report_state.record_observed_llm_cost(float(cost))
    ceiling = getattr(budget_hooks, "max_budget_usd", None)
    if ceiling is None:
        ceiling = max_budget_usd
    if ceiling is None:
        return
    total = report_state.get_total_llm_cost()
    if total >= ceiling:
        if interactive:
            await coordinator.pause_for_budget(agent_id)
            raise BudgetPausedError(
                f"Scan budget of ${ceiling:.2f} reached (spent ${total:.4f}); "
                "pausing until the user continues"
            )
        await coordinator.set_status(agent_id, "stopped")
        raise BudgetExceededError(f"Token budget of ${ceiling:.2f} exceeded (spent ${total:.4f})")
    reserve_limit = ceiling * _SUBAGENT_BUDGET_RESERVE
    if not interactive and not is_root and total >= reserve_limit:
        await coordinator.set_status(agent_id, "stopped")
        raise SubagentBudgetReservedError(
            f"Sub-agent budget reserve reached: spent ${total:.4f} of ${ceiling:.2f}"
        )


@dataclass
class _EngineTurn:
    """What one ``query`` -> ``receive_response`` pass produced."""

    final_output: str | None
    turns_used: int


@dataclass(frozen=True)
class _LoopConfig:
    """The per-run knobs every turn of one agent's loop needs."""

    agent_id: str
    is_root: bool
    interactive: bool
    max_turns: int
    max_budget_usd: float | None
    budget_hooks: Any = None
    event_sink: Callable[[str, Any], None] | None = None
    # Last lifecycle tool result seen this turn, keyed "last" (N3), and the
    # bare-tool-name rewrite applied to everything sent to the CLI (N4).
    lifecycle_output: dict[str, str] = field(default_factory=dict)
    rewrite: Callable[[str], str] = _identity


async def _consume_response(
    client: Any,
    *,
    coordinator: AgentCoordinator,
    cfg: _LoopConfig,
    turns_used: int,
) -> _EngineTurn:
    final_output: str | None = None
    agent_id = cfg.agent_id
    async for message in client.receive_response():
        if cfg.event_sink is not None:
            try:
                cfg.event_sink(agent_id, message)
            except Exception:
                logger.exception("stream event sink failed for %s", agent_id)

        if isinstance(message, RateLimitEvent):
            quota_error = _quota_error_from_rate_limit(message)
            if quota_error is not None:
                await coordinator.set_status(agent_id, "stopped")
                raise quota_error
            continue

        text = await _result_text(message, coordinator=coordinator, agent_id=agent_id)
        if text is not None:
            final_output = text
        turns_used = _log_turn_stage(
            message,
            turns_used=turns_used,
            max_turns=cfg.max_turns,
            is_root=cfg.is_root,
            agent_id=agent_id,
        )
        await _apply_cost(
            message,
            coordinator=coordinator,
            agent_id=agent_id,
            is_root=cfg.is_root,
            interactive=cfg.interactive,
            max_budget_usd=cfg.max_budget_usd,
            budget_hooks=cfg.budget_hooks,
        )
    return _EngineTurn(final_output=final_output, turns_used=turns_used)


async def _run_until_lifecycle(
    client: Any,
    *,
    prompt: str,
    coordinator: AgentCoordinator,
    cfg: _LoopConfig,
    turns_used: int,
) -> tuple[ClaudeCodeRunResult | None, int]:
    """Drive the CLI session until an explicit lifecycle tool settles the status.

    The Claude Code counterpart of ``strix.core.execution._run_until_lifecycle``:
    a turn that ends without ``finish_scan``/``agent_finish``/
    ``respond_to_user``/``wait_for_agents`` leaves the agent ``running``, and is
    nudged back into a tool call, bounded by the same recovery limit.
    """
    agent_id = cfg.agent_id
    recovery_limit = _INTERACTIVE_TOOL_RECOVERY_LIMIT if cfg.interactive else max(1, cfg.max_turns)
    result: ClaudeCodeRunResult | None = None
    text = prompt

    while True:
        await _check_already_stopped(
            coordinator=coordinator, agent_id=agent_id, is_root=cfg.is_root
        )
        await coordinator.mark_running(agent_id)
        # Single choke point for the bare-tool-name rewrite: the initial input,
        # every nudge, and every mailbox-derived prompt goes through here.
        await client.query(cfg.rewrite(text))
        turn = await _consume_response(
            client, coordinator=coordinator, cfg=cfg, turns_used=turns_used
        )
        turns_used = turn.turns_used
        # A lifecycle tool's JSON beats the CLI's closing prose: `strix.core.runner`
        # parses `final_output` for `{"success": true, "scan_completed": true}` --
        # the shape the default engine's `_finish_tool_use_behavior` forces -- and
        # would otherwise log a false "ended without calling finish_scan" error on
        # every successful claude-code scan.
        result = ClaudeCodeRunResult(
            final_output=cfg.lifecycle_output.pop("last", None) or turn.final_output
        )

        status = await _agent_status(coordinator, agent_id)
        if status != "running":
            await coordinator.reset_recovery(agent_id)
            return result, turns_used

        recoveries = await coordinator.record_recovery(agent_id)
        logger.warning(
            "agent %s ended a Claude Code turn without a lifecycle tool call "
            "(interactive=%s); forcing tool continuation (%d/%d)",
            agent_id,
            cfg.interactive,
            recoveries,
            recovery_limit,
        )
        if recoveries >= recovery_limit:
            settled = await _exhausted_recovery(
                coordinator, agent_id, result, interactive=cfg.interactive
            )
            return settled if isinstance(settled, ClaudeCodeRunResult) else None, turns_used

        text = tool_required_message(
            finish_tool=_MCP_TOOL_PREFIX + ("finish_scan" if cfg.is_root else "agent_finish"),
            attempt=recoveries,
            limit=recovery_limit,
            interactive=cfg.interactive,
        )


def _pending_prompt(items: Sequence[Any]) -> str:
    """Render the messages drained from the mailbox as one CLI prompt."""
    parts = [
        str(item.get("content", "")).strip()
        for item in items
        if isinstance(item, dict) and str(item.get("content", "")).strip()
    ]
    return "\n\n".join(parts)


_AUTO_RESUME_MESSAGE = {
    "from": "system",
    "type": "auto_resume",
    "content": "Waiting timeout reached. Resuming execution.",
}


async def _await_next_input(
    *, coordinator: AgentCoordinator, agent_id: str, is_root: bool
) -> str | None:
    """Park until something is worth resuming on; None means stay parked.

    Mirrors the interactive wait in ``strix.core.execution``: a real message
    resets the nudge budget, a waiting timeout auto-resumes a limited number of
    times, and an agent that keeps re-parking is left for a human.
    """
    timeout = await _plain_waiting_timeout(coordinator, agent_id)
    woke = await coordinator.wait_for_message(agent_id, timeout=timeout)

    await _check_already_stopped(coordinator=coordinator, agent_id=agent_id, is_root=is_root)

    if woke:
        # Real input is real progress, so the nudge budget starts over. A bare
        # auto-resume is not: it must not hand a wedged agent a fresh budget.
        await coordinator.reset_recovery(agent_id)
        await coordinator.reset_idle_resumes(agent_id)
    else:
        idle_resumes = await coordinator.record_idle_resume(agent_id)
        if idle_resumes >= _MAX_IDLE_AUTO_RESUMES:
            logger.warning(
                "agent %s auto-resumed %d times without hearing from anyone; "
                "leaving it parked until a real message arrives",
                agent_id,
                idle_resumes,
            )
            await coordinator.park_waiting(agent_id, wait_kind="stalled")
            # A parked child owes its parent a report it can no longer send.
            await _notify_parent_on_stall(coordinator, agent_id)
            return None
        logger.info("agent %s reached its waiting timeout; auto-resuming", agent_id)
        await coordinator.send(agent_id, dict(_AUTO_RESUME_MESSAGE), interrupt=False)

    _count, items = await coordinator.consume_pending(agent_id, include_items=True)
    return _pending_prompt(items) or "Continue."


async def _first_prompt(initial_input: Any, *, coordinator: AgentCoordinator, agent_id: str) -> str:
    """The opening CLI prompt for whatever shape the runner handed this engine.

    An empty input means a resume or a sub-agent respawn: the default engine
    replays the agent's persisted SDK session there, which this engine cannot
    do. Rather than opening the fresh CLI conversation with nothing, it is told
    that it is resuming and given back its own registered task plus anything
    already queued for it (``strix --resume --instruction ...`` lands there).
    """
    prompt = _prompt_text(initial_input)
    if prompt:
        return prompt

    parts = [_RESUME_PROMPT]
    metadata = getattr(coordinator, "metadata", {}) or {}
    task = str((metadata.get(agent_id) or {}).get("task") or "").strip()
    if task:
        parts.append(f"Your assigned task:\n\n{task}")
    _count, items = await coordinator.consume_pending(agent_id, include_items=True)
    if queued := _pending_prompt(items):
        parts.append(f"Messages queued while you were stopped:\n\n{queued}")
    return "\n\n".join(parts)


# Already-settled failures: each of these sets the agent's status (and notifies
# its parent) before it unwinds, so the loop's own containment must not re-handle
# them. Everything else that escapes is unexpected -- see the `except` below.
_SETTLED_ERRORS = (
    BudgetExceededError,
    BudgetPausedError,
    SubagentBudgetReservedError,
    SubscriptionQuotaExceededError,
    MaxTurnsExceeded,
)


async def run_claude_code_agent_loop(
    *,
    tools: Sequence[Tool],
    instructions: str,
    model_slug: str,
    initial_input: Any,
    max_turns: int,
    max_budget_usd: float | None,
    context: dict[str, Any],
    coordinator: AgentCoordinator,
    agent_id: str,
    is_root: bool,
    interactive: bool = False,
    session: Session | None = None,
    start_parked: bool = False,
    event_sink: Callable[[str, Any], None] | None = None,
    budget_hooks: Any = None,
) -> ClaudeCodeRunResult | None:
    """Run one agent's turn loop on the Claude Code engine.

    Structurally the same shape as ``strix.core.execution``'s default-engine
    loop: one lifecycle-bounded cycle, then (interactively) park for messages
    and run another cycle each time one arrives. ``session`` is attached so the
    coordinator persists queued messages for ``strix --resume``; the live
    conversation itself lives in the CLI session held by the SDK client.
    """
    await coordinator.attach_runtime(agent_id, session=session, interrupt_on_message=interactive)
    await _check_already_stopped(coordinator=coordinator, agent_id=agent_id, is_root=is_root)

    if coordinator.reserve_stopped and start_parked and interactive and is_root:
        await coordinator.send(agent_id, _reserve_notice())

    lifecycle_output: dict[str, str] = {}
    options = _build_options(
        tools=tools,
        instructions=instructions,
        model_slug=model_slug,
        max_turns=max_turns,
        agent_id=agent_id,
        context=context,
        lifecycle_output=lifecycle_output,
    )

    cfg = _LoopConfig(
        agent_id=agent_id,
        is_root=is_root,
        interactive=interactive,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        budget_hooks=budget_hooks,
        event_sink=event_sink,
        lifecycle_output=lifecycle_output,
        rewrite=_tool_name_rewriter(tools),
    )

    result: ClaudeCodeRunResult | None = None
    turns_used = 0
    client = _open_client(options=options)
    try:
        async with client:
            if not (start_parked and interactive):
                first_prompt = await _first_prompt(
                    initial_input, coordinator=coordinator, agent_id=agent_id
                )
                with contextlib.suppress(BudgetPausedError):
                    result, turns_used = await _run_until_lifecycle(
                        client,
                        prompt=first_prompt,
                        coordinator=coordinator,
                        cfg=cfg,
                        turns_used=turns_used,
                    )

            if not interactive:
                return result

            await coordinator.attach_stream(agent_id, _InterruptHandle(client))
            while True:
                try:
                    prompt = await _await_next_input(
                        coordinator=coordinator, agent_id=agent_id, is_root=is_root
                    )
                except asyncio.CancelledError:
                    return result
                if prompt is None:
                    continue
                with contextlib.suppress(BudgetPausedError):
                    result, turns_used = await _run_until_lifecycle(
                        client,
                        prompt=prompt,
                        coordinator=coordinator,
                        cfg=cfg,
                        turns_used=turns_used,
                    )
    except _SETTLED_ERRORS:
        raise
    except Exception as exc:
        # Anything else escaping the CLI session -- a transport failure,
        # CLIConnectionError, ProcessError, a bug in a bridged tool -- would
        # otherwise leave a sub-agent at "running" forever (its parent never
        # told to stop waiting) and kill an interactive root's loop outright.
        # Settled the way the default engine's `_run_cycle`/`_run_cycle_parked`
        # settle their own uncaught errors.
        status: Status = "failed" if interactive else "crashed"
        logger.exception("Claude Code engine loop failed for %s; marking %s", agent_id, status)
        with contextlib.suppress(Exception):
            await coordinator.set_status(agent_id, status, error=str(exc) or type(exc).__name__)
            await notify_parent_on_terminal(coordinator, agent_id, status)
        if interactive:
            # An interactive run stays resumable: `set_status("failed")` gates the
            # agent on a user wake instead of tearing the whole scan down.
            return result
        raise
    else:
        # Only reachable if the client's __aexit__ swallowed an exception.
        return result
    finally:
        _clear_scratch_cwd(agent_id)
