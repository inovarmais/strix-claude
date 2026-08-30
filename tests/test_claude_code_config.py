"""Tests for the claude-code engine's model-prefix parsing and CLI checks."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta, timezone
from unittest import mock

import pytest

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


_LOGGED_IN_JSON = """{
  "loggedIn": true,
  "authMethod": "claude.ai",
  "apiProvider": "firstParty",
  "email": "dev@example.com",
  "orgName": "Example Inc",
  "subscriptionType": "team"
}
"""

_LOGGED_OUT_JSON = """{
  "loggedIn": false,
  "authMethod": null,
  "apiProvider": "firstParty",
  "analyticsDisabled": false
}
"""


def _status_run(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["claude", "auth", "status"], returncode=0, stdout=stdout, stderr=""
    )


def test_cli_login_status_reads_logged_in_from_real_json_output() -> None:
    """``claude auth status`` emits JSON; its ``loggedIn`` field is the answer."""
    with (
        mock.patch("shutil.which", return_value="/usr/local/bin/claude"),
        mock.patch("subprocess.run", return_value=_status_run(_LOGGED_IN_JSON)),
    ):
        logged_in, detail = claude_code.cli_login_status()
    assert logged_in is True
    assert detail is not None
    assert "dev@example.com" in detail
    assert "team" in detail
    assert "{" not in detail


def test_cli_login_status_false_on_real_logged_out_json() -> None:
    """A logged-out CLI's JSON contains none of the prose "not logged in"
    markers, so text matching alone reports it as logged in."""
    with (
        mock.patch("shutil.which", return_value="/usr/local/bin/claude"),
        mock.patch("subprocess.run", return_value=_status_run(_LOGGED_OUT_JSON)),
    ):
        logged_in, detail = claude_code.cli_login_status()
    assert logged_in is False
    assert detail is None


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
    # Mock local time: 2024-01-15 20:00:00 UTC (for deterministic test)
    # So 11:45pm (23:45 UTC) will be parsed and converted correctly
    utc_tz = timezone(timedelta(hours=0))
    local_time = datetime(2024, 1, 15, 20, 0, 0, tzinfo=utc_tz)

    mock_now_result = mock.MagicMock()
    mock_now_result.astimezone.return_value = local_time

    with mock.patch("strix.config.claude_code.datetime") as mock_dt:
        mock_dt.now.return_value = mock_now_result
        mock_dt.UTC = UTC
        mock_dt.timedelta = timedelta

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


def test_classify_quota_error_converts_local_time_to_utc() -> None:
    # Mock local time: 2024-01-15 14:00:00 EST (UTC-5)
    # This should convert to 2024-01-15 19:00:00 UTC
    est = timezone(timedelta(hours=-5))
    local_time = datetime(2024, 1, 15, 14, 0, 0, tzinfo=est)

    # Create a mock that returns our local time when astimezone() is called
    mock_now_result = mock.MagicMock()
    mock_now_result.astimezone.return_value = local_time

    with mock.patch("strix.config.claude_code.datetime") as mock_dt:
        # Make datetime.now() return our mock result
        mock_dt.now.return_value = mock_now_result
        # Preserve UTC and timedelta for use in the function
        mock_dt.UTC = UTC
        mock_dt.timedelta = timedelta

        err = claude_code.classify_quota_error("5-hour limit reached - resets 2pm")
        assert err is not None
        assert err.reset_at is not None
        # 2pm EST (UTC-5) should convert to 7pm UTC
        assert err.reset_at.hour == 19
        assert err.reset_at.minute == 0
        assert err.reset_at.tzinfo == UTC
