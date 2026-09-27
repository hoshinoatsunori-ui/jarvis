"""VAD（音声区間検出）。発話の終わりを見つけて STT に渡す役。

`energy` は依存ゼロで動く単純なもの。静かな部屋なら実用になる。
`silero` は ML ベースで雑音に強い。騒がしい環境や遠距離集音ではこちらを使う。
"""

from __future__ import annotations

from .audio import rms
from .errors import EngineUnavailable
from .interfaces import VoiceActivityDetector


class EnergyVad(VoiceActivityDetector):
    """実効値がしきい値を超えたら音声とみなす。

    しきい値は環境依存なので、`jarvis level` で実測して決めること。
    """

    def __init__(self, threshold: float = 0.02) -> None:
        self.threshold = threshold

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        del sample_rate  # エネルギー判定にサンプリングレートは効かない
        return rms(frame) >= self.threshold


class SileroVad(VoiceActivityDetector):
    """Silero VAD (ONNX)。32ms(512サンプル@16kHz) 単位で判定する。"""

    #: Silero v5 の入力は 16kHz なら 512 サンプル固定。
    WINDOW = 512

    def __init__(self, threshold: float = 0.5, model_path: str | None = None) -> None:
        self.threshold = threshold
        self._model = _load_silero(model_path)
        self._buffer = b""

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        if sample_rate != 16000:
            raise ValueError("silero VAD は 16kHz のみ対応")
        import numpy as np

        self._buffer += frame
        need = self.WINDOW * 2
        speech = False
        while len(self._buffer) >= need:
            window, self._buffer = self._buffer[:need], self._buffer[need:]
            samples = np.frombuffer(window, dtype=np.int16).astype("float32") / 32768.0
            if self._model(samples) >= self.threshold:
                speech = True
        return speech

    def reset(self) -> None:
        self._buffer = b""
        self._model.reset()


class _SileroSession:
    """onnxruntime セッションと LSTM 状態の保持。"""

    def __init__(self, session, sample_rate: int = 16000) -> None:
        import numpy as np

        self._session = session
        self._sr = np.array(sample_rate, dtype="int64")
        self._np = np
        self.reset()

    def reset(self) -> None:
        self._state = self._np.zeros((2, 1, 128), dtype="float32")

    def __call__(self, samples) -> float:
        out, state = self._session.run(
            None,
            {"input": samples.reshape(1, -1), "state": self._state, "sr": self._sr},
        )
        self._state = state
        return float(out[0][0])


def _load_silero(model_path: str | None):
    try:
        import onnxruntime  # noqa: PLC0415
    except ImportError as exc:
        raise EngineUnavailable(
            "vad:silero",
            "onnxruntime が入っていない",
            'pip install "jarvis[vad-silero]"',
        ) from exc
    if not model_path:
        raise EngineUnavailable(
            "vad:silero",
            "silero_vad.onnx のパスが未設定",
            "https://github.com/snakers4/silero-vad の silero_vad.onnx を落として "
            "vad.options.model_path に指定する",
        )
    opts = onnxruntime.SessionOptions()
    opts.inter_op_num_threads = 1
    opts.intra_op_num_threads = 1
    session = onnxruntime.InferenceSession(model_path, sess_options=opts, providers=["CPUExecutionProvider"])
    return _SileroSession(session)
