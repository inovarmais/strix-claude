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
