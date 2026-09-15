"""テスト用の最小 MCP サーバ（stdio）。

`tests/test_mcp_bridge.py` が子プロセスとして起動し、McpToolProvider が
実際に接続してツールを呼べることを確かめるために使う。
"""

from mcp.server.mcpserver import MCPServer

server = MCPServer("jarvis-test")


@server.tool()
def echo(text: str) -> str:
    """渡された文字列をそのまま返す。"""
    return f"echo: {text}"


@server.tool()
def add(a: int, b: int) -> int:
    """2つの整数を足す。"""
    return a + b


if __name__ == "__main__":
    server.run()
