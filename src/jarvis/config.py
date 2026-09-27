"""設定の読み込み。

設定は YAML 1枚にまとまっていて、各段は `engine`（実装名）と `options`（その実装への引数）
という同じ形をしている。段を差し替えるときに触るのは `engine` の1行だけで済む。

値の中の `${ENV_VAR}` は環境変数に展開される。APIキーを YAML に直接書かないための仕組み。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

DEFAULT_CONFIG_NAMES = ("config.yaml", "config.yml")

DEFAULT_ENV_FILE = ".env"


@dataclass
class StageConfig:
    """1つの段の設定。"""

    engine: str
    options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Any, *, stage: str, default_engine: str) -> "StageConfig":
        if raw is None:
            return cls(engine=default_engine)
        if isinstance(raw, str):
            return cls(engine=raw)
        if not isinstance(raw, dict):
            raise ConfigError(f"{stage}: マッピングか文字列で書くこと (現在: {type(raw).__name__})")
        engine = raw.get("engine", default_engine)
        if engine is None:
            raise ConfigError(
                f"{stage}.engine が null になっている。YAML では `null` は文字列ではなく"
                f" null リテラルとして読まれる。何もしないエンジンを指定したいなら `none` と書くこと。"
            )
        if not isinstance(engine, str):
            raise ConfigError(f"{stage}.engine: 文字列で書くこと (現在: {type(engine).__name__})")
        options = raw.get("options") or {}
        if not isinstance(options, dict):
            raise ConfigError(f"{stage}.options: マッピングで書くこと")
        return cls(engine=engine, options=options)


@dataclass
class AudioConfig:
    sample_rate: int = 16000
    frame_ms: int = 30
    input_device: int | str | None = None


@dataclass
class ToolsConfig:
    builtin: bool = True
    #: サーバ名 -> {"command": str, "args": [str], "env": {str: str}}
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class MemoryConfig:
    path: str = "state/memory.json"
    log_path: str = "state/conversation.jsonl"


@dataclass
class Config:
    language: str = "ja"
    audio: AudioConfig = field(default_factory=AudioConfig)
    wake: StageConfig = field(default_factory=lambda: StageConfig("manual"))
    vad: StageConfig = field(default_factory=lambda: StageConfig("energy"))
    stt: StageConfig = field(default_factory=lambda: StageConfig("faster_whisper"))
    llm: StageConfig = field(default_factory=lambda: StageConfig("anthropic"))
    tts: StageConfig = field(default_factory=lambda: StageConfig("voicevox"))
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    #: 読み込み元。相対パスの基準に使う。
    source: Path | None = None

    def resolve(self, path: str) -> Path:
        """設定ファイルからの相対パスを絶対パスにする。"""
        p = Path(path).expanduser()
        if p.is_absolute() or self.source is None:
            return p
        return (self.source.parent / p).resolve()

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        """設定ファイルを読む。パス未指定なら既定の名前を探し、無ければ既定値で動く。"""
        resolved = _locate(path)
        if resolved is None:
            return cls()
        try:
            raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{resolved}: YAML として読めない\n{exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{resolved}: トップレベルはマッピングで書くこと")
        return cls.from_dict(raw, source=resolved)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, source: Path | None = None) -> "Config":
        raw = expand_env(raw)
        audio_raw = raw.get("audio") or {}
        tools_raw = raw.get("tools") or {}
        memory_raw = raw.get("memory") or {}
        defaults = cls()
        return cls(
            language=raw.get("language", defaults.language),
            audio=AudioConfig(
                sample_rate=int(audio_raw.get("sample_rate", defaults.audio.sample_rate)),
                frame_ms=int(audio_raw.get("frame_ms", defaults.audio.frame_ms)),
                input_device=audio_raw.get("input_device"),
            ),
            wake=StageConfig.parse(raw.get("wake"), stage="wake", default_engine=defaults.wake.engine),
            vad=StageConfig.parse(raw.get("vad"), stage="vad", default_engine=defaults.vad.engine),
            stt=StageConfig.parse(raw.get("stt"), stage="stt", default_engine=defaults.stt.engine),
            llm=StageConfig.parse(raw.get("llm"), stage="llm", default_engine=defaults.llm.engine),
            tts=StageConfig.parse(raw.get("tts"), stage="tts", default_engine=defaults.tts.engine),
            tools=ToolsConfig(
                builtin=bool(tools_raw.get("builtin", defaults.tools.builtin)),
                mcp_servers=tools_raw.get("mcp_servers") or {},
            ),
            memory=MemoryConfig(
                path=memory_raw.get("path", defaults.memory.path),
                log_path=memory_raw.get("log_path", defaults.memory.log_path),
            ),
            source=source,
        )


def _locate(path: str | Path | None) -> Path | None:
    if path is not None:
        p = Path(path).expanduser()
        if not p.exists():
            raise ConfigError(f"設定ファイルが無い: {p}")
        return p
    for name in DEFAULT_CONFIG_NAMES:
        p = Path(name)
        if p.exists():
            return p.resolve()
    return None


def expand_env(value: Any) -> Any:
    """設定値の中の `${VAR}` / `${VAR:-既定値}` を環境変数に展開する。"""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(_replace_env, value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


def _replace_env(match: re.Match[str]) -> str:
    name, default = match.group(1), match.group(2)
    env = os.environ.get(name)
    if env is not None:
        return env
    if default is not None:
        return default
    raise ConfigError(
        f"環境変数 {name} が未設定（設定ファイル中の ${{{name}}} を解決できない）。"
        f" `export {name}=...` するか、${{{name}:-既定値}} の形で既定値を書くこと。"
    )


def load_env_file(path: str | Path | None = None) -> list[str]:
    """`.env` を読んで環境変数に入れ、入れた変数名を返す。

    既に設定済みの変数は上書きしない。その場で `$env:` や `export` で指定した
    値のほうが、ファイルに書き置いた値より新しい意図だとみなす。

    APIキーを毎回シェルで設定し直さずに済ませるための仕組み。とくに
    PowerShell の `$env:` はウィンドウを閉じると消えるため、これが無いと
    ターミナルを開き直すたびに設定し直すことになる。
    """
    env_path = Path(path) if path else Path(DEFAULT_ENV_FILE)
    if not env_path.exists():
        return []
    loaded = []
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        parsed = _parse_env_line(line)
        if parsed is None:
            continue
        name, value = parsed
        if name in os.environ:
            continue
        os.environ[name] = value
        loaded.append(name)
    return loaded


def _parse_env_line(line: str) -> tuple[str, str] | None:
    """`.env` の1行を (名前, 値) にする。コメント・空行・壊れた行は None。"""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export "):].lstrip()
    name, sep, value = line.partition("=")
    name = name.strip()
    if not sep or not name:
        return None
    value = value.strip()
    # 値全体が引用符で囲まれていれば外す。中身のエスケープまでは解釈しない。
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return name, value
