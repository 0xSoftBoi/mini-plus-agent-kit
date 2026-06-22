"""MCP server adapter: dispatch content-blocks → MCP content parts (pure)."""

import _bootstrap  # noqa: F401

from mini_plus_agent_kit.mcp_server import _content_parts


def test_content_parts_splits_text_and_images():
    blocks = [
        {"type": "text", "text": "moved 2 ft"},
        {"type": "text", "text": "View: a hallway"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAA"}},
        {"type": "image", "source": {"data": "BBB"}},
    ]
    texts, images = _content_parts(blocks)
    assert texts == ["moved 2 ft", "View: a hallway"]
    assert images == ["AAA", "BBB"]


def test_content_parts_robust_to_junk():
    texts, images = _content_parts([
        {"type": "text", "text": "ok"},
        {"type": "image", "source": {}},   # missing data → skipped
        "not-a-dict",                       # ignored
        {"type": "other"},                  # ignored
    ])
    assert texts == ["ok"] and images == []


def test_mcp_server_module_imports_without_mcp_dep():
    # The mcp imports are deferred into functions, so the module + the rest of
    # the package import fine even when `mcp` isn't installed.
    import mini_plus_agent_kit as M
    assert all(hasattr(M, s) for s in ["build_mcp_server", "run_stdio", "serve"])


if __name__ == "__main__":
    import _runner
    _runner.run(globals())
