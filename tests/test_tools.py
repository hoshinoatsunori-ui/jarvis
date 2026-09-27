import json

import pytest

from jarvis.memory import Memory
from jarvis.tools import ToolRegistry, ToolSpec, builtin_tools


@pytest.fixture
def registry(tmp_path):
    reg = ToolRegistry()
    reg.extend(builtin_tools(Memory(tmp_path / "m.json")))
    return reg


def test_builtin_tools_are_exposed_in_anthropic_shape(registry):
    specs = {s["name"]: s for s in registry.to_anthropic()}
    assert set(specs) == {"get_current_time", "remember", "recall", "forget"}
    assert specs["remember"]["input_schema"]["required"] == ["text"]


def test_remember_then_recall_round_trips(registry):
    assert registry.call("remember", {"text": "猫を飼っている"})[1] is False
    output, is_error = registry.call("recall", {"query": "猫"})
    assert is_error is False
    assert json.loads(output) == ["猫を飼っている"]


def test_unknown_tool_is_reported_as_an_error_not_an_exception(registry):
    output, is_error = registry.call("does_not_exist", {})
    assert is_error is True
    assert "does_not_exist" in output


def test_failing_tool_is_reported_as_an_error(registry):
    # 必須引数が無いと remember は KeyError を投げるが、会話は止めない。
    output, is_error = registry.call("remember", {})
    assert is_error is True
    assert "remember" in output


def test_duplicate_tool_names_are_rejected():
    spec = ToolSpec(name="dup", description="d", input_schema={}, run=lambda a: "")
    registry = ToolRegistry()
    registry.add(spec)
    with pytest.raises(ValueError, match="dup"):
        registry.add(spec)


def test_closers_run_once():
    calls = []
    registry = ToolRegistry()
    registry.add_closer(lambda: calls.append(1))
    registry.close()
    registry.close()
    assert calls == [1]
