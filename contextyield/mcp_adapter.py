"""Optional, local stdio MCP facade for safe ContextYield read operations.

Run with the `mcp` extra. The host's local process owns CY_SCAN_ROOT and the
gateway bearer token; neither is a model-supplied tool argument.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import httpx
from mcp.server.mcpserver import MCPServer

from .repo_scan import scan_repository

mcp = MCPServer(name="ContextYield", description="Read-only AI workflow efficiency evidence")


def _gateway_get(path: str) -> dict:
    key = os.getenv("CY_GATEWAY_KEY")
    if not key:
        raise RuntimeError("CY_GATEWAY_KEY is not configured")
    base = os.getenv("CY_GATEWAY_URL", "http://127.0.0.1:18080").rstrip("/")
    with httpx.Client(timeout=5, follow_redirects=False) as client:
        try:
            response = client.get(base + path, headers={"Authorization": f"Bearer {key}"})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError("ContextYield gateway request failed") from exc
    return response.json()


@mcp.tool()
def scan_project() -> dict:
    """Inventory AI SDKs and possible call sites in the operator-approved source root.

    This does not execute code or report actual runtime tokens, cost, or energy.
    """
    root = os.getenv("CY_SCAN_ROOT")
    if not root:
        raise RuntimeError("CY_SCAN_ROOT is not configured")
    report = scan_repository(Path(root))
    total = len(report["signals"])
    report["signals"] = report["signals"][:50]
    report["signals_returned"] = len(report["signals"])
    report["signals_total"] = total
    return report


@mcp.tool()
def search_registered_tools(query: str, limit: int = 5) -> dict:
    """Find registered read-only tool capabilities by name, without listing every tool."""
    if not 1 <= len(query) <= 80 or not 1 <= limit <= 20:
        raise ValueError("query length must be 1..80 and limit 1..20")
    return _gateway_get(f"/v1/tools/search?{httpx.QueryParams({'q': query, 'limit': limit})}")


@mcp.tool()
def get_task_summary(task_id: str) -> dict:
    """Read tenant-scoped task steps, provider usage coverage, and outcome evidence."""
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", task_id):
        raise ValueError("invalid task ID")
    return _gateway_get(f"/v1/tasks/{task_id}/summary")


if __name__ == "__main__":
    mcp.run()
