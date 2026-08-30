"""Tests for --auto-resume: sleeping until a quota reset instead of exiting."""

from __future__ import annotations

import logging
import types
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from agents import ModelSettings

import strix.tools.notes.tools as notes_tools
import strix.tools.todo.tools as todo_tools
from strix.config.claude_code import SubscriptionQuotaExceededError
from strix.core import runner
from strix.core.agents import AgentCoordinator
from strix.runtime import session_manager


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
async def test_without_auto_resume_returns_none_and_does_not_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


def _patch_run_once_scaffolding(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Mock everything _run_strix_scan_once needs before its real try/except,
    so only ``run_agent_loop`` (the deep call that can raise the quota error)
    is left doing anything interesting. Mirrors tests/test_runner_rate_limit.py.
    """
    monkeypatch.setattr(runner, "run_dir_for", lambda _scan_id: tmp_path)
    monkeypatch.setattr(runner, "runtime_state_dir", lambda _run_dir: tmp_path)
    monkeypatch.setattr(runner, "setup_scan_logging", lambda _run_dir: lambda: None)
    monkeypatch.setattr(runner, "set_scan_id", lambda _scan_id: None)

    settings = types.SimpleNamespace(
        llm=types.SimpleNamespace(
            model="openai/gpt-4o",
            reasoning_effort="high",
            force_required_tool_choice=False,
            timeout=300,
            prompt_cache=True,
            extra_headers=None,
        ),
        runtime=types.SimpleNamespace(max_context_images=3),
    )
    monkeypatch.setattr(runner, "load_settings", lambda: settings)
    monkeypatch.setattr(runner, "configure_sdk_model_defaults", lambda _settings: None)
    monkeypatch.setattr(
        runner, "uses_chat_completions_tool_schema", lambda _model, _settings: False
    )

    monkeypatch.setattr(todo_tools, "hydrate_todos_from_disk", lambda _state_dir: None)
    monkeypatch.setattr(notes_tools, "hydrate_notes_from_disk", lambda _state_dir: None)

    async def _create_or_reuse(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"client": object(), "session": object(), "caido_client": None}

    async def _cleanup(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(session_manager, "create_or_reuse", _create_or_reuse)
    monkeypatch.setattr(session_manager, "cleanup", _cleanup)

    monkeypatch.setattr(runner, "build_root_task", lambda _scan_config: "task")
    monkeypatch.setattr(runner, "build_scope_context", lambda _scan_config: "")
    monkeypatch.setattr(runner, "make_model_settings", lambda *_args, **_kwargs: ModelSettings())
    monkeypatch.setattr(runner, "build_strix_agent", lambda **_kwargs: object())
    monkeypatch.setattr(runner, "make_child_factory", lambda **_kwargs: lambda **_k: object())
    monkeypatch.setattr(runner, "open_agent_session", lambda _root_id, _db: object())


@pytest.mark.asyncio
async def test_subscription_quota_exceeded_propagates_out_of_run_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A SubscriptionQuotaExceededError raised deep in the real turn loop must
    escape ``_run_strix_scan_once`` uncaught (not be swallowed into a ``None``
    return) so the ``run_strix_scan`` wrapper's own except clause can act on
    it. This is the regression test for the bug where the inner function's
    ``except`` tuple stole the exception before the wrapper ever saw it.
    """
    _patch_run_once_scaffolding(monkeypatch, tmp_path)

    reset_at = datetime.now(UTC) + timedelta(seconds=30)

    async def _raise_quota_exceeded(*_args: Any, **_kwargs: Any) -> None:
        raise SubscriptionQuotaExceededError("5-hour limit reached - resets soon", reset_at)

    monkeypatch.setattr(runner, "run_agent_loop", _raise_quota_exceeded)

    coordinator = AgentCoordinator()

    with pytest.raises(SubscriptionQuotaExceededError):
        await runner._run_strix_scan_once(
            scan_config={"targets": [], "scan_mode": "deep"},
            scan_id="scan-test",
            image="img",
            coordinator=coordinator,
        )


@pytest.mark.asyncio
async def test_quota_stop_is_not_logged_as_a_crash_and_keeps_the_stopped_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A quota stop is a clean pause: it must not fall through to the generic
    ``except BaseException`` handler, which logs a crash traceback and
    overwrites the ``stopped`` status with ``failed``."""
    _patch_run_once_scaffolding(monkeypatch, tmp_path)
    reset_at = datetime.now(UTC) + timedelta(seconds=30)

    async def _raise_quota_exceeded(*_args: Any, **_kwargs: Any) -> None:
        raise SubscriptionQuotaExceededError("5-hour limit reached - resets soon", reset_at)

    monkeypatch.setattr(runner, "run_agent_loop", _raise_quota_exceeded)
    coordinator = AgentCoordinator()

    with caplog.at_level(logging.INFO), pytest.raises(SubscriptionQuotaExceededError):
        await runner._run_strix_scan_once(
            scan_config={"targets": [], "scan_mode": "deep"},
            scan_id="scan-test",
            image="img",
            coordinator=coordinator,
        )

    assert set(coordinator.statuses.values()) == {"stopped"}
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert "quota exhausted" in caplog.text


@pytest.mark.asyncio
async def test_quota_stop_prints_reset_time_and_resume_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The user-facing hint names the reset time and goes to the console -- the
    scan's logging handlers are already torn down by the time it runs."""
    reset_at = datetime(2026, 8, 30, 19, 30, tzinfo=UTC)

    async def fake_run_once(*_args: object, **_kwargs: object) -> str | None:
        raise SubscriptionQuotaExceededError("5-hour limit reached", reset_at)

    monkeypatch.setattr(runner, "_run_strix_scan_once", fake_run_once)

    result = await runner.run_strix_scan(
        scan_config={"targets": []},
        scan_id="scan-test",
        image="strix-sandbox:latest",
        auto_resume=False,
    )

    out = capsys.readouterr().out
    assert result is None
    assert "2026-08-30 19:30 UTC" in out
    assert "strix --resume scan-test" in out


@pytest.mark.asyncio
async def test_auto_resume_without_scan_id_logs_distinctly_and_returns_none(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    reset_at = datetime.now(UTC) + timedelta(seconds=30)

    async def fake_run_once(*_args: object, **_kwargs: object) -> str | None:
        raise SubscriptionQuotaExceededError("5-hour limit reached - resets soon", reset_at)

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(runner, "_run_strix_scan_once", fake_run_once)
    monkeypatch.setattr(runner.asyncio, "sleep", fake_sleep)

    with caplog.at_level(logging.WARNING):
        result = await runner.run_strix_scan(
            scan_config={"targets": []},
            scan_id=None,
            image="strix-sandbox:latest",
            auto_resume=True,
        )

    assert result is None
    assert sleep_calls == []
    assert "cannot be honored" in caplog.text


@pytest.mark.asyncio
async def test_auto_resume_gives_up_after_max_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_at = datetime.now(UTC) + timedelta(seconds=1)
    calls = {"n": 0}

    async def fake_run_once(*_args: object, **_kwargs: object) -> str | None:
        calls["n"] += 1
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
        auto_resume=True,
    )

    assert result is None
    assert calls["n"] == runner._MAX_AUTO_RESUME_RETRIES + 1
    assert len(sleep_calls) == runner._MAX_AUTO_RESUME_RETRIES
