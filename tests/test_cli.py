import pytest

from jarvis.cli import main


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


OFFLINE = ["--wake", "manual", "--vad", "energy", "--stt", "none", "--tts", "none", "--llm", "echo"]


def test_doctor_reports_every_stage_ok_when_nothing_is_missing(workdir, capsys):
    assert main([*OFFLINE, "doctor"]) == 0
    out = capsys.readouterr().out
    for stage in ("wake", "vad", "stt", "tts", "llm", "tools"):
        assert stage in out
    assert "NG" not in out


def test_doctor_explains_what_to_install_for_a_missing_backend(workdir, capsys):
    # silero は onnxruntime とモデルファイルが要る。入っていなければ対処が出る。
    code = main([*OFFLINE, "--vad", "silero", "doctor"])
    out = capsys.readouterr().out
    if code == 0:
        pytest.skip("この環境では silero VAD が利用可能")
    assert "NG" in out and "対処" in out


def test_unknown_engine_exits_with_a_config_error(workdir, capsys):
    assert main([*OFFLINE, "--tts", "nope", "doctor"]) == 1
    assert "nope" in capsys.readouterr().out


def test_say_writes_through_the_null_engine(workdir, capsys):
    assert main([*OFFLINE, "say", "こんばんは"]) == 0
    assert "こんばんは" in capsys.readouterr().out


def test_transcribe_reads_a_wav_file(workdir, capsys):
    from jarvis.audio import Audio

    path = workdir / "a.wav"
    path.write_bytes(Audio.silence(0.2, 16000).to_wav_bytes())
    assert main([*OFFLINE, "transcribe", str(path)]) == 0


def test_tools_lists_the_builtins(workdir, capsys):
    assert main([*OFFLINE, "tools"]) == 0
    assert "get_current_time" in capsys.readouterr().out


def test_chat_runs_one_turn_then_stops_on_a_blank_line(workdir, capsys, monkeypatch):
    answers = iter(["やあ", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert main([*OFFLINE, "chat"]) == 0
    assert "やあ" in capsys.readouterr().out


def test_config_file_is_read_from_the_working_directory(workdir, capsys):
    (workdir / "config.yaml").write_text(
        "wake: manual\nvad: energy\nstt: none\nllm: echo\ntts:\n  engine: none\n  options:\n    prefix: '>> '\n",
        encoding="utf-8",
    )
    assert main(["say", "設定から"]) == 0
    assert ">> 設定から" in capsys.readouterr().out


def test_missing_config_file_is_reported(workdir, capsys):
    assert main(["-c", str(workdir / "absent.yaml"), "doctor"]) == 2
    assert "設定エラー" in capsys.readouterr().err


def test_devices_reports_how_to_install_audio_support_when_it_is_missing(workdir, capsys):
    pytest.importorskip  # noqa: B018
    try:
        import sounddevice  # noqa: F401
    except Exception:
        assert main([*OFFLINE, "devices"]) == 1
        assert "sounddevice" in capsys.readouterr().err
    else:
        assert main([*OFFLINE, "devices"]) in (0, 1)


def test_chat_does_not_print_the_reply_twice(workdir, capsys):
    # 文字を出す TTS と画面出力が重なると、同じ応答が二度並ぶ。
    answers = iter(["やあ", ""])
    import builtins

    original = builtins.input
    builtins.input = lambda prompt="": next(answers)
    try:
        assert main([*OFFLINE, "chat"]) == 0
    finally:
        builtins.input = original
    assert capsys.readouterr().out.count("「やあ」と聞こえました。") == 1
