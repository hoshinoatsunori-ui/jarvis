"""音声データ型とマイク／スピーカーの入出力。

Jarvis 内部の音声は一貫して **16bit 符号付きリトルエンディアン・モノラルPCM** で扱う。
Whisper 系 STT もウェイクワード検出器もこの形式を前提にしているため、
段の間で変換を挟まずに済む。
"""

from __future__ import annotations

import array
import io
import math
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .errors import EngineUnavailable

SAMPLE_WIDTH = 2  # 16bit


@dataclass(frozen=True)
class Audio:
    """16bit モノラルPCM の音声片。"""

    pcm: bytes
    sample_rate: int

    @property
    def duration_s(self) -> float:
        return len(self.pcm) / (self.sample_rate * SAMPLE_WIDTH)

    def to_wav_bytes(self) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(SAMPLE_WIDTH)
            w.setframerate(self.sample_rate)
            w.writeframes(self.pcm)
        return buf.getvalue()

    def to_float32(self):
        """faster-whisper に渡すための float32 numpy 配列 (-1.0..1.0)。"""
        import numpy as np

        return np.frombuffer(self.pcm, dtype=np.int16).astype("float32") / 32768.0

    @classmethod
    def from_wav_bytes(cls, data: bytes) -> "Audio":
        with wave.open(io.BytesIO(data), "rb") as w:
            if w.getsampwidth() != SAMPLE_WIDTH:
                raise ValueError(f"16bit PCM のみ対応 (sampwidth={w.getsampwidth()})")
            frames = w.readframes(w.getnframes())
            if w.getnchannels() == 2:
                frames = _stereo_to_mono(frames)
            return cls(pcm=frames, sample_rate=w.getframerate())

    @classmethod
    def from_wav_file(cls, path: str | Path) -> "Audio":
        return cls.from_wav_bytes(Path(path).read_bytes())

    @classmethod
    def silence(cls, seconds: float, sample_rate: int) -> "Audio":
        return cls(pcm=b"\x00\x00" * int(seconds * sample_rate), sample_rate=sample_rate)


def _stereo_to_mono(pcm: bytes) -> bytes:
    samples = array.array("h")
    samples.frombytes(pcm)
    mono = array.array("h", [(samples[i] + samples[i + 1]) // 2 for i in range(0, len(samples) - 1, 2)])
    return mono.tobytes()


def rms(pcm: bytes) -> float:
    """PCM の実効値を 0.0..1.0 で返す。エネルギーVADとレベル表示で使う。"""
    if not pcm:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % SAMPLE_WIDTH])
    if not samples:
        return 0.0
    total = 0
    for s in samples:
        total += s * s
    return math.sqrt(total / len(samples)) / 32768.0


class Microphone:
    """sounddevice を使ったマイク入力。固定長フレームのイテレータを返す。"""

    def __init__(self, sample_rate: int = 16000, frame_ms: int = 30, device: int | str | None = None) -> None:
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.device = device
        self.frame_samples = int(sample_rate * frame_ms / 1000)
        self._sd = _import_sounddevice()
        self._stream = None

    def __enter__(self) -> "Microphone":
        self._stream = self._sd.RawInputStream(
            samplerate=self.sample_rate,
            blocksize=self.frame_samples,
            device=self.device,
            dtype="int16",
            channels=1,
        )
        self._stream.start()
        return self

    def __exit__(self, *exc) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def frames(self) -> Iterator[bytes]:
        """マイクからフレームを読み続ける。`with` の中でのみ使う。"""
        if self._stream is None:
            raise RuntimeError("Microphone は with 文の中で使うこと")
        while True:
            data, overflowed = self._stream.read(self.frame_samples)
            del overflowed  # オーバーフローは切り捨てて継続する（会話の取りこぼしより継続を優先）
            yield bytes(data)


def _import_sounddevice():
    try:
        import sounddevice  # noqa: PLC0415
    except Exception as exc:  # ImportError だけでなく PortAudio 未導入の OSError も拾う
        raise EngineUnavailable(
            "microphone",
            f"sounddevice を読み込めない ({exc})",
            'pip install "jarvis[audio]"（Linux では別途 `apt install libportaudio2`）',
        ) from exc
    return sounddevice


def play_wav(data: bytes) -> None:
    """WAV バイト列を再生する。sounddevice があればそれを使い、無ければ OS のプレイヤーに委ねる。"""
    try:
        sd = _import_sounddevice()
    except EngineUnavailable:
        _play_wav_external(data)
        return

    import numpy as np

    audio = Audio.from_wav_bytes(data)
    samples = np.frombuffer(audio.pcm, dtype=np.int16)
    sd.play(samples, audio.sample_rate)
    sd.wait()


_EXTERNAL_PLAYERS = (
    ("afplay", []),           # macOS
    ("aplay", ["-q"]),        # Linux (alsa-utils)
    ("ffplay", ["-nodisp", "-autoexit", "-loglevel", "quiet"]),
)


def _play_wav_external(data: bytes) -> None:
    for player, args in _EXTERNAL_PLAYERS:
        if shutil.which(player):
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as f:
                f.write(data)
                f.flush()
                subprocess.run([player, *args, f.name], check=False)
            return
    raise EngineUnavailable(
        "speaker",
        "音声を再生する手段が無い",
        'pip install "jarvis[audio]" か、afplay / aplay / ffplay のいずれかを用意する',
    )


def list_input_devices() -> list[dict]:
    """入力に使えるデバイスを列挙する。`jarvis devices` の実体。"""
    sd = _import_sounddevice()
    devices = []
    try:
        default_input = sd.default.device[0]
    except (TypeError, IndexError):
        default_input = None
    for index, info in enumerate(sd.query_devices()):
        if info.get("max_input_channels", 0) < 1:
            continue
        devices.append(
            {
                "index": index,
                "name": info.get("name", "?"),
                "channels": info["max_input_channels"],
                "default_samplerate": int(info.get("default_samplerate", 0)),
                "is_default": index == default_input,
            }
        )
    return devices


def percentile(values: list[float], fraction: float) -> float:
    """ソート済みでない値列の分位点。numpy を持ち込まずに済ませるための最小実装。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def suggest_threshold(levels: list[float]) -> float:
    """実測した実効値の列から、エネルギーVAD のしきい値を提案する。

    静かなときの水準（下位20%）と喋ったときの水準（上位5%）の間を取る。
    発話の立ち上がりを拾いたいので、真ん中ではなく暗騒音寄りの1/4の位置に置く。
    """
    floor = percentile(levels, 0.2)
    peak = percentile(levels, 0.95)
    if peak <= floor:
        # 声が入っていない。暗騒音の3倍を下限として返す。
        return max(round(floor * 3, 4), 0.01)
    return round(floor + (peak - floor) * 0.25, 4)
