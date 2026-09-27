"""STT（音声→テキスト）。

日本語の既定は kotoba-whisper-v2.0 の faster-whisper 変換版。
記事の比較表どおり、large-v3 より CER が良く（ReazonSpeech 内部テストで 11.6% 対 14.9%）
かつ約6.3倍速いという公式モデルカードの主張に基づく選定。
日本語の精度は WER ではなく CER で見ること（分かち書きの曖昧さで WER は当てにならない）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from .audio import Audio
from .errors import EngineUnavailable
from .interfaces import SpeechToText

log = logging.getLogger("jarvis.stt")


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
        self.model = model
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.beam_size = beam_size
        self.vad_filter = vad_filter
        self.min_silence_duration_ms = min_silence_duration_ms
        self._loaded = None
        # ここでは依存の有無だけ見る。モデルの取得は初回の書き起こしまで遅らせる。
        # 構築しただけで 1.5GB 取りに行くと、`jarvis doctor` が診断にならない。
        try:
            import faster_whisper  # noqa: F401, PLC0415
        except ImportError as exc:
            raise EngineUnavailable(
                "stt:faster_whisper",
                "faster-whisper が入っていない",
                'pip install "jarvis[stt-local]"',
            ) from exc

    def load(self):
        """モデルを読み込む。未取得なら、ここで初めてダウンロードが走る。"""
        if self._loaded is None:
            from faster_whisper import WhisperModel  # noqa: PLC0415

            try:
                self._loaded = WhisperModel(
                    self.model, device=self.device, compute_type=self.compute_type
                )
            except Exception as exc:
                raise EngineUnavailable(
                    "stt:faster_whisper",
                    f"モデル {self.model!r} を読み込めない ({exc})",
                    "初回はモデルの取得が走る。ネットワークとディスク容量を確認する",
                ) from exc
        return self._loaded

    def warm_up(self) -> None:
        """待受に入る前に、読み込みから推論まで一度通しておく。

        device: auto は GPU を選ぶことがあるが、CUDA ライブラリが揃って
        いないと**読み込みではなく最初の書き起こしで**落ちる。会話の最中に
        それが起きないよう、ここで一度走らせて確かめる。auto を指定して
        いる場合は、GPU が使えなければ CPU に落とす。
        """
        self.load()
        try:
            self._probe()
        except Exception as exc:
            if not (self.device == "auto" and _is_gpu_problem(exc)):
                raise _describe_failure(exc, self.device) from exc
            log.warning("GPU を使えないため CPU に切り替える (%s)", exc)
            self.device = "cpu"
            self._loaded = None
            self.load()
            try:
                self._probe()
            except Exception as retry:
                raise _describe_failure(retry, self.device) from retry

    def _probe(self) -> None:
        """短い無音を1回通して、推論経路が生きているかを確かめる。

        transcribe は生成器を返すので、回さないと encode まで到達しない。
        VAD を切ってあるのは、無音だと区間なしで encode を素通りするため。
        """
        segments, _info = self.load().transcribe(
            Audio.silence(1.0, 16000).to_float32(),
            language=self.language,
            beam_size=1,
            vad_filter=False,
        )
        for _ in segments:
            break

    def check(self) -> str:
        """取得済みかどうかを、ダウンロードせずに報告する。"""
        if Path(self.model).exists():
            return f"ローカルのモデル {self.model}"
        cached = _is_cached(self.model)
        if cached is True:
            return f"{self.model} 取得済み"
        if cached is False:
            return f"{self.model} 未取得。初回の書き起こしで取得する"
        return f"{self.model}（取得状況は不明）"

    def transcribe(self, audio: Audio) -> str:
        try:
            segments, _info = self.load().transcribe(
                audio.to_float32(),
                language=self.language,
                beam_size=self.beam_size,
                vad_filter=self.vad_filter,
                vad_parameters={"min_silence_duration_ms": self.min_silence_duration_ms},
            )
            # 生成器なので、ここで回して初めて推論が走る。例外もここで出る。
            return "".join(segment.text for segment in segments).strip()
        except Exception as exc:
            raise _describe_failure(exc, self.device) from exc


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


def _is_cached(repo_id: str) -> bool | None:
    """Hugging Face のキャッシュに既にあるか。判定できなければ None。

    ここでダウンロードを起こしてはいけない。取得状況を見るだけ。
    """
    try:
        from huggingface_hub import try_to_load_from_cache  # noqa: PLC0415
    except ImportError:
        return None
    try:
        found = try_to_load_from_cache(repo_id=repo_id, filename="model.bin")
    except Exception:
        return None
    return isinstance(found, str)


#: CUDA まわりが揃っていないときに出てくる語。
_GPU_MARKERS = ("cublas", "cudnn", "cuda", "libcu", "gpu")


def _is_gpu_problem(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _GPU_MARKERS)


def _describe_failure(exc: Exception, device: str) -> EngineUnavailable:
    """書き起こしの失敗に、何をすれば直るかを添える。"""
    if _is_gpu_problem(exc):
        return EngineUnavailable(
            "stt:faster_whisper",
            f"GPU で実行しようとして CUDA ライブラリを読み込めない ({exc})",
            "stt.options.device を cpu にする。GPU を使うなら NVIDIA の "
            "cuBLAS と cuDNN を導入し、DLL が PATH から見えるようにする",
        )
    return EngineUnavailable(
        "stt:faster_whisper",
        f"書き起こしに失敗した ({exc})",
        f"stt.options.device（現在 {device}）と compute_type を見直す。"
        "cpu / int8 の組み合わせがもっとも確実",
    )
