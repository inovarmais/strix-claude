"""Tests for the Claude Code engine's turn loop: lifecycle recovery, turn
limits, cost accounting, interactive parking, native-tool scoping, and
quota-exceeded propagation.

``RateLimitEvent``/``RateLimitInfo`` are real ``claude_agent_sdk`` dataclasses
(trivially constructable, no CLI subprocess needed) so the rejection path is
exercised against the actual wire-level type the SDK yields, not a guess at
its shape. ``ResultMessage``-shaped fields are read via a plain fake dataclass
(``_FakeResultMessage``) since the loop only duck-types those fields.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from unittest import mock

import pytest
from agents import RunConfig, function_tool
from agents.exceptions import MaxTurnsExceeded
from claude_agent_sdk import RateLimitEvent, RateLimitInfo
from mcp import types as mcp_types

from strix.config.claude_code import SubscriptionQuotaExceededError
from strix.core import claude_code_execution
from strix.core.agents import AgentRuntime
from strix.core.claude_code_execution import (
    ClaudeCodeRunResult,
    _build_options,
    _scratch_path_guard,
    _tool_name_rewriter,
    run_claude_code_agent_loop,
)
from strix.core.execution import run_agent_loop, tool_required_message
from strix.core.hooks import (
    BudgetExceededError,
    ReportUsageHooks,
    SubagentBudgetReservedError,
)
from strix.core.inputs import child_initial_input


@function_tool
async def finish_scan(summary: str) -> str:
    """Finish the scan."""
    return json.dumps({"success": True, "scan_completed": True, "summary": summary})


@function_tool
async def agent_finish(summary: str) -> str:
    """Finish this sub-agent's task."""
    return json.dumps({"success": True, "task_completed": True, "summary": summary})


@function_tool
async def respond_to_user(message: str) -> str:
    """Say something to the user and park."""
    return json.dumps({"success": True, "message": message})


@function_tool
async def wait_for_agents(agent_ids: list[str]) -> str:
    """Wait for other agents."""
    return json.dumps({"success": True, "wait_outcome": "waiting", "agent_ids": agent_ids})


@function_tool
async def think(thought: str) -> str:
    """Think out loud."""
    return thought


@function_tool
async def list_reports() -> str:
    """List findings filed so far."""
    return "[]"


@function_tool
async def get_report(report_id: str) -> str:
    """Get a single finding by id."""
    return report_id


_LIFECYCLE_TOOLS = [finish_scan, agent_finish, respond_to_user, wait_for_agents, think]


async def _call_bridged_tool(server: dict, name: str, arguments: dict) -> Any:
    """Dispatch a real ``tools/call`` at the in-process MCP server, as the CLI does."""
    handler = server["instance"].request_handlers[mcp_types.CallToolRequest]
    result = await handler(
        mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
        )
    )
    return result.root


@dataclass
class _FakeResultMessage:
    is_error: bool
    result: str
    total_cost_usd: float | None
    num_turns: int = 1


class _FakeClient:
    """Stands in for claude_agent_sdk.ClaudeSDKClient in tests."""

    def __init__(
        self, messages: list[object], *, on_turn_end: Any = None, per_turn: Any = None
    ) -> None:
        self._messages = messages
        self._per_turn = per_turn
        self._on_turn_end = on_turn_end
        self.prompts: list[str] = []
        self.interrupts = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def query(self, prompt: str, session_id: str = "default") -> None:  # noqa: ARG002
        self.prompts.append(prompt)

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def receive_response(self):
        messages = self._per_turn(len(self.prompts)) if self._per_turn else self._messages
        for message in messages:
            yield message
        if self._on_turn_end is not None:
            self._on_turn_end(len(self.prompts))


@dataclass
class _FakeCoordinator:
    """Real status bookkeeping, no I/O -- enough for the engine loop's checks.

    Statuses move exactly the way the real coordinator's do: ``mark_running``
    sets ``running`` and only a lifecycle tool (simulated here by the fake
    client's ``on_turn_end``) settles it to something else.
    """

    statuses: dict[str, str] = field(default_factory=dict)
    budget_stopped: bool = False
    reserve_stopped: bool = False
    recovery_counts: dict[str, int] = field(default_factory=dict)
    idle_resume_counts: dict[str, int] = field(default_factory=dict)
    parent_of: dict[str, str | None] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    runtimes: dict[str, Any] = field(default_factory=dict)
    wait_kinds: dict[str, str] = field(default_factory=dict)
    mailbox: list[dict[str, Any]] = field(default_factory=list)
    sent: list[dict[str, Any]] = field(default_factory=list)
    streams: list[Any] = field(default_factory=list)
    parked: list[str] = field(default_factory=list)
    status_log: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._lock = asyncio.Lock()

    async def attach_runtime(self, agent_id: str, **_kwargs: Any) -> None:
        self.runtimes.setdefault(agent_id, AgentRuntime())

    async def mark_running(self, agent_id: str) -> None:
        self.statuses[agent_id] = "running"

    async def set_status(self, agent_id: str, status: str, **_kwargs: Any) -> None:
        self.statuses[agent_id] = status
        self.status_log.append(status)

    async def record_recovery(self, agent_id: str) -> int:
        self.recovery_counts[agent_id] = self.recovery_counts.get(agent_id, 0) + 1
        return self.recovery_counts[agent_id]

    async def reset_recovery(self, agent_id: str) -> None:
        self.recovery_counts.pop(agent_id, None)

    async def record_idle_resume(self, agent_id: str) -> int:
        self.idle_resume_counts[agent_id] = self.idle_resume_counts.get(agent_id, 0) + 1
        return self.idle_resume_counts[agent_id]

    async def reset_idle_resumes(self, agent_id: str) -> None:
        self.idle_resume_counts.pop(agent_id, None)

    async def park_waiting(self, agent_id: str, *, wait_kind: str) -> None:
        self.statuses[agent_id] = "waiting"
        self.wait_kinds[agent_id] = wait_kind
        self.parked.append(agent_id)

    async def wait_for_message(self, agent_id: str, *, timeout: float | None = None) -> bool:
        _ = agent_id
        if self.mailbox:
            return True
        # Park like the real coordinator does; the test cancels the loop.
        await asyncio.sleep(timeout if timeout is not None else 3600)
        return bool(self.mailbox)

    async def consume_pending(
        self, agent_id: str, *, include_items: bool = False
    ) -> tuple[int, list[Any]]:
        _ = agent_id
        queued = list(self.mailbox)
        self.mailbox.clear()
        items = [{"role": "user", "content": str(m.get("content", ""))} for m in queued]
        return len(queued), (items if include_items else [])

    async def send(self, agent_id: str, message: dict[str, Any], **_kwargs: Any) -> bool:
        self.sent.append(message)
        self.mailbox.append(message)
        _ = agent_id
        return True

    async def pause_for_budget(self, agent_id: str) -> None:
        self.statuses[agent_id] = "budget_paused"
        self.parked.append(agent_id)

    async def attach_stream(self, agent_id: str, stream: Any) -> None:
        _ = agent_id
        self.streams.append(stream)

    async def claim_parent_notice(self, agent_id: str) -> bool:
        _ = agent_id
        return True


def _coordinator() -> Any:
    return _FakeCoordinator()


def _settles(coordinator: Any, status: str = "completed", agent_id: str = "agent-1") -> Any:
    """Simulate the agent calling a lifecycle tool during its turn."""

    def _on_turn_end(_turn: int) -> None:
        coordinator.statuses[agent_id] = status

    return _on_turn_end


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> None:
    monkeypatch.setattr("strix.core.claude_code_execution._open_client", lambda **_kwargs: client)


def _patch_report_state(monkeypatch: pytest.MonkeyPatch, report_state: Any) -> None:
    monkeypatch.setattr(
        "strix.core.claude_code_execution.get_global_report_state", lambda: report_state
    )


async def _run(coordinator: Any, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "tools": [],
        "instructions": "you are a test agent",
        "model_slug": "sonnet",
        "initial_input": "do the thing",
        "max_turns": 10,
        "max_budget_usd": None,
        "context": {"parent_id": None},
        "coordinator": coordinator,
        "agent_id": "agent-1",
        "is_root": True,
    }
    kwargs.update(overrides)
    return await run_claude_code_agent_loop(**kwargs)


@pytest.mark.asyncio
async def test_run_records_cost_and_returns_final_output(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _coordinator()
    final = _FakeResultMessage(
        is_error=False, result='{"success": true, "scan_completed": true}', total_cost_usd=0.42
    )
    _patch_client(monkeypatch, _FakeClient([final], on_turn_end=_settles(coordinator)))
    report_state = mock.MagicMock()
    report_state.get_total_llm_cost.return_value = 0.0
    _patch_report_state(monkeypatch, report_state)

    result = await _run(coordinator)

    assert isinstance(result, ClaudeCodeRunResult)
    assert result.final_output == final.result
    report_state.record_observed_llm_cost.assert_called_once_with(0.42)


@pytest.mark.asyncio
async def test_run_raises_budget_exceeded_when_cost_crosses_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = _coordinator()
    final = _FakeResultMessage(is_error=False, result="{}", total_cost_usd=5.0)
    _patch_client(monkeypatch, _FakeClient([final], on_turn_end=_settles(coordinator)))
    report_state = mock.MagicMock()
    report_state.get_total_llm_cost.return_value = 5.0
    _patch_report_state(monkeypatch, report_state)

    with pytest.raises(BudgetExceededError):
        await _run(coordinator, max_budget_usd=1.0)


@pytest.mark.asyncio
async def test_run_raises_subagent_budget_reserved_when_reserve_crossed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sub-agents stop earlier than root agents once the reserve fraction is hit."""
    coordinator = _coordinator()
    final = _FakeResultMessage(is_error=False, result="{}", total_cost_usd=0.95)
    _patch_client(monkeypatch, _FakeClient([final], on_turn_end=_settles(coordinator)))
    report_state = mock.MagicMock()
    report_state.get_total_llm_cost.return_value = 0.95
    _patch_report_state(monkeypatch, report_state)

    with pytest.raises(SubagentBudgetReservedError):
        await _run(coordinator, max_budget_usd=1.0, is_root=False)


@pytest.mark.asyncio
async def test_run_raises_subscription_quota_error_on_known_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = _coordinator()
    final = _FakeResultMessage(
        is_error=True, result="5-hour limit reached - resets 7pm", total_cost_usd=None
    )
    _patch_client(monkeypatch, _FakeClient([final], on_turn_end=_settles(coordinator)))
    _patch_report_state(monkeypatch, None)

    with pytest.raises(SubscriptionQuotaExceededError):
        await _run(coordinator)


@pytest.mark.asyncio
async def test_run_raises_subscription_quota_error_on_rate_limit_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A structured ``RateLimitEvent`` with status "rejected" is the primary
    quota signal -- it must be caught even though it arrives before any
    ``ResultMessage`` and carries no free-text message to pattern-match."""
    coordinator = _coordinator()
    resets_at = int(datetime(2026, 8, 30, 19, 0, tzinfo=UTC).timestamp())
    event = RateLimitEvent(
        rate_limit_info=RateLimitInfo(
            status="rejected", resets_at=resets_at, rate_limit_type="five_hour"
        ),
        uuid="evt-1",
        session_id="sess-1",
    )
    _patch_client(monkeypatch, _FakeClient([event], on_turn_end=_settles(coordinator)))
    _patch_report_state(monkeypatch, None)

    with pytest.raises(SubscriptionQuotaExceededError) as excinfo:
        await _run(coordinator)

    assert excinfo.value.reset_at == datetime.fromtimestamp(resets_at, tz=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["allowed", "allowed_warning"])
async def test_run_ignores_non_rejected_rate_limit_events(
    status: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``allowed``/``allowed_warning`` rate-limit events are informational --
    they must not be mistaken for a quota-exceeded signal."""
    coordinator = _coordinator()
    warning = RateLimitEvent(
        rate_limit_info=RateLimitInfo(status=status, resets_at=None),  # type: ignore[arg-type]
        uuid="evt-1",
        session_id="sess-1",
    )
    final = _FakeResultMessage(is_error=False, result="done", total_cost_usd=0.01)
    _patch_client(monkeypatch, _FakeClient([warning, final], on_turn_end=_settles(coordinator)))
    report_state = mock.MagicMock()
    report_state.get_total_llm_cost.return_value = 0.01
    _patch_report_state(monkeypatch, report_state)

    result = await _run(coordinator)

    assert isinstance(result, ClaudeCodeRunResult)
    assert result.final_output == "done"


@pytest.mark.asyncio
async def test_run_returns_early_when_scan_budget_already_stopped() -> None:
    coordinator = _coordinator()
    coordinator.budget_stopped = True

    with pytest.raises(BudgetExceededError):
        await _run(coordinator, max_budget_usd=1.0)


@pytest.mark.asyncio
async def test_run_nudges_a_turn_that_ended_without_a_lifecycle_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lifecycle recovery parity with the default engine: a text-only turn is
    nudged back into a tool call instead of being treated as a clean finish."""
    coordinator = _coordinator()

    def _on_turn_end(turn: int) -> None:
        if turn >= 2:
            coordinator.statuses["agent-1"] = "completed"

    client = _FakeClient(
        [_FakeResultMessage(is_error=False, result="just some prose", total_cost_usd=None)],
        on_turn_end=_on_turn_end,
    )
    _patch_client(monkeypatch, client)
    _patch_report_state(monkeypatch, None)

    result = await _run(coordinator)

    assert len(client.prompts) == 2
    assert "without a lifecycle tool" in client.prompts[1]
    # Named with the bridge prefix: bare `finish_scan` does not exist on this
    # engine, and an agent nudged toward it just gets "No such tool available".
    assert "mcp__strix__finish_scan" in client.prompts[1]
    assert isinstance(result, ClaudeCodeRunResult)
    assert coordinator.statuses["agent-1"] == "completed"


@pytest.mark.asyncio
async def test_run_crashes_after_exhausting_lifecycle_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An autonomous agent that never calls a lifecycle tool fails loudly,
    exactly as the default engine's ``_exhausted_recovery`` does."""
    coordinator = _coordinator()
    client = _FakeClient(
        [_FakeResultMessage(is_error=False, result="prose again", total_cost_usd=None)]
    )
    _patch_client(monkeypatch, client)
    _patch_report_state(monkeypatch, None)

    with pytest.raises(MaxTurnsExceeded):
        await _run(coordinator, max_turns=2)

    assert coordinator.statuses["agent-1"] == "crashed"


@pytest.mark.asyncio
async def test_interactive_run_parks_then_resumes_on_a_user_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interactive parity: after ``respond_to_user`` parks the agent, a queued
    message wakes it and is delivered to the same CLI session as a new turn."""
    coordinator = _coordinator()
    coordinator.mailbox.append({"from": "user", "content": "look at /admin next"})

    def _on_turn_end(turn: int) -> None:
        coordinator.statuses["agent-1"] = "waiting" if turn == 1 else "completed"

    client = _FakeClient(
        [_FakeResultMessage(is_error=False, result="parked", total_cost_usd=None)],
        on_turn_end=_on_turn_end,
    )
    _patch_client(monkeypatch, client)
    _patch_report_state(monkeypatch, None)

    # The interactive loop only ends when the scan tears the agent down, so run
    # it until it parks again, then stop it.
    task = asyncio.create_task(_run(coordinator, interactive=True))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert client.prompts[0] == "do the thing"
    assert "look at /admin next" in client.prompts[1]
    assert coordinator.statuses["agent-1"] == "completed"
    assert coordinator.streams, "no interrupt handle attached for the interactive run"


@pytest.mark.asyncio
async def test_interactive_message_interrupts_the_in_flight_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handle attached for an interactive run maps the coordinator's stream
    cancellation onto the SDK client's own ``interrupt()``."""
    coordinator = _coordinator()
    client = _FakeClient(
        [_FakeResultMessage(is_error=False, result="done", total_cost_usd=None)],
        on_turn_end=_settles(coordinator, "waiting"),
    )
    _patch_client(monkeypatch, client)
    _patch_report_state(monkeypatch, None)

    task = asyncio.create_task(_run(coordinator, interactive=True))
    await asyncio.sleep(0.05)

    (handle,) = coordinator.streams
    handle.cancel(mode="immediate")
    await asyncio.sleep(0)
    assert client.interrupts == 1

    handle.cancel(mode="after_turn")
    await asyncio.sleep(0)
    assert client.interrupts == 1

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_interactive_run_pauses_at_the_budget_instead_of_stopping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interactive parity with ``ReportUsageHooks``: reaching the budget parks
    the agent for the user to continue, rather than stopping the scan."""
    coordinator = _coordinator()
    client = _FakeClient(
        [_FakeResultMessage(is_error=False, result="{}", total_cost_usd=5.0)],
        on_turn_end=_settles(coordinator),
    )
    _patch_client(monkeypatch, client)
    report_state = mock.MagicMock()
    report_state.get_total_llm_cost.return_value = 5.0
    _patch_report_state(monkeypatch, report_state)

    task = asyncio.create_task(_run(coordinator, max_budget_usd=1.0, interactive=True))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert coordinator.statuses["agent-1"] == "budget_paused"


@pytest.mark.asyncio
async def test_budget_ceiling_follows_an_extension_made_through_the_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user extending the budget mid-run raises the ceiling on the hooks
    object; the engine enforces the budget itself and must read it from there."""
    coordinator = _coordinator()
    _patch_client(
        monkeypatch,
        _FakeClient(
            [_FakeResultMessage(is_error=False, result="{}", total_cost_usd=5.0)],
            on_turn_end=_settles(coordinator),
        ),
    )
    report_state = mock.MagicMock()
    report_state.get_total_llm_cost.return_value = 5.0
    _patch_report_state(monkeypatch, report_state)
    hooks = ReportUsageHooks(model="claude-code/sonnet", max_budget_usd=1.0)
    hooks.extend_budget()
    hooks.extend_budget()
    hooks.extend_budget()
    hooks.extend_budget()
    hooks.extend_budget()  # ceiling is now $6

    result = await _run(coordinator, max_budget_usd=1.0, budget_hooks=hooks)

    assert isinstance(result, ClaudeCodeRunResult)


@pytest.mark.asyncio
async def test_run_cleans_up_its_scratch_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _coordinator()
    _patch_client(
        monkeypatch,
        _FakeClient(
            [_FakeResultMessage(is_error=False, result="done", total_cost_usd=None)],
            on_turn_end=_settles(coordinator, agent_id="agent-scratch"),
        ),
    )
    _patch_report_state(monkeypatch, None)

    await _run(coordinator, agent_id="agent-scratch")

    assert not (
        Path(claude_code_execution.gettempdir())
        / claude_code_execution._SCRATCH_DIR_NAME
        / "agent-scratch"
    ).exists()


def test_options_sandbox_native_tools_and_path_guard() -> None:
    """Native tool scoping is enforced by the SDK sandbox and a PreToolUse hook,
    not by ``cwd`` (which jails nothing)."""
    options = _build_options(
        tools=[],
        instructions="be careful",
        model_slug="sonnet",
        max_turns=5,
        agent_id="agent-opts",
        context={"parent_id": None},
    )

    assert options.sandbox is not None
    assert options.sandbox["enabled"] is True
    assert options.sandbox["allowUnsandboxedCommands"] is False
    assert options.sandbox["network"]["allowedDomains"] == []
    # `tools` restricts what exists; `allowed_tools` only pre-approves.
    assert options.tools is not None
    assert set(options.tools) <= {"Bash", "Read", "Write", "WebSearch"}
    assert "WebSearch" in options.tools
    assert options.hooks is not None
    assert options.hooks["PreToolUse"][0].matcher == "Read|Write"
    # Isolation from the operator's own Claude Code config: no settings files,
    # no MCP servers other than Strix's own in-process one.
    assert options.setting_sources == []
    assert options.strict_mcp_config is True
    # The prompt is written to a file, not passed inline: Strix's rendered
    # prompt is well past Windows' ~32K CreateProcess argv limit on its own.
    assert options.system_prompt is not None
    assert isinstance(options.system_prompt, dict)
    assert options.system_prompt["type"] == "file"
    prompt_text = Path(options.system_prompt["path"]).read_text(encoding="utf-8")
    # Strix's prompt names tools bare; the bridge exposes them prefixed.
    assert "mcp__strix__finish_scan" in prompt_text
    assert "be careful" in prompt_text

    claude_code_execution._clear_scratch_cwd("agent-opts")


@pytest.mark.asyncio
async def test_path_guard_denies_native_reads_outside_the_scratch_dir(tmp_path: Path) -> None:
    guard = _scratch_path_guard(tmp_path)

    inside = await guard(
        {"tool_name": "Read", "tool_input": {"file_path": str(tmp_path / "notes.md")}}, None, None
    )
    outside = await guard(
        {"tool_name": "Read", "tool_input": {"file_path": str(Path.home() / ".ssh" / "id_rsa")}},
        None,
        None,
    )

    assert inside == {}
    assert outside["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.asyncio
async def test_run_agent_loop_dispatches_to_claude_code_engine() -> None:
    coordinator = mock.AsyncMock()
    coordinator.budget_stopped = False
    coordinator.reserve_stopped = False
    fake_agent = mock.MagicMock()
    fake_agent.instructions = "you are a test agent"
    fake_agent.tools = []
    fake_agent.capabilities = []
    context = {"parent_id": None, "max_budget_usd": 12.5, "coordinator": coordinator}

    with mock.patch(
        "strix.core.claude_code_execution.run_claude_code_agent_loop",
        new=mock.AsyncMock(return_value="sentinel-result"),
    ) as bridged:
        result = await run_agent_loop(
            agent=fake_agent,
            initial_input="do the thing",
            run_config=RunConfig(model="claude-code/sonnet"),
            context=context,
            max_turns=5,
            coordinator=coordinator,
            agent_id="agent-1",
            interactive=False,
        )

    bridged.assert_awaited_once()
    kwargs = bridged.await_args.kwargs
    assert result == "sentinel-result"
    # The run's budget ceiling and the real run context must both reach the
    # engine: a typo in either key silently disables budget stops / every
    # context-dependent tool.
    assert kwargs["max_budget_usd"] == 12.5
    assert kwargs["context"] is context
    assert kwargs["is_root"] is True


# --------------------------------------------------------------------------
# N1: the shapes ``run_agent_loop`` actually hands this engine.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial_input",
    [
        # `strix --resume` / every `--auto-resume` retry (runner.py sets
        # `initial_input = [] if is_resume else root_task`), and
        # `respawn_subagents` restarting a child -- both pass `[]`.
        [],
        # A spawned sub-agent's task, from `child_initial_input`.
        [{"role": "user", "content": "You are agent Recon (c1). Map the API surface."}],
    ],
)
async def test_run_agent_loop_forwards_non_string_initial_input_unstringified(
    initial_input: Any,
) -> None:
    """``str(initial_input)`` used to turn these into ``"[]"`` and a Python repr.

    This is the real dispatch path ``runner.py`` calls, so it pins the fix at the
    point the resume / auto-resume / respawn flows actually reach.
    """
    coordinator = mock.AsyncMock()
    coordinator.budget_stopped = False
    coordinator.reserve_stopped = False
    fake_agent = mock.MagicMock()
    fake_agent.instructions = "you are a test agent"
    fake_agent.tools = []
    fake_agent.capabilities = []

    with mock.patch(
        "strix.core.claude_code_execution.run_claude_code_agent_loop",
        new=mock.AsyncMock(return_value=None),
    ) as bridged:
        await run_agent_loop(
            agent=fake_agent,
            initial_input=initial_input,
            run_config=RunConfig(model="claude-code/sonnet"),
            context={"parent_id": None, "coordinator": coordinator},
            max_turns=5,
            coordinator=coordinator,
            agent_id="agent-1",
            interactive=False,
        )

    assert bridged.await_args.kwargs["initial_input"] == initial_input


@pytest.mark.asyncio
async def test_plain_string_task_is_sent_to_the_cli_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = _coordinator()
    client = _FakeClient(
        [_FakeResultMessage(is_error=False, result="ok", total_cost_usd=None)],
        on_turn_end=_settles(coordinator),
    )
    _patch_client(monkeypatch, client)
    _patch_report_state(monkeypatch, None)

    await _run(coordinator)

    assert client.prompts == ["do the thing"]


@pytest.mark.asyncio
async def test_empty_resume_input_becomes_a_real_resume_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed root used to be handed the literal string ``"[]"``."""
    coordinator = _coordinator()
    coordinator.metadata["agent-1"] = {"task": "Pentest https://example.com"}
    # `strix --resume --instruction ...` queues the new instruction on root.
    coordinator.mailbox.append({"from": "user", "content": "focus on auth this time"})
    client = _FakeClient(
        [_FakeResultMessage(is_error=False, result="ok", total_cost_usd=None)],
        on_turn_end=_settles(coordinator),
    )
    _patch_client(monkeypatch, client)
    _patch_report_state(monkeypatch, None)

    await _run(coordinator, initial_input=[])

    prompt = client.prompts[0]
    assert prompt != "[]"
    assert "resuming an interrupted Strix scan" in prompt
    assert "Pentest https://example.com" in prompt
    assert "focus on auth this time" in prompt


@pytest.mark.asyncio
async def test_resume_task_and_instruction_survive_rewrite_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registered task and a queued ``--instruction`` can legitimately
    contain a bridged tool's name as a substring (a target URL path, a plain
    mention of an endpoint) and must reach the CLI unmodified, even though
    this resumed root's opening prompt also carries Strix's own (rewritten)
    resume wording right next to them."""
    coordinator = _coordinator()
    coordinator.metadata["agent-1"] = {
        "task": "Pentest https://shop.example.com/api/v2/list_reports?id=1"
    }
    coordinator.mailbox.append(
        {"from": "user", "content": "focus on the get_report endpoint this time"}
    )
    client = _FakeClient(
        [_FakeResultMessage(is_error=False, result="ok", total_cost_usd=None)],
        on_turn_end=_settles(coordinator),
    )
    _patch_client(monkeypatch, client)
    _patch_report_state(monkeypatch, None)

    await _run(
        coordinator,
        tools=[*_LIFECYCLE_TOOLS, list_reports, get_report],
        initial_input=[],
    )

    prompt = client.prompts[0]
    assert "https://shop.example.com/api/v2/list_reports?id=1" in prompt
    assert "focus on the get_report endpoint this time" in prompt
    # Strix's own resume wording is present and rewritten (it names no bridged
    # tool here, so this just pins that the call site is actually exercised).
    assert "resuming an interrupted Strix scan" in prompt


@pytest.mark.asyncio
async def test_subagent_message_list_input_is_unwrapped_not_repr_dumped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = _coordinator()
    client = _FakeClient(
        [_FakeResultMessage(is_error=False, result="ok", total_cost_usd=None)],
        on_turn_end=_settles(coordinator),
    )
    _patch_client(monkeypatch, client)
    _patch_report_state(monkeypatch, None)

    initial = child_initial_input(
        name="Recon",
        child_id="c1",
        parent_id="p1",
        task="Map the API surface of /v2.",
        parent_history=[],
    )
    await _run(coordinator, initial_input=initial, is_root=False)

    prompt = client.prompts[0]
    assert prompt.startswith("You are agent Recon (c1)")
    assert "Map the API surface of /v2." in prompt
    # The old `str(list_of_dicts)` repr leaked these.
    assert "'role'" not in prompt
    assert "[{" not in prompt


# --------------------------------------------------------------------------
# N2: error containment in the engine's turn loop.
# --------------------------------------------------------------------------


class _ExplodingClient(_FakeClient):
    async def query(self, prompt: str, session_id: str = "default") -> None:  # noqa: ARG002
        raise RuntimeError("CLI transport died")


@pytest.mark.asyncio
async def test_unexpected_error_crashes_the_agent_and_notifies_its_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without containment a sub-agent sat at "running" forever and its parent
    burned its whole wait timeout on a report that was never coming."""
    coordinator = _coordinator()
    coordinator.parent_of["agent-1"] = "root-1"
    coordinator.names["agent-1"] = "Recon"
    _patch_client(monkeypatch, _ExplodingClient([]))
    _patch_report_state(monkeypatch, None)

    with pytest.raises(RuntimeError, match="CLI transport died"):
        await _run(coordinator, is_root=False)

    assert coordinator.statuses["agent-1"] == "crashed"
    assert [m["type"] for m in coordinator.sent] == ["crashed"]
    assert "Recon" in coordinator.sent[0]["content"]


@pytest.mark.asyncio
async def test_interactive_unexpected_error_parks_the_agent_as_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parity with ``_run_cycle_parked``: an interactive run stays resumable
    instead of taking the whole loop down."""
    coordinator = _coordinator()
    _patch_client(monkeypatch, _ExplodingClient([]))
    _patch_report_state(monkeypatch, None)

    result = await _run(coordinator, interactive=True)

    assert result is None
    assert coordinator.statuses["agent-1"] == "failed"


@pytest.mark.asyncio
async def test_containment_does_not_swallow_already_settled_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A budget/quota stop settles its own status; containment must leave it
    alone rather than relabelling a clean stop as a crash."""
    coordinator = _coordinator()
    final = _FakeResultMessage(
        is_error=True, result="5-hour limit reached - resets 7pm", total_cost_usd=None
    )
    _patch_client(monkeypatch, _FakeClient([final]))
    _patch_report_state(monkeypatch, None)

    with pytest.raises(SubscriptionQuotaExceededError):
        await _run(coordinator, interactive=True)

    assert coordinator.statuses["agent-1"] == "stopped"


# --------------------------------------------------------------------------
# N3: final_output carries the lifecycle tool's JSON, not the closing prose.
# --------------------------------------------------------------------------


def _runner_scan_completed(final: Any) -> bool:
    """``strix.core.runner``'s completion check, verbatim."""
    if isinstance(final, str):
        try:
            parsed = json.loads(final)
        except (ValueError, TypeError):
            return False
        return bool(isinstance(parsed, dict) and parsed.get("scan_completed"))
    if isinstance(final, dict):
        return bool(final.get("scan_completed"))
    return False


@pytest.mark.asyncio
async def test_lifecycle_tool_result_is_recorded_by_the_bridged_mcp_server() -> None:
    """The recorder is wired through the real in-process MCP server, so the
    JSON is captured exactly where the CLI actually calls the tool."""
    lifecycle: dict[str, str] = {}
    options = _build_options(
        tools=[think, finish_scan],
        instructions="be careful",
        model_slug="sonnet",
        max_turns=5,
        agent_id="agent-rec",
        context={"parent_id": None},
        lifecycle_output=lifecycle,
    )
    server = options.mcp_servers["strix"]

    await _call_bridged_tool(server, "think", {"thought": "hmm"})
    assert lifecycle == {}, "a non-lifecycle tool must not claim final_output"

    await _call_bridged_tool(server, "finish_scan", {"summary": "3 findings"})
    assert _runner_scan_completed(lifecycle["last"])

    claude_code_execution._clear_scratch_cwd("agent-rec")


@pytest.mark.asyncio
async def test_final_output_prefers_the_lifecycle_json_over_the_closing_prose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ResultMessage.result`` is the model's closing prose, which never parses
    as the finish_scan shape -- so every successful claude-code scan used to log
    "ended without calling finish_scan"."""
    coordinator = _coordinator()
    captured: dict[str, Any] = {}
    real_build = claude_code_execution._build_options

    def _capture(**kwargs: Any) -> Any:
        captured["lifecycle_output"] = kwargs["lifecycle_output"]
        return real_build(**kwargs)

    monkeypatch.setattr(claude_code_execution, "_build_options", _capture)

    def _on_turn_end(_turn: int) -> None:
        captured["lifecycle_output"]["last"] = '{"success": true, "scan_completed": true}'
        coordinator.statuses["agent-1"] = "completed"

    _patch_client(
        monkeypatch,
        _FakeClient(
            [
                _FakeResultMessage(
                    is_error=False,
                    result="All done! I found 3 issues and wrote the report.",
                    total_cost_usd=None,
                )
            ],
            on_turn_end=_on_turn_end,
        ),
    )
    _patch_report_state(monkeypatch, None)

    result = await _run(coordinator)

    assert isinstance(result, ClaudeCodeRunResult)
    assert _runner_scan_completed(result.final_output)


@pytest.mark.asyncio
async def test_final_output_stays_prose_when_no_lifecycle_tool_was_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The completion signal stays meaningful: a scan that really never called a
    lifecycle tool still fails runner.py's check."""
    coordinator = _coordinator()
    _patch_client(
        monkeypatch,
        _FakeClient(
            [_FakeResultMessage(is_error=False, result="I think that's it.", total_cost_usd=None)],
            # `wait_for_agents` settles the status without completing the scan.
            on_turn_end=_settles(coordinator, "waiting"),
        ),
    )
    _patch_report_state(monkeypatch, None)

    result = await _run(coordinator)

    assert isinstance(result, ClaudeCodeRunResult)
    assert result.final_output == "I think that's it."
    assert not _runner_scan_completed(result.final_output)


# --------------------------------------------------------------------------
# N4: bare tool names are rewritten on this engine's send path only.
# --------------------------------------------------------------------------


def test_tool_name_rewriter_prefixes_known_bridged_names() -> None:
    rewrite = _tool_name_rewriter(_LIFECYCLE_TOOLS)
    text = (
        "If you have something to tell the user, call respond_to_user. "
        "If you are blocked, call wait_for_agents. "
        "When the engagement is done call finish_scan; a sub-agent calls agent_finish. "
        "mcp__strix__finish_scan is already correct. "
        "Take a moment to think about it first."
    )

    out = rewrite(text)

    for name in ("respond_to_user", "wait_for_agents", "finish_scan", "agent_finish"):
        assert f"mcp__strix__{name}" in out
    # Never double-prefixed, and an already-prefixed mention is left alone.
    assert "mcp__strix__mcp__strix__" not in out
    assert out.count("mcp__strix__finish_scan") == 2
    # Single-word tool names are also ordinary English words: rewriting them
    # would corrupt prose, so `_TOOL_NAMING_NOTE` covers those instead.
    assert "think about it" in out


@pytest.mark.asyncio
async def test_only_the_lifecycle_nudge_is_rewritten_not_the_initial_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bare-tool-name rewrite must be scoped to Strix-authored text only.

    It used to be applied as a blanket transform on every outgoing prompt at
    ``client.query()``, which corrupted a root task/target description (or a
    user ``--instruction``) that happened to contain a bridged tool's name as
    a substring -- e.g. a real target URL ending in ``/list_reports``. The
    lifecycle nudge (Strix's own wording) must still be rewritten so the CLI
    can actually call the tool it names.
    """
    coordinator = _coordinator()

    def _on_turn_end(turn: int) -> None:
        if turn >= 2:
            coordinator.statuses["agent-1"] = "completed"

    client = _FakeClient(
        [_FakeResultMessage(is_error=False, result="prose", total_cost_usd=None)],
        on_turn_end=_on_turn_end,
    )
    _patch_client(monkeypatch, client)
    _patch_report_state(monkeypatch, None)

    # Interactive so the nudge is the interactive one, which is the wording that
    # names respond_to_user and wait_for_agents; that loop only ends when the
    # scan tears the agent down, so run it until it parks and then stop it.
    target_task = (
        "Start by calling think, then finish_scan when done. Target: "
        "https://shop.example.com/api/v2/list_reports?id=1"
    )
    task = asyncio.create_task(
        _run(
            coordinator,
            tools=[*_LIFECYCLE_TOOLS, list_reports],
            initial_input=target_task,
            interactive=True,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    # The task/target text reaches the CLI byte-for-byte, bridged tool name
    # substrings and all -- neither "finish_scan" nor "list_reports" (both
    # real bridged tools in this agent's toolset) is touched.
    assert client.prompts[0] == target_task
    assert "mcp__strix__" not in client.prompts[0]

    nudge = client.prompts[1]
    for name in ("respond_to_user", "wait_for_agents", "finish_scan"):
        assert f"mcp__strix__{name}" in nudge
    assert "mcp__strix__mcp__strix__" not in nudge


@pytest.mark.asyncio
async def test_reserve_notice_is_rewritten_before_it_reaches_the_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_reserve_notice()`` tells a parked root to call ``finish_scan`` bare.

    Unlike the task/instruction content it travels alongside once queued (same
    mailbox, same ``_pending_prompt`` render), this notice is Strix's own
    wording, so it must reach the CLI bridged -- rewritten when it is sent,
    not by rewriting whatever the mailbox later hands back (which would also
    corrupt real user/agent messages queued next to it).
    """
    coordinator = _coordinator()
    coordinator.reserve_stopped = True

    client = _FakeClient(
        [_FakeResultMessage(is_error=False, result="ok", total_cost_usd=None)],
        on_turn_end=_settles(coordinator),
    )
    _patch_client(monkeypatch, client)
    _patch_report_state(monkeypatch, None)

    task = asyncio.create_task(
        _run(coordinator, tools=_LIFECYCLE_TOOLS, interactive=True, start_parked=True)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert "mcp__strix__finish_scan" in client.prompts[0]

    claude_code_execution._clear_scratch_cwd("agent-1")


def test_shared_system_prompt_is_rewritten_only_on_the_claude_code_engine() -> None:
    """The jinja template is shared with the default engine, so it must stay
    bare-named there and be transformed on the way into the CLI here."""
    options = _build_options(
        tools=_LIFECYCLE_TOOLS,
        instructions="When the engagement is complete, call finish_scan.",
        model_slug="sonnet",
        max_turns=5,
        agent_id="agent-sysprompt",
        context={"parent_id": None},
    )
    assert isinstance(options.system_prompt, dict)
    prompt_text = Path(options.system_prompt["path"]).read_text(encoding="utf-8")
    assert "call mcp__strix__finish_scan." in prompt_text

    # The default engine's own nudge builder is untouched: bare names are
    # correct there and prefixing them would break it.
    default_nudge = tool_required_message(
        finish_tool="finish_scan", attempt=1, limit=3, interactive=True
    )
    assert "mcp__strix__" not in default_nudge
    assert "call respond_to_user" in default_nudge

    claude_code_execution._clear_scratch_cwd("agent-sysprompt")
