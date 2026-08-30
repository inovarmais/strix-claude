"""The TUI's transcript must render a Claude-Code-driven agent too.

Under that engine the run's event sink receives ``claude_agent_sdk`` message
objects instead of openai-agents stream events, so the live view projects them
onto the same chat/tool events. Real SDK dataclasses are used here so the
projection is checked against the actual wire-level shapes.
"""

from __future__ import annotations

from claude_agent_sdk import AssistantMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage

from strix.interface.tui.live_view import TuiLiveView


def test_assistant_text_and_tool_call_are_projected() -> None:
    view = TuiLiveView()

    view.ingest_sdk_event(
        "agent-1",
        AssistantMessage(
            content=[
                TextBlock(text="Checking the login flow."),
                ToolUseBlock(
                    id="call-1",
                    name="mcp__strix__create_note",
                    input={"title": "auth", "content": "notes"},
                ),
            ],
            model="claude-sonnet",
        ),
    )

    chat, tool = (event["data"] for event in view.events)
    assert chat["role"] == "assistant"
    assert chat["content"] == "Checking the login flow."
    # The bridge exposes Strix tools as mcp__strix__<name>; the transcript shows
    # them under their own names, like on the default engine.
    assert tool["tool_name"] == "create_note"
    assert tool["args"] == {"title": "auth", "content": "notes"}
    assert tool["status"] == "running"


def test_tool_result_completes_the_matching_call_event() -> None:
    view = TuiLiveView()

    view.ingest_sdk_event(
        "agent-1",
        AssistantMessage(
            content=[ToolUseBlock(id="call-1", name="mcp__strix__list_todos", input={})],
            model="claude-sonnet",
        ),
    )
    view.ingest_sdk_event(
        "agent-1",
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="call-1", content=[{"type": "text", "text": '{"success": true}'}]
                )
            ]
        ),
    )

    (tool,) = (event["data"] for event in view.events)
    assert tool["result"] == {"success": True}
    assert tool["status"] == "completed"


def test_unknown_message_shapes_are_ignored() -> None:
    view = TuiLiveView()

    view.ingest_sdk_event("agent-1", object())
    view.ingest_sdk_event("agent-1", UserMessage(content="plain text turn"))

    assert view.events == []
