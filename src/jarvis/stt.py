"""STT（音声→テキスト）。

日本語の既定は kotoba-whisper-v2.0 の faster-whisper 変換版。
記事の比較表どおり、large-v3 より CER が良く（ReazonSpeech 内部テストで 11.6% 対 14.9%）
かつ約6.3倍速いという公式モデルカードの主張に基づく選定。
日本語の精度は WER ではなく CER で見ること（分かち書きの曖昧さで WER は当てにならない）。
"""

from __future__ import annotations

from .audio import Audio
from .errors import EngineUnavailable
from .interfaces import SpeechToText


class FasterWhisper(SpeechToText):
    """faster-whisper (CTranslate2) によるローカル STT。"""

    def __init__(
        self,
        model: str = "kotoba-tech/kotoba-whisper-v2.0-faster",
        device: str = "auto",
        compute_type: str = "default",
        language: str | None = "ja",
        beam_size: int = 5,
        vad_filter: bool = True,
        min_silence_duration_ms: int = 500,
    ) -> None:
        self.language = language
        self.beam_size = beam_size
        self.vad_filter = vad_filter
        self.min_silence_duration_ms = min_silence_duration_ms
        try:
            from faster_whisper import WhisperModel  # noqa: PLC0415
        except ImportError as exc:
            raise EngineUnavailable(
                "stt:faster_whisper",
                "faster-whisper が入っていない",
                'pip install "jarvis[stt-local]"',
            ) from exc
        try:
            self._model = WhisperModel(model, device=device, compute_type=compute_type)
        except Exception as exc:
            raise EngineUnavailable(
                "stt:faster_whisper",
                f"モデル {model!r} を読み込めない ({exc})",
                "初回はモデルのダウンロードが走る。ネットワークとディスク容量を確認する",
            ) from exc

    def transcribe(self, audio: Audio) -> str:
        segments, _info = self._model.transcribe(
            audio.to_float32(),
            language=self.language,
            beam_size=self.beam_size,
            vad_filter=self.vad_filter,
            vad_parameters={"min_silence_duration_ms": self.min_silence_duration_ms},
        )
        return "".join(segment.text for segment in segments).strip()


class OpenAIWhisper(SpeechToText):
    """OpenAI の音声認識 API。ローカルが重い機種のフォールバック。"""

    def __init__(self, model: str = "whisper-1", language: str | None = "ja", api_key: str | None = None) -> None:
        self.model = model
        self.language = language
        self._client = openai_client(api_key, "stt:openai")

    def transcribe(self, audio: Audio) -> str:
        import io

        buf = io.BytesIO(audio.to_wav_bytes())
        buf.name = "speech.wav"  # SDK はファイル名から形式を判断する
        kwargs = {"model": self.model, "file": buf}
        if self.language:
            kwargs["language"] = self.language
        return self._client.audio.transcriptions.create(**kwargs).text.strip()


class NullStt(SpeechToText):
    """常に空文字を返す。配線の確認とテスト用。"""

    def transcribe(self, audio: Audio) -> str:
        del audio
        return ""


def openai_client(api_key: str | None, engine: str):
    try:
        from openai import OpenAI  # noqa: PLC0415
    except ImportError as exc:
        raise EngineUnavailable(engine, "openai が入っていない", 'pip install "jarvis[openai]"') from exc
    try:
        return OpenAI(api_key=api_key) if api_key else OpenAI()
    except Exception as exc:
        raise EngineUnavailable(
            engine, f"OpenAI クライアントを作れない ({exc})", "OPENAI_API_KEY を設定する"
        ) from exc
