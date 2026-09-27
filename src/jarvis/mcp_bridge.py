"""MCP サーバのツールを、LLM に渡せる `ToolSpec` に変換する。

MCP の Python SDK は asyncio 前提だが、Jarvis のパイプラインは同期で回っている。
そこで専用スレッドでイベントループを1本動かし、そこに接続を保持したまま、
同期側からは `run_coroutine_threadsafe` で呼び出す形にしている。

各サーバの接続（stdio のプロセスと ClientSession）は1つの常駐タスクが
開いたまま持ち続ける。async context manager は入ったタスクと同じタスクで
抜ける必要があるため、接続と切断を別々の呼び出しに分けてはいけない。
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from concurrent.futures import Future
from typing import Any

from .errors import EngineUnavailable
from .tools import ToolSpec

log = logging.getLogger("jarvis.mcp")

_NAME_SAFE = re.compile(r"[^a-zA-Z0-9_-]")


class McpToolProvider:
    """設定された MCP サーバに繋ぎ、公開ツールを取り込む。"""

    def __init__(
        self,
        servers: dict[str, dict[str, Any]],
        timeout: float = 60.0,
        connect_timeout: float = 30.0,
    ) -> None:
        self.timeout = timeout
        self._sessions: dict[str, Any] = {}
        self._tools: list[ToolSpec] = []
        self._loop = asyncio.new_event_loop()
        self._stop = threading.Event()
        self._shutdown: asyncio.Event | None = None
        # ループスレッドが立ち上がりきるまで接続を始めない。asyncio.Event はループの
        # 中で作る必要があるので、その完了をこの threading.Event で待ち合わせる。
        self._loop_ready = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, name="jarvis-mcp", daemon=True)
        self._thread.start()
        if not self._loop_ready.wait(timeout=10):
            raise EngineUnavailable("mcp", "イベントループを起動できなかった", "")
        self._connect_all(servers, connect_timeout)

    # -- 接続 ---------------------------------------------------------------

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._shutdown = asyncio.Event()
        self._loop_ready.set()
        self._loop.run_forever()

    def _connect_all(self, servers: dict[str, dict[str, Any]], connect_timeout: float) -> None:
        params_cls = _stdio_params_cls()
        for name, spec in servers.items():
            if not isinstance(spec, dict) or "command" not in spec:
                raise EngineUnavailable(
                    f"mcp:{name}",
                    "command が指定されていない",
                    'tools.mcp_servers.<名前>.command に実行コマンドを書く',
                )
            params = params_cls(
                command=spec["command"],
                args=list(spec.get("args") or []),
                env=spec.get("env") or None,
                cwd=spec.get("cwd") or None,
            )
            ready: Future = Future()
            asyncio.run_coroutine_threadsafe(self._serve(name, params, ready), self._loop)
            try:
                tools = ready.result(timeout=connect_timeout)
            except Exception as exc:
                raise EngineUnavailable(
                    f"mcp:{name}",
                    f"MCP サーバに接続できない ({exc})",
                    f"`{spec['command']} {' '.join(spec.get('args') or [])}` が単体で起動するか確認する",
                ) from exc
            self._tools.extend(self._wrap(name, tools))
            log.info("MCP %s: %d 個のツールを取り込んだ", name, len(tools))

    async def _serve(self, name: str, params, ready: Future) -> None:
        """1サーバ分の接続を、このタスクの中で開いたまま保持する。"""
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    self._sessions[name] = session
                    ready.set_result(list(listed.tools))
                    # close() が呼ばれるまで、この1タスクが接続を開いたまま保持する。
                    await self._shutdown.wait()
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
            else:
                log.warning("MCP %s の接続が切れた: %s", name, exc)
        finally:
            self._sessions.pop(name, None)

    def _wrap(self, server: str, tools: list[Any]) -> list[ToolSpec]:
        specs = []
        for tool in tools:
            schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
            specs.append(
                ToolSpec(
                    name=_qualified_name(server, tool.name),
                    description=(tool.description or f"{server} の {tool.name}").strip(),
                    input_schema=schema or {"type": "object", "properties": {}},
                    run=self._make_runner(server, tool.name),
                )
            )
        return specs

    def _make_runner(self, server: str, tool_name: str):
        def run(arguments: dict[str, Any]) -> str:
            return self.call(server, tool_name, arguments)

        return run

    # -- 呼び出し -----------------------------------------------------------

    def call(self, server: str, tool_name: str, arguments: dict[str, Any]) -> str:
        session = self._sessions.get(server)
        if session is None:
            raise RuntimeError(f"MCP サーバ {server} に接続していない")
        future = asyncio.run_coroutine_threadsafe(
            session.call_tool(tool_name, arguments or {}), self._loop
        )
        result = future.result(timeout=self.timeout)
        text = _render_result(result)
        if getattr(result, "is_error", False) or getattr(result, "isError", False):
            raise RuntimeError(text or "MCP ツールがエラーを返した")
        return text

    def tools(self) -> list[ToolSpec]:
        return list(self._tools)

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        if self._shutdown is not None:
            self._loop.call_soon_threadsafe(self._shutdown.set)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


def _qualified_name(server: str, tool: str) -> str:
    """サーバ名で修飾し、ツール名として使える文字だけに落とす。"""
    return _NAME_SAFE.sub("_", f"{server}__{tool}")[:128]


def _render_result(result: Any) -> str:
    """CallToolResult を、モデルに返すプレーンテキストにする。"""
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
            continue
        uri = getattr(getattr(block, "resource", None), "uri", None)
        parts.append(f"[{getattr(block, 'type', 'content')}{f' {uri}' if uri else ''}]")
    if parts:
        return "\n".join(parts)
    structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if structured is not None:
        import json

        return json.dumps(structured, ensure_ascii=False)
    return ""


def _stdio_params_cls():
    try:
        from mcp import StdioServerParameters  # noqa: PLC0415
    except ImportError as exc:
        raise EngineUnavailable("mcp", "mcp が入っていない", 'pip install "jarvis[mcp]"') from exc
    return StdioServerParameters
