"""Claude Code engine selection and CLI availability/login checks.

Unlike ``strix.config.codex``, this module manages no credentials of its
own: ``STRIX_LLM=claude-code/<model>`` drives the agent's turn loop via the
real ``claude`` CLI (see ``strix.core.claude_code_execution``, added in a
later task), reusing whatever account it is already logged into
(``claude /login``).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta


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


class SubscriptionQuotaExceededError(RuntimeError):
    """A Claude subscription's usage limit was hit mid-scan.

    Carries a best-effort ``reset_at`` (UTC) parsed from the CLI's own
    message, so callers can print it (default behavior) or sleep until it
    (``--auto-resume``, see ``strix.core.runner``).
    """

    def __init__(self, raw_message: str, reset_at: datetime | None) -> None:
        self.raw_message = raw_message
        self.reset_at = reset_at
        super().__init__(raw_message)


_QUOTA_MARKERS = (
    "usage limit reached",
    "5-hour limit reached",
    "5-hour limit resets",
)

_CLOCK_TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.IGNORECASE)

# Anthropic's session limit window; used when the message names no time we
# can parse (e.g. "please try again later"). Best-effort only — a real reset
# time from the message always takes precedence.
_FALLBACK_QUOTA_WAIT = timedelta(hours=5)


def _parse_reset_time(message: str) -> datetime | None:
    match = _CLOCK_TIME_RE.search(message)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    minute = int(match.group(2) or 0)
    if match.group(3).lower() == "pm":
        hour += 12
    now_local = datetime.now().astimezone()
    candidate_local = now_local.replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if candidate_local <= now_local:
        candidate_local += timedelta(days=1)
    return candidate_local.astimezone(UTC)


def classify_quota_error(message: str) -> SubscriptionQuotaExceededError | None:
    """Return a ``SubscriptionQuotaExceededError`` if ``message`` looks like a
    Claude subscription quota/usage-limit message, else None.

    The CLI reports this as free text (there is no structured error code for
    it as of this writing), so detection is substring-based on the phrasing
    Anthropic currently uses. If Anthropic changes this phrasing, update
    ``_QUOTA_MARKERS``.
    """
    lowered = message.lower()
    if not any(marker in lowered for marker in _QUOTA_MARKERS):
        return None
    reset_at = _parse_reset_time(message) or (datetime.now(UTC) + _FALLBACK_QUOTA_WAIT)
    return SubscriptionQuotaExceededError(message, reset_at)
