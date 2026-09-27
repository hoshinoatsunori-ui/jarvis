"""SystemTts の OS 分岐。

Windows 実機が無いので、分岐の選択と、SAPI に渡すスクリプトの組み立て、
失敗時の案内までを確かめる。実際の発話は実機でしか確認できない。
"""

from pathlib import Path

import pytest

from jarvis.errors import EngineUnavailable
from jarvis.tts import SystemTts, _sapi_script


@pytest.fixture
def on(monkeypatch):
    """OS と、PATH にあるコマンドを差し替える。"""

    def _setup(system: str, available: set[str]):
        monkeypatch.setattr("jarvis.tts.platform.system", lambda: system)
        monkeypatch.setattr("jarvis.tts.shutil.which", lambda c: c if c in available else None)

    return _setup


def test_windows_uses_powershell_not_say(on):
    on("Windows", {"powershell", "say"})
    assert SystemTts()._command == "powershell"


def test_windows_falls_back_to_pwsh(on):
    on("Windows", {"pwsh"})
    assert SystemTts()._command == "pwsh"


def test_macos_uses_say(on):
    on("Darwin", {"say", "espeak-ng"})
    assert SystemTts()._command == "say"


def test_linux_uses_espeak(on):
    on("Linux", {"espeak-ng"})
    assert SystemTts()._command == "espeak-ng"


def test_windows_without_powershell_points_at_voicevox(on):
    on("Windows", set())
    with pytest.raises(EngineUnavailable, match="voicevox"):
        SystemTts()


def test_other_os_without_any_engine_says_what_to_install(on):
    on("Linux", set())
    with pytest.raises(EngineUnavailable, match="espeak-ng"):
        SystemTts()


def test_sapi_script_pins_16bit_mono():
    # 既定のままだとビット深度が環境で変わり、再生側が受け取れないことがある。
    script = _sapi_script(Path("t.txt"), Path("o.wav"), None)
    assert "AudioBitsPerSample]::Sixteen" in script
    assert "AudioChannel]::Mono" in script


def test_sapi_script_prefers_a_japanese_voice_by_default():
    script = _sapi_script(Path("t.txt"), Path("o.wav"), None)
    assert "'ja-JP'" in script
    assert "SelectVoice" in script


def test_sapi_script_uses_an_explicit_voice_when_given():
    script = _sapi_script(Path("t.txt"), Path("o.wav"), "Microsoft Haruka Desktop")
    assert "$s.SelectVoice('Microsoft Haruka Desktop')" in script
    assert "ja-JP" not in script


def test_sapi_script_reads_the_text_from_a_file():
    # 日本語をコマンドラインに載せるとコードページ次第で壊れる。
    script = _sapi_script(Path("speech-input.txt"), Path("o.wav"), None)
    assert "Get-Content -Raw -Encoding UTF8 'speech-input.txt'" in script


def test_sapi_failure_is_reported_with_what_to_check(on, monkeypatch, tmp_path):
    on("Windows", {"powershell"})
    tts = SystemTts()

    class Result:
        returncode = 1
        stderr = "音声が見つかりません".encode()

    monkeypatch.setattr("jarvis.tts.subprocess.run", lambda *a, **k: Result())
    with pytest.raises(EngineUnavailable, match="日本語音声"):
        tts.synthesize("こんにちは")


def test_sapi_success_returns_the_wav_bytes(on, monkeypatch):
    on("Windows", {"powershell"})
    tts = SystemTts()

    def fake_run(args, **kwargs):
        # -File の次がスクリプト。その隣に置かれた out.wav を書いたことにする。
        script = Path(args[args.index("-File") + 1])
        (script.parent / "speech.wav").write_bytes(b"RIFFFAKE")
        return type("R", (), {"returncode": 0, "stderr": b""})()

    monkeypatch.setattr("jarvis.tts.subprocess.run", fake_run)
    assert tts.synthesize("こんにちは") == b"RIFFFAKE"
