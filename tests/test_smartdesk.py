"""Tests for strix.report.smartdesk: the optional SmartDesk upload step."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import requests

from strix.report import smartdesk


if TYPE_CHECKING:
    from pathlib import Path


def _settings(base_url: str | None = "https://desk.example.com", api_key: str | None = "k") -> Any:
    return SimpleNamespace(
        integrations=SimpleNamespace(smartdesk_base_url=base_url, smartdesk_api_key=api_key)
    )


def _finding(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "vuln-0001",
        "title": "SQL injection in login",
        "severity": "high",
        "timestamp": "2026-08-31 12:00:00 UTC",
        "description": "desc",
        "target": "https://app.example.com",
        "cvss": 8.1,
        "cvss_breakdown": {"attack_vector": "N"},
        "finding_class": "dynamic",
    }
    base.update(overrides)
    return base


def _repo_target(target_repo: str = "https://github.com/org/app") -> dict[str, Any]:
    return {"type": "repository", "details": {"target_repo": target_repo}}


def _run_record(targets_info: list[dict[str, Any]]) -> dict[str, Any]:
    return {"targets_info": targets_info, "start_time": "2026-08-31T12:00:00Z"}


def test_finding_payload_renames_fields_and_drops_unmapped_ones() -> None:
    payload = smartdesk._finding_payload(_finding())

    assert payload["title"] == "SQL injection in login"
    assert payload["severity"] == "high"
    assert payload["cvssScore"] == 8.1
    assert payload["cvssBreakdown"] == {"attack_vector": "N"}
    assert "id" not in payload
    assert "timestamp" not in payload
    assert "finding_class" not in payload


def test_application_identity_resolves_from_a_repository_target() -> None:
    identity = smartdesk._application_identity([_repo_target("https://github.com/org/app")])

    assert identity == ("app", "github.com:443/org/app")


def test_application_identity_none_when_no_target_has_a_usable_field() -> None:
    assert smartdesk._application_identity([{"type": "api_spec", "details": {}}]) is None


@patch("strix.report.smartdesk.requests.post")
def test_skips_entirely_when_smartdesk_is_not_configured(
    mock_post: MagicMock, tmp_path: Path
) -> None:
    with patch("strix.report.smartdesk.load_settings", return_value=_settings(base_url=None)):
        smartdesk.upload_to_smartdesk(
            tmp_path, "run1", [_finding()], _run_record([_repo_target()])
        )

    mock_post.assert_not_called()
    assert not (tmp_path / "smartdesk_upload.json").exists()


@patch("strix.report.smartdesk.requests.post")
def test_skips_entirely_when_there_are_no_findings(mock_post: MagicMock, tmp_path: Path) -> None:
    with patch("strix.report.smartdesk.load_settings", return_value=_settings()):
        smartdesk.upload_to_smartdesk(tmp_path, "run1", [], _run_record([_repo_target()]))

    mock_post.assert_not_called()


@patch("strix.report.smartdesk.requests.post")
def test_uploads_findings_and_records_a_successful_outcome(
    mock_post: MagicMock, tmp_path: Path
) -> None:
    mock_post.return_value = MagicMock(
        json=lambda: {"applicationId": 7, "created": 1, "updated": 0, "errors": []}
    )

    with patch("strix.report.smartdesk.load_settings", return_value=_settings()):
        smartdesk.upload_to_smartdesk(
            tmp_path, "run1", [_finding()], _run_record([_repo_target()])
        )

    mock_post.assert_called_once()
    call = mock_post.call_args
    assert call.args[0] == "https://desk.example.com/api/gestao-vulnerabilidades/scans"
    assert call.kwargs["headers"] == {"X-Api-Key": "k"}
    body = call.kwargs["json"]
    assert body["application"] == {"name": "app", "gitIdentity": "github.com:443/org/app"}
    assert body["runName"] == "run1"
    assert len(body["findings"]) == 1
    assert body["findings"][0]["title"] == "SQL injection in login"

    outcome = json.loads((tmp_path / "smartdesk_upload.json").read_text(encoding="utf-8"))
    assert outcome["success"] is True
    assert outcome["application_id"] == 7
    assert outcome["created"] == 1


@patch(
    "strix.report.smartdesk.requests.post",
    side_effect=requests.exceptions.ConnectionError("refused"),
)
def test_a_network_failure_never_raises_and_records_the_error(
    mock_post: MagicMock, tmp_path: Path
) -> None:
    with patch("strix.report.smartdesk.load_settings", return_value=_settings()):
        smartdesk.upload_to_smartdesk(
            tmp_path, "run1", [_finding()], _run_record([_repo_target()])
        )

    mock_post.assert_called_once()
    outcome = json.loads((tmp_path / "smartdesk_upload.json").read_text(encoding="utf-8"))
    assert outcome["success"] is False
    assert "error" in outcome


@patch("strix.report.smartdesk.requests.post")
def test_records_an_error_outcome_when_no_target_identity_is_resolvable(
    mock_post: MagicMock, tmp_path: Path
) -> None:
    with patch("strix.report.smartdesk.load_settings", return_value=_settings()):
        smartdesk.upload_to_smartdesk(
            tmp_path, "run1", [_finding()], _run_record([{"type": "api_spec", "details": {}}])
        )

    mock_post.assert_not_called()
    outcome = json.loads((tmp_path / "smartdesk_upload.json").read_text(encoding="utf-8"))
    assert outcome["success"] is False
