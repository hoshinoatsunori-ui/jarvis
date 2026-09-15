import pytest

from jarvis import factory
from jarvis.config import Config, StageConfig
from jarvis.errors import ConfigError


def test_unknown_engine_names_the_available_ones():
    with pytest.raises(ConfigError, match="voicevox"):
        factory.build_tts(StageConfig("does_not_exist"))


def test_unknown_option_names_the_accepted_ones():
    with pytest.raises(ConfigError, match="prefix"):
        factory.build_tts(StageConfig("none", {"bogus": 1}))


def test_aivisspeech_defaults_to_its_own_port():
    tts = factory.build_tts(StageConfig("aivisspeech"))
    assert tts.base_url == "http://127.0.0.1:10101"


def test_explicit_base_url_overrides_the_engine_default():
    tts = factory.build_tts(StageConfig("aivisspeech", {"base_url": "http://box:1234"}))
    assert tts.base_url == "http://box:1234"


def test_agent_only_receives_tools_and_memory_if_it_accepts_them(tmp_path):
    memory = factory.build_memory(Config(memory=Config().memory))
    del memory
    # EchoAgent は memory を受け取らない。渡されても TypeError にならないこと。
    agent = factory.build_agent(StageConfig("echo"), tools=None, memory=None)
    assert "".join(agent.respond("やあ"))


def test_builtin_tools_can_be_switched_off(tmp_path):
    config = Config.from_dict({"tools": {"builtin": False}})
    registry = factory.build_tools(config, factory.build_memory(config))
    assert len(registry) == 0
