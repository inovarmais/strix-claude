# Claude Code Agent Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a scan run with `STRIX_LLM=claude-code/<model>`, where the real `claude` CLI (via `claude-agent-sdk`) drives an agent's turn-by-turn tool-calling loop instead of the default OpenAI-Agents-SDK loop, while every side effect against the scan target still goes through Strix's existing sandboxed tools.

**Architecture:** A new engine (`strix/core/claude_code_execution.py`) sits alongside the existing OpenAI-Agents-SDK loop (`strix/core/execution.py`), selected per run by a `claude-code/` model prefix (mirroring the existing `chatgpt/` convention in `strix/config/codex.py`). Strix's existing tools are bridged into the Claude Agent SDK via an in-process MCP server; Claude Code's own native `Bash`/`Read`/`Write`/`WebSearch` stay enabled but scoped to a non-`/workspace` scratch directory with no sandbox network access, so they're useful for the agent's own reasoning but cannot touch the target. Subscription quota exhaustion reuses the existing rate-limit-stops-the-scan path in `strix/core/runner.py`, printing a `strix --resume <run>` hint by default, with a new `--auto-resume` flag to sleep until the reported reset time and continue automatically.

**Tech Stack:** Python 3.12+, `claude-agent-sdk` (new optional dependency, wraps the `claude` CLI subprocess), existing `openai-agents` SDK types reused for interface parity (`RunResultBase`-shaped duck type, `AgentCoordinator`), `pytest` + `pytest-asyncio` + `unittest.mock`.

**Spec:** `docs/superpowers/specs/2026-08-30-claude-code-engine-design.md`

## Global Constraints

- Opt-in only: no change to the default engine's behavior for any `STRIX_LLM` value that isn't `claude-code/<model>`.
- No new OAuth flow. `claude`'s own login/session is used as-is; Strix reads no Anthropic credentials directly.
- No mixing engines within one run: a child spawned by a `claude-code/`-engine agent is built with the same engine.
- Claude Code's native `Bash`/`Read`/`Write` run with `cwd` outside `/workspace` and are never given a path into the sandbox; the target's filesystem/shell/HTTP are reachable only via Strix's MCP-bridged tools.
- Reuse existing state machinery — `strix --resume`, `AgentCoordinator`, `ReportState` — rather than inventing new persistence for quota pause/resume.
- Python 3.12+ typing (`from __future__ import annotations`, `X | None`), matching the rest of the codebase.

---

### Task 1: `claude-code/<model>` prefix parsing and CLI availability check

**Files:**
- Modify: `pyproject.toml` (add optional dependency extra)
- Create: `strix/config/claude_code.py`
- Test: `tests/test_claude_code_config.py`

**Interfaces:**
- Produces: `strix.config.claude_code.ENGINE_PREFIX: str` (`"claude-code/"`), `engine_model(model_name: str | None) -> str | None`, `is_cli_available() -> bool`, `cli_login_status() -> tuple[bool, str | None]` (returns `(logged_in, detail)`; `detail` is a best-effort human-readable line from `claude auth status`, or `None`).

- [ ] **Step 1: Add the optional dependency**

In `pyproject.toml`, in the `[project.optional-dependencies]` table (alongside `vertex` and `bedrock`), add:

```toml
claude-code = ["claude-agent-sdk>=0.1.0"]
```

Check PyPI (`https://pypi.org/project/claude-agent-sdk/`) for the current release and use that as the floor instead of `0.1.0` if it's higher — this package moves fast and `0.1.0` may already be stale by the time this task runs.

- [ ] **Step 2: Write the failing tests for prefix parsing**

```python
# tests/test_claude_code_config.py
"""Tests for the claude-code engine's model-prefix parsing and CLI checks."""

from __future__ import annotations

import subprocess
from unittest import mock

from strix.config import claude_code


def test_engine_model_strips_prefix() -> None:
    assert claude_code.engine_model("claude-code/sonnet") == "sonnet"


def test_engine_model_is_case_insensitive_on_prefix() -> None:
    assert claude_code.engine_model("Claude-Code/opus") == "opus"


def test_engine_model_none_for_other_prefixes() -> None:
    assert claude_code.engine_model("chatgpt/gpt-5.4") is None
    assert claude_code.engine_model("anthropic/claude-sonnet-5") is None


def test_engine_model_none_for_empty_or_missing() -> None:
    assert claude_code.engine_model(None) is None
    assert claude_code.engine_model("") is None
    assert claude_code.engine_model("claude-code/") is None


def test_is_cli_available_true_when_on_path() -> None:
    with mock.patch("shutil.which", return_value="/usr/local/bin/claude"):
        assert claude_code.is_cli_available() is True


def test_is_cli_available_false_when_missing() -> None:
    with mock.patch("shutil.which", return_value=None):
        assert claude_code.is_cli_available() is False


def test_cli_login_status_false_when_cli_missing() -> None:
    with mock.patch("shutil.which", return_value=None):
        assert claude_code.cli_login_status() == (False, None)


def test_cli_login_status_true_on_normal_output() -> None:
    completed = subprocess.CompletedProcess(
        args=["claude", "auth", "status"],
        returncode=0,
        stdout="Logged in via OAuth\nAccount: dev@example.com\nPlan: Max\n",
        stderr="",
    )
    with (
        mock.patch("shutil.which", return_value="/usr/local/bin/claude"),
        mock.patch("subprocess.run", return_value=completed),
    ):
        logged_in, detail = claude_code.cli_login_status()
    assert logged_in is True
    assert detail is not None and "Max" in detail


def test_cli_login_status_false_on_not_logged_in_marker() -> None:
    completed = subprocess.CompletedProcess(
        args=["claude", "auth", "status"],
        returncode=0,
        stdout="Not logged in. Run `claude /login` to authenticate.\n",
        stderr="",
    )
    with (
        mock.patch("shutil.which", return_value="/usr/local/bin/claude"),
        mock.patch("subprocess.run", return_value=completed),
    ):
        logged_in, detail = claude_code.cli_login_status()
    assert logged_in is False


def test_cli_login_status_false_on_command_failure() -> None:
    with (
        mock.patch("shutil.which", return_value="/usr/local/bin/claude"),
        mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=10)),
    ):
        logged_in, detail = claude_code.cli_login_status()
    assert (logged_in, detail) == (False, None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_claude_code_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'strix.config.claude_code'`

- [ ] **Step 3: Implement `strix/config/claude_code.py`**

```python
"""Claude Code engine selection and CLI availability/login checks.

Unlike ``strix.config.codex``, this module manages no credentials of its
own: ``STRIX_LLM=claude-code/<model>`` drives the agent's turn loop via the
real ``claude`` CLI (see ``strix.core.claude_code_execution``), reusing
whatever account it is already logged into (``claude /login``).
"""

from __future__ import annotations

import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)

ENGINE_PREFIX = "claude-code/"

_STATUS_TIMEOUT_S = 10
_NOT_LOGGED_IN_MARKERS = ("not logged in", "no credentials", "not authenticated")


def engine_model(model_name: str | None) -> str | None:
    """The model slug behind a ``claude-code/<model>`` STRIX_LLM, or None."""
    name = (model_name or "").strip()
    if not name.lower().startswith(ENGINE_PREFIX):
        return None
    return name[len(ENGINE_PREFIX) :] or None


def is_cli_available() -> bool:
    return shutil.which("claude") is not None


def cli_login_status() -> tuple[bool, str | None]:
    """Best-effort ``(logged_in, detail)`` for the ``claude`` CLI's own login.

    ``claude auth status`` prints a human-readable report and is documented
    as not meant to be scripted against its exit code, so this parses stdout
    for a known "not logged in" marker rather than trusting the return code.
    """
    if not is_cli_available():
        return False, None
    try:
        result = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=_STATUS_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.debug("claude auth status failed to run", exc_info=True)
        return False, None
    output = (result.stdout or "") + (result.stderr or "")
    lowered = output.lower()
    if any(marker in lowered for marker in _NOT_LOGGED_IN_MARKERS):
        return False, output.strip() or None
    return True, output.strip() or None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_claude_code_config.py -v`
Expected: PASS (10 passed)

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml strix/config/claude_code.py tests/test_claude_code_config.py
git commit -m "feat(claude-code): add claude-code/ engine prefix parsing and CLI checks"
```

---

### Task 2: Subscription quota-error classification

**Files:**
- Modify: `strix/config/claude_code.py`
- Test: `tests/test_claude_code_config.py`

**Interfaces:**
- Consumes: nothing new from Task 1 beyond the module itself.
- Produces: `SubscriptionQuotaExceededError(RuntimeError)` with attributes `.reset_at: datetime | None` and `.raw_message: str`; `classify_quota_error(message: str) -> SubscriptionQuotaExceededError | None`; `_FALLBACK_QUOTA_WAIT: timedelta` (used by Task 8 when no reset time can be parsed).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_claude_code_config.py`:

```python
from datetime import datetime, timedelta, UTC

import pytest


@pytest.mark.parametrize(
    "message",
    [
        "Claude AI usage limit reached, please try again after 3pm",
        "5-hour limit reached - resets 7:30pm",
        "5-hour limit resets 7pm - continuing with usage credits.",
    ],
)
def test_classify_quota_error_detects_known_messages(message: str) -> None:
    err = claude_code.classify_quota_error(message)
    assert err is not None
    assert err.raw_message == message


def test_classify_quota_error_returns_none_for_unrelated_text() -> None:
    assert claude_code.classify_quota_error("connection reset by peer") is None
    assert claude_code.classify_quota_error("invalid API key") is None


def test_classify_quota_error_parses_explicit_clock_time() -> None:
    err = claude_code.classify_quota_error("5-hour limit reached - resets 11:45pm")
    assert err is not None
    assert err.reset_at is not None
    assert err.reset_at.hour == 23
    assert err.reset_at.minute == 45


def test_classify_quota_error_falls_back_when_time_unparseable() -> None:
    before = datetime.now(UTC)
    err = claude_code.classify_quota_error("Claude AI usage limit reached, please try again later")
    assert err is not None
    assert err.reset_at is not None
    assert err.reset_at >= before + claude_code._FALLBACK_QUOTA_WAIT - timedelta(seconds=5)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_claude_code_config.py -v -k quota`
Expected: FAIL — `AttributeError: module 'strix.config.claude_code' has no attribute 'classify_quota_error'`

- [ ] **Step 3: Implement the classifier**

Append to `strix/config/claude_code.py`:

```python
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC


class SubscriptionQuotaExceededError(RuntimeError):
    """A Claude subscription's usage limit was hit mid-scan.

    Carries a best-effort ``reset_at`` (UTC) parsed from the CLI's own
    message, so callers can print it (default behavior) or sleep until it
    (``--auto-resume``, see ``strix.core.runner``).
    """

    def __init__(self, raw_message: str, reset_at: datetime | None) -> None:
        self.raw_message = raw_message
        self.reset_at = reset_at
        super().__init__(raw_message)


_QUOTA_MARKERS = (
    "usage limit reached",
    "5-hour limit reached",
    "5-hour limit resets",
)

_CLOCK_TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.IGNORECASE)

# Anthropic's session limit window; used when the message names no time we
# can parse (e.g. "please try again later"). Best-effort only — a real reset
# time from the message always takes precedence.
_FALLBACK_QUOTA_WAIT = timedelta(hours=5)


def _parse_reset_time(message: str) -> datetime | None:
    match = _CLOCK_TIME_RE.search(message)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    minute = int(match.group(2) or 0)
    if match.group(3).lower() == "pm":
        hour += 12
    now = datetime.now(UTC)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def classify_quota_error(message: str) -> SubscriptionQuotaExceededError | None:
    """Return a ``SubscriptionQuotaExceededError`` if ``message`` looks like a
    Claude subscription quota/usage-limit message, else None.

    The CLI reports this as free text (there is no structured error code for
    it as of this writing), so detection is substring-based on the phrasing
    Anthropic currently uses. If Anthropic changes this phrasing, update
    ``_QUOTA_MARKERS``.
    """
    lowered = message.lower()
    if not any(marker in lowered for marker in _QUOTA_MARKERS):
        return None
    reset_at = _parse_reset_time(message) or (datetime.now(UTC) + _FALLBACK_QUOTA_WAIT)
    return SubscriptionQuotaExceededError(message, reset_at)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_claude_code_config.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add strix/config/claude_code.py tests/test_claude_code_config.py
git commit -m "feat(claude-code): classify subscription quota/usage-limit errors"
```

---

### Task 3: Bridge Strix tools into an in-process MCP server for the SDK

**Files:**
- Create: `strix/agents/claude_code_tools.py`
- Test: `tests/test_claude_code_tools.py`

**Interfaces:**
- Consumes: `agents.tool.Tool` / `FunctionTool` / `CustomTool` (the same objects `strix/agents/factory.py:_BASE_TOOLS` and `register_agent_tools` already produce — no change to any tool implementation).
- Produces: `build_mcp_server(tools: Sequence[Tool], *, name: str = "strix") -> McpSdkServerConfig` and `bridged_tool_names(tools: Sequence[Tool], *, server_name: str = "strix") -> list[str]` (the `mcp__<server>__<tool>` permission names for `ClaudeAgentOptions.allowed_tools`, consumed by Task 4).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_claude_code_tools.py
"""Tests bridging existing agents-SDK FunctionTool objects into the
claude-agent-sdk's in-process MCP tool format."""

from __future__ import annotations

import json

import pytest
from agents import function_tool

from strix.agents.claude_code_tools import bridged_tool_names, build_mcp_server


@function_tool
async def echo(text: str) -> str:
    """Echo the given text back."""
    return f"echo: {text}"


def test_bridged_tool_names_uses_mcp_permission_format() -> None:
    names = bridged_tool_names([echo], server_name="strix")
    assert names == ["mcp__strix__echo"]


@pytest.mark.asyncio
async def test_build_mcp_server_wraps_same_implementation() -> None:
    server = build_mcp_server([echo], name="strix")
    assert server["name"] == "strix"
    (wrapped,) = server["tools"]
    assert wrapped.name == "echo"
    result = await wrapped.handler({"text": "hi"})
    assert result["content"][0]["text"] == "echo: hi"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_claude_code_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'strix.agents.claude_code_tools'`

- [ ] **Step 3: Implement the adapter**

```python
"""Bridge Strix's existing agents-SDK tools into claude-agent-sdk's
in-process MCP tool format, so a Claude-Code-driven agent calls the exact
same tool implementations (sandboxing, output bounding, argument coercion
included) as the default engine.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import create_sdk_mcp_server, tool as sdk_tool

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agents.tool import CustomTool, FunctionTool, Tool
    from claude_agent_sdk import McpSdkServerConfig


def bridged_tool_names(tools: Sequence[Tool], *, server_name: str = "strix") -> list[str]:
    """``mcp__<server>__<tool>`` permission names for ``ClaudeAgentOptions.allowed_tools``."""
    return [f"mcp__{server_name}__{_tool_name(t)}" for t in tools]


def _tool_name(tool: Tool) -> str:
    name = getattr(tool, "name", None)
    if not isinstance(name, str) or not name:
        msg = f"tool {tool!r} has no usable name"
        raise ValueError(msg)
    return name


def _schema_properties(tool: FunctionTool) -> dict[str, Any]:
    schema = tool.params_json_schema or {}
    properties = schema.get("properties")
    return properties if isinstance(properties, dict) else {}


def _adapt_function_tool(tool: FunctionTool) -> Any:
    """Wrap an ``agents.tool.FunctionTool`` as an ``SdkMcpTool``.

    Reuses ``tool.on_invoke_tool`` verbatim (already bounded/coerced by
    ``strix.agents.factory``), so behavior is identical to the default
    engine calling the same tool.
    """

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        raw_input = json.dumps(args, ensure_ascii=False)
        output = await tool.on_invoke_tool(None, raw_input)
        text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        return {"content": [{"type": "text", "text": text}]}

    return sdk_tool(tool.name, tool.description or tool.name, _schema_properties(tool))(handler)


def _adapt_custom_tool(tool: CustomTool) -> Any:
    """Wrap a native ``CustomTool`` (e.g. ``apply_patch``) as an ``SdkMcpTool``.

    Custom tools take one raw string payload; expose it as a single ``input``
    field so the bridged tool's schema matches the non-Responses fallback
    already used for chat-completions routes in ``strix.agents.factory``.
    """

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        text = await tool.on_invoke_tool(None, str(args.get("input", "")))
        return {"content": [{"type": "text", "text": text if isinstance(text, str) else str(text)}]}

    return sdk_tool(tool.name, tool.description or tool.name, {"input": str})(handler)


def build_mcp_server(tools: Sequence[Tool], *, name: str = "strix") -> McpSdkServerConfig:
    """An in-process MCP server exposing ``tools`` to a Claude Agent SDK session."""
    from agents.tool import CustomTool, FunctionTool

    adapted = []
    for t in tools:
        if isinstance(t, FunctionTool):
            adapted.append(_adapt_function_tool(t))
        elif isinstance(t, CustomTool):
            adapted.append(_adapt_custom_tool(t))
        else:
            msg = f"unsupported tool type for claude-code bridging: {type(t)!r}"
            raise TypeError(msg)
    return create_sdk_mcp_server(name=name, version="1.0.0", tools=adapted)
```

Note for the implementer: `claude_agent_sdk.tool`'s exact `input_schema` acceptance (a `dict[str, type]` shorthand vs. a full JSON-schema dict) should be double-checked against the installed package version before this task is considered done — the example in the package's own docs uses the `{"name": str}` shorthand, but Strix's tools carry full JSON-schema `properties` blocks (including nested objects/arrays for some tools). If the shorthand can't express a given tool's schema, pass `_schema_properties(tool)`'s JSON-schema dict directly instead — the search results above show `input_schema` accepts either `type | dict[str, Any]`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_claude_code_tools.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add strix/agents/claude_code_tools.py tests/test_claude_code_tools.py
git commit -m "feat(claude-code): bridge strix tools into an in-process MCP server"
```

---

### Task 4: `ClaudeCodeEngine` run loop

**Files:**
- Create: `strix/core/claude_code_execution.py`
- Test: `tests/test_claude_code_execution.py`

**Interfaces:**
- Consumes: `strix.config.claude_code.engine_model`, `.classify_quota_error`, `SubscriptionQuotaExceededError` (Tasks 1–2); `strix.agents.claude_code_tools.build_mcp_server`, `.bridged_tool_names` (Task 3); `strix.core.hooks.BudgetPausedError`, `BudgetExceededError`, `SubagentBudgetReservedError`, `LLM_TURN_KEY`, `_TURN_WARN_BANDS`, `_ROOT_DIRECTIVES`, `_SUBAGENT_DIRECTIVES`, `_crossed_stage` (module-private but intentionally reused across `strix/core/*` — see comment in code below); `strix.report.state.get_global_report_state` and its `.record_observed_llm_cost(cost: float)` / `.get_total_llm_cost()`; `strix.core.agents.AgentCoordinator` (`.attach_runtime`, `.mark_running`, `.set_status`, `.pause_for_budget`, `.budget_stopped`, `.reserve_stopped`).
- Produces: `ClaudeCodeRunResult` (dataclass with `final_output: str | None`, duck-type compatible with the `getattr(result, "final_output", None)` read in `strix/core/runner.py:590`) and `async def run_claude_code_agent_loop(*, tools: Sequence[Tool], instructions: str, model_slug: str, initial_input: str, max_turns: int, max_budget_usd: float | None, coordinator: AgentCoordinator, agent_id: str, is_root: bool, event_sink: StreamEventSink | None = None) -> ClaudeCodeRunResult | None`, consumed by Task 5.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_claude_code_execution.py
"""Tests for the Claude Code engine's turn loop: turn limits, cost
accounting, and quota-exceeded propagation."""

from __future__ import annotations

from dataclasses import dataclass
from unittest import mock

import pytest

from strix.config.claude_code import SubscriptionQuotaExceededError
from strix.core.claude_code_execution import ClaudeCodeRunResult, run_claude_code_agent_loop
from strix.core.hooks import BudgetExceededError, BudgetPausedError


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

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def query(self, prompt: str) -> None:
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
    final = _FakeResultMessage(is_error=False, result='{"success": true, "scan_completed": true}', total_cost_usd=0.42)
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
async def test_run_raises_budget_exceeded_when_cost_crosses_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
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
async def test_run_raises_subscription_quota_error_on_known_message(monkeypatch: pytest.MonkeyPatch) -> None:
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_claude_code_execution.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'strix.core.claude_code_execution'`

- [ ] **Step 3: Implement the engine loop**

```python
"""Claude Code agent engine: drives one agent's turn loop via the real
``claude`` CLI (through ``claude-agent-sdk``), for STRIX_LLM=claude-code/<model>.

Every tool call still executes through Strix's existing implementations
(bridged in via ``strix.agents.claude_code_tools``); this module is only
responsible for the turn loop, cost/turn accounting, and translating the
SDK's own error signals into the exceptions ``strix.core.runner`` and
``strix.core.agents.AgentCoordinator`` already know how to handle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from strix.agents.claude_code_tools import bridged_tool_names, build_mcp_server
from strix.config.claude_code import classify_quota_error
from strix.core.hooks import (
    # Reused intentionally from strix.core.hooks despite the leading
    # underscore: both engines share one set of wind-down bands/directives
    # so the agent sees identical wording regardless of which engine is
    # driving it. See strix/core/hooks.py.
    _ROOT_DIRECTIVES,
    _SUBAGENT_DIRECTIVES,
    _TURN_WARN_BANDS,
    _crossed_stage,
    BudgetExceededError,
    BudgetPausedError,
    SubagentBudgetReservedError,
)
from strix.report.state import get_global_report_state

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agents.tool import Tool

    from strix.core.agents import AgentCoordinator

logger = logging.getLogger(__name__)

_SUBAGENT_BUDGET_RESERVE = 0.90


@dataclass
class ClaudeCodeRunResult:
    """Duck-type match for the ``.final_output`` read on ``RunResultBase``
    in ``strix/core/runner.py`` — the rest of ``run_strix_scan`` needs
    nothing else from this object."""

    final_output: str | None


def _open_client(*, options: ClaudeAgentOptions) -> ClaudeSDKClient:
    """Seam for tests: patch this to avoid spawning a real ``claude`` process."""
    return ClaudeSDKClient(options=options)


def _wind_down_directive(is_root: bool, stage: int) -> str:
    directives = _ROOT_DIRECTIVES if is_root else _SUBAGENT_DIRECTIVES
    return directives[stage]


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
    event_sink: Any = None,
) -> ClaudeCodeRunResult | None:
    await coordinator.attach_runtime(agent_id, session=None, interrupt_on_message=False)
    await coordinator.mark_running(agent_id)

    if coordinator.budget_stopped:
        await coordinator.set_status(agent_id, "stopped")
        raise BudgetExceededError("scan budget reached")
    if coordinator.reserve_stopped and not is_root:
        await coordinator.set_status(agent_id, "stopped")
        raise SubagentBudgetReservedError("scan reached the sub-agent budget reserve")

    server = build_mcp_server(tools, name="strix")
    allowed = [
        *bridged_tool_names(tools, server_name="strix"),
        "Bash",
        "Read",
        "Write",
        "WebSearch",
    ]
    options = ClaudeAgentOptions(
        system_prompt=instructions,
        model=model_slug,
        mcp_servers={"strix": server},
        allowed_tools=allowed,
        max_turns=max_turns,
        # Auxiliary-only: outside the sandbox workspace, no target network
        # access — see "Native tool scoping" in the design spec.
        cwd=_scratch_cwd(agent_id),
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

            result_text = getattr(message, "result", None)
            is_error = bool(getattr(message, "is_error", False))
            if isinstance(result_text, str):
                final_output = result_text
                if is_error:
                    quota_error = classify_quota_error(result_text)
                    if quota_error is not None:
                        await coordinator.set_status(agent_id, "stopped")
                        raise quota_error

            turns_used = int(getattr(message, "num_turns", turns_used) or turns_used)
            stage = _crossed_stage(turns_used / max_turns, _TURN_WARN_BANDS) if max_turns else None
            if stage is not None:
                logger.info(
                    "agent %s turn budget stage %d: %s",
                    agent_id,
                    stage,
                    _wind_down_directive(is_root, stage),
                )

            cost = getattr(message, "total_cost_usd", None)
            if isinstance(cost, int | float):
                report_state = get_global_report_state()
                if report_state is not None:
                    report_state.record_observed_llm_cost(float(cost))
                    if max_budget_usd is not None:
                        total = report_state.get_total_llm_cost()
                        if total >= max_budget_usd:
                            await coordinator.set_status(agent_id, "stopped")
                            raise BudgetExceededError(
                                f"Token budget of ${max_budget_usd:.2f} exceeded "
                                f"(spent ${total:.4f})"
                            )
                        reserve_limit = max_budget_usd * _SUBAGENT_BUDGET_RESERVE
                        if not is_root and total >= reserve_limit:
                            await coordinator.set_status(agent_id, "stopped")
                            raise SubagentBudgetReservedError(
                                f"Sub-agent budget reserve reached: spent ${total:.4f} of "
                                f"${max_budget_usd:.2f}"
                            )

    await coordinator.set_status(agent_id, "completed")
    return ClaudeCodeRunResult(final_output=final_output)


def _scratch_cwd(agent_id: str) -> str:
    from pathlib import Path
    from tempfile import gettempdir

    path = Path(gettempdir()) / "strix-claude-code-scratch" / agent_id
    path.mkdir(parents=True, exist_ok=True)
    return str(path)
```

Notes for the implementer, to verify against the installed `claude-agent-sdk` version before merging (flagged in the design spec's open items, not guessed here):
- `ClaudeSDKClient.receive_response()` is documented as an async generator; confirm the exact message types it yields (`AssistantMessage`/`ResultMessage`/etc.) and that `is_error`/`result`/`total_cost_usd`/`num_turns` land on the same message object rather than split across types — adjust the `getattr` reads above if the final result fields arrive on a distinct `ResultMessage` only.
- `BudgetPausedError` (interactive pause) is imported but intentionally unused in this v1 loop — `--auto-resume`/interactive pause-on-quota for the Claude Code engine is out of scope for this plan (interactive mode isn't wired to this engine in Task 9); remove the unused import if a linter flags it, or wire it in when interactive support is added.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_claude_code_execution.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add strix/core/claude_code_execution.py tests/test_claude_code_execution.py
git commit -m "feat(claude-code): add the Claude Code engine run loop"
```

---

### Task 5: Dispatch to the new engine from the existing run loop

**Files:**
- Modify: `strix/core/execution.py:186` (`run_agent_loop`)
- Test: `tests/test_claude_code_execution.py`

**Interfaces:**
- Consumes: `run_agent_loop`'s existing signature (unchanged — `agent`, `initial_input`, `run_config`, `context`, `max_turns`, `coordinator`, `agent_id`, `interactive`, `session`, `start_parked`, `event_sink`, `hooks`); `strix.config.claude_code.engine_model`; `run_claude_code_agent_loop` (Task 4); the existing `_run_config_model(run_config) -> str | None` helper already defined in `strix/core/execution.py`.
- Produces: no new public interface — `run_agent_loop` transparently routes to the Claude Code engine when the model carries the `claude-code/` prefix.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_claude_code_execution.py

from unittest import mock

import pytest
from agents import RunConfig

from strix.core.execution import run_agent_loop


@pytest.mark.asyncio
async def test_run_agent_loop_dispatches_to_claude_code_engine() -> None:
    coordinator = mock.AsyncMock()
    coordinator.budget_stopped = False
    coordinator.reserve_stopped = False
    fake_agent = mock.MagicMock()
    fake_agent.instructions = "you are a test agent"
    fake_agent.tools = []

    with mock.patch(
        "strix.core.execution.run_claude_code_agent_loop",
        new=mock.AsyncMock(return_value="sentinel-result"),
    ) as bridged:
        result = await run_agent_loop(
            agent=fake_agent,
            initial_input="do the thing",
            run_config=RunConfig(model="claude-code/sonnet"),
            context={"parent_id": None},
            max_turns=5,
            coordinator=coordinator,
            agent_id="agent-1",
            interactive=False,
        )

    bridged.assert_awaited_once()
    assert result == "sentinel-result"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_claude_code_execution.py -v -k dispatches`
Expected: FAIL — `run_agent_loop` runs the OpenAI-Agents-SDK path instead (no such attribute `run_claude_code_agent_loop` to patch, or the fake agent errors when `Runner.run_streamed` tries to use it).

- [ ] **Step 3: Add the dispatch branch**

In `strix/core/execution.py`, add the import near the top (with the other `strix.*` imports):

```python
from strix.config import claude_code
from strix.core.claude_code_execution import run_claude_code_agent_loop
```

Then, at the very top of `run_agent_loop` (before the existing `await coordinator.attach_runtime(...)` line, i.e. right after the `async def run_agent_loop(...) -> RunResultBase | None:` signature and its docstring if any), insert:

```python
    model_slug = claude_code.engine_model(_run_config_model(run_config))
    if model_slug is not None:
        is_root = context.get("parent_id") is None
        return await run_claude_code_agent_loop(
            tools=list(getattr(agent, "tools", []) or []),
            instructions=_agent_instructions(agent),
            model_slug=model_slug,
            initial_input=str(initial_input),
            max_turns=max_turns,
            max_budget_usd=context.get("max_budget_usd"),
            coordinator=coordinator,
            agent_id=agent_id,
            is_root=is_root,
            event_sink=event_sink,
        )
```

`_agent_instructions` and `_run_config_model` already exist in this file (used elsewhere for refusal/error reporting) — reuse them rather than re-deriving instructions/model. Leave the rest of `run_agent_loop` (the existing OpenAI-Agents-SDK path) untouched.

Note: `context.get("max_budget_usd")` assumes the run's max-budget is reachable through the per-agent `context` dict at this point. Confirm this key during implementation by tracing where `context` is built in `strix/core/runner.py` (search for `"max_budget_usd"` — if it isn't already in that dict, add it there when constructing `context`, since the Claude Code engine has no other way to see the run's budget ceiling).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_claude_code_execution.py -v`
Expected: PASS

- [ ] **Step 5: Run the full existing test suite to check for regressions**

Run: `uv run pytest tests/ -v`
Expected: PASS (no change to any pre-existing test)

- [ ] **Step 6: Commit**

```bash
git add strix/core/execution.py tests/test_claude_code_execution.py
git commit -m "feat(claude-code): dispatch run_agent_loop to the Claude Code engine"
```

---

### Task 6: `strix auth status` reports the `claude` CLI's login state

**Files:**
- Modify: `strix/interface/auth_cli.py:245` (`_status`)
- Test: `tests/test_auth_cli_claude_code.py`

**Interfaces:**
- Consumes: `strix.config.claude_code.is_cli_available`, `.cli_login_status`, `.engine_model`.
- Produces: no new function — `_status` prints one additional block; behavior only, not a new interface for other tasks.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_auth_cli_claude_code.py
"""Tests that `strix auth status` reports the claude CLI's own login state."""

from __future__ import annotations

from unittest import mock

from rich.console import Console

from strix.interface import auth_cli


def test_status_reports_claude_cli_logged_in(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(auth_cli.codex, "read_record", lambda: None)
    monkeypatch.setattr(
        "strix.config.claude_code.is_cli_available", lambda: True
    )
    monkeypatch.setattr(
        "strix.config.claude_code.cli_login_status", lambda: (True, "Plan: Max")
    )
    console = Console(record=True)
    auth_cli._status(console)
    output = console.export_text()
    assert "claude" in output.lower()
    assert "Max" in output


def test_status_reports_claude_cli_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_cli.codex, "read_record", lambda: None)
    monkeypatch.setattr("strix.config.claude_code.is_cli_available", lambda: False)
    console = Console(record=True)
    auth_cli._status(console)
    output = console.export_text()
    assert "claude" in output.lower()
    assert "not installed" in output.lower() or "not found" in output.lower()
```

Add `import pytest` at the top of the test file (needed for the `monkeypatch` fixture type hint).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_auth_cli_claude_code.py -v`
Expected: FAIL — current `_status` output has no mention of "claude"/CLI availability.

- [ ] **Step 3: Extend `_status`**

In `strix/interface/auth_cli.py`, add the import near the existing `from strix.config import codex, load_settings`:

```python
from strix.config import claude_code, codex, load_settings
```

Then, at the end of `_status` (after the existing ChatGPT block, before its `return 0`), add:

```python
    console.print()
    if claude_code.is_cli_available():
        logged_in, detail = claude_code.cli_login_status()
        if logged_in:
            console.print("[green]claude CLI:[/] logged in.")
            if detail:
                console.print(f"  {detail.splitlines()[0]}")
            if claude_code.engine_model(settings.llm.model):
                console.print(
                    f"  Runs use the Claude Code engine (STRIX_LLM=[bold]{settings.llm.model}[/])."
                )
            else:
                console.print(
                    "  [yellow]Note:[/] set [cyan]STRIX_LLM[/] to e.g. "
                    "[cyan]claude-code/sonnet[/] to use it."
                )
        else:
            console.print(
                "[yellow]claude CLI:[/] installed but not logged in. Run [cyan]claude /login[/]."
            )
    else:
        console.print(
            "[dim]claude CLI: not installed.[/] Install it to use STRIX_LLM=claude-code/<model>."
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_auth_cli_claude_code.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add strix/interface/auth_cli.py tests/test_auth_cli_claude_code.py
git commit -m "feat(claude-code): report claude CLI login state in strix auth status"
```

---

### Task 7: Fail fast at startup when the engine is selected but unavailable

**Files:**
- Modify: `strix/interface/environment.py:22` (`validate_environment`)
- Test: `tests/test_environment_claude_code.py`

**Interfaces:**
- Consumes: `strix.config.claude_code.engine_model`, `.is_cli_available`, `.cli_login_status`.
- Produces: none new — behavior only.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_environment_claude_code.py
"""Tests that startup validation fails fast for a misconfigured claude-code engine."""

from __future__ import annotations

from unittest import mock

import pytest

from strix.interface import environment


def _settings(model: str) -> mock.MagicMock:
    settings = mock.MagicMock()
    settings.llm.model = model
    return settings


def test_exits_when_cli_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(environment, "load_settings", lambda: _settings("claude-code/sonnet"))
    monkeypatch.setattr("strix.config.claude_code.is_cli_available", lambda: False)
    with pytest.raises(SystemExit):
        environment.validate_environment()


def test_exits_when_cli_not_logged_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(environment, "load_settings", lambda: _settings("claude-code/sonnet"))
    monkeypatch.setattr("strix.config.claude_code.is_cli_available", lambda: True)
    monkeypatch.setattr("strix.config.claude_code.cli_login_status", lambda: (False, None))
    with pytest.raises(SystemExit):
        environment.validate_environment()


def test_passes_when_cli_available_and_logged_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(environment, "load_settings", lambda: _settings("claude-code/sonnet"))
    monkeypatch.setattr("strix.config.claude_code.is_cli_available", lambda: True)
    monkeypatch.setattr("strix.config.claude_code.cli_login_status", lambda: (True, "Plan: Max"))
    environment.validate_environment()  # should not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_environment_claude_code.py -v`
Expected: FAIL — today `validate_environment` only special-cases `codex.subscription_model`, so a `claude-code/sonnet` model falls through to the generic "missing LLM_API_KEY" checks rather than exiting for CLI-availability reasons and never hits `SystemExit` for these specific fixtures.

- [ ] **Step 3: Add the check**

In `strix/interface/environment.py`, add the import:

```python
from strix.config import claude_code, codex, load_settings
```

Then, right after the existing `if codex.subscription_model(settings.llm.model): ... return` block, add:

```python
    if claude_code.engine_model(settings.llm.model):
        if not claude_code.is_cli_available():
            console.print(
                f"[red]STRIX_LLM={settings.llm.model} uses the Claude Code engine, "
                "but the `claude` CLI isn't installed.[/] Install Claude Code, then re-run."
            )
            sys.exit(1)
        logged_in, _detail = claude_code.cli_login_status()
        if not logged_in:
            console.print(
                f"[red]STRIX_LLM={settings.llm.model} uses the Claude Code engine, "
                "but `claude` isn't signed in.[/] Run [cyan]claude /login[/] first."
            )
            sys.exit(1)
        logger.info("Environment OK (Claude Code engine)")
        return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_environment_claude_code.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add strix/interface/environment.py tests/test_environment_claude_code.py
git commit -m "feat(claude-code): fail fast when the claude CLI is missing or logged out"
```

---

### Task 8: `--auto-resume` flag and sleep-then-continue on quota exhaustion

**Files:**
- Modify: `strix/interface/cli_args.py` (near the existing `--resume`/`--max-budget` flags)
- Modify: `strix/core/runner.py:610` (the `except RateLimitError` block) and `strix/core/runner.py:182` (`run_strix_scan` signature)
- Test: `tests/test_runner_auto_resume.py`

**Interfaces:**
- Consumes: `strix.config.claude_code.SubscriptionQuotaExceededError` (Task 2); `run_strix_scan`'s existing full signature (see Task 9 for the exact call-site parameters already in use).
- Produces: `run_strix_scan(..., auto_resume: bool = False)` (new keyword-only parameter, defaulted so every existing caller keeps working unchanged); `args.auto_resume: bool` on the parsed CLI namespace.

- [ ] **Step 1: Add the CLI flag**

In `strix/interface/cli_args.py`, near the existing `--resume` argument definition, add:

```python
    parser.add_argument(
        "--auto-resume",
        action="store_true",
        default=False,
        help=(
            "On a Claude Code subscription quota/usage-limit stop, sleep until the "
            "reported reset time and continue automatically instead of exiting. "
            "For unattended/CI runs; the default is to exit and print "
            "'strix --resume <run_name>'."
        ),
    )
```

- [ ] **Step 2: Write the failing test for the runner behavior**

```python
# tests/test_runner_auto_resume.py
"""Tests for --auto-resume: sleeping until a quota reset instead of exiting."""

from __future__ import annotations

from datetime import datetime, timedelta, UTC
from unittest import mock

import pytest

from strix.config.claude_code import SubscriptionQuotaExceededError
from strix.core import runner


@pytest.mark.asyncio
async def test_auto_resume_sleeps_then_recurses_once(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_at = datetime.now(UTC) + timedelta(seconds=30)
    calls = {"n": 0}

    async def fake_run_once(*_args: object, **_kwargs: object) -> str | None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise SubscriptionQuotaExceededError("5-hour limit reached - resets soon", reset_at)
        return "done"

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(runner, "_run_strix_scan_once", fake_run_once)
    monkeypatch.setattr(runner.asyncio, "sleep", fake_sleep)

    result = await runner.run_strix_scan(
        scan_config={"targets": []},
        scan_id="scan-test",
        image="strix-sandbox:latest",
        auto_resume=True,
    )

    assert result == "done"
    assert calls["n"] == 2
    assert sleep_calls and sleep_calls[0] > 0


@pytest.mark.asyncio
async def test_without_auto_resume_returns_none_and_does_not_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_at = datetime.now(UTC) + timedelta(seconds=30)

    async def fake_run_once(*_args: object, **_kwargs: object) -> str | None:
        raise SubscriptionQuotaExceededError("5-hour limit reached - resets soon", reset_at)

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(runner, "_run_strix_scan_once", fake_run_once)
    monkeypatch.setattr(runner.asyncio, "sleep", fake_sleep)

    result = await runner.run_strix_scan(
        scan_config={"targets": []},
        scan_id="scan-test",
        image="strix-sandbox:latest",
        auto_resume=False,
    )

    assert result is None
    assert sleep_calls == []
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_runner_auto_resume.py -v`
Expected: FAIL — `run_strix_scan` has no `auto_resume` parameter yet, and there is no `_run_strix_scan_once` to patch.

- [ ] **Step 4: Refactor `run_strix_scan` into a thin auto-resume wrapper**

In `strix/core/runner.py`:

1. Rename the current `async def run_strix_scan(...)` function (the one whose body starts at line 182 and contains the `try`/`except BudgetExceededError`/`except RateLimitError` block read above) to `async def _run_strix_scan_once(...)`, keeping its full existing body and signature unchanged, **except**: add the new `SubscriptionQuotaExceededError` to the existing rate-limit `except` clause so it's caught the same way a generic provider rate limit is today. Change:

```python
    except RateLimitError as exc:
```

to:

```python
    except (RateLimitError, SubscriptionQuotaExceededError) as exc:
```

(add `from strix.config.claude_code import SubscriptionQuotaExceededError` to the imports at the top of the file). Leave the body of that `except` block (the `logger.warning(...)` + `coordinator.set_status(root_id, "stopped")` + `return None`) exactly as-is — it already logs the right "Resume with 'strix --resume %s'" guidance for either exception type, since both carry a message via `str(exc)`.

2. Add a new top-level function with the *original* name and a superset of the original signature, so every existing caller (`strix/interface/cli.py:193`, `strix/interface/tui/runtime.py:177`) keeps working by only adding one new optional keyword argument:

```python
async def run_strix_scan(
    *,
    auto_resume: bool = False,
    **kwargs: Any,
) -> RunResultBase | None:
    """Run (or resume) one Strix scan; see ``_run_strix_scan_once`` for the
    full parameter list and behavior.

    ``auto_resume=True`` additionally catches a Claude Code subscription
    quota stop, sleeps until the reported reset time, and continues the
    same run (by scan_id) instead of returning ``None`` — see the design
    spec's "Quota-exceeded handling and resume" section.
    """
    scan_id = kwargs.get("scan_id")
    while True:
        try:
            return await _run_strix_scan_once(**kwargs)
        except SubscriptionQuotaExceededError as exc:
            if not auto_resume or scan_id is None:
                logger.warning(
                    "Scan %s stopped: Claude Code subscription quota exhausted (%s). "
                    "Resume with 'strix --resume %s' once it resets.",
                    scan_id,
                    exc,
                    scan_id,
                )
                return None
            wait_seconds = max((exc.reset_at - datetime.now(UTC)).total_seconds(), 0.0) if exc.reset_at else 0.0
            logger.warning(
                "Scan %s paused: Claude Code subscription quota exhausted (%s). "
                "--auto-resume is set; sleeping %.0fs until the reported reset.",
                scan_id,
                exc,
                wait_seconds,
            )
            await asyncio.sleep(wait_seconds)
            kwargs["scan_id"] = scan_id  # unchanged; the next call resumes this run
```

Add `from datetime import datetime, UTC` to the file's imports if not already present (check first — `runner.py` may already import `datetime` for other fields; reuse the existing import rather than duplicating it).

Note for the implementer: since `_run_strix_scan_once` is renamed from the original `run_strix_scan`, re-check every other in-file reference to the old name (e.g. recursive calls, log messages referencing the function by name) and update them to `_run_strix_scan_once` — the read excerpts above did not show any, but confirm with `grep -n "run_strix_scan" strix/core/runner.py` before considering this task done.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_runner_auto_resume.py -v`
Expected: PASS

- [ ] **Step 6: Run the full existing test suite to check for regressions**

Run: `uv run pytest tests/ -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add strix/interface/cli_args.py strix/core/runner.py tests/test_runner_auto_resume.py
git commit -m "feat(claude-code): add --auto-resume to sleep-and-continue past a quota stop"
```

---

### Task 9: Wire `--auto-resume` through the CLI entry point; docs

**Files:**
- Modify: `strix/interface/cli.py:193` (the `run_strix_scan(...)` call site)
- Modify: `docs/llm-providers` (add a page or section; follow the existing structure under `docs/llm-providers/`)
- Test: `tests/test_cli_auto_resume.py`

**Interfaces:**
- Consumes: `args.auto_resume` (Task 8), `run_strix_scan(..., auto_resume=...)` (Task 8).
- Produces: none — final integration glue + docs.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_auto_resume.py
"""Test that the CLI entry point forwards --auto-resume to run_strix_scan."""

from __future__ import annotations

from unittest import mock

import pytest


@pytest.mark.asyncio
async def test_cli_forwards_auto_resume_flag() -> None:
    from strix.interface import cli

    args = mock.MagicMock()
    args.auto_resume = True
    args.run_name = "scan-test"
    args.local_sources = []
    args.workspace_files = None
    args.interactive = False
    args.max_budget_usd = None
    args.max_turns = 25

    with mock.patch("strix.interface.cli.run_strix_scan", new=mock.AsyncMock()) as run_mock:
        # call whichever function in strix/interface/cli.py wraps the
        # try/Live block shown around line 193 — name TBD by the
        # implementer reading that file's enclosing function signature.
        await cli._run_headless_scan(args)  # placeholder name; see Step 2

    _, called_kwargs = run_mock.call_args
    assert called_kwargs.get("auto_resume") is True
```

Note for the implementer: `cli._run_headless_scan` is a placeholder — open `strix/interface/cli.py` and use the actual enclosing function name for the block shown around line 193 (the one containing `await run_strix_scan(scan_config=scan_config, scan_id=args.run_name, ...)`). Fix the test's call to match before running it.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli_auto_resume.py -v`
Expected: FAIL, either on the placeholder name (fix per the note above) or on the missing `auto_resume` kwarg.

- [ ] **Step 3: Forward the flag**

In `strix/interface/cli.py`, add `auto_resume=bool(getattr(args, "auto_resume", False)),` to the existing `await run_strix_scan(...)` call (the one shown at line 193 in the excerpt above), following the same `getattr(args, ..., default)` style already used for `interactive`/`max_budget_usd`/`max_turns` in that call:

```python
                await run_strix_scan(
                    scan_config=scan_config,
                    scan_id=args.run_name,
                    image=_resolve_sandbox_image(),
                    local_sources=getattr(args, "local_sources", None) or [],
                    extra_files=read_workspace_files(getattr(args, "workspace_files", None)),
                    interactive=bool(getattr(args, "interactive", False)),
                    max_budget_usd=getattr(args, "max_budget_usd", None),
                    max_turns=getattr(args, "max_turns", DEFAULT_MAX_TURNS),
                    status_sink=_note_startup_phase,
                    auto_resume=bool(getattr(args, "auto_resume", False)),
                )
```

Do not wire `auto_resume` into `strix/interface/tui/runtime.py:177` — the interactive TUI path has its own budget-pause UX (`BudgetPausedError` parks the agent for the user to resume live) and `--auto-resume` is scoped to headless/unattended runs per the design spec.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_auto_resume.py -v`
Expected: PASS

- [ ] **Step 5: Add a docs page**

Create `docs/llm-providers/claude-code.mdx` (match the frontmatter/structure of an existing file in that directory — read one sibling file first to copy its frontmatter shape) covering: what the engine is, `STRIX_LLM=claude-code/<model>` usage, the `claude /login` prerequisite, `strix auth status` reporting it, and `--auto-resume` for quota exhaustion. Keep it to the same length/style as the existing provider pages in that directory.

- [ ] **Step 6: Run the full test suite**

Run: `uv run pytest tests/ -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add strix/interface/cli.py tests/test_cli_auto_resume.py docs/llm-providers/claude-code.mdx
git commit -m "feat(claude-code): forward --auto-resume from the CLI; add docs"
```

---

## Self-Review Notes

- **Spec coverage:** engine seam (Tasks 4–5), tool bridging (Task 3), native tool scoping (Task 4's `cwd`/`allowed_tools`), auth/setup UX (Tasks 6–7), quota classification (Task 2), default exit-and-print + `--auto-resume` (Task 8), CLI wiring + docs (Task 9), dependency addition (Task 1). All spec sections have a task.
- **Known follow-ups intentionally left out of this plan** (call out to the user before merging, don't silently drop): interactive-mode support for the Claude Code engine (Task 4's loop does not implement the `interactive`/pause-for-live-input path `run_agent_loop` has); multi-agent graph children spawned by a Claude-Code-driven agent are not exercised by any test in this plan (the spec requires "child inherits parent's engine" — Task 5's dispatch is generic enough to cover it since `run_agent_loop` is the same function children go through, but add an explicit child-inherits-engine test during implementation if `spawn_child_agent` turns out to route model selection differently than assumed here).
- **Type consistency:** `SubscriptionQuotaExceededError` (Task 2) is the one type threaded through Tasks 4, 8, and 9 — same import path (`strix.config.claude_code`) used everywhere it appears above.
