"""The model preflight must skip its litellm ping for the Claude Code engine.

A ``claude-code/<model>`` route never builds an Agents-SDK ``Model`` -- it
drives the real ``claude`` CLI instead (``strix.core.claude_code_execution``).
Calling ``StrixProvider().get_model()`` for it raised
``litellm.BadRequestError: LLM Provider NOT provided`` and failed every
non-interactive scan before it started.
"""

from __future__ import annotations

import sys
from unittest import mock

import pytest

import strix.interface.main  # noqa: F401  (registers it in sys.modules)
from strix.interface import scan_setup


# ``strix.interface/__init__.py`` does ``from .main import main``, which
# overwrites the ``main`` submodule with that function on the package's own
# namespace -- so any dotted/attribute-based import of ``strix.interface.main``
# resolves to the function, not the module. Only ``sys.modules`` still holds
# the real module.
interface_main = sys.modules["strix.interface.main"]


@pytest.mark.asyncio
async def test_preflight_skips_litellm_for_claude_code_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("StrixProvider().get_model() must not be called for claude-code/*")

    monkeypatch.setattr(
        "strix.config.models.StrixProvider.get_model",
        _fail_if_called,
    )

    await scan_setup.preflight_model_connection("claude-code/sonnet")


@pytest.mark.asyncio
async def test_preflight_still_pings_non_claude_code_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def _fake_get_model(_self: object, _model_name: str | None) -> object:
        nonlocal called
        called = True

        class _Model:
            async def get_response(self, **_kwargs: object) -> str:
                return "OK"

        return _Model()

    monkeypatch.setattr("strix.config.models.StrixProvider.get_model", _fake_get_model)
    monkeypatch.setattr("strix.core.inputs.make_model_settings", lambda *_a, **_k: object())

    await scan_setup.preflight_model_connection("openai/gpt-5.4")

    assert called


def _settings(model: str) -> mock.MagicMock:
    settings = mock.MagicMock()
    settings.llm.model = model
    return settings


def test_win32_selector_loop_skipped_for_claude_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(interface_main.sys, "platform", "win32")
    monkeypatch.setattr(interface_main, "load_settings", lambda: _settings("claude-code/sonnet"))
    assert interface_main._needs_win32_selector_event_loop() is False


def test_win32_selector_loop_used_for_other_engines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(interface_main.sys, "platform", "win32")
    monkeypatch.setattr(interface_main, "load_settings", lambda: _settings("openai/gpt-5.4"))
    assert interface_main._needs_win32_selector_event_loop() is True


def test_win32_selector_loop_not_needed_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(interface_main.sys, "platform", "linux")
    monkeypatch.setattr(interface_main, "load_settings", lambda: _settings("claude-code/sonnet"))
    assert interface_main._needs_win32_selector_event_loop() is False
