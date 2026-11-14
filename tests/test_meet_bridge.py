"""meet_bridge のデバイス解決ロジックだけを見る。実デバイスには触らない。"""

import types

import pytest

from jarvis.errors import EngineUnavailable
from jarvis.meet_bridge import AudioRoute, _pick_device


def _d(name, input_ch=0, output_ch=0):
    """`sd.query_devices` が返す dict の最小再現。"""
    return {"name": name, "max_input_channels": input_ch, "max_output_channels": output_ch}


# -- 候補名マッチング -------------------------------------------------------


def test_pick_device_matches_first_candidate_by_substring():
    devices = [
        _d("Realtek Speakers", output_ch=2),
        _d("CABLE Input (VB-Audio Virtual Cable)", output_ch=8),
        _d("CABLE Input (VB-Audio Point)", output_ch=8),
    ]
    result = _pick_device(
        devices,
        ("CABLE Input (VB-Audio Virtual Cable)", "CABLE Input"),
        want_input=False,
    )
    assert result == (1, "CABLE Input (VB-Audio Virtual Cable)")


def test_pick_device_falls_through_to_next_candidate():
    """候補 1 が無くても、候補 2 に当たれば拾う。"""
    devices = [
        _d("Realtek Speakers", output_ch=2),
        _d("CABLE Input (VB-Audio Point)", output_ch=8),
    ]
    result = _pick_device(
        devices,
        ("CABLE Input (VB-Audio Virtual Cable)", "CABLE Input"),
        want_input=False,
    )
    assert result == (1, "CABLE Input (VB-Audio Point)")


def test_pick_device_rejects_wrong_direction():
    """出力を探しているのに入力しか持たないデバイスは拾わない。"""
    devices = [
        _d("CABLE Input (VB-Audio Virtual Cable)", input_ch=8),  # 入力しかない
    ]
    assert _pick_device(devices, ("CABLE Input",), want_input=False) is None


def test_pick_device_returns_none_when_no_match():
    devices = [_d("Realtek Speakers", output_ch=2)]
    assert _pick_device(devices, ("CABLE Input",), want_input=False) is None


# -- resolve_route (下り = loopback / 上り = 仮想ケーブル) --------------------


class _FakePyAudio:
    """pyaudiowpatch の最小モック。既定 loopback デバイス情報だけ返す。"""

    def __init__(self, loopback_info: dict | Exception):
        self._loopback = loopback_info

    def get_default_wasapi_loopback(self):
        if isinstance(self._loopback, Exception):
            raise self._loopback
        return self._loopback

    def terminate(self):
        pass


def _patch_backends(monkeypatch, *, sd_devices, loopback_info):
    import jarvis.meet_bridge as mb

    # type(...) で作ったクラスの属性に関数を置くと method 化されて self が入る。
    # SimpleNamespace ならただの属性アクセスなので余計な引数が付かない。
    monkeypatch.setattr(
        mb, "_import_sounddevice",
        lambda: types.SimpleNamespace(query_devices=lambda: sd_devices),
    )
    monkeypatch.setattr(
        mb, "_import_pyaudiowpatch",
        lambda: types.SimpleNamespace(PyAudio=lambda: _FakePyAudio(loopback_info)),
    )


def test_resolve_route_returns_loopback_and_cable(monkeypatch):
    import jarvis.meet_bridge as mb

    _patch_backends(
        monkeypatch,
        sd_devices=[
            _d("Realtek Mic", input_ch=2),
            _d("CABLE Input (VB-Audio Virtual Cable)", output_ch=8),
        ],
        loopback_info={"index": 32, "name": "Realtek Speakers [Loopback]"},
    )
    route = mb.resolve_route()
    assert route.downlink_input == 32
    assert "Realtek Speakers" in route.downlink_name
    assert route.uplink_output == 1
    assert "VB-Audio Virtual Cable" in route.uplink_name


def test_resolve_route_errors_when_loopback_unavailable(monkeypatch):
    import jarvis.meet_bridge as mb

    _patch_backends(
        monkeypatch,
        sd_devices=[_d("CABLE Input (VB-Audio Virtual Cable)", output_ch=8)],
        loopback_info=OSError("no default device"),
    )
    with pytest.raises(EngineUnavailable) as excinfo:
        mb.resolve_route()
    assert "WASAPI" in str(excinfo.value) or "loopback" in str(excinfo.value).lower()


def test_resolve_route_errors_when_cable_missing(monkeypatch):
    import jarvis.meet_bridge as mb

    _patch_backends(
        monkeypatch,
        sd_devices=[_d("Realtek Mic", input_ch=2)],  # 出力候補なし
        loopback_info={"index": 10, "name": "Speakers [Loopback]"},
    )
    with pytest.raises(EngineUnavailable) as excinfo:
        mb.resolve_route()
    assert "上り" in str(excinfo.value) or "CABLE" in str(excinfo.value)


# -- Chrome 側の設定案内 ----------------------------------------------------


def test_chrome_speaker_hint_is_the_loopback_source_itself():
    """loopback 方式では Chrome のスピーカーは『そのまま使う』のが答え。"""
    route = AudioRoute(
        downlink_input=32,
        uplink_output=19,
        downlink_name="Realtek Speakers [Loopback]",
        uplink_name="CABLE Input (VB-Audio Virtual Cable)",
    )
    assert "Realtek" in route.chrome_speaker_hint()
    assert route.chrome_mic_hint() == "CABLE Output (VB-Audio Virtual Cable)"
