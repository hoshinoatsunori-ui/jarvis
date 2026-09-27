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


# --- .env の読み込み ---------------------------------------------------------

def test_env_file_values_are_loaded(tmp_path, monkeypatch):
    from jarvis.config import load_env_file

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JARVIS_SAMPLE_KEY", raising=False)
    (tmp_path / ".env").write_text("JARVIS_SAMPLE_KEY=abc123\n", encoding="utf-8")

    assert load_env_file() == ["JARVIS_SAMPLE_KEY"]
    import os

    assert os.environ["JARVIS_SAMPLE_KEY"] == "abc123"


def test_existing_environment_wins_over_the_file(tmp_path, monkeypatch):
    # その場で設定した値のほうが、書き置いた値より新しい意図とみなす。
    from jarvis.config import load_env_file

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JARVIS_SAMPLE_KEY", "シェルで設定した値")
    (tmp_path / ".env").write_text("JARVIS_SAMPLE_KEY=ファイルの値\n", encoding="utf-8")

    assert load_env_file() == []
    import os

    assert os.environ["JARVIS_SAMPLE_KEY"] == "シェルで設定した値"


def test_missing_env_file_is_not_an_error(tmp_path, monkeypatch):
    from jarvis.config import load_env_file

    monkeypatch.chdir(tmp_path)
    assert load_env_file() == []


@pytest.mark.parametrize(
    "line, expected",
    [
        ("KEY=value", ("KEY", "value")),
        ("  KEY = value  ", ("KEY", "value")),
        ("export KEY=value", ("KEY", "value")),
        ('KEY="quoted"', ("KEY", "quoted")),
        ("KEY='quoted'", ("KEY", "quoted")),
        ("KEY=", ("KEY", "")),
        ("KEY=a=b", ("KEY", "a=b")),
        ("# コメント", None),
        ("", None),
        ("   ", None),
        ("値のない行", None),
        ("=値だけ", None),
    ],
)
def test_env_line_parsing(line, expected):
    from jarvis.config import _parse_env_line

    assert _parse_env_line(line) == expected


def test_env_file_with_bom_is_read(tmp_path, monkeypatch):
    # Windows のエディタは BOM 付き UTF-8 で保存することがある。
    from jarvis.config import load_env_file

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JARVIS_BOM_KEY", raising=False)
    (tmp_path / ".env").write_text("JARVIS_BOM_KEY=ok\n", encoding="utf-8-sig")

    assert load_env_file() == ["JARVIS_BOM_KEY"]
