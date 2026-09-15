"""LLM の段。ツール実行のループをここに閉じ込める。

音声アシスタントなので、体感レイテンシを削る方向に倒してある:
- ストリーミングで受け、パイプライン側が文単位で TTS に流す
- `effort` を下げて思考を浅くする（雑談・家電操作は深い推論を要さない）
- 思考そのものは切らない。Opus 5 で thinking を disabled にすると、
  ツール呼び出しを tool_use ブロックではなく本文テキストに書いてしまい、
  呼び出しが実行されないまま会話が進む既知の失敗モードがあるため。
"""

from __future__ import annotations

from typing import Any, Iterator

from .errors import EngineUnavailable
from .interfaces import Agent
from .memory import Memory
from .tools import ToolRegistry

DEFAULT_MODEL = "claude-opus-5"

DEFAULT_PERSONA = """あなたは「ジャービス」という名前の音声アシスタントです。
応答は音声で読み上げられます。次の制約を必ず守ってください。

- 日本語で、話し言葉として自然に答える。
- 1〜3文、長くても120文字程度に収める。箇条書き・見出し・記号装飾は使わない。
- URL やコード、長い数字の羅列は読み上げに向かないので、要点だけ言う。
- 知らないことは知らないと言う。推測を事実のように言わない。
- 時刻・予定・記憶に関わることは、必ず対応するツールを呼んでから答える。
"""


class AnthropicAgent(Agent):
    """Claude を使う応答生成器。ツールは tool use で呼ぶ。"""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 2048,
        effort: str | None = "low",
        persona: str = DEFAULT_PERSONA,
        tools: ToolRegistry | None = None,
        memory: Memory | None = None,
        max_history_turns: int = 12,
        max_tool_rounds: int = 6,
        api_key: str | None = None,
        timeout: float = 60.0,
        fallbacks: bool = True,
        client: Any = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        self.persona = persona
        self.tools = tools or ToolRegistry()
        self.memory = memory
        self.max_history_turns = max_history_turns
        self.max_tool_rounds = max_tool_rounds
        self.fallbacks = fallbacks
        self.messages: list[dict[str, Any]] = []
        # client はテストで差し替えるための注入口。通常は None のまま SDK を組み立てる。
        self._client = client if client is not None else _anthropic_client(api_key, timeout)

    # -- 応答 ---------------------------------------------------------------

    def respond(self, user_text: str) -> Iterator[str]:
        self.messages.append({"role": "user", "content": user_text})
        for _ in range(self.max_tool_rounds):
            final = yield from self._stream_turn()
            if final.stop_reason == "refusal":
                yield "その依頼にはお答えできません。"
                break
            self.messages.append({"role": "assistant", "content": final.content})
            tool_uses = [b for b in final.content if b.type == "tool_use"]
            if not tool_uses:
                break
            self.messages.append({"role": "user", "content": self._run_tools(tool_uses)})
        else:
            yield "ツールの呼び出しが続いたので、いったん止めます。"
        self._trim()

    def _stream_turn(self):
        """1ターン分をストリーミングし、テキスト片を yield して最終メッセージを返す。"""
        params = self._request_params()
        create = self._client.beta.messages.stream if self.fallbacks else self._client.messages.stream
        with create(**params) as stream:
            for text in stream.text_stream:
                yield text
            return stream.get_final_message()

    def _request_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": self._system_blocks(),
            "messages": self.messages,
        }
        if self.effort:
            params["output_config"] = {"effort": self.effort}
        if len(self.tools):
            params["tools"] = self.tools.to_anthropic()
        if self.fallbacks:
            # 安全分類器が応答を拒否した場合に、サーバ側で別モデルへ回してもらう。
            params["betas"] = ["server-side-fallback-2026-07-01"]
            params["fallbacks"] = "default"
        return params

    def _system_blocks(self) -> list[dict[str, Any]]:
        # 前半は不変のペルソナ、後半は毎回変わりうる記憶。プロンプトキャッシュを効かせるなら
        # 不変部分を先に置く必要があるため、この順序は入れ替えないこと。
        blocks = [{"type": "text", "text": self.persona}]
        if self.memory is not None:
            block = self.memory.as_prompt_block()
            if block:
                blocks.append({"type": "text", "text": block})
        return blocks

    def _run_tools(self, tool_uses) -> list[dict[str, Any]]:
        """並列に来たツール呼び出しをまとめて実行し、結果を1つのユーザーメッセージに詰める。

        結果を複数メッセージに分けるとモデルが並列呼び出しをしなくなるので、必ずまとめる。
        """
        results = []
        for block in tool_uses:
            arguments = block.input if isinstance(block.input, dict) else {}
            output, is_error = self.tools.call(block.name, arguments)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                    "is_error": is_error,
                }
            )
        return results

    def _trim(self) -> None:
        """履歴を上限まで切り詰める。ツール結果が孤立しない位置で切る。"""
        limit = self.max_history_turns * 2
        if len(self.messages) <= limit:
            return
        start = len(self.messages) - limit
        while start < len(self.messages) and not _is_plain_user_turn(self.messages[start]):
            start += 1
        self.messages = self.messages[start:]

    def reset(self) -> None:
        self.messages = []


def _is_plain_user_turn(message: dict[str, Any]) -> bool:
    """ツール結果ではない、素のユーザー発話かどうか。"""
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    return not any(getattr(b, "type", None) == "tool_result" or (isinstance(b, dict) and b.get("type") == "tool_result") for b in content or [])


class EchoAgent(Agent):
    """API を叩かずに配線だけ確かめるためのエージェント。

    `jarvis doctor` や、APIキー無しでマイク〜TTS の経路を通したいときに使う。
    """

    def __init__(self, template: str = "「{text}」と聞こえました。", tools: ToolRegistry | None = None) -> None:
        self.template = template
        self.tools = tools or ToolRegistry()

    def respond(self, user_text: str) -> Iterator[str]:
        yield self.template.format(text=user_text)


def _anthropic_client(api_key: str | None, timeout: float):
    try:
        import anthropic  # noqa: PLC0415
    except ImportError as exc:
        raise EngineUnavailable(
            "llm:anthropic", "anthropic が入っていない", 'pip install "jarvis[llm]"'
        ) from exc
    try:
        # 引数なしの場合、SDK が ANTHROPIC_API_KEY → ANTHROPIC_AUTH_TOKEN →
        # `ant auth login` のプロファイル、の順に資格情報を解決する。
        return anthropic.Anthropic(api_key=api_key, timeout=timeout) if api_key else anthropic.Anthropic(timeout=timeout)
    except Exception as exc:
        raise EngineUnavailable(
            "llm:anthropic",
            f"Anthropic クライアントを作れない ({exc})",
            "ANTHROPIC_API_KEY を設定するか `ant auth login` する",
        ) from exc
