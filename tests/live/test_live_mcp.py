"""LIVE test — the REAL MCP server driving the REAL HarnessClient over real HTTP.

Builds the kit's MCP server (real `mcp` package) over a real HarnessClient pointed
at the live harness emulator, connects a real MCP client session, and exercises
the protocol: initialize → list_tools → call_tool. Run:

    .venv/bin/python tests/live/test_live_mcp.py
"""

import asyncio
import os
import sys
import threading
from http.server import ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))
sys.path.insert(0, _HERE)

from test_live_harness import Harness  # the real harness emulator
import mini_plus_agent_kit.rover as rover_mod
from mini_plus_agent_kit.harness_client import HarnessClient
from mini_plus_agent_kit.mcp_server import build_mcp_server
from mcp.shared.memory import create_connected_server_and_client_session


async def main():
    rover_mod.TICK_SECONDS = 0.0
    server = ThreadingHTTPServer(("127.0.0.1", 0), Harness)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    print(f"harness emulator on {base}")

    client = HarnessClient(base, speed_mode="medium")          # real authorize over HTTP
    mcp_server = build_mcp_server(client)                       # real mcp.server.Server

    async with create_connected_server_and_client_session(mcp_server) as session:
        # initialize handshake (real MCP protocol)
        await session.initialize()

        tools = await session.list_tools()
        names = {t.name for t in tools.tools}
        assert {"status_report", "move", "photo", "set_lamp", "camera_move", "finish"} <= names, names
        print(f"list_tools: {len(names)} tools — {sorted(names)}")

        # call_tool over MCP → routes through dispatch → real HTTP to the harness
        r = await session.call_tool("status_report", {})
        text = " ".join(c.text for c in r.content if getattr(c, "type", None) == "text")
        assert "battery=12.4" in text and "lidar_front=1.50m" in text, text
        print("call_tool status_report:", text)

        # photo returns real image content (the JPEG bytes, b64) via MCP
        p = await session.call_tool("photo", {})
        imgs = [c for c in p.content if getattr(c, "type", None) == "image"]
        assert imgs and imgs[0].data and imgs[0].mimeType == "image/jpeg", p.content
        print(f"call_tool photo: ImageContent {imgs[0].mimeType}, {len(imgs[0].data)} b64 chars")

        # a tool with args → real drive over the wire
        m = await session.call_tool("move", {"distance_ft": 1})
        mt = " ".join(c.text for c in m.content if getattr(c, "type", None) == "text")
        assert "ok" in mt.lower(), mt
        print("call_tool move:", mt.splitlines()[0])

    server.shutdown()
    print("\nLIVE MCP PASSED (real mcp protocol → dispatch → real HTTP to harness)")


if __name__ == "__main__":
    asyncio.run(main())
