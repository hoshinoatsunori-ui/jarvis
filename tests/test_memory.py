import json

import pytest

from jarvis.memory import Memory


@pytest.fixture
def memory(tmp_path):
    return Memory(tmp_path / "memory.json", tmp_path / "log.jsonl")


def test_facts_survive_a_restart(memory, tmp_path):
    memory.remember("コーヒーはブラック")
    reloaded = Memory(tmp_path / "memory.json")
    assert [f.text for f in reloaded.recall()] == ["コーヒーはブラック"]


def test_recall_filters_by_substring(memory):
    memory.remember("コーヒーはブラック")
    memory.remember("紅茶はミルクティー")
    assert [f.text for f in memory.recall("紅茶")] == ["紅茶はミルクティー"]


def test_forget_removes_matching_facts(memory):
    memory.remember("消す対象")
    assert memory.forget("消す対象") == 1
    assert memory.recall() == []
    assert memory.forget("もう無い") == 0


def test_empty_fact_is_rejected(memory):
    with pytest.raises(ValueError):
        memory.remember("   ")


def test_oldest_facts_are_dropped_past_the_cap(tmp_path):
    memory = Memory(tmp_path / "memory.json", max_facts=3)
    for i in range(5):
        memory.remember(f"事実{i}")
    assert [f.text for f in memory.recall()] == ["事実2", "事実3", "事実4"]


def test_corrupt_memory_file_does_not_block_startup(tmp_path):
    path = tmp_path / "memory.json"
    path.write_text("{ これは JSON ではない", encoding="utf-8")
    assert Memory(path).recall() == []


def test_prompt_block_is_empty_without_facts(memory):
    assert memory.as_prompt_block() == ""
    memory.remember("朝は7時起き")
    assert "朝は7時起き" in memory.as_prompt_block()


def test_turns_are_appended_to_the_log(memory, tmp_path):
    memory.log_turn("user", "おはよう")
    memory.log_turn("assistant", "おはようございます")
    lines = (tmp_path / "log.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["role"] for line in lines] == ["user", "assistant"]
