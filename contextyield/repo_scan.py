"""Read-only, best-effort inventory of AI integration points in a source tree.

This scanner does not execute code, inspect credentials, or infer runtime usage.
It deliberately reports only categories and relative source locations.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

SOURCE_SUFFIXES = frozenset({".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"})
EXCLUDED_DIRS = frozenset({
    ".git", ".venv", "venv", "node_modules", "dist", "build", ".next",
    "__pycache__", ".pytest_cache", ".ruff_cache", "coverage", "graphify-out",
})
INTEGRATIONS = {
    "openai": "model_sdk", "anthropic": "model_sdk", "google.genai": "model_sdk",
    "google.generativeai": "model_sdk", "litellm": "model_gateway",
    "langchain": "agent_framework", "langgraph": "agent_framework",
    "crewai": "agent_framework", "autogen": "agent_framework",
    "semantic_kernel": "agent_framework", "agents": "agent_framework",
    "@modelcontextprotocol/sdk": "mcp", "mcp": "mcp",
    "@opentelemetry": "observability", "opentelemetry": "observability",
    "mlflow": "mlops", "kubeflow": "mlops", "wandb": "mlops",
    "vllm": "model_server", "ollama": "model_server",
}
CALL_PATTERNS = (
    ("chat.completions.create", "model_call"),
    ("responses.create", "model_call"),
    ("messages.create", "model_call"),
    ("embeddings.create", "embedding_call"),
    ("tools.call", "tool_call"),
    ("invoke", "possible_agent_call"),
    ("ainvoke", "possible_agent_call"),
)
IMPORT_RE = re.compile(r"^\s*(?:import\s+.*?from\s+|import\s+|.*?require\s*\(\s*)['\"]([^'\"]+)")


def _integration(name: str) -> tuple[str, str] | None:
    for prefix, family in sorted(INTEGRATIONS.items(), key=lambda item: -len(item[0])):
        if name == prefix or name.startswith((prefix + ".", prefix + "/")):
            return prefix, family
    return None


def _call_name(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _python_signals(source: str, path: str) -> tuple[list[dict], bool]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return [], True
    signals: list[dict] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            found = _integration(name)
            if found:
                signals.append({"family": found[1], "name": found[0], "path": path,
                                "line": node.lineno, "basis": "import"})
        if isinstance(node, ast.Call):
            call_name = _call_name(node.func)
            for suffix, family in CALL_PATTERNS:
                if call_name == suffix or call_name.endswith("." + suffix):
                    signals.append({"family": family, "name": suffix, "path": path,
                                    "line": node.lineno, "basis": "possible_callsite"})
                    break
    return signals, False


def _javascript_signals(source: str, path: str) -> list[dict]:
    signals: list[dict] = []
    for number, line in enumerate(source.splitlines(), 1):
        match = IMPORT_RE.match(line)
        if match:
            found = _integration(match.group(1))
            if found:
                signals.append({"family": found[1], "name": found[0], "path": path,
                                "line": number, "basis": "import"})
        for suffix, family in CALL_PATTERNS:
            if "." + suffix + "(" in line.replace(" ", ""):
                signals.append({"family": family, "name": suffix, "path": path,
                                "line": number, "basis": "possible_callsite"})
                break
    return signals


def scan_repository(root: Path, *, max_files: int = 5000,
                    max_file_bytes: int = 1_000_000) -> dict:
    """Return a metadata-only inventory; never follow directory or file symlinks."""
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("repository path must be a directory")
    if max_files < 1 or max_file_bytes < 1:
        raise ValueError("scan limits must be positive")
    signals: list[dict] = []
    examined = skipped_large = parse_errors = 0
    truncated = False
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIRS
                         and not (Path(current) / d).is_symlink())
        for filename in sorted(files):
            path = Path(current) / filename
            if path.suffix not in SOURCE_SUFFIXES or path.is_symlink():
                continue
            if examined >= max_files:
                truncated = True
                break
            if path.stat().st_size > max_file_bytes:
                skipped_large += 1
                continue
            examined += 1
            relative = path.relative_to(root).as_posix()
            source = path.read_text(encoding="utf-8", errors="replace")
            if path.suffix == ".py":
                found, error = _python_signals(source, relative)
                parse_errors += int(error)
                signals.extend(found)
            else:
                signals.extend(_javascript_signals(source, relative))
        if truncated:
            break
    signals.sort(key=lambda item: (item["path"], item["line"], item["family"], item["name"]))
    families = sorted({item["family"] for item in signals})
    return {
        "schema_version": 1,
        "root_name": root.name,
        "files_examined": examined,
        "files_skipped_large": skipped_large,
        "parse_errors": parse_errors,
        "truncated": truncated,
        "signals": signals,
        "families": families,
        "runtime_measurements": "not_available_from_source",
        "next_step": "instrument task spans to measure actual calls, usage, latency, and outcomes",
    }
