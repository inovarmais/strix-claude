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
        logged_in, _detail = claude_code.cli_login_status()
    assert logged_in is False


def test_cli_login_status_false_on_command_failure() -> None:
    with (
        mock.patch("shutil.which", return_value="/usr/local/bin/claude"),
        mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=10),
        ),
    ):
        logged_in, detail = claude_code.cli_login_status()
    assert (logged_in, detail) == (False, None)
