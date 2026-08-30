"""Bridge Strix's existing agents-SDK tools into claude-agent-sdk's
in-process MCP tool format, so a Claude-Code-driven agent calls the exact
same tool implementations (sandboxing, output bounding, argument coercion
included) as the default engine.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

from agents.tool import CustomTool, FunctionTool
from agents.tool_context import ToolContext
from claude_agent_sdk import create_sdk_mcp_server
from claude_agent_sdk import tool as sdk_tool

from strix.agents.factory import _custom_tool_as_function_tool


if TYPE_CHECKING:
    from collections.abc import Sequence

    from agents.tool import Tool
    from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool


def bridged_tool_names(tools: Sequence[Tool], *, server_name: str = "strix") -> list[str]:
    """``mcp__<server>__<tool>`` permission names for ``ClaudeAgentOptions.allowed_tools``."""
    return [f"mcp__{server_name}__{_tool_name(t)}" for t in tools]


def _tool_name(tool: Tool) -> str:
    name = getattr(tool, "name", None)
    if not isinstance(name, str) or not name:
        msg = f"tool {tool!r} has no usable name"
        raise ValueError(msg)
    return name


def _input_schema(tool: FunctionTool) -> dict[str, Any]:
    """The tool's full JSON schema, as ``claude_agent_sdk`` requires it.

    ``claude_agent_sdk``'s ``_build_input_schema`` only passes a dict through
    verbatim when it already has top-level ``type``/``properties`` keys;
    otherwise it treats each value as a Python type and defaults every field
    to ``{"type": "string"}``. ``params_json_schema`` already has both keys
    (pydantic-generated), so it must be passed whole -- not just its
    ``properties`` sub-dict.
    """
    schema = tool.params_json_schema
    if isinstance(schema, dict) and "type" in schema and "properties" in schema:
        return schema
    return {"type": "object", "properties": {}}


def _adapt_function_tool(tool: FunctionTool) -> SdkMcpTool[Any]:
    """Wrap an ``agents.tool.FunctionTool`` as an ``SdkMcpTool``.

    Reuses ``tool.on_invoke_tool`` verbatim (already bounded/coerced by
    ``strix.agents.factory``), so behavior is identical to the default
    engine calling the same tool.
    """

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        raw_input = json.dumps(args, ensure_ascii=False)
        ctx = ToolContext(
            context=None,
            tool_name=tool.name,
            tool_call_id=uuid.uuid4().hex,
            tool_arguments=raw_input,
        )
        output = await tool.on_invoke_tool(ctx, raw_input)
        text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        return {"content": [{"type": "text", "text": text}]}

    return sdk_tool(tool.name, tool.description or tool.name, _input_schema(tool))(handler)


def _adapt_custom_tool(tool: CustomTool) -> SdkMcpTool[Any]:
    """Wrap a native ``CustomTool`` (e.g. ``apply_patch``) as an ``SdkMcpTool``.

    Delegates to ``strix.agents.factory``'s existing Responses-custom-tool ->
    function-tool conversion (already used for chat-completions routes), so
    the raw string payload's field name (``patch`` for ``apply_patch``,
    ``input`` otherwise) and error-as-result handling stay identical to the
    default engine instead of being reimplemented here.
    """
    return _adapt_function_tool(_custom_tool_as_function_tool(tool))


def build_mcp_server(tools: Sequence[Tool], *, name: str = "strix") -> McpSdkServerConfig:
    """An in-process MCP server exposing ``tools`` to a Claude Agent SDK session."""
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
