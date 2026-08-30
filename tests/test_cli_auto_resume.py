"""Test that the CLI entry point forwards --auto-resume to run_strix_scan."""

from __future__ import annotations

import ast
import inspect

from strix.interface import cli


def test_run_cli_forwards_auto_resume_to_run_strix_scan() -> None:
    source = inspect.getsource(cli.run_cli)
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run_strix_scan"
    ]
    assert calls, "run_cli no longer calls run_strix_scan(...) directly"
    keyword_names = {kw.arg for kw in calls[0].keywords}
    assert "auto_resume" in keyword_names
