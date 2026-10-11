"""各段のインタフェース。

記事の構成 `マイク → ウェイクワード → VAD → STT → LLM → TTS → スピーカー` の
各段をこの4つの抽象に対応させ、`jarvis.factory` で名前から差し替える。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable, Iterator

from .audio import Audio


class WakeWordEngine(ABC):
    """ウェイクワード検出器。常時オンで動き、検出したら True を返す。"""

    #: 1回の `accept` に渡すフレームのサンプル数。マイク側がこれに合わせる。
    frame_samples: int = 480
    sample_rate: int = 16000

    #: 直近の `accept` での確信度 (0.0..1.0)。連続値を出せない実装は None のまま。
    #: しきい値を実測で決めるために `jarvis wake-test` が読む。
    last_score: float | None = None

    @abstractmethod
    def accept(self, frame: bytes) -> bool:
        """フレームを1つ食わせ、ウェイクワードを検出したら True。"""

    def reset(self) -> None:
        """発話処理が終わった後に内部状態を戻す。"""

    def check(self) -> str | None:
        """使える状態かを確かめ、確認できた内容を一言で返す。

        使えない場合は EngineUnavailable を投げること。`jarvis doctor` が呼ぶ。
        """
        return None

    def warm_up(self) -> None:
        """重い初期化を先に済ませる。待受に入る前に呼ぶ。"""


class VoiceActivityDetector(ABC):
    """VAD。フレーム単位で音声区間かどうかを判定する。"""

    @abstractmethod
    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        ...

    def reset(self) -> None:
        ...


class SpeechToText(ABC):
    """STT。音声を文字にする。"""

    @abstractmethod
    def transcribe(self, audio: Audio) -> str:
        ...

    def check(self) -> str | None:
        """実際に使える状態かを確かめ、確認できた内容を一言で返す。

        使えない場合は EngineUnavailable を投げること。`jarvis doctor` が呼ぶ。
        """
        return None

    def warm_up(self) -> None:
        """重い初期化を先に済ませる。

        待受に入る前に呼ぶ。最初の発話の最中にモデルを読み込むと、
        会話が止まったように見えるため。
        """


class TextToSpeech(ABC):
    """TTS。文を音声にする。

    `speak` は再生まで行う。体感レイテンシ（TTFA）を縮めるため、
    パイプラインは LLM の出力を文単位に切って1文ずつ `speak` を呼ぶ。
    """

    #: 音声の代わりに文字を出す実装は True。呼び出し側が画面出力を二重にしないために使う。
    displays_text: bool = False

    def check(self) -> str | None:
        """実際に使える状態かを確かめ、確認できた内容を一言で返す。

        使えない場合は EngineUnavailable を投げること。`jarvis doctor` が呼ぶ。
        """
        return None

    @abstractmethod
    def synthesize(self, text: str) -> bytes:
        """WAV バイト列を返す。"""

    def speak(self, text: str) -> None:
        from .audio import play_wav

        play_wav(self.synthesize(text))


class Agent(ABC):
    """LLM 応答生成器。ツール実行のループはこの中に閉じる。"""

    @abstractmethod
    def respond(self, user_text: str) -> Iterator[str]:
        """ユーザー発話に対する応答をテキスト片のストリームで返す。"""

    def reset(self) -> None:
        """会話履歴を捨てる。"""

    def check(self) -> str | None:
        """実際に使える状態かを確かめ、確認できた内容を一言で返す。

        使えない場合は EngineUnavailable を投げること。外部に問い合わせない
        実装は None を返せばよい。`jarvis doctor` がこれを呼ぶ。
        """
        return None

    @staticmethod
    def join(chunks: Iterable[str]) -> str:
        return "".join(chunks)
