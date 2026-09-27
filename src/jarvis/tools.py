"""LLM に渡すツール群。

Anthropic の tool use（OpenAI でいう function calling）に載る形に揃える。
MCP サーバのツールも `mcp_bridge` が同じ `ToolSpec` に変換するので、
LLM 側から見ると組み込みツールと MCP ツールの区別は無い。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from .memory import Memory


@dataclass
class ToolSpec:
    """1つのツール。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    run: Callable[[dict[str, Any]], str]

    def to_anthropic(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


class ToolRegistry:
    """名前からツールを引ける表。同名は後勝ちではなく明示的に拒否する。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._closers: list[Callable[[], None]] = []

    def add(self, tool: ToolSpec) -> None:
        if tool.name in self._tools:
            raise ValueError(f"ツール名が重複している: {tool.name}")
        self._tools[tool.name] = tool

    def extend(self, tools: list[ToolSpec]) -> None:
        for tool in tools:
            self.add(tool)

    def add_closer(self, close: Callable[[], None]) -> None:
        """終了時に後始末が要るツール提供元（MCP 接続など）を登録する。"""
        self._closers.append(close)

    def close(self) -> None:
        for close in self._closers:
            close()
        self._closers.clear()

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return list(self._tools)

    def to_anthropic(self) -> list[dict[str, Any]]:
        return [t.to_anthropic() for t in self._tools.values()]

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """ツールを実行し `(結果テキスト, エラーか)` を返す。

        ツールの失敗で会話を落とさない。エラーもモデルに返して判断させる。
        """
        tool = self._tools.get(name)
        if tool is None:
            return f"そのようなツールは無い: {name}（利用可能: {', '.join(self.names())}）", True
        try:
            return tool.run(arguments), False
        except Exception as exc:  # ツールの例外は会話を止める理由にならない
            return f"{name} の実行に失敗: {exc}", True


def builtin_tools(memory: Memory) -> list[ToolSpec]:
    """依存なしで動く最小限のツール。"""

    def current_time(args: dict[str, Any]) -> str:
        del args
        now = datetime.now().astimezone()
        return now.strftime("%Y-%m-%d %H:%M:%S %Z (%A)")

    def remember(args: dict[str, Any]) -> str:
        fact = memory.remember(str(args["text"]))
        return f"覚えた: {fact.text}"

    def recall(args: dict[str, Any]) -> str:
        hits = memory.recall(args.get("query"), int(args.get("limit", 20)))
        if not hits:
            return "該当する記憶は無い"
        return json.dumps([f.text for f in hits], ensure_ascii=False)

    def forget(args: dict[str, Any]) -> str:
        removed = memory.forget(str(args["text"]))
        return f"{removed} 件消した" if removed else "一致する記憶が無かった"

    return [
        ToolSpec(
            name="get_current_time",
            description="現在の日付・時刻・曜日を取得する。時間に関する質問には必ずこれを使うこと。",
            input_schema={"type": "object", "properties": {}, "required": []},
            run=current_time,
        ),
        ToolSpec(
            name="remember",
            description=(
                "ユーザーに関する事実を長期記憶に保存する。"
                "好み・予定・名前など、次回以降も覚えておくべきことを言われたときに使う。"
            ),
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string", "description": "覚える内容を一文で"}},
                "required": ["text"],
            },
            run=remember,
        ),
        ToolSpec(
            name="recall",
            description="長期記憶から事実を検索する。query 省略で最近のものを返す。",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "部分一致で絞り込むキーワード"},
                    "limit": {"type": "integer", "description": "最大件数（既定20）"},
                },
                "required": [],
            },
            run=recall,
        ),
        ToolSpec(
            name="forget",
            description="長期記憶から、内容が一致する事実を消す。",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string", "description": "消す内容（完全一致）"}},
                "required": ["text"],
            },
            run=forget,
        ),
    ]
