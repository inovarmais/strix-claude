"""Normalize a scan target (URL, git remote, or local checkout) onto one key.

Split out of ``strix.tools.threat_model.tools`` so it can be reused by code
that must stay outside the ``agents`` SDK's import graph (that module is a
``@function_tool`` host and pulls in ``agents`` at import time; a module in
``strix.report`` importing it would drag ``agents`` along too -- see
``tests/test_import_warmup.py`` for the deadlock that import shape caused in
production). This module has no dependency beyond the standard library.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit


logger = logging.getLogger(__name__)

_GIT_TIMEOUT_SECONDS = 10
_DEFAULT_PORTS = {"http": "80", "https": "443"}


def _git(repo: Path, args: list[str]) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(repo), *args],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("git %s failed in %s", args, repo, exc_info=True)
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def local_directory(target: str) -> Path | None:
    """Return the target as a local directory, or None if it is not one."""
    if "://" in target:
        return None
    try:
        resolved = Path(target).expanduser().resolve()
    except OSError:
        return None
    return resolved if resolved.is_dir() else None


def remote_authority(target: str) -> str:
    """The ``host[:port]`` a remote target lives on, or "" if it has none."""
    candidate = target if "://" in target else f"//{target}"
    parts = urlsplit(candidate)
    host = (parts.hostname or "").lower()
    if not host:
        return ""
    scheme = (parts.scheme or "https").lower()
    port = str(parts.port) if parts.port else _DEFAULT_PORTS.get(scheme, "")
    return f"{host}:{port}" if port else host


def normalize_remote_target(target: str) -> str:
    """Collapse the spellings of one remote target onto a single key."""
    authority = remote_authority(target)
    if not authority:
        return re.sub(r"\s+", " ", target.lower()).strip()
    candidate = target if "://" in target else f"//{target}"
    path = urlsplit(candidate).path.rstrip("/")
    return f"{authority}{path}"


def normalize_git_remote(remote: str) -> str:
    """Collapse a git remote URL onto the same key its clone URL would produce.

    A remote reaches us in whichever spelling the clone used —
    ``git@github.com:org/repo.git``, ``https://github.com/org/repo``,
    ``ssh://git@github.com/org/repo.git`` — and each is the same repository.
    Rewriting scp-style syntax into a URL and dropping the ``.git`` suffix and
    any embedded credentials lets :func:`normalize_remote_target` produce one
    identity for all of them.
    """
    candidate = remote.strip()
    scp_style = re.match(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?P<path>.+)$", candidate)
    if scp_style and "://" not in candidate:
        candidate = f"https://{scp_style['host']}/{scp_style['path'].lstrip('/')}"
    elif "://" in candidate:
        # The transport a clone happened to use says nothing about which
        # repository this is, and each scheme carries a different default
        # port into the authority. Collapsing them all onto https keeps one
        # repository on one key however it was cloned.
        candidate = f"https://{candidate.split('://', 1)[1]}"
    normalized = normalize_remote_target(candidate)
    return normalized.removesuffix(".git")


def target_identity(target: str) -> str:
    """Return the stable identity a target normalizes to.

    A checkout is keyed on its remote, so the same repository checked out at
    two paths shares one identity and a subdirectory resolves to the whole
    tree. Everything else — a host, a URL, an API base, a named scope — is
    keyed on its normalized form. Both routes run through the same
    normalization, so a checkout and the URL it was cloned from land on one
    key.
    """
    directory = local_directory(target)
    if directory is None:
        return normalize_remote_target(target).removesuffix(".git")
    remote = _git(directory, ["config", "--get", "remote.origin.url"])
    if remote:
        return normalize_git_remote(remote)
    toplevel = _git(directory, ["rev-parse", "--show-toplevel"])
    return toplevel or str(directory)
