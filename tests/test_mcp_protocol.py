"""
Real MCP client/server protocol tests -- these spin up
mcp_server/analytics_server.py as an actual subprocess over stdio and talk
to it through mcp.client.stdio + mcp.ClientSession: a genuine JSON-RPC
initialize() -> list_tools()/list_resources()/list_prompts() ->
call_tool()/read_resource()/get_prompt() exchange, not a mock and not a
direct Python function call (see test_mcp_tools.py for those).

Each test opens and closes its own stdio_client/ClientSession within a
single coroutine (rather than sharing one across a pytest fixture) --
anyio's task-scoped cancel scopes inside mcp's stdio transport don't
tolerate being entered and exited from different tasks, which is exactly
what a cross-test fixture teardown does under pytest-asyncio. One
subprocess per test is slightly slower; it's also the version that
actually works.
"""

import os

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_PATH = os.path.join(REPO_ROOT, "mcp_server", "analytics_server.py")
SERVER_PARAMS = StdioServerParameters(command="python3", args=[SERVER_PATH])


@pytest.mark.asyncio
async def test_lists_all_ten_tools():
    async with stdio_client(SERVER_PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert names == {
                "get_schema", "run_query", "pipeline_summary", "mrr_trend",
                "arr_waterfall", "retention", "retention_decomposition",
                "top_accounts", "get_account_detail", "support_ticket_summary",
            }


@pytest.mark.asyncio
async def test_lists_the_data_dictionary_resource():
    async with stdio_client(SERVER_PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            resources = await session.list_resources()
            uris = {str(r.uri) for r in resources.resources}
            assert "data://dictionary" in uris


@pytest.mark.asyncio
async def test_lists_the_business_review_prompt():
    async with stdio_client(SERVER_PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            prompts = await session.list_prompts()
            names = {p.name for p in prompts.prompts}
            assert "gtm_business_review" in names


@pytest.mark.asyncio
async def test_call_tool_get_schema():
    async with stdio_client(SERVER_PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("get_schema", {})
            assert "accounts:" in result.content[0].text


@pytest.mark.asyncio
async def test_call_tool_retention():
    async with stdio_client(SERVER_PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "retention", {"base_month": "2025-09", "current_month": "2026-08"}
            )
            text = result.content[0].text
            assert "BLENDED" in text
            assert "Mid-Market" in text


@pytest.mark.asyncio
async def test_call_tool_run_query_rejects_a_drop():
    async with stdio_client(SERVER_PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("run_query", {"sql": "DROP TABLE accounts"})
            assert "Error" in result.content[0].text


@pytest.mark.asyncio
async def test_read_resource_data_dictionary():
    async with stdio_client(SERVER_PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.read_resource("data://dictionary")
            assert "opportunities" in result.contents[0].text


@pytest.mark.asyncio
async def test_get_prompt_business_review():
    async with stdio_client(SERVER_PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.get_prompt("gtm_business_review", {"period": "2026-08"})
            assert "2026-08" in result.messages[0].content.text
