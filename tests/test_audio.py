import array

import pytest

from jarvis.audio import Audio, rms


def test_wav_roundtrip_preserves_samples():
    pcm = array.array("h", range(-1000, 1000)).tobytes()
    original = Audio(pcm=pcm, sample_rate=16000)
    restored = Audio.from_wav_bytes(original.to_wav_bytes())
    assert restored.pcm == pcm
    assert restored.sample_rate == 16000


def test_duration_is_derived_from_sample_rate():
    assert Audio.silence(1.5, 16000).duration_s == pytest.approx(1.5)
    assert Audio.silence(1.5, 48000).duration_s == pytest.approx(1.5)


def test_rms_of_silence_is_zero_and_of_full_scale_is_near_one():
    assert rms(Audio.silence(0.1, 16000).pcm) == 0.0
    loud = array.array("h", [32000] * 480).tobytes()
    assert rms(loud) == pytest.approx(0.9765, abs=0.01)


def test_rms_of_empty_buffer_does_not_divide_by_zero():
    assert rms(b"") == 0.0


def test_stereo_wav_is_downmixed_to_mono():
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(array.array("h", [100, 300] * 10).tobytes())
    audio = Audio.from_wav_bytes(buf.getvalue())
    assert array.array("h", audio.pcm).tolist() == [200] * 10


def test_non_16bit_wav_is_rejected():
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(8000)
        w.writeframes(b"\x00" * 10)
    with pytest.raises(ValueError):
        Audio.from_wav_bytes(buf.getvalue())


def test_percentile_picks_order_statistics():
    from jarvis.audio import percentile

    values = [0.5, 0.1, 0.3, 0.2, 0.4]
    assert percentile(values, 0.0) == 0.1
    assert percentile(values, 1.0) == 0.5
    assert percentile(values, 0.5) == 0.3
    assert percentile([], 0.5) == 0.0


def test_suggested_threshold_sits_between_noise_floor_and_speech():
    from jarvis.audio import suggest_threshold

    levels = [0.002] * 40 + [0.09] * 10
    threshold = suggest_threshold(levels)
    assert 0.002 < threshold < 0.09


def test_suggested_threshold_falls_back_when_no_speech_was_recorded():
    from jarvis.audio import suggest_threshold

    # ずっと無音だったなら、暗騒音のすぐ上ではなく安全側の下限を返す。
    assert suggest_threshold([0.001] * 50) == 0.01
    assert suggest_threshold([]) == 0.01


def test_suggested_threshold_scales_with_a_noisy_room():
    from jarvis.audio import suggest_threshold

    quiet = suggest_threshold([0.002] * 40 + [0.09] * 10)
    noisy = suggest_threshold([0.02] * 40 + [0.09] * 10)
    assert noisy > quiet
