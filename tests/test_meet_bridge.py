"""meet_bridge のデバイス解決ロジックだけを見る。実デバイスには触らない。"""

import pytest

from jarvis.errors import EngineUnavailable
from jarvis.meet_bridge import _pick_device


def _d(name, input_ch=0, output_ch=0):
    """`sd.query_devices` が返す dict の最小再現。"""
    return {"name": name, "max_input_channels": input_ch, "max_output_channels": output_ch}


def test_pick_device_matches_first_candidate_by_substring():
    devices = [
        _d("Realtek Microphone", input_ch=2),
        _d("CABLE Output (VB-Audio Virtual Cable)", input_ch=8),
        _d("CABLE Output (VB-Audio Point)", input_ch=8),
    ]
    result = _pick_device(
        devices,
        ("CABLE Output (VB-Audio Virtual Cable)", "CABLE Output"),
        want_input=True,
    )
    assert result == (1, "CABLE Output (VB-Audio Virtual Cable)")


def test_pick_device_falls_through_to_next_candidate():
    """候補 1 が無くても、候補 2 に当たれば拾う。"""
    devices = [
        _d("Realtek Mic", input_ch=2),
        _d("CABLE Output (VB-Audio Point)", input_ch=8),
    ]
    result = _pick_device(
        devices,
        ("CABLE Output (VB-Audio Virtual Cable)", "CABLE Output"),
        want_input=True,
    )
    assert result == (1, "CABLE Output (VB-Audio Point)")


def test_pick_device_rejects_wrong_direction():
    """入力を探しているのに出力しか持たないデバイスは拾わない。"""
    devices = [
        _d("CABLE Output (VB-Audio Virtual Cable)", output_ch=8),  # 出力しかない
    ]
    assert _pick_device(devices, ("CABLE Output",), want_input=True) is None


def test_pick_device_returns_none_when_no_match():
    devices = [_d("Realtek Mic", input_ch=2)]
    assert _pick_device(devices, ("CABLE Output",), want_input=True) is None


def test_resolve_route_errors_when_downlink_missing(monkeypatch):
    import jarvis.meet_bridge as mb

    monkeypatch.setattr(
        mb, "_import_sounddevice",
        lambda: type("sd", (), {"query_devices": staticmethod(lambda: [_d("Speakers", output_ch=2)])})(),
    )
    with pytest.raises(EngineUnavailable) as excinfo:
        mb.resolve_route()
    assert "下り" in str(excinfo.value)


def test_resolve_route_errors_when_uplink_missing(monkeypatch):
    import jarvis.meet_bridge as mb

    devices = [_d("CABLE Output (VB-Audio Virtual Cable)", input_ch=8)]
    monkeypatch.setattr(
        mb, "_import_sounddevice",
        lambda: type("sd", (), {"query_devices": staticmethod(lambda: devices)})(),
    )
    with pytest.raises(EngineUnavailable) as excinfo:
        mb.resolve_route()
    assert "上り" in str(excinfo.value)


def test_resolve_route_returns_two_distinct_cables(monkeypatch):
    import jarvis.meet_bridge as mb

    devices = [
        _d("Realtek Mic", input_ch=2),
        _d("CABLE Output (VB-Audio Virtual Cable)", input_ch=8),
        _d("Speakers (VB-Audio Point)", output_ch=8),
    ]
    monkeypatch.setattr(
        mb, "_import_sounddevice",
        lambda: type("sd", (), {"query_devices": staticmethod(lambda: devices)})(),
    )
    route = mb.resolve_route()
    assert route.downlink_input == 1
    assert route.uplink_output == 2
    assert "VB-Audio Virtual Cable" in route.downlink_name
    assert "VB-Audio Point" in route.uplink_name


def test_chromium_hints_point_to_opposite_end():
    """Chromium 向け案内は、jarvis が使う側の『反対側』を指さなければならない。"""
    from jarvis.meet_bridge import AudioRoute

    route = AudioRoute(
        downlink_input=10,
        uplink_output=31,
        downlink_name="CABLE Output (VB-Audio Virtual Cable)",
        uplink_name="Speakers (VB-Audio Point)",
    )
    assert route.chromium_speaker_hint() == "CABLE Input (VB-Audio Virtual Cable)"
    assert route.chromium_mic_hint() == "CABLE Output (VB-Audio Point)"
