"""Tests bridging existing agents-SDK FunctionTool/CustomTool objects into the
claude-agent-sdk's in-process MCP tool format.

``McpSdkServerConfig`` (the real return type of ``create_sdk_mcp_server``) is a
plain dict with ``type``/``name``/``instance`` keys -- ``instance`` is an
``mcp.server.lowlevel.Server`` and does not expose the adapted tool list
directly. So "wraps the same implementation" is verified by dispatching a real
``tools/call`` request through that server's registered MCP request handlers,
which is exactly what a Claude Agent SDK session does at runtime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from agents import function_tool
from agents.tool import CustomTool
from mcp import types as mcp_types

from strix.agents.claude_code_tools import bridged_tool_names, build_mcp_server
from strix.agents.factory import build_strix_agent
from strix.core.agents import AgentCoordinator
from strix.core.execution import _sandbox_capability_tools
from strix.tools.agents_graph.tools import view_agent_graph


if TYPE_CHECKING:
    from pathlib import Path


@function_tool
async def echo(text: str) -> str:
    """Echo the given text back."""
    return f"echo: {text}"


@function_tool
async def count_items(items: list[str]) -> str:
    """Count the given items."""
    return f"count: {len(items)}"


async def _call_tool(server: dict, name: str, arguments: dict) -> mcp_types.CallToolResult:
    handler = server["instance"].request_handlers[mcp_types.CallToolRequest]
    request = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
    )
    result = await handler(request)
    return result.root


async def _list_tools(server: dict) -> dict[str, dict]:
    handler = server["instance"].request_handlers[mcp_types.ListToolsRequest]
    result = await handler(mcp_types.ListToolsRequest(method="tools/list"))
    return {t.name: t.inputSchema for t in result.root.tools}


def test_bridged_tool_names_uses_mcp_permission_format() -> None:
    names = bridged_tool_names([echo], server_name="strix")
    assert names == ["mcp__strix__echo"]


def test_build_mcp_server_shape() -> None:
    server = build_mcp_server([echo], context={}, name="strix")
    assert server["type"] == "sdk"
    assert server["name"] == "strix"


@pytest.mark.asyncio
async def test_build_mcp_server_wraps_same_implementation() -> None:
    server = build_mcp_server([echo], context={}, name="strix")

    result = await _call_tool(server, "echo", {"text": "hi"})

    assert result.isError is False
    assert result.content[0].text == "echo: hi"


@pytest.mark.asyncio
async def test_build_mcp_server_adapts_custom_tool_default_input_field() -> None:
    async def invoke(_ctx: object, raw_input: str) -> str:
        return f"got: {raw_input}"

    read_file = CustomTool(name="read_file", description="read a file", on_invoke_tool=invoke)

    server = build_mcp_server([read_file], context={}, name="strix")
    result = await _call_tool(server, "read_file", {"input": "/etc/hosts"})

    assert result.content[0].text == "got: /etc/hosts"


@pytest.mark.asyncio
async def test_build_mcp_server_adapts_apply_patch_custom_tool_patch_field() -> None:
    """``apply_patch`` uses a ``patch`` input field, not the generic ``input``
    field -- the bridge must go through ``strix.agents.factory``'s existing
    field-name mapping rather than hardcoding ``input`` for every custom tool.
    """

    async def invoke(_ctx: object, raw_input: str) -> str:
        return f"applied: {raw_input}"

    apply_patch = CustomTool(name="apply_patch", description="apply a patch", on_invoke_tool=invoke)

    server = build_mcp_server([apply_patch], context={}, name="strix")
    result = await _call_tool(server, "apply_patch", {"patch": "diff --git a b"})

    assert result.content[0].text == "applied: diff --git a b"


def test_build_mcp_server_rejects_unsupported_tool_type() -> None:
    with pytest.raises(TypeError):
        build_mcp_server([object()], context={}, name="strix")  # type: ignore[list-item]


@pytest.mark.asyncio
async def test_build_mcp_server_preserves_non_string_parameter_schema() -> None:
    """A non-``str`` parameter must keep its real wire-level type, not degrade
    to ``{"type": "string"}`` (which is also what ``claude_agent_sdk`` returns
    for any type it fails to recognize, so a call-level assertion is needed
    too -- a real ``list`` argument must actually validate and dispatch).
    """
    server = build_mcp_server([count_items], context={}, name="strix")

    schemas = await _list_tools(server)
    items_schema = schemas["count_items"]["properties"]["items"]
    assert items_schema["type"] == "array"

    result = await _call_tool(server, "count_items", {"items": ["a", "b", "c"]})

    assert result.isError is False
    assert result.content[0].text == "count: 3"


@pytest.mark.asyncio
async def test_bridged_tool_receives_the_real_run_context() -> None:
    """A bridged tool must reach its per-run dependencies through the agent's
    real context dict. ``view_agent_graph`` reads the coordinator out of it, so
    an empty context is the exact failure this guards against ("Agent
    coordinator not initialized in context")."""
    coordinator = AgentCoordinator()
    await coordinator.register("root-1", "Root Agent", parent_id=None, task="scan")
    context = {"coordinator": coordinator, "agent_id": "root-1", "parent_id": None}

    server = build_mcp_server([view_agent_graph], context=context, name="strix")
    result = await _call_tool(server, "view_agent_graph", {})

    text = result.content[0].text
    assert "not initialized in context" not in text
    assert "Root Agent" in text


@pytest.mark.asyncio
async def test_bridged_tool_without_context_still_reports_the_dependency_error() -> None:
    """Sanity check that the assertion above is meaningful: with no context the
    same tool returns the 'not initialized' error."""
    server = build_mcp_server([view_agent_graph], context={}, name="strix")
    result = await _call_tool(server, "view_agent_graph", {})

    assert "not initialized in context" in result.content[0].text


class _StubExecResult:
    def __init__(self, stdout: bytes, exit_code: int = 0) -> None:
        self.stdout = stdout
        self.stderr = b""
        self.exit_code = exit_code


class _StubSandboxSession:
    """The transport half of a sandbox session: enough for the SDK's shell and
    filesystem capability tools to run for real against a scripted container."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def supports_pty(self) -> bool:
        return False

    def normalize_path(self, path: Path) -> Path:
        return path

    async def exec(self, command: str, **_kwargs: object) -> _StubExecResult:
        self.commands.append(command)
        return _StubExecResult(b"uid=0(root) gid=0(root)\n")


@pytest.mark.asyncio
async def test_sandbox_capability_tools_are_bridged_and_execute() -> None:
    """Shell/filesystem access lives in ``SandboxAgent(capabilities=...)``, not
    in ``agent.tools``. The Claude Code engine has to materialize those tools
    itself and bridge them, or an agent on this engine cannot run a command or
    touch a file in the sandbox at all."""
    agent = build_strix_agent(name="Root Agent", is_root=True)
    session = _StubSandboxSession()

    capability_tools = _sandbox_capability_tools(agent, {"sandbox_session": session})
    names = {tool.name for tool in capability_tools}
    assert {"exec_command", "apply_patch", "view_image"} <= names

    server = build_mcp_server(capability_tools, context={}, name="strix")
    result = await _call_tool(server, "exec_command", {"cmd": "id"})

    assert result.isError is False
    assert "uid=0(root)" in result.content[0].text
    assert session.commands == ["id"]
