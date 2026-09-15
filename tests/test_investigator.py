"""
Tests for gtm-intelligence-investigator/investigator.py.

Most of this file needs no Anthropic API key -- the MCP-to-Anthropic tool
translation is plain data transformation, tested against the real live
server. The one true integration test (test_investigate_flagship_question)
actually calls the Anthropic API and is skipped automatically when
ANTHROPIC_API_KEY isn't set, so CI doesn't fail on a missing secret --- it
runs for real if you add ANTHROPIC_API_KEY as a repo secret and reference
it in .github/workflows/ci.yml, and it always runs locally once you've
exported the key yourself.
"""

import os
import sys

import pytest
from mcp import ClientSession
from mcp.client.stdio import stdio_client

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "gtm-intelligence-investigator",
))
import investigator  # noqa: E402


def test_demo_questions_has_all_nine():
    assert len(investigator.DEMO_QUESTIONS) == 9
    assert investigator.DEMO_QUESTIONS["1"].startswith("Why did Mid-Market")


@pytest.mark.asyncio
async def test_tool_translation_covers_all_ten_tools():
    async with stdio_client(investigator.SERVER_PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mcp_tools = (await session.list_tools()).tools
            tools = investigator.mcp_tools_to_anthropic_format(mcp_tools)

    assert len(tools) == 10
    for tool in tools:
        assert "name" in tool
        assert "description" in tool and tool["description"]
        assert "input_schema" in tool


@pytest.mark.asyncio
async def test_tool_translation_preserves_schema_shape():
    async with stdio_client(investigator.SERVER_PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mcp_tools = (await session.list_tools()).tools
            tools = investigator.mcp_tools_to_anthropic_format(mcp_tools)

    retention = next(t for t in tools if t["name"] == "retention")
    assert "base_month" in retention["input_schema"]["properties"]
    assert "current_month" in retention["input_schema"]["properties"]


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="needs a real ANTHROPIC_API_KEY -- set it locally, or as a repo secret in CI, to run this",
)
@pytest.mark.asyncio
async def test_investigate_flagship_question_finds_the_incident():
    """The real bar for this layer: does it land on the March 2026 SMS
    price hike for Mid-Market without being told? Checks for the key
    facts (the month and the segment), not exact wording."""
    answer = await investigator.investigate(
        investigator.DEMO_QUESTIONS["1"], verbose=False
    )
    lower = answer.lower()
    assert "mid-market" in lower
    assert "2026-03" in answer or "march 2026" in lower
