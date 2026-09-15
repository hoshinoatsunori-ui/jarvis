"""AnthropicAgent のツールループを、SDK を差し替えて検証する。

ネットワークには出ない。検証したいのは「ツール呼び出しを実行して結果を正しい形で
返し、会話が続くか」であって、モデルの応答内容ではない。
"""

from dataclasses import dataclass, field
from typing import Any

import pytest

from jarvis.llm import AnthropicAgent
from jarvis.tools import ToolRegistry, ToolSpec


@dataclass
class Block:
    type: str
    text: str = ""
    name: str = ""
    id: str = ""
    input: dict = field(default_factory=dict)


@dataclass
class FakeMessage:
    content: list[Block]
    stop_reason: str = "end_turn"


class FakeStream:
    def __init__(self, message: FakeMessage) -> None:
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        for block in self._message.content:
            if block.type == "text":
                # 実際の SDK と同じく、1文が複数の差分に割れて届く状況を模す。
                for i in range(0, len(block.text), 3):
                    yield block.text[i : i + 3]

    def get_final_message(self) -> FakeMessage:
        return self._message


class FakeMessages:
    def __init__(self, responses: list[FakeMessage]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def stream(self, **params):
        # 実際の SDK は呼び出し時点で messages を直列化する。エージェントは同じリストを
        # 使い回すので、記録側でスナップショットを取らないと後の変更が混ざる。
        self.calls.append({**params, "messages": list(params["messages"])})
        if not self._responses:
            raise AssertionError("想定より多く API が呼ばれた")
        return FakeStream(self._responses.pop(0))


class FakeClient:
    def __init__(self, responses: list[FakeMessage]) -> None:
        self.messages = FakeMessages(responses)
        self.beta = type("Beta", (), {"messages": self.messages})()


def make_agent(responses, tools=None, **kwargs) -> AnthropicAgent:
    return AnthropicAgent(client=FakeClient(responses), tools=tools, **kwargs)


def test_plain_reply_is_streamed_back():
    agent = make_agent([FakeMessage([Block("text", "はい、承知しました。")])])
    assert "".join(agent.respond("よろしく")) == "はい、承知しました。"


def test_tool_call_is_executed_and_result_fed_back():
    calls = []
    registry = ToolRegistry()
    registry.add(
        ToolSpec(
            name="get_time",
            description="時刻",
            input_schema={"type": "object", "properties": {}},
            run=lambda args: calls.append(args) or "12:00",
        )
    )
    agent = make_agent(
        [
            FakeMessage([Block("tool_use", name="get_time", id="tu_1", input={})], stop_reason="tool_use"),
            FakeMessage([Block("text", "12時です。")]),
        ],
        tools=registry,
    )
    assert "".join(agent.respond("今何時")) == "12時です。"
    assert calls == [{}]

    second_request = agent._client.messages.calls[1]
    tool_result = second_request["messages"][-1]["content"][0]
    assert tool_result == {
        "type": "tool_result",
        "tool_use_id": "tu_1",
        "content": "12:00",
        "is_error": False,
    }


def test_parallel_tool_calls_are_returned_in_one_message():
    registry = ToolRegistry()
    for name in ("a", "b"):
        registry.add(
            ToolSpec(name=name, description=name, input_schema={}, run=lambda args, n=name: n.upper())
        )
    agent = make_agent(
        [
            FakeMessage(
                [Block("tool_use", name="a", id="1"), Block("tool_use", name="b", id="2")],
                stop_reason="tool_use",
            ),
            FakeMessage([Block("text", "done")]),
        ],
        tools=registry,
    )
    list(agent.respond("両方やって"))
    results = agent._client.messages.calls[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["1", "2"]


def test_failing_tool_is_reported_to_the_model_as_an_error():
    registry = ToolRegistry()
    registry.add(
        ToolSpec(
            name="boom",
            description="壊れる",
            input_schema={},
            run=lambda args: (_ for _ in ()).throw(RuntimeError("故障")),
        )
    )
    agent = make_agent(
        [
            FakeMessage([Block("tool_use", name="boom", id="1")], stop_reason="tool_use"),
            FakeMessage([Block("text", "うまくいきませんでした。")]),
        ],
        tools=registry,
    )
    list(agent.respond("やって"))
    result = agent._client.messages.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True
    assert "故障" in result["content"]


def test_refusal_is_turned_into_a_spoken_line():
    agent = make_agent([FakeMessage([], stop_reason="refusal")])
    assert "".join(agent.respond("まずい依頼")) == "その依頼にはお答えできません。"


def test_tool_loop_stops_at_the_round_limit():
    registry = ToolRegistry()
    registry.add(ToolSpec(name="loop", description="", input_schema={}, run=lambda args: "again"))
    responses = [
        FakeMessage([Block("tool_use", name="loop", id=str(i))], stop_reason="tool_use") for i in range(3)
    ]
    agent = make_agent(responses, tools=registry, max_tool_rounds=3)
    assert "止めます" in "".join(agent.respond("無限ループ"))


def test_memory_is_injected_into_the_system_prompt(tmp_path):
    from jarvis.memory import Memory

    memory = Memory(tmp_path / "m.json")
    memory.remember("犬の名前はポチ")
    agent = make_agent([FakeMessage([Block("text", "はい")])], memory=memory)
    list(agent.respond("犬の名前は"))
    system = agent._client.messages.calls[0]["system"]
    assert system[0]["text"].startswith("あなたは「ジャービス」")
    assert "犬の名前はポチ" in system[1]["text"]


def test_request_carries_effort_and_refusal_fallbacks():
    agent = make_agent([FakeMessage([Block("text", "はい")])], effort="low")
    list(agent.respond("やあ"))
    params = agent._client.messages.calls[0]
    assert params["model"] == "claude-opus-5"
    assert params["output_config"] == {"effort": "low"}
    assert params["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in params["betas"]


def test_history_is_trimmed_without_orphaning_tool_results():
    agent = make_agent([FakeMessage([Block("text", "ok")])], max_history_turns=1)
    agent.messages = [
        {"role": "user", "content": "1回目"},
        {"role": "assistant", "content": [Block("tool_use", name="x", id="1")]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1", "content": "r"}]},
        {"role": "assistant", "content": [Block("text", "答え")]},
    ]
    list(agent.respond("2回目"))
    # 先頭はツール結果ではない素のユーザー発話であること（孤立した tool_result は 400 になる）。
    assert agent.messages[0]["role"] == "user"
    assert isinstance(agent.messages[0]["content"], str)


def test_reset_clears_history():
    agent = make_agent([FakeMessage([Block("text", "ok")])])
    list(agent.respond("やあ"))
    agent.reset()
    assert agent.messages == []


@pytest.mark.parametrize("fallbacks", [True, False])
def test_fallbacks_flag_selects_the_endpoint(fallbacks):
    agent = make_agent([FakeMessage([Block("text", "ok")])], fallbacks=fallbacks)
    list(agent.respond("やあ"))
    params = agent._client.messages.calls[0]
    assert ("fallbacks" in params) is fallbacks
