"""ウェイクワード検出。常時オンで回る唯一の段。

ライセンス注意（記事の指摘どおり）:
- openWakeWord のコードは Apache 2.0 だが、**同梱の学習済みモデルは CC BY-NC-SA 4.0（非商用）**。
- Porcupine の Personal プランは **非商用かつ x86_64 限定**。ARM(Raspberry Pi) でのカスタム語や
  商用転用では有償プラン、または microWakeWord(Apache-2.0) を検討する。
家庭内の非商用利用ならどちらもそのまま使える。
"""

from __future__ import annotations

import logging

from .errors import EngineUnavailable
from .interfaces import WakeWordEngine

log = logging.getLogger("jarvis.wake")


class ManualWake(WakeWordEngine):
    """Enter キーを押したら起動する押しボタン方式。

    マイクのウェイクワードを詰める前に、STT→LLM→TTS の流れだけ先に動かしたいとき用。
    フレームは読み捨てるので、マイクが無い環境でも `listen --wake manual` で試せる。
    """

    def __init__(self, prompt: str = "[Enter で話しかける] ") -> None:
        self.prompt = prompt
        self._armed = True

    def accept(self, frame: bytes) -> bool:
        del frame
        if not self._armed:
            return False
        self._armed = False
        try:
            input(self.prompt)
        except EOFError:
            raise KeyboardInterrupt from None
        return True

    def reset(self) -> None:
        self._armed = True


class OpenWakeWord(WakeWordEngine):
    """openWakeWord。1280サンプル(80ms)@16kHz 単位で食わせる。

    配布モデルは alexa / hey_mycroft / hey_jarvis / hey_rhasspy / timer / weather。
    自分で学習したモデルのパスを渡すこともできる。

    推論は既定で onnx。tflite のほうが軽いが、tflite-runtime は Windows 向けの
    wheel が無いことがあるため、環境を選ばない onnx を既定にしてある。
    """

    frame_samples = 1280

    def __init__(
        self,
        model: str = "hey_jarvis",
        threshold: float = 0.5,
        inference_framework: str = "onnx",
        auto_download: bool = True,
    ) -> None:
        self.model_name = model
        self.threshold = threshold
        self.inference_framework = inference_framework
        self.auto_download = auto_download
        self._model = None
        # ここでは依存の有無だけ見る。モデルの取得は warm_up まで遅らせる。
        _require_openwakeword()

    def load(self):
        """モデルを読み込む。未取得なら、ここで初めて取得する。"""
        if self._model is None:
            self._model = _build_model(self.model_name, self.inference_framework, self.auto_download)
        return self._model

    def warm_up(self) -> None:
        self.load()

    def check(self) -> str:
        """取得済みかどうかを、ダウンロードせずに報告する。"""
        path = _model_path(self.model_name, self.inference_framework)
        if path is None:
            return f"独自モデル {self.model_name}"
        if path.exists():
            return f"{self.model_name} 取得済み"
        return f"{self.model_name} 未取得。待受の開始時に取得する（数 MB）"

    def accept(self, frame: bytes) -> bool:
        import numpy as np

        scores = self.load().predict(np.frombuffer(frame, dtype=np.int16))
        self.last_score = max(scores.values()) if scores else None
        return self.last_score is not None and self.last_score >= self.threshold

    def reset(self) -> None:
        if self._model is not None:
            self._model.reset()


class Porcupine(WakeWordEngine):
    """Picovoice Porcupine。frame_length はライブラリ側が決める（通常 512）。"""

    def __init__(
        self,
        access_key: str,
        keyword: str = "jarvis",
        keyword_path: str | None = None,
        sensitivity: float = 0.5,
    ) -> None:
        try:
            import pvporcupine  # noqa: PLC0415
        except ImportError as exc:
            raise EngineUnavailable(
                "wake:porcupine",
                "pvporcupine が入っていない",
                'pip install "jarvis[wake-porcupine]"',
            ) from exc
        kwargs = {"access_key": access_key, "sensitivities": [sensitivity]}
        if keyword_path:
            kwargs["keyword_paths"] = [keyword_path]
        else:
            kwargs["keywords"] = [keyword]
        try:
            self._porcupine = pvporcupine.create(**kwargs)
        except Exception as exc:
            raise EngineUnavailable(
                "wake:porcupine",
                f"Porcupine を初期化できない ({exc})",
                "AccessKey を Picovoice Console で発行し wake.options.access_key に設定する",
            ) from exc
        self.frame_samples = self._porcupine.frame_length
        self.sample_rate = self._porcupine.sample_rate

    def accept(self, frame: bytes) -> bool:
        import struct

        pcm = struct.unpack_from(f"{self.frame_samples}h", frame)
        detected = self._porcupine.process(pcm) >= 0
        # Porcupine はキーワードの番号しか返さないので、連続値は作れない。
        self.last_score = 1.0 if detected else 0.0
        return detected


def _require_openwakeword():
    try:
        import openwakeword  # noqa: PLC0415
    except ImportError as exc:
        raise EngineUnavailable(
            "wake:openwakeword",
            "openwakeword が入っていない",
            'pip install "jarvis[wake-oww]"',
        ) from exc
    return openwakeword


def _model_path(model: str, inference_framework: str):
    """配布モデルの保存先を返す。独自モデルを指定している場合は None。"""
    from pathlib import Path  # noqa: PLC0415

    openwakeword = _require_openwakeword()
    entry = openwakeword.MODELS.get(model)
    if entry is None:
        return None
    path = Path(entry["model_path"])
    suffix = ".onnx" if inference_framework == "onnx" else ".tflite"
    return path.with_suffix(suffix)


def _build_model(model: str, inference_framework: str, auto_download: bool):
    openwakeword = _require_openwakeword()
    from openwakeword.model import Model  # noqa: PLC0415

    path = _model_path(model, inference_framework)
    if path is None and model not in openwakeword.MODELS:
        from pathlib import Path  # noqa: PLC0415

        if not Path(model).exists():
            raise EngineUnavailable(
                "wake:openwakeword",
                f"モデル {model!r} が見つからない",
                f"配布モデルは {', '.join(openwakeword.MODELS)}。"
                "独自モデルを使うならファイルのパスを指定する",
            )
    if path is not None and not path.exists():
        if not auto_download:
            raise EngineUnavailable(
                "wake:openwakeword",
                f"モデル {model!r} が未取得",
                'python -c "import openwakeword; openwakeword.utils.download_models()"',
            )
        _download(model)

    try:
        return Model(wakeword_models=[model], inference_framework=inference_framework)
    except Exception as exc:
        raise EngineUnavailable(
            "wake:openwakeword",
            f"モデル {model!r} を読み込めない ({exc})",
            "wake.options.inference_framework を onnx と tflite で切り替えて試す",
        ) from exc


def _download(model: str) -> None:
    """配布モデルを取得する。数 MB なので待受の開始時でも許容できる。"""
    import openwakeword.utils  # noqa: PLC0415

    log.info("ウェイクワードのモデル %s を取得しています…", model)
    try:
        openwakeword.utils.download_models(model_names=[model])
    except Exception as exc:
        raise EngineUnavailable(
            "wake:openwakeword",
            f"モデル {model!r} を取得できない ({exc})",
            "ネットワークを確認する。手動なら "
            'python -c "import openwakeword; openwakeword.utils.download_models()"',
        ) from exc
