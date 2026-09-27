"""Jarvis: 常時待受型の音声AIアシスタント。

パイプライン: マイク → ウェイクワード → VAD → STT → LLM(tool use) → TTS → スピーカー
各段は `jarvis.factory` 経由で差し替えられる。
"""

__version__ = "0.1.0"
