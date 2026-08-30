"""Tests that `strix auth status` reports the claude CLI's own login state."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Console

from strix.interface import auth_cli


if TYPE_CHECKING:
    import pytest


def test_status_reports_claude_cli_logged_in(monkeypatch: pytest.MonkeyPatch) -> None:
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
