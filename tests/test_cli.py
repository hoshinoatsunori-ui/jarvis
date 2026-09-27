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


def test_stdio_is_left_alone_off_windows(monkeypatch):
    from jarvis import cli

    calls = []

    class Stream:
        def reconfigure(self, **kw):
            calls.append(kw)

    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cli.sys, "stdout", Stream())
    cli._force_utf8_stdio()
    assert calls == []


def test_stdio_is_switched_to_utf8_on_windows(monkeypatch):
    # Windows のコンソール既定は cp932。日本語がそのままだと化ける。
    from jarvis import cli

    calls = []

    class Stream:
        def reconfigure(self, **kw):
            calls.append(kw)

    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    for name in ("stdout", "stderr", "stdin"):
        monkeypatch.setattr(cli.sys, name, Stream())
    cli._force_utf8_stdio()
    assert calls == [{"encoding": "utf-8", "errors": "replace"}] * 3


def test_stdio_reconfigure_failure_does_not_stop_startup(monkeypatch):
    from jarvis import cli

    class Stream:
        def reconfigure(self, **kw):
            raise OSError("リダイレクト先が再設定に対応していない")

    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    for name in ("stdout", "stderr", "stdin"):
        monkeypatch.setattr(cli.sys, name, Stream())
    cli._force_utf8_stdio()  # 例外が漏れないこと


# --- 全体オプションはサブコマンドの前後どちらでも通ること -------------------

def test_stage_override_works_after_the_subcommand(workdir, capsys):
    # argparse の既定では全体オプションはサブコマンドより前だけ。利用者は
    # 後ろに書きがちなので、両方の位置を受けられること。
    assert main(["say", "こんばんは", "--tts", "none", "--llm", "echo"]) == 0
    assert "こんばんは" in capsys.readouterr().out


def test_stage_override_works_before_the_subcommand(workdir, capsys):
    assert main(["--tts", "none", "--llm", "echo", "say", "こんばんは"]) == 0
    assert "こんばんは" in capsys.readouterr().out


def test_options_can_be_split_across_both_positions(workdir, capsys):
    assert main(["--llm", "echo", "say", "こんばんは", "--tts", "none"]) == 0
    assert "こんばんは" in capsys.readouterr().out


def test_option_after_the_subcommand_wins(workdir, capsys):
    (workdir / "config.yaml").write_text("llm: echo\n", encoding="utf-8")
    assert main(["--tts", "system", "say", "テスト", "--tts", "none"]) == 0
    assert "テスト" in capsys.readouterr().out


def test_config_path_is_accepted_after_the_subcommand(workdir, capsys):
    path = workdir / "elsewhere.yaml"
    path.write_text(
        "wake: manual\nvad: energy\nstt: none\nllm: echo\n"
        "tts:\n  engine: none\n  options:\n    prefix: '>> '\n",
        encoding="utf-8",
    )
    assert main(["say", "設定から", "-c", str(path)]) == 0
    assert ">> 設定から" in capsys.readouterr().out


def test_verbose_is_accepted_after_the_subcommand(workdir):
    assert main([*OFFLINE, "tools", "-v"]) == 0


def test_doctor_reports_a_bad_api_key_instead_of_crashing(workdir, capsys, monkeypatch):
    """キーが通らないことを doctor の段階で掴めること。"""
    from jarvis import factory
    from jarvis.errors import EngineUnavailable
    from jarvis.interfaces import Agent

    class BadKeyAgent(Agent):
        def respond(self, user_text):
            yield ""

        def check(self):
            raise EngineUnavailable("llm:anthropic", "APIキーが受け付けられなかった", "キーを確認する")

    monkeypatch.setattr(factory, "build_agent", lambda *a, **k: BadKeyAgent())
    assert main([*OFFLINE, "doctor"]) == 1
    out = capsys.readouterr().out
    assert "NG" in out and "APIキー" in out


def test_doctor_offline_skips_the_api_check(workdir, capsys, monkeypatch):
    from jarvis import factory
    from jarvis.errors import EngineUnavailable
    from jarvis.interfaces import Agent

    class BadKeyAgent(Agent):
        def respond(self, user_text):
            yield ""

        def check(self):
            raise EngineUnavailable("llm:anthropic", "呼ばれてはいけない", "")

    monkeypatch.setattr(factory, "build_agent", lambda *a, **k: BadKeyAgent())
    assert main([*OFFLINE, "doctor", "--offline"]) == 0


def test_env_file_is_loaded_before_the_config_is_read(workdir, capsys, monkeypatch):
    """設定の ${VAR} を .env の値で解決できること。"""
    monkeypatch.delenv("JARVIS_CLI_PREFIX", raising=False)
    # 末尾の空白は引用符で囲まないと落ちる（.env の一般的な挙動）。
    (workdir / ".env").write_text('JARVIS_CLI_PREFIX=">>> "\n', encoding="utf-8")
    (workdir / "config.yaml").write_text(
        "wake: manual\nvad: energy\nstt: none\nllm: echo\n"
        "tts:\n  engine: none\n  options:\n    prefix: ${JARVIS_CLI_PREFIX}\n",
        encoding="utf-8",
    )
    assert main(["say", "環境変数から"]) == 0
    assert ">>> 環境変数から" in capsys.readouterr().out
