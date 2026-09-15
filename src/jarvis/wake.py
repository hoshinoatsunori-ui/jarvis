"""ウェイクワード検出。常時オンで回る唯一の段。

ライセンス注意（記事の指摘どおり）:
- openWakeWord のコードは Apache 2.0 だが、**同梱の学習済みモデルは CC BY-NC-SA 4.0（非商用）**。
- Porcupine の Personal プランは **非商用かつ x86_64 限定**。ARM(Raspberry Pi) でのカスタム語や
  商用転用では有償プラン、または microWakeWord(Apache-2.0) を検討する。
家庭内の非商用利用ならどちらもそのまま使える。
"""

from __future__ import annotations

from .errors import EngineUnavailable
from .interfaces import WakeWordEngine


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
    """openWakeWord。1280サンプル(80ms)@16kHz 単位で食わせる。"""

    frame_samples = 1280

    def __init__(
        self,
        model: str = "hey_jarvis",
        threshold: float = 0.5,
        inference_framework: str = "onnx",
    ) -> None:
        self.model_name = model
        self.threshold = threshold
        self._model = _load_oww(model, inference_framework)

    def accept(self, frame: bytes) -> bool:
        import numpy as np

        samples = np.frombuffer(frame, dtype=np.int16)
        scores = self._model.predict(samples)
        return any(score >= self.threshold for score in scores.values())

    def reset(self) -> None:
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
        return self._porcupine.process(pcm) >= 0


def _load_oww(model: str, inference_framework: str):
    try:
        from openwakeword.model import Model  # noqa: PLC0415
    except ImportError as exc:
        raise EngineUnavailable(
            "wake:openwakeword",
            "openwakeword が入っていない",
            'pip install "jarvis[wake-oww]" のあと '
            '`python -c "import openwakeword; openwakeword.utils.download_models()"`',
        ) from exc
    try:
        return Model(wakeword_models=[model], inference_framework=inference_framework)
    except Exception as exc:
        raise EngineUnavailable(
            "wake:openwakeword",
            f"モデル {model!r} を読み込めない ({exc})",
            'python -c "import openwakeword; openwakeword.utils.download_models()" で'
            "事前学習モデルを取得する（同梱モデルは非商用ライセンス）",
        ) from exc

