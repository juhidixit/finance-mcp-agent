"""Expose the gateway itself as an MCP server.

Any MCP client (Claude Desktop, Claude Code, an IDE, your own agent) can connect to this one
server and get the back-office tools, already filtered, bound and audited by the policy:

    finagent serve-gateway --db data/acme.db --principal E002 --role finance_analyst
"""

from __future__ import annotations

from typing import Any

from mcp import types
from mcp.server.lowlevel import Server

from finagent.gateway.gateway import Gateway


def build_proxy_server(gw: Gateway) -> Server:
    async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[
            types.Tool(name=t.name, description=t.description, input_schema=t.input_schema,
                       annotations=types.ToolAnnotations(read_only_hint=t.read_only))
            for t in gw.tools_for_agent()])

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        res = await gw.call(params.name, dict(params.arguments or {}))
        return types.CallToolResult(content=[types.TextContent(type="text", text=res.to_text())],
                                    is_error=res.status != "ok")

    return Server("finagent-gateway", instructions="Policy-enforcing gateway to Acme back-office tools.",
                  on_list_tools=list_tools, on_call_tool=call_tool)
