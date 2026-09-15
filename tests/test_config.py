import pytest

from jarvis.config import Config, StageConfig, expand_env
from jarvis.errors import ConfigError


def test_stage_can_be_written_as_a_bare_string():
    config = Config.from_dict({"tts": "null"})
    assert config.tts == StageConfig(engine="null", options={})


def test_missing_stages_fall_back_to_defaults():
    config = Config.from_dict({})
    assert config.wake.engine == "manual"
    assert config.stt.engine == "faster_whisper"
    assert config.audio.sample_rate == 16000


def test_env_vars_are_expanded(monkeypatch):
    monkeypatch.setenv("JARVIS_TEST_KEY", "s3cret")
    config = Config.from_dict({"llm": {"options": {"api_key": "${JARVIS_TEST_KEY}"}}})
    assert config.llm.options["api_key"] == "s3cret"


def test_env_var_default_is_used_when_unset(monkeypatch):
    monkeypatch.delenv("JARVIS_ABSENT", raising=False)
    assert expand_env("${JARVIS_ABSENT:-http://localhost:50021}") == "http://localhost:50021"


def test_missing_env_var_without_default_is_an_error(monkeypatch):
    monkeypatch.delenv("JARVIS_ABSENT", raising=False)
    with pytest.raises(ConfigError, match="JARVIS_ABSENT"):
        expand_env("${JARVIS_ABSENT}")


def test_options_must_be_a_mapping():
    with pytest.raises(ConfigError, match="options"):
        Config.from_dict({"tts": {"engine": "null", "options": ["nope"]}})


def test_relative_paths_resolve_against_the_config_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("memory:\n  path: state/memory.json\n", encoding="utf-8")
    config = Config.load(path)
    assert config.resolve(config.memory.path) == tmp_path / "state" / "memory.json"


def test_missing_config_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError):
        Config.load(tmp_path / "absent.yaml")


def test_yaml_null_as_an_engine_name_is_caught_with_an_explanation():
    # `tts: {engine: null}` は YAML の null。利用者が「何もしないエンジン」のつもりで
    # 書きがちなので、`none` を使うよう明示する。
    with pytest.raises(ConfigError, match="none"):
        Config.from_dict({"tts": {"engine": None}})
