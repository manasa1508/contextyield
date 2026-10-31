"""The optional MCP facade exposes a bounded, read-only tool surface."""

import pytest

mcp_sdk = pytest.importorskip("mcp")
from mcp import Client

from contextyield.mcp_adapter import mcp


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_mcp_scan_tool_is_bounded_and_metadata_only(tmp_path, monkeypatch):
    monkeypatch.setenv("CY_SCAN_ROOT", str(tmp_path))
    (tmp_path / "agent.py").write_text("import openai\nSECRET = 'private'\n")
    async with Client(mcp, raise_exceptions=True) as client:
        listed = await client.list_tools()
        names = {tool.name for tool in listed.tools}
        assert names == {"scan_project", "search_registered_tools", "get_task_summary"}
        result = await client.call_tool("scan_project", {})
        assert result.is_error is not True
        assert "model_sdk" in result.content[0].text
        assert "private" not in result.content[0].text
