"""Expose the rover as a standard MCP server — the elegant, runtime-agnostic core.

Instead of a bespoke agent loop, publish the openClaw verb surface over the Model
Context Protocol. Then *any* MCP client drives the robot — Claude Desktop, Claude
Code, Cursor, the Agent SDK, openClaw — with zero kit-specific glue.

The server is a thin adapter over the kit's existing single source of truth:

    make_tools(verbs.capabilities, has_work) → the tool list (schemas)
    dispatch(verbs, name, args, work, ...)   → the handlers

So one verb definition powers the Claude agent (``agent.py``), the chat surface
(``telegram.py``), *and* this MCP server — no third copy.

    mpak mcp --backend waveshare         # stdio (for Claude Desktop / Code)

Requires the ``mcp`` package (``pip install "mini-plus-agent-kit[mcp]"``); the
import is deferred so the rest of the kit works without it.
"""

from __future__ import annotations

from typing import Any

from .rover import make_verbs
from .tools import dispatch, make_tools

SERVER_NAME = "mini-plus-rover"


def _content_parts(blocks: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Split dispatch ``ToolOutcome.blocks`` into (texts, image_b64s).

    Pure data transform — unit-testable without the ``mcp`` package. The dicts
    are the same content blocks the Claude agent puts in ``tool_result``.
    """
    texts, images = [], []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            texts.append(b.get("text", ""))
        elif b.get("type") == "image":
            data = (b.get("source") or {}).get("data")
            if data:
                images.append(data)
    return texts, images


def build_mcp_server(rover, work=None, resource_name: str | None = None,
                     name: str = SERVER_NAME):
    """Build an MCP ``Server`` whose tools are the rover's verbs.

    Tool list = ``make_tools(verbs.capabilities, has_work)``; each call routes
    through ``dispatch`` and its content blocks become MCP text/image content.
    """
    from mcp import types
    from mcp.server import Server

    verbs = make_verbs(rover)
    tool_defs = make_tools(verbs.capabilities, has_work=work is not None)
    server: Any = Server(name)

    @server.list_tools()
    async def list_tools() -> list:
        return [
            types.Tool(name=t["name"], description=t["description"],
                       inputSchema=t["input_schema"])
            for t in tool_defs
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> list:
        outcome = dispatch(verbs, name, arguments or {}, work=work,
                           resource_name=resource_name)
        texts, images = _content_parts(outcome.blocks)
        content: list = [types.TextContent(type="text", text=t) for t in texts]
        content += [types.ImageContent(type="image", data=d, mimeType="image/jpeg")
                    for d in images]
        return content or [types.TextContent(type="text", text="(no output)")]

    return server


async def run_stdio(rover, work=None, resource_name: str | None = None,
                    name: str = SERVER_NAME) -> None:
    """Run the MCP server over stdio (for Claude Desktop / Code / Cursor)."""
    from mcp.server.stdio import stdio_server

    server = build_mcp_server(rover, work=work, resource_name=resource_name, name=name)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def serve(rover, transport: str = "stdio", **kwargs) -> None:
    """Blocking entry point. transport: ``stdio`` (more via the SDK app)."""
    import asyncio

    if transport != "stdio":
        raise ValueError("only stdio is wired here; mount streamable-http via the SDK app")
    asyncio.run(run_stdio(rover, **kwargs))
