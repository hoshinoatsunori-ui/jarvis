"""STT の遅延読み込み。

`jarvis doctor` が 1.5GB のモデルを取りに行かないことを担保する。
faster_whisper を偽装して、いつ WhisperModel が作られるかだけを見る。
"""

import sys
import types

import pytest

from jarvis.audio import Audio


@pytest.fixture
def fake_whisper(monkeypatch):
    """faster_whisper を差し替え、モデルが作られた回数を記録する。"""
    built = []

    class WhisperModel:
        def __init__(self, model, device=None, compute_type=None):
            built.append(model)

        def transcribe(self, *a, **k):
            segment = types.SimpleNamespace(text="こんにちは")
            return [segment], None

    module = types.ModuleType("faster_whisper")
    module.WhisperModel = WhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    # float32 への変換には numpy が要る。ここで見たいのは読み込みの時機なので、
    # テストを numpy 非依存に保つために変換だけ差し替える。
    monkeypatch.setattr(Audio, "to_float32", lambda self: "samples")
    return built


def test_construction_does_not_download_the_model(fake_whisper):
    from jarvis.stt import FasterWhisper

    FasterWhisper(model="dummy/model")
    assert fake_whisper == []


def test_model_is_loaded_on_first_transcription(fake_whisper):
    from jarvis.stt import FasterWhisper

    stt = FasterWhisper(model="dummy/model")
    assert stt.transcribe(Audio.silence(0.1, 16000)) == "こんにちは"
    assert fake_whisper == ["dummy/model"]


def test_model_is_loaded_only_once(fake_whisper):
    from jarvis.stt import FasterWhisper

    stt = FasterWhisper(model="dummy/model")
    stt.transcribe(Audio.silence(0.1, 16000))
    stt.transcribe(Audio.silence(0.1, 16000))
    assert fake_whisper == ["dummy/model"]


def test_check_does_not_load_the_model(fake_whisper):
    from jarvis.stt import FasterWhisper

    stt = FasterWhisper(model="dummy/model")
    assert stt.check()
    assert fake_whisper == []


def test_check_reports_a_local_model_path(fake_whisper, tmp_path):
    from jarvis.stt import FasterWhisper

    local = tmp_path / "model-dir"
    local.mkdir()
    assert "ローカル" in FasterWhisper(model=str(local)).check()


def test_missing_dependency_is_reported_at_construction(monkeypatch):
    from jarvis.errors import EngineUnavailable
    from jarvis.stt import FasterWhisper

    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    with pytest.raises(EngineUnavailable, match="stt-local"):
        FasterWhisper()


def test_cache_probe_reports_unknown_without_huggingface_hub(monkeypatch):
    from jarvis.stt import _is_cached

    monkeypatch.setitem(sys.modules, "huggingface_hub", None)
    assert _is_cached("some/model") is None


def test_warm_up_loads_the_model_ahead_of_the_first_utterance(fake_whisper):
    """待受に入る前に読み込む。会話の最中に長い間が入らないように。"""
    from jarvis.stt import FasterWhisper

    stt = FasterWhisper(model="dummy/model")
    stt.warm_up()
    assert fake_whisper == ["dummy/model"]

    stt.transcribe(Audio.silence(0.1, 16000))
    assert fake_whisper == ["dummy/model"]  # 二重に読み込まない


def test_warm_up_is_a_no_op_for_engines_without_heavy_setup():
    from jarvis.stt import NullStt

    NullStt().warm_up()  # 例外が出ないこと


# --- GPU が使えないときの振る舞い -------------------------------------------

@pytest.fixture
def whisper_that_needs_cuda(monkeypatch):
    """encode の時点で CUDA ライブラリが無いと分かる状況を作る。

    実機で起きたのはこの形。読み込みは通り、最初の書き起こしで落ちる。
    """
    built = []

    class WhisperModel:
        def __init__(self, model, device=None, compute_type=None):
            built.append(device)
            self.device = device

        def transcribe(self, *a, **k):
            if self.device != "cpu":
                def failing():
                    raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
                    yield  # pragma: no cover

                return failing(), None
            return iter([types.SimpleNamespace(text="こんにちは")]), None

    module = types.ModuleType("faster_whisper")
    module.WhisperModel = WhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    monkeypatch.setattr(Audio, "to_float32", lambda self: "samples")
    return built


def test_auto_falls_back_to_cpu_when_the_gpu_is_unusable(whisper_that_needs_cuda):
    from jarvis.stt import FasterWhisper

    stt = FasterWhisper(model="dummy/model", device="auto")
    stt.warm_up()

    assert whisper_that_needs_cuda == ["auto", "cpu"]
    assert stt.device == "cpu"
    assert stt.transcribe(Audio.silence(0.1, 16000)) == "こんにちは"


def test_an_explicit_gpu_choice_is_not_silently_overridden(whisper_that_needs_cuda):
    """cuda と書いた人は GPU を使いたい。黙って CPU にすると気づけない。"""
    from jarvis.errors import EngineUnavailable
    from jarvis.stt import FasterWhisper

    stt = FasterWhisper(model="dummy/model", device="cuda")
    with pytest.raises(EngineUnavailable, match="cuBLAS"):
        stt.warm_up()
    assert whisper_that_needs_cuda == ["cuda"]


def test_cuda_failure_during_transcription_names_the_setting_to_change(whisper_that_needs_cuda):
    from jarvis.errors import EngineUnavailable
    from jarvis.stt import FasterWhisper

    stt = FasterWhisper(model="dummy/model", device="cuda")
    with pytest.raises(EngineUnavailable) as exc:
        stt.transcribe(Audio.silence(0.1, 16000))
    assert "stt.options.device を cpu" in str(exc.value)


def test_an_unrelated_failure_still_points_at_the_settings(fake_whisper, monkeypatch):
    from jarvis.errors import EngineUnavailable
    from jarvis.stt import FasterWhisper

    stt = FasterWhisper(model="dummy/model")

    def boom(*a, **k):
        raise RuntimeError("何か別の理由")

    monkeypatch.setattr(stt.load(), "transcribe", boom)
    with pytest.raises(EngineUnavailable, match="compute_type"):
        stt.transcribe(Audio.silence(0.1, 16000))
