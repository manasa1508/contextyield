"""Scanner safety, signal provenance, and limitation checks."""

import json
from pathlib import Path

from contextyield.repo_scan import scan_repository


def test_scan_identifies_imports_and_call_sites_without_source_or_secrets(tmp_path: Path):
    (tmp_path / "agent.py").write_text(
        "import openai\nfrom langgraph.graph import StateGraph\n"
        "SECRET = 'do-not-leak'\nclient.chat.completions.create(model='x')\n")
    (tmp_path / "app.ts").write_text(
        "import { Client } from '@modelcontextprotocol/sdk/client/index.js';\n"
        "const key = 'also-do-not-leak';\n")
    (tmp_path / ".env").write_text("API_KEY=do-not-leak")
    report = scan_repository(tmp_path)
    assert {"model_sdk", "agent_framework", "mcp", "model_call"} <= set(report["families"])
    assert report["runtime_measurements"] == "not_available_from_source"
    assert "do-not-leak" not in json.dumps(report)
    assert all(not item["path"].startswith("/") for item in report["signals"])


def test_scan_ignores_symlinks_and_generated_directories(tmp_path: Path):
    outside = tmp_path.parent / "outside-secret.py"
    outside.write_text("import anthropic\n")
    (tmp_path / "linked.py").symlink_to(outside)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.py").write_text("import openai\n")
    (tmp_path / "good.py").write_text("import mlflow\n")
    report = scan_repository(tmp_path)
    assert report["files_examined"] == 1
    assert report["families"] == ["mlops"]


def test_scan_reports_truncation_and_syntax_error(tmp_path: Path):
    (tmp_path / "a.py").write_text("not valid python (")
    (tmp_path / "b.py").write_text("import openai")
    report = scan_repository(tmp_path, max_files=1)
    assert report["truncated"] is True
    assert report["parse_errors"] == 1
    assert report["families"] == []
