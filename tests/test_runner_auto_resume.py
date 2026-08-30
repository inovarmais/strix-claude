"""Tests for --auto-resume: sleeping until a quota reset instead of exiting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
