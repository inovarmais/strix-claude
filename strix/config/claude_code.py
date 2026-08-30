"""Claude Code engine selection and CLI availability/login checks.

Unlike ``strix.config.codex``, this module manages no credentials of its
own: ``STRIX_LLM=claude-code/<model>`` drives the agent's turn loop via the
real ``claude`` CLI (see ``strix.core.claude_code_execution``, added in a
later task), reusing whatever account it is already logged into
(``claude /login``).
"""

from __future__ import annotations

import logging
import shutil
import subprocess


logger = logging.getLogger(__name__)

ENGINE_PREFIX = "claude-code/"

_STATUS_TIMEOUT_S = 10
_NOT_LOGGED_IN_MARKERS = ("not logged in", "no credentials", "not authenticated")


def engine_model(model_name: str | None) -> str | None:
    """The model slug behind a ``claude-code/<model>`` STRIX_LLM, or None."""
    name = (model_name or "").strip()
    if not name.lower().startswith(ENGINE_PREFIX):
        return None
    return name[len(ENGINE_PREFIX) :] or None


def is_cli_available() -> bool:
    return shutil.which("claude") is not None


def cli_login_status() -> tuple[bool, str | None]:
    """Best-effort ``(logged_in, detail)`` for the ``claude`` CLI's own login.

    ``claude auth status`` prints a human-readable report and is documented
    as not meant to be scripted against its exit code, so this parses stdout
    for a known "not logged in" marker rather than trusting the return code.
    """
    if not is_cli_available():
        return False, None
    try:
        result = subprocess.run(
            ["claude", "auth", "status"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=_STATUS_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.debug("claude auth status failed to run", exc_info=True)
        return False, None
    output = (result.stdout or "") + (result.stderr or "")
    lowered = output.lower()
    if any(marker in lowered for marker in _NOT_LOGGED_IN_MARKERS):
        return False, output.strip() or None
    return True, output.strip() or None
