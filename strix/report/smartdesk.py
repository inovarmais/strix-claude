"""Optional: push a completed scan's findings to a SmartDesk instance.

SmartDesk's "Gestao de Vulnerabilidades" module exposes a bulk-ingestion API
(``POST {base_url}/api/gestao-vulnerabilidades/scans``) that this module
calls once, at the end of a scan, when both ``SMARTDESK_BASE_URL`` and
``SMARTDESK_API_KEY`` are configured (``strix.config.load_settings().
integrations``). Either unset -> the upload is silently skipped, same as
Perplexity search when ``PERPLEXITY_API_KEY`` is absent.

Integration:
  - ``ReportState.save_run_data`` calls :func:`upload_to_smartdesk` only on
    the final save of a run (``mark_complete`` or a terminal ``status``), not
    on every individual finding, to avoid re-uploading the growing findings
    list after each ``create_vulnerability_report`` call.
  - Wrapped in try/except at the call site so a SmartDesk outage never blocks
    a scan or its local CSV/markdown/JSON/SARIF artifacts -- this call is
    the last, and least essential, thing a run does.
  - The outcome (not the payload -- that already lives in vulnerabilities.json)
    is recorded locally as ``smartdesk_upload.json`` alongside the run's other
    artifacts, so a user can check whether a scan reached SmartDesk without
    digging through logs.

Field mapping: Strix's own finding schema and SmartDesk's ingestion contract
were designed to mirror each other field-for-field (see
``strix/tools/reporting/tool.py:create_vulnerability_report`` and SmartDesk's
``FindingUploadItem``), so this is a snake_case -> camelCase rename, not a
transformation. ``application.gitIdentity``/``commitHash``/``branch`` reuse
the same target-identity normalization already written for threat modeling
(``strix.utils.target_identity``, shared rather than duplicated so both
callers stay consistent -- and kept out of ``strix.tools.threat_model.tools``
precisely so a module under ``strix.report`` can use it without pulling in
the ``agents`` SDK; see ``tests/test_import_warmup.py``).
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from strix.config import load_settings
from strix.report.writer import atomic_write_text
from strix.utils.target_identity import target_identity


logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECONDS = 30
_GIT_TIMEOUT_SECONDS = 5

# strix finding field -> SmartDesk FindingUploadItem field. A straight rename;
# "id"/"timestamp"/"finding_class"/"dependency_metadata" have no SmartDesk
# counterpart and are intentionally omitted.
_FIELD_MAP = {
    "title": "title",
    "description": "description",
    "impact": "impact",
    "target": "target",
    "endpoint": "endpoint",
    "method": "method",
    "technical_analysis": "technicalAnalysis",
    "poc_description": "pocDescription",
    "poc_script_code": "pocScriptCode",
    "remediation_steps": "remediationSteps",
    "evidence": "evidence",
    "assumptions": "assumptions",
    "counterevidence": "counterevidence",
    "confidence": "confidence",
    "confidence_rationale": "confidenceRationale",
    "severity_change_conditions": "severityChangeConditions",
    "fix_effort": "fixEffort",
    "cvss_breakdown": "cvssBreakdown",
    "cvss": "cvssScore",
    "severity": "severity",
    "cve": "cve",
    "cwe": "cwe",
    "code_locations": "codeLocations",
    "fix_verification": "fixVerification",
    "fix_pr_body": "fixPrBody",
    "agent_name": "agentName",
}


def _git(repo: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(repo), *args],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def _local_checkout(targets_info: list[dict[str, Any]]) -> Path | None:
    """The first target that resolves to a local directory strix can read."""
    for target in targets_info:
        details = target.get("details") if isinstance(target, dict) else None
        if not isinstance(details, dict):
            continue
        path_text = details.get("cloned_repo_path") or details.get("target_path")
        if isinstance(path_text, str) and path_text.strip():
            path = Path(path_text.strip())
            if path.is_dir():
                return path
    return None


def _application_identity(targets_info: list[dict[str, Any]]) -> tuple[str, str] | None:
    """``(name, gitIdentity)`` derived from the run's targets, or None."""
    for target in targets_info:
        details = target.get("details") if isinstance(target, dict) else None
        if not isinstance(details, dict):
            continue
        candidate = (
            details.get("target_repo") or details.get("target_path") or details.get("target_url")
        )
        if isinstance(candidate, str) and candidate.strip():
            identity = target_identity(candidate.strip())
            name = identity.rstrip("/").rsplit("/", 1)[-1] or identity
            return name, identity
    return None


def _finding_payload(report: dict[str, Any]) -> dict[str, Any]:
    return {dest: report[src] for src, dest in _FIELD_MAP.items() if src in report}


def upload_to_smartdesk(
    run_dir: Path,
    run_name: str,
    vulnerability_reports: list[dict[str, Any]],
    run_record: dict[str, Any],
) -> None:
    """Best-effort push of ``vulnerability_reports`` to SmartDesk.

    Never raises. Records the outcome to ``run_dir/smartdesk_upload.json``.
    """
    integrations = load_settings().integrations
    base_url = (integrations.smartdesk_base_url or "").strip()
    api_key = (integrations.smartdesk_api_key or "").strip()
    if not base_url or not api_key or not vulnerability_reports:
        return

    targets_info = run_record.get("targets_info")
    if not isinstance(targets_info, list):
        targets_info = []
    identity = _application_identity(targets_info)
    if identity is None:
        _write_outcome(run_dir, base_url, error="no resolvable target identity")
        return
    name, git_identity = identity

    commit_hash: str | None = None
    branch: str | None = None
    checkout = _local_checkout(targets_info)
    if checkout is not None:
        commit_hash = _git(checkout, "rev-parse", "HEAD")
        branch = _git(checkout, "rev-parse", "--abbrev-ref", "HEAD")
        if branch == "HEAD":  # detached checkout, no branch to report
            branch = None

    payload = {
        "application": {"name": name, "gitIdentity": git_identity},
        "runName": run_name,
        "scannedAt": run_record.get("end_time")
        or run_record.get("start_time")
        or datetime.now(UTC).isoformat(),
        "commitHash": commit_hash,
        "branch": branch,
        "findings": [_finding_payload(report) for report in vulnerability_reports],
    }

    try:
        response = requests.post(
            f"{base_url.rstrip('/')}/api/gestao-vulnerabilidades/scans",
            json=payload,
            headers={"X-Api-Key": api_key},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("SmartDesk upload failed (non-fatal): %s", exc)
        _write_outcome(run_dir, base_url, error=str(exc))
        return

    logger.info(
        "Uploaded %d finding(s) to SmartDesk (application_id=%s, created=%s, updated=%s)",
        len(vulnerability_reports),
        result.get("applicationId"),
        result.get("created"),
        result.get("updated"),
    )
    _write_outcome(run_dir, base_url, result=result)


def _write_outcome(
    run_dir: Path,
    base_url: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Record the upload's outcome locally -- never the API key or payload."""
    record = {
        "attempted_at": datetime.now(UTC).isoformat(),
        "base_url": base_url,
        "success": error is None,
    }
    if result is not None:
        record["application_id"] = result.get("applicationId")
        record["created"] = result.get("created")
        record["updated"] = result.get("updated")
        record["errors"] = result.get("errors")
    if error is not None:
        record["error"] = error
    try:
        atomic_write_text(
            run_dir / "smartdesk_upload.json",
            json.dumps(record, ensure_ascii=False, indent=2),
        )
    except OSError:
        logger.exception("Failed to record smartdesk_upload.json (non-fatal)")
