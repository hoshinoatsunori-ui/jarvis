"""ウェイクワード検出まわり。

openwakeword を入れずに済むよう、ライブラリは差し替えて「いつ何を呼ぶか」を見る。
実際に声で起きるかは実機でしか確かめられない。
"""

import sys
import types

import pytest

from jarvis.errors import EngineUnavailable
from jarvis.interfaces import WakeWordEngine
from jarvis.pipeline import WakeProbe, probe_wake_word


@pytest.fixture
def fake_oww(monkeypatch, tmp_path):
    """openwakeword を差し替え、読み込みと取得の回数を記録する。"""
    state = {"built": [], "downloaded": [], "scores": [0.0]}
    model_file = tmp_path / "hey_jarvis_v0.1.tflite"

    class Model:
        def __init__(self, wakeword_models=None, inference_framework=None):
            state["built"].append((tuple(wakeword_models or ()), inference_framework))

        def predict(self, samples):
            return {"hey_jarvis": state["scores"].pop(0) if state["scores"] else 0.0}

        def reset(self):
            state.setdefault("resets", 0)
            state["resets"] += 1

    pkg = types.ModuleType("openwakeword")
    pkg.MODELS = {"hey_jarvis": {"model_path": str(model_file)}}
    model_mod = types.ModuleType("openwakeword.model")
    model_mod.Model = Model
    utils = types.ModuleType("openwakeword.utils")

    def download_models(model_names=None):
        state["downloaded"].append(tuple(model_names or ()))
        model_file.with_suffix(".onnx").write_bytes(b"fake")

    utils.download_models = download_models
    pkg.model = model_mod
    pkg.utils = utils
    for name, mod in [("openwakeword", pkg), ("openwakeword.model", model_mod),
                      ("openwakeword.utils", utils)]:
        monkeypatch.setitem(sys.modules, name, mod)
    state["path"] = model_file.with_suffix(".onnx")
    return state


def test_construction_does_not_load_or_download(fake_oww):
    from jarvis.wake import OpenWakeWord

    OpenWakeWord()
    assert fake_oww["built"] == []
    assert fake_oww["downloaded"] == []


def test_check_reports_a_missing_model_without_fetching_it(fake_oww):
    from jarvis.wake import OpenWakeWord

    assert "未取得" in OpenWakeWord().check()
    assert fake_oww["downloaded"] == []


def test_warm_up_downloads_then_loads(fake_oww):
    from jarvis.wake import OpenWakeWord

    OpenWakeWord().warm_up()
    assert fake_oww["downloaded"] == [("hey_jarvis",)]
    assert fake_oww["built"] == [(("hey_jarvis",), "onnx")]


def test_an_already_present_model_is_not_downloaded_again(fake_oww):
    from jarvis.wake import OpenWakeWord

    fake_oww["path"].write_bytes(b"fake")
    OpenWakeWord().warm_up()
    assert fake_oww["downloaded"] == []
    assert "取得済み" in OpenWakeWord().check()


def test_auto_download_can_be_switched_off(fake_oww):
    from jarvis.wake import OpenWakeWord

    with pytest.raises(EngineUnavailable, match="download_models"):
        OpenWakeWord(auto_download=False).warm_up()


def test_unknown_model_name_lists_the_available_ones(fake_oww):
    from jarvis.wake import OpenWakeWord

    with pytest.raises(EngineUnavailable, match="hey_jarvis"):
        OpenWakeWord(model="hey_siri").warm_up()


def test_accept_records_the_confidence(fake_oww):
    from jarvis.wake import OpenWakeWord

    fake_oww["scores"] = [0.12, 0.84]
    wake = OpenWakeWord(threshold=0.5)
    frame = b"\x00\x00" * wake.frame_samples

    assert wake.accept(frame) is False
    assert wake.last_score == pytest.approx(0.12)
    assert wake.accept(frame) is True
    assert wake.last_score == pytest.approx(0.84)


def test_missing_library_is_reported_at_construction(monkeypatch):
    from jarvis.wake import OpenWakeWord

    monkeypatch.setitem(sys.modules, "openwakeword", None)
    with pytest.raises(EngineUnavailable, match="wake-oww"):
        OpenWakeWord()


# --- しきい値の実測 ----------------------------------------------------------

class ScriptedWake(WakeWordEngine):
    """決めた並びの確信度を返す検出器。"""

    frame_samples = 8

    def __init__(self, scores, threshold=0.5):
        self.scores = list(scores)
        self.threshold = threshold
        self.resets = 0

    def accept(self, frame):
        self.last_score = self.scores.pop(0) if self.scores else 0.0
        return self.last_score >= self.threshold

    def reset(self):
        self.resets += 1


def frames(n):
    return [b"\x00" * 16] * n


def test_probe_counts_detections_and_tracks_the_peak():
    wake = ScriptedWake([0.1, 0.9, 0.2, 0.7])
    result = probe_wake_word(wake, frames(4))

    assert result == WakeProbe(frames=4, detections=2, max_score=pytest.approx(0.9))


def test_probe_resets_the_engine_after_each_detection():
    # 検出のたびに戻さないと、同じ発話で何度も発火する。
    wake = ScriptedWake([0.9, 0.9])
    probe_wake_word(wake, frames(2))
    assert wake.resets == 2


def test_probe_reports_every_score_to_the_display():
    seen = []
    probe_wake_word(ScriptedWake([0.1, 0.6]), frames(2), on_score=lambda s, d: seen.append((s, d)))
    assert seen == [(0.1, 0), (0.6, 1)]


def test_advice_tells_you_to_lower_a_threshold_that_just_misses():
    probe = WakeProbe(frames=100, detections=0, max_score=0.42)
    assert "下げる" in probe.advice(0.5)
    assert "0.37" in probe.advice(0.5)


def test_advice_distinguishes_no_reaction_at_all():
    probe = WakeProbe(frames=100, detections=0, max_score=0.001)
    assert "反応していない" in probe.advice(0.5)


def test_advice_suggests_raising_when_detection_works():
    probe = WakeProbe(frames=100, detections=3, max_score=0.9)
    assert "上げる" in probe.advice(0.5)


def test_advice_handles_engines_without_a_confidence():
    probe = WakeProbe(frames=100, detections=1, max_score=None)
    assert "調整はできない" in probe.advice(0.5)
