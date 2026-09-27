"""MCP サーバを実際に子プロセスで起動して、ツールの取り込みと呼び出しを確かめる。"""

import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="MCP を使うには pip install 'jarvis[mcp]'")

from jarvis.errors import EngineUnavailable  # noqa: E402
from jarvis.mcp_bridge import McpToolProvider  # noqa: E402

SERVER = str(Path(__file__).with_name("mcp_echo_server.py"))


@pytest.fixture
def provider():
    p = McpToolProvider({"test": {"command": sys.executable, "args": [SERVER]}})
    yield p
    p.close()


def test_server_tools_are_imported_with_a_qualified_name(provider):
    names = {t.name for t in provider.tools()}
    assert names == {"test__echo", "test__add"}


def test_tool_descriptions_and_schemas_survive_the_conversion(provider):
    echo = next(t for t in provider.tools() if t.name == "test__echo")
    assert "そのまま返す" in echo.description
    assert "text" in echo.input_schema["properties"]


def test_calling_a_tool_returns_the_servers_text(provider):
    echo = next(t for t in provider.tools() if t.name == "test__echo")
    assert "echo: やあ" in echo.run({"text": "やあ"})


def test_tools_plug_into_the_registry_like_builtins(provider):
    from jarvis.tools import ToolRegistry

    registry = ToolRegistry()
    registry.extend(provider.tools())
    output, is_error = registry.call("test__add", {"a": 2, "b": 3})
    assert is_error is False
    assert "5" in output


def test_server_error_is_surfaced_as_a_tool_error(provider):
    from jarvis.tools import ToolRegistry

    registry = ToolRegistry()
    registry.extend(provider.tools())
    # add は整数を要求するので、型違いはサーバ側でエラーになる。
    output, is_error = registry.call("test__add", {"a": "いち", "b": 3})
    assert is_error is True


def test_a_server_that_cannot_start_reports_what_to_check():
    with pytest.raises(EngineUnavailable, match="接続できない"):
        McpToolProvider(
            {"broken": {"command": sys.executable, "args": ["-c", "raise SystemExit(1)"]}},
            connect_timeout=10,
        )


def test_missing_command_is_rejected_before_starting_anything():
    with pytest.raises(EngineUnavailable, match="command"):
        McpToolProvider({"bad": {"args": ["x"]}})
