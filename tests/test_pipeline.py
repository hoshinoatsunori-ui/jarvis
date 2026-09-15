"""ウェイクワード検出→録音→STT→応答 の流れを、偽のマイクで通す。"""

import array

import pytest

from jarvis.audio import Audio
from jarvis.interfaces import Agent, SpeechToText, TextToSpeech, VoiceActivityDetector, WakeWordEngine
from jarvis.pipeline import Conversation, ListenSettings, VoiceLoop

FRAME_MS = 30
SAMPLE_RATE = 16000
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000


def silence(n=1):
    return [b"\x00\x00" * FRAME_SAMPLES] * n


def speech(n=1):
    return [array.array("h", [8000] * FRAME_SAMPLES).tobytes()] * n


class TriggerOnce(WakeWordEngine):
    frame_samples = FRAME_SAMPLES

    def __init__(self, at=0):
        self.at = at
        self.seen = 0

    def accept(self, frame):
        self.seen += 1
        return self.seen == self.at + 1


class LoudIsSpeech(VoiceActivityDetector):
    def is_speech(self, frame, sample_rate):
        from jarvis.audio import rms

        return rms(frame) > 0.05


class RecordingStt(SpeechToText):
    def __init__(self, text="こんにちは"):
        self.text = text
        self.received: list[Audio] = []

    def transcribe(self, audio):
        self.received.append(audio)
        return self.text


class CannedAgent(Agent):
    def __init__(self, reply="はい、聞こえています。"):
        self.reply = reply
        self.prompts: list[str] = []

    def respond(self, user_text):
        self.prompts.append(user_text)
        yield self.reply


class CollectingTts(TextToSpeech):
    def __init__(self):
        self.spoken: list[str] = []

    def synthesize(self, text):
        return b""

    def speak(self, text):
        self.spoken.append(text)


def build_loop(wake, stt, agent, tts, settings=None):
    return VoiceLoop(
        wake=wake,
        vad=LoudIsSpeech(),
        stt=stt,
        conversation=Conversation(agent, tts),
        sample_rate=SAMPLE_RATE,
        frame_ms=FRAME_MS,
        settings=settings or ListenSettings(silence_ms=90, start_timeout_s=0.3, max_utterance_s=2.0),
    )


def test_wake_then_speech_reaches_the_agent_and_the_speaker():
    stt, agent, tts = RecordingStt(), CannedAgent(), CollectingTts()
    loop = build_loop(TriggerOnce(at=0), stt, agent, tts)
    loop.run_on_frames(iter(silence(1) + speech(10) + silence(5)))

    assert agent.prompts == ["こんにちは"]
    assert tts.spoken == ["はい、聞こえています。"]


def test_recorded_audio_includes_preroll_so_the_first_syllable_is_kept():
    stt, agent, tts = RecordingStt(), CannedAgent(), CollectingTts()
    loop = build_loop(TriggerOnce(at=0), stt, agent, tts)
    loop.run_on_frames(iter(silence(1) + speech(10) + silence(5)))

    recorded = stt.received[0]
    # 発話10フレームぶんより長い = 直前のフレームも含まれている。
    assert recorded.duration_s > 10 * FRAME_MS / 1000


def test_silence_after_wake_returns_to_standby_without_calling_the_agent():
    stt, agent, tts = RecordingStt(), CannedAgent(), CollectingTts()
    loop = build_loop(TriggerOnce(at=0), stt, agent, tts)
    loop.run_on_frames(iter(silence(40)))

    assert stt.received == []
    assert agent.prompts == []


def test_empty_transcript_does_not_reach_the_agent():
    stt, agent, tts = RecordingStt(text="  "), CannedAgent(), CollectingTts()
    loop = build_loop(TriggerOnce(at=0), stt, agent, tts)
    loop.run_on_frames(iter(silence(1) + speech(10) + silence(5)))

    assert agent.prompts == []


def test_utterance_is_cut_off_at_the_maximum_length():
    stt, agent, tts = RecordingStt(), CannedAgent(), CollectingTts()
    settings = ListenSettings(silence_ms=90, start_timeout_s=0.3, max_utterance_s=0.3)
    loop = build_loop(TriggerOnce(at=0), stt, agent, tts, settings)
    loop.run_on_frames(iter(silence(1) + speech(100)))

    assert stt.received[0].duration_s == pytest.approx(0.3, abs=FRAME_MS / 1000)


def test_conversation_speaks_each_sentence_as_it_arrives():
    class ChunkedAgent(Agent):
        def respond(self, user_text):
            yield from ["一つ目の文です。", "二つ目の", "文です。"]

    tts = CollectingTts()
    reply = Conversation(ChunkedAgent(), tts).handle("どうぞ")

    assert tts.spoken == ["一つ目の文です。", "二つ目の文です。"]
    assert reply == "一つ目の文です。二つ目の文です。"


def test_conversation_logs_both_sides(tmp_path):
    import json

    from jarvis.memory import Memory

    memory = Memory(tmp_path / "m.json", tmp_path / "log.jsonl")
    Conversation(CannedAgent(), CollectingTts(), memory=memory).handle("やあ")

    roles = [json.loads(line)["role"] for line in (tmp_path / "log.jsonl").read_text().splitlines()]
    assert roles == ["user", "assistant"]
