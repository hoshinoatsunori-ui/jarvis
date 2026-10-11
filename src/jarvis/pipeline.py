"""パイプラインの組み立て。

- `Conversation`: テキスト入力 → LLM → TTS。音声モードとテキストモードで共有する。
- `VoiceLoop`: マイク → ウェイクワード → VAD → STT → `Conversation`。
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Callable, Iterator

from .audio import Audio, Microphone
from .interfaces import Agent, SpeechToText, TextToSpeech, VoiceActivityDetector, WakeWordEngine
from .memory import Memory
from .textutil import iter_sentences

log = logging.getLogger("jarvis")


@dataclass
class ListenSettings:
    """発話の切り出し方。"""

    #: 発話が終わったと判断するまでの無音の長さ。
    silence_ms: int = 800
    #: ウェイクワード後、これだけ待っても声が来なければ諦めて待受に戻る。
    start_timeout_s: float = 6.0
    #: 1発話の上限。これを超えたら打ち切って STT に回す。
    max_utterance_s: float = 20.0
    #: ウェイクワード検出の直前ぶんを取っておく長さ。語頭の欠けを防ぐ。
    preroll_ms: int = 300


class Conversation:
    """1往復の対話。LLM の出力を文単位で TTS に流す。"""

    def __init__(
        self,
        agent: Agent,
        tts: TextToSpeech,
        memory: Memory | None = None,
        on_reply_chunk: Callable[[str], None] | None = None,
    ) -> None:
        self.agent = agent
        self.tts = tts
        self.memory = memory
        self.on_reply_chunk = on_reply_chunk

    def handle(self, user_text: str) -> str:
        """ユーザー発話を処理し、読み上げ終わった応答全文を返す。"""
        if self.memory is not None:
            self.memory.log_turn("user", user_text)
        spoken: list[str] = []
        for sentence in iter_sentences(self.agent.respond(user_text)):
            if self.on_reply_chunk is not None:
                self.on_reply_chunk(sentence)
            self.tts.speak(sentence)
            spoken.append(sentence)
        reply = "".join(spoken)
        if self.memory is not None and reply:
            self.memory.log_turn("assistant", reply)
        return reply


@dataclass
class WakeProbe:
    """ウェイクワードの反応を測った結果。"""

    frames: int
    detections: int
    max_score: float | None

    def advice(self, threshold: float) -> str:
        """しきい値をどう動かすべきかを一言で返す。"""
        if self.max_score is None:
            return "このエンジンは確信度を出さないので、しきい値の調整はできない"
        if self.detections == 0:
            if self.max_score < 0.05:
                return "反応していない。マイクの入力とウェイクワードの発音を確認する"
            return f"惜しい。しきい値を {max(0.05, round(self.max_score - 0.05, 2))} まで下げると拾える"
        return f"検出できている。誤検出が多いなら {min(0.95, round(threshold + 0.1, 2))} まで上げる"


def probe_wake_word(wake, frames, on_score=None) -> WakeProbe:
    """フレーム列を検出器に流し、確信度と検出回数を数える。

    `jarvis wake-test` の中身。マイクの代わりに任意のフレーム列を渡せるよう、
    表示とは分けてある。
    """
    count = detections = 0
    best: float | None = None
    for frame in frames:
        count += 1
        if wake.accept(frame):
            detections += 1
            wake.reset()
        score = wake.last_score
        if score is not None:
            best = score if best is None else max(best, score)
        if on_score is not None:
            on_score(score, detections)
    return WakeProbe(frames=count, detections=detections, max_score=best)


class VoiceLoop:
    """常時待受のループ。"""

    def __init__(
        self,
        wake: WakeWordEngine,
        vad: VoiceActivityDetector,
        stt: SpeechToText,
        conversation: Conversation,
        sample_rate: int = 16000,
        frame_ms: int = 30,
        input_device: int | str | None = None,
        settings: ListenSettings | None = None,
    ) -> None:
        self.wake = wake
        self.vad = vad
        self.stt = stt
        self.conversation = conversation
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.input_device = input_device
        self.settings = settings or ListenSettings()
        self.frame_samples = int(sample_rate * frame_ms / 1000)
        self.frame_bytes = self.frame_samples * 2

    def run(self) -> None:
        with Microphone(self.sample_rate, self.frame_ms, self.input_device) as mic:
            self.run_on_frames(mic.frames())

    def run_on_frames(self, frames: Iterator[bytes]) -> None:
        """フレームのイテレータを回し続ける。テストではマイクの代わりに配列を渡せる。"""
        preroll = deque(maxlen=max(1, self.settings.preroll_ms // self.frame_ms))
        wake_buffer = b""
        wake_bytes = self.wake.frame_samples * 2
        for frame in frames:
            preroll.append(frame)
            wake_buffer += frame
            triggered = False
            while len(wake_buffer) >= wake_bytes:
                chunk, wake_buffer = wake_buffer[:wake_bytes], wake_buffer[wake_bytes:]
                if self.wake.accept(chunk):
                    triggered = True
            if not triggered:
                continue
            log.info("ウェイクワード検出")
            wake_buffer = b""
            audio = self._record(frames, list(preroll))
            preroll.clear()
            self.wake.reset()
            self.vad.reset()
            if audio is None:
                log.info("発話が無かったので待受に戻る")
                continue
            self._handle(audio)

    def _record(self, frames: Iterator[bytes], preroll: list[bytes]) -> Audio | None:
        """声が始まるのを待ち、無音が続いたら切る。"""
        collected: list[bytes] = list(preroll)
        silence_frames_needed = max(1, self.settings.silence_ms // self.frame_ms)
        start_limit = int(self.settings.start_timeout_s * 1000 / self.frame_ms)
        max_frames = int(self.settings.max_utterance_s * 1000 / self.frame_ms)

        started = False
        silence_run = 0
        for index, frame in enumerate(frames):
            speech = self.vad.is_speech(frame, self.sample_rate)
            if not started:
                if speech:
                    started = True
                    collected.append(frame)
                elif index >= start_limit:
                    return None
                else:
                    collected.append(frame)
                    del collected[: max(0, len(collected) - len(preroll) - 1)]
                continue
            collected.append(frame)
            silence_run = 0 if speech else silence_run + 1
            if silence_run >= silence_frames_needed or len(collected) >= max_frames:
                break
        return Audio(pcm=b"".join(collected), sample_rate=self.sample_rate)

    def _handle(self, audio: Audio) -> None:
        text = self.stt.transcribe(audio)
        if not text.strip():
            log.info("聞き取れなかった (%.1f秒)", audio.duration_s)
            return
        log.info("認識: %s", text)
        self.conversation.handle(text)
