"""Official MCP SDK v2 transport for the shared read-only research catalog.

Run ``python -m observatory.mcp_server`` for stdio. Optional HTTP binds only
loopback on its own port; it is never mounted on the public dashboard server.
"""

from __future__ import annotations

import argparse
import ipaddress
import json

import anyio
from mcp import stdio_server
from mcp.server.lowlevel import Server
from mcp.types import (
    CallToolResult,
    ListToolsResult,
    TextContent,
    Tool,
    ToolAnnotations,
)

from .models import Filters
from .research_tools import ToolCatalog


def build_mcp_server(catalog: ToolCatalog):
    """Register exact schemas, preserving forbidden extras at the executor."""

    async def list_tools(context, params):
        return ListToolsResult(tools=[Tool(name=definition["name"], description=definition["description"],
                                          input_schema=definition["inputSchema"], annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False,
        )) for definition in catalog.mcp_definitions()])

    async def call_tool(context, params):
        # Blocking PostgreSQL reads run outside the SDK event loop. This is a
        # fixed catalog dispatch, never a dynamically selected Python function.
        result = await anyio.to_thread.run_sync(catalog.call, params.name, params.arguments or {})
        return CallToolResult(
            structured_content=result,
            content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False, allow_nan=False))],
            is_error=result.get("status") in {"invalid_request", "unavailable"},
        )

    return Server(
        "Advertising Observatory", version="0.1.0",
        instructions=("Read-only tools for eligible advertising records. Respect filters, candidate identity "
                      "and source-version limitations. Counts come from record_statistics; graph pages and "
                      "retrieved passages do not establish full-corpus totals. Historical categories do not "
                      "establish verified greenwashing. No SQL, arbitrary URLs, local paths or writes are exposed."),
        on_list_tools=list_tools, on_call_tool=call_tool,
    )


def loopback_host(value):
    if value == "localhost":
        return value
    try:
        if ipaddress.ip_address(value).is_loopback:
            return value
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("MCP HTTP is restricted to a loopback host.")


def port_number(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Port must be an integer.") from exc
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("Port must be between 1 and 65535.")
    return number


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    result.add_argument("--host", type=loopback_host, default="127.0.0.1")
    result.add_argument("--port", type=port_number, default=8051)
    result.add_argument("--dataset", choices=("native", "social", "all"), default="native")
    result.add_argument("--publisher", action="append", default=[])
    result.add_argument("--sponsor", action="append", default=[])
    return result


async def run_stdio(server):
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main(argv=None):
    args = parser().parse_args(argv)
    # Constructing the service does not initialize or modify the database.
    from .config import Settings
    from .service import Service

    catalog = ToolCatalog(Service(Settings.from_env()), Filters(
        dataset=args.dataset, publishers=args.publisher, sponsors=args.sponsor,
    ))
    server = build_mcp_server(catalog)
    if args.transport == "stdio":
        anyio.run(run_stdio, server)
    else:
        import uvicorn

        app = server.streamable_http_app(host=args.host, max_request_body_size=65536,
                                         session_idle_timeout=300, max_sessions=20)
        uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
