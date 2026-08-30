"""Tests for the Claude Code engine's turn loop: turn limits, cost
accounting, and quota-exceeded propagation.

``RateLimitEvent``/``RateLimitInfo`` are real ``claude_agent_sdk`` dataclasses
(trivially constructable, no CLI subprocess needed) so the rejection path is
exercised against the actual wire-level type the SDK yields, not a guess at
its shape. ``ResultMessage``-shaped fields are read via a plain fake dataclass
(``_FakeResultMessage``) since the loop only duck-types those fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self
from unittest import mock

import pytest
from claude_agent_sdk import RateLimitEvent, RateLimitInfo

from strix.config.claude_code import SubscriptionQuotaExceededError
from strix.core.claude_code_execution import ClaudeCodeRunResult, run_claude_code_agent_loop
from strix.core.hooks import BudgetExceededError, SubagentBudgetReservedError


@dataclass
class _FakeResultMessage:
    is_error: bool
    result: str
    total_cost_usd: float | None
    num_turns: int = 1


class _FakeClient:
    """Stands in for claude_agent_sdk.ClaudeSDKClient in tests."""

    def __init__(self, messages: list[object]) -> None:
        self._messages = messages

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def query(self, prompt: str) -> None:  # noqa: ARG002
        return None

    async def receive_response(self):
        for message in self._messages:
            yield message


def _coordinator() -> mock.AsyncMock:
    coordinator = mock.AsyncMock()
    coordinator.budget_stopped = False
    coordinator.reserve_stopped = False
    return coordinator


@pytest.mark.asyncio
async def test_run_records_cost_and_returns_final_output(monkeypatch: pytest.MonkeyPatch) -> None:
    final = _FakeResultMessage(
        is_error=False, result='{"success": true, "scan_completed": true}', total_cost_usd=0.42
    )
    monkeypatch.setattr(
        "strix.core.claude_code_execution._open_client",
        lambda **_kwargs: _FakeClient([final]),
    )
    report_state = mock.MagicMock()
    report_state.get_total_llm_cost.return_value = 0.0
    monkeypatch.setattr(
        "strix.core.claude_code_execution.get_global_report_state",
        lambda: report_state,
    )

    result = await run_claude_code_agent_loop(
        tools=[],
        instructions="you are a test agent",
        model_slug="sonnet",
        initial_input="do the thing",
        max_turns=10,
        max_budget_usd=None,
        coordinator=_coordinator(),
        agent_id="agent-1",
        is_root=True,
    )

    assert isinstance(result, ClaudeCodeRunResult)
    assert result.final_output == final.result
    report_state.record_observed_llm_cost.assert_called_once_with(0.42)


@pytest.mark.asyncio
async def test_run_raises_budget_exceeded_when_cost_crosses_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final = _FakeResultMessage(is_error=False, result="{}", total_cost_usd=5.0)
    monkeypatch.setattr(
        "strix.core.claude_code_execution._open_client",
        lambda **_kwargs: _FakeClient([final]),
    )
    report_state = mock.MagicMock()
    report_state.get_total_llm_cost.return_value = 5.0
    monkeypatch.setattr(
        "strix.core.claude_code_execution.get_global_report_state",
        lambda: report_state,
    )

    with pytest.raises(BudgetExceededError):
        await run_claude_code_agent_loop(
            tools=[],
            instructions="you are a test agent",
            model_slug="sonnet",
            initial_input="do the thing",
            max_turns=10,
            max_budget_usd=1.0,
            coordinator=_coordinator(),
            agent_id="agent-1",
            is_root=True,
        )


@pytest.mark.asyncio
async def test_run_raises_subagent_budget_reserved_when_reserve_crossed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sub-agents stop earlier than root agents once the reserve fraction is hit."""
    final = _FakeResultMessage(is_error=False, result="{}", total_cost_usd=0.95)
    monkeypatch.setattr(
        "strix.core.claude_code_execution._open_client",
        lambda **_kwargs: _FakeClient([final]),
    )
    report_state = mock.MagicMock()
    report_state.get_total_llm_cost.return_value = 0.95
    monkeypatch.setattr(
        "strix.core.claude_code_execution.get_global_report_state",
        lambda: report_state,
    )

    with pytest.raises(SubagentBudgetReservedError):
        await run_claude_code_agent_loop(
            tools=[],
            instructions="you are a test agent",
            model_slug="sonnet",
            initial_input="do the thing",
            max_turns=10,
            max_budget_usd=1.0,
            coordinator=_coordinator(),
            agent_id="agent-1",
            is_root=False,
        )


@pytest.mark.asyncio
async def test_run_raises_subscription_quota_error_on_known_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final = _FakeResultMessage(
        is_error=True,
        result="5-hour limit reached - resets 7pm",
        total_cost_usd=None,
    )
    monkeypatch.setattr(
        "strix.core.claude_code_execution._open_client",
        lambda **_kwargs: _FakeClient([final]),
    )
    monkeypatch.setattr(
        "strix.core.claude_code_execution.get_global_report_state",
        lambda: None,
    )

    with pytest.raises(SubscriptionQuotaExceededError):
        await run_claude_code_agent_loop(
            tools=[],
            instructions="you are a test agent",
            model_slug="sonnet",
            initial_input="do the thing",
            max_turns=10,
            max_budget_usd=None,
            coordinator=_coordinator(),
            agent_id="agent-1",
            is_root=True,
        )


@pytest.mark.asyncio
async def test_run_raises_subscription_quota_error_on_rate_limit_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A structured ``RateLimitEvent`` with status "rejected" is the primary
    quota signal -- it must be caught even though it arrives before any
    ``ResultMessage`` and carries no free-text message to pattern-match."""
    resets_at = int(datetime(2026, 8, 30, 19, 0, tzinfo=UTC).timestamp())
    event = RateLimitEvent(
        rate_limit_info=RateLimitInfo(
            status="rejected",
            resets_at=resets_at,
            rate_limit_type="five_hour",
        ),
        uuid="evt-1",
        session_id="sess-1",
    )
    monkeypatch.setattr(
        "strix.core.claude_code_execution._open_client",
        lambda **_kwargs: _FakeClient([event]),
    )
    monkeypatch.setattr(
        "strix.core.claude_code_execution.get_global_report_state",
        lambda: None,
    )

    with pytest.raises(SubscriptionQuotaExceededError) as excinfo:
        await run_claude_code_agent_loop(
            tools=[],
            instructions="you are a test agent",
            model_slug="sonnet",
            initial_input="do the thing",
            max_turns=10,
            max_budget_usd=None,
            coordinator=_coordinator(),
            agent_id="agent-1",
            is_root=True,
        )

    assert excinfo.value.reset_at == datetime.fromtimestamp(resets_at, tz=UTC)


@pytest.mark.asyncio
async def test_run_ignores_non_rejected_rate_limit_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """``allowed``/``allowed_warning`` rate-limit events are informational --
    they must not be mistaken for a quota-exceeded signal."""
    warning = RateLimitEvent(
        rate_limit_info=RateLimitInfo(status="allowed_warning", resets_at=None),
        uuid="evt-1",
        session_id="sess-1",
    )
    final = _FakeResultMessage(is_error=False, result="done", total_cost_usd=0.01)
    monkeypatch.setattr(
        "strix.core.claude_code_execution._open_client",
        lambda **_kwargs: _FakeClient([warning, final]),
    )
    report_state = mock.MagicMock()
    report_state.get_total_llm_cost.return_value = 0.01
    monkeypatch.setattr(
        "strix.core.claude_code_execution.get_global_report_state",
        lambda: report_state,
    )

    result = await run_claude_code_agent_loop(
        tools=[],
        instructions="you are a test agent",
        model_slug="sonnet",
        initial_input="do the thing",
        max_turns=10,
        max_budget_usd=None,
        coordinator=_coordinator(),
        agent_id="agent-1",
        is_root=True,
    )

    assert isinstance(result, ClaudeCodeRunResult)
    assert result.final_output == "done"


@pytest.mark.asyncio
async def test_run_sets_agent_completed_status(monkeypatch: pytest.MonkeyPatch) -> None:
    final = _FakeResultMessage(is_error=False, result="done", total_cost_usd=None)
    monkeypatch.setattr(
        "strix.core.claude_code_execution._open_client",
        lambda **_kwargs: _FakeClient([final]),
    )
    monkeypatch.setattr(
        "strix.core.claude_code_execution.get_global_report_state",
        lambda: None,
    )
    coordinator = _coordinator()

    await run_claude_code_agent_loop(
        tools=[],
        instructions="you are a test agent",
        model_slug="sonnet",
        initial_input="do the thing",
        max_turns=10,
        max_budget_usd=None,
        coordinator=coordinator,
        agent_id="agent-1",
        is_root=True,
    )

    coordinator.set_status.assert_awaited_with("agent-1", "completed")


@pytest.mark.asyncio
async def test_run_returns_early_when_scan_budget_already_stopped() -> None:
    coordinator = _coordinator()
    coordinator.budget_stopped = True

    with pytest.raises(BudgetExceededError):
        await run_claude_code_agent_loop(
            tools=[],
            instructions="you are a test agent",
            model_slug="sonnet",
            initial_input="do the thing",
            max_turns=10,
            max_budget_usd=1.0,
            coordinator=coordinator,
            agent_id="agent-1",
            is_root=True,
        )
