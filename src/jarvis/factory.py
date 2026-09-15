"""設定の `engine` 名から各段の実装を組み立てる。

ここが唯一の「名前 → 実装」の対応表。段を足すときはこの表に1行足す。
import は呼ばれたときに初めて行う。faster-whisper も openwakeword も重い依存なので、
使わないエンジンのせいで起動が遅くなったり ImportError で落ちたりしないようにしている。
"""

from __future__ import annotations

import importlib
import inspect
from typing import Any

from .config import Config, StageConfig
from .errors import ConfigError
from .interfaces import Agent, SpeechToText, TextToSpeech, VoiceActivityDetector, WakeWordEngine
from .memory import Memory
from .tools import ToolRegistry, builtin_tools

WAKE_ENGINES = {
    "manual": ("jarvis.wake", "ManualWake"),
    "openwakeword": ("jarvis.wake", "OpenWakeWord"),
    "porcupine": ("jarvis.wake", "Porcupine"),
}

VAD_ENGINES = {
    "energy": ("jarvis.vad", "EnergyVad"),
    "silero": ("jarvis.vad", "SileroVad"),
}

# YAML では `null` が null リテラルとして解釈されてしまうため、
# 「何もしないエンジン」の名前は `none` にしてある（`null` は CLI 用の別名）。
STT_ENGINES = {
    "faster_whisper": ("jarvis.stt", "FasterWhisper"),
    "openai": ("jarvis.stt", "OpenAIWhisper"),
    "none": ("jarvis.stt", "NullStt"),
    "null": ("jarvis.stt", "NullStt"),
}

TTS_ENGINES = {
    "voicevox": ("jarvis.tts", "VoicevoxTts"),
    "aivisspeech": ("jarvis.tts", "VoicevoxTts"),  # VOICEVOX 互換 API。base_url だけ違う
    "openai": ("jarvis.tts", "OpenAITts"),
    "system": ("jarvis.tts", "SystemTts"),
    "none": ("jarvis.tts", "NullTts"),
    "null": ("jarvis.tts", "NullTts"),
}

LLM_ENGINES = {
    "anthropic": ("jarvis.llm", "AnthropicAgent"),
    "echo": ("jarvis.llm", "EchoAgent"),
}

#: aivisspeech は VOICEVOX と同じ実装を使うが既定ポートが違う。
ENGINE_DEFAULTS = {
    ("tts", "aivisspeech"): {"base_url": "http://127.0.0.1:10101"},
}


def build_wake(cfg: StageConfig) -> WakeWordEngine:
    return _build(WAKE_ENGINES, "wake", cfg)


def build_vad(cfg: StageConfig) -> VoiceActivityDetector:
    return _build(VAD_ENGINES, "vad", cfg)


def build_stt(cfg: StageConfig) -> SpeechToText:
    return _build(STT_ENGINES, "stt", cfg)


def build_tts(cfg: StageConfig) -> TextToSpeech:
    return _build(TTS_ENGINES, "tts", cfg)


def build_agent(cfg: StageConfig, *, tools: ToolRegistry | None = None, memory: Memory | None = None) -> Agent:
    extra: dict[str, Any] = {}
    module, attr = _lookup(LLM_ENGINES, "llm", cfg.engine)
    target = getattr(importlib.import_module(module), attr)
    accepted = _accepted_params(target)
    if tools is not None and "tools" in accepted:
        extra["tools"] = tools
    if memory is not None and "memory" in accepted:
        extra["memory"] = memory
    return _construct(target, "llm", cfg, extra)


def build_tools(config: Config, memory: Memory) -> ToolRegistry:
    """組み込みツールと、設定された MCP サーバのツールをまとめる。"""
    registry = ToolRegistry()
    if config.tools.builtin:
        registry.extend(builtin_tools(memory))
    if config.tools.mcp_servers:
        from .mcp_bridge import McpToolProvider

        provider = McpToolProvider(config.tools.mcp_servers)
        registry.extend(provider.tools())
        registry.add_closer(provider.close)
    return registry


def build_memory(config: Config) -> Memory:
    return Memory(config.resolve(config.memory.path), config.resolve(config.memory.log_path))


def _lookup(table: dict[str, tuple[str, str]], stage: str, engine: str) -> tuple[str, str]:
    try:
        return table[engine]
    except KeyError:
        raise ConfigError(
            f"{stage}.engine: 未知のエンジン {engine!r}（利用可能: {', '.join(sorted(table))}）"
        ) from None


def _build(table: dict[str, tuple[str, str]], stage: str, cfg: StageConfig):
    module, attr = _lookup(table, stage, cfg.engine)
    target = getattr(importlib.import_module(module), attr)
    return _construct(target, stage, cfg)


def _construct(target, stage: str, cfg: StageConfig, extra: dict[str, Any] | None = None):
    options = dict(ENGINE_DEFAULTS.get((stage, cfg.engine), {}))
    options.update(cfg.options)
    options.update(extra or {})
    accepted = _accepted_params(target)
    unknown = sorted(set(options) - accepted)
    if unknown:
        raise ConfigError(
            f"{stage}.options: {cfg.engine} が受け付けない項目 {unknown}"
            f"（指定できるのは: {', '.join(sorted(accepted))}）"
        )
    return target(**options)


def _accepted_params(target) -> set[str]:
    signature = inspect.signature(target)
    return {
        name
        for name, param in signature.parameters.items()
        if param.kind in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY)
    }
