"""Claude Code agent engine: drives one agent's turn loop via the real
``claude`` CLI (through ``claude-agent-sdk``), for STRIX_LLM=claude-code/<model>.

Every tool call still executes through Strix's existing implementations
(bridged in via ``strix.agents.claude_code_tools``); this module is only
responsible for the turn loop, cost/turn accounting, and translating the
SDK's own error signals into the exceptions ``strix.core.runner`` and
``strix.core.agents.AgentCoordinator`` already know how to handle.

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

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import gettempdir
from typing import TYPE_CHECKING

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, RateLimitEvent

from strix.agents.claude_code_tools import bridged_tool_names, build_mcp_server
from strix.config.claude_code import SubscriptionQuotaExceededError, classify_quota_error
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
    SubagentBudgetReservedError,
    _crossed_stage,
)
from strix.report.state import get_global_report_state


if TYPE_CHECKING:
    from collections.abc import Sequence

    from agents.tool import Tool

    from strix.core.agents import AgentCoordinator
    from strix.core.execution import StreamEventSink

logger = logging.getLogger(__name__)


@dataclass
class ClaudeCodeRunResult:
    """Duck-type match for the ``.final_output`` read on ``RunResultBase``
    in ``strix/core/runner.py`` -- the rest of ``run_strix_scan`` needs
    nothing else from this object."""

    final_output: str | None


def _open_client(*, options: ClaudeAgentOptions) -> ClaudeSDKClient:
    """Seam for tests: patch this to avoid spawning a real ``claude`` process."""
    return ClaudeSDKClient(options=options)


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


def _scratch_cwd(agent_id: str) -> str:
    path = Path(gettempdir()) / "strix-claude-code-scratch" / agent_id
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _build_options(
    *, tools: Sequence[Tool], instructions: str, model_slug: str, max_turns: int, agent_id: str
) -> ClaudeAgentOptions:
    server = build_mcp_server(tools, name="strix")
    allowed = [
        *bridged_tool_names(tools, server_name="strix"),
        "Bash",
        "Read",
        "Write",
        "WebSearch",
    ]
    return ClaudeAgentOptions(
        system_prompt=instructions,
        model=model_slug,
        mcp_servers={"strix": server},
        allowed_tools=allowed,
        max_turns=max_turns,
        # Auxiliary-only: outside the sandbox workspace, no target network
        # access -- see "Native tool scoping" in the design spec.
        cwd=_scratch_cwd(agent_id),
    )


async def _check_already_stopped(
    *, coordinator: AgentCoordinator, agent_id: str, is_root: bool
) -> None:
    if coordinator.budget_stopped:
        await coordinator.set_status(agent_id, "stopped")
        raise BudgetExceededError("scan budget reached")
    if coordinator.reserve_stopped and not is_root:
        await coordinator.set_status(agent_id, "stopped")
        raise SubagentBudgetReservedError("scan reached the sub-agent budget reserve")


async def _apply_result_text(
    message: object, *, coordinator: AgentCoordinator, agent_id: str, final_output: str | None
) -> str | None:
    """Update ``final_output`` from a message's ``result``/``is_error`` fields
    (duck-typed: only ``ResultMessage`` carries them), raising the free-text
    quota fallback when the terminal result reads like a usage-limit message."""
    result_text = getattr(message, "result", None)
    if not isinstance(result_text, str):
        return final_output
    if bool(getattr(message, "is_error", False)):
        quota_error = classify_quota_error(result_text)
        if quota_error is not None:
            await coordinator.set_status(agent_id, "stopped")
            raise quota_error
    return result_text


def _log_turn_stage(
    message: object, *, turns_used: int, max_turns: int, is_root: bool, agent_id: str
) -> int:
    turns_used = int(getattr(message, "num_turns", turns_used) or turns_used)
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
    max_budget_usd: float | None,
) -> None:
    cost = getattr(message, "total_cost_usd", None)
    if not isinstance(cost, int | float):
        return
    report_state = get_global_report_state()
    if report_state is None:
        return
    report_state.record_observed_llm_cost(float(cost))
    if max_budget_usd is None:
        return
    total = report_state.get_total_llm_cost()
    if total >= max_budget_usd:
        await coordinator.set_status(agent_id, "stopped")
        raise BudgetExceededError(
            f"Token budget of ${max_budget_usd:.2f} exceeded (spent ${total:.4f})"
        )
    reserve_limit = max_budget_usd * _SUBAGENT_BUDGET_RESERVE
    if not is_root and total >= reserve_limit:
        await coordinator.set_status(agent_id, "stopped")
        raise SubagentBudgetReservedError(
            f"Sub-agent budget reserve reached: spent ${total:.4f} of ${max_budget_usd:.2f}"
        )


async def run_claude_code_agent_loop(
    *,
    tools: Sequence[Tool],
    instructions: str,
    model_slug: str,
    initial_input: str,
    max_turns: int,
    max_budget_usd: float | None,
    coordinator: AgentCoordinator,
    agent_id: str,
    is_root: bool,
    event_sink: StreamEventSink | None = None,
) -> ClaudeCodeRunResult | None:
    await coordinator.attach_runtime(agent_id, session=None, interrupt_on_message=False)
    await coordinator.mark_running(agent_id)
    await _check_already_stopped(coordinator=coordinator, agent_id=agent_id, is_root=is_root)

    options = _build_options(
        tools=tools,
        instructions=instructions,
        model_slug=model_slug,
        max_turns=max_turns,
        agent_id=agent_id,
    )

    final_output: str | None = None
    turns_used = 0

    client = _open_client(options=options)
    async with client:
        await client.query(initial_input)
        async for message in client.receive_response():
            if event_sink is not None:
                try:
                    event_sink(agent_id, message)
                except Exception:
                    logger.exception("stream event sink failed for %s", agent_id)

            if isinstance(message, RateLimitEvent):
                quota_error = _quota_error_from_rate_limit(message)
                if quota_error is not None:
                    await coordinator.set_status(agent_id, "stopped")
                    raise quota_error
                continue

            final_output = await _apply_result_text(
                message, coordinator=coordinator, agent_id=agent_id, final_output=final_output
            )
            turns_used = _log_turn_stage(
                message,
                turns_used=turns_used,
                max_turns=max_turns,
                is_root=is_root,
                agent_id=agent_id,
            )
            await _apply_cost(
                message,
                coordinator=coordinator,
                agent_id=agent_id,
                is_root=is_root,
                max_budget_usd=max_budget_usd,
            )

    await coordinator.set_status(agent_id, "completed")
    return ClaudeCodeRunResult(final_output=final_output)
