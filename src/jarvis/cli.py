"""コマンドライン。

    jarvis doctor        どの段が動く状態かを一覧する
    jarvis chat          キーボードで対話する（マイク不要。配線とプロンプトの確認向け）
    jarvis listen        常時待受で音声対話する
    jarvis devices       入力デバイスを一覧する
    jarvis level         マイクの入力レベルを測り、VAD のしきい値を提案する
    jarvis say TEXT      TTS だけを試す
    jarvis transcribe F  STT だけを試す
    jarvis tools         LLM に渡されるツールを一覧する
"""

from __future__ import annotations

import argparse
import logging
import platform
import sys
from typing import Any

from . import __version__, factory
from .audio import Audio, Microphone, list_input_devices, rms, suggest_threshold
from .config import Config, StageConfig, load_env_file
from .errors import ConfigError, EngineUnavailable, JarvisError
from .pipeline import Conversation, ListenSettings, VoiceLoop

STAGE_OVERRIDES = ("wake", "vad", "stt", "llm", "tts")


def _force_utf8_stdio() -> None:
    """Windows のコンソール既定は cp932 なので、日本語の出力が化ける。

    VS Code の統合ターミナルも Windows Terminal も UTF-8 で受けるため、
    こちら側を合わせる。errors="replace" にしてあるのは、表示できない文字で
    会話そのものが落ちるのを避けるため。
    """
    if platform.system() != "Windows":
        return
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                # リダイレクト先によっては再設定できない。化けても動作は続ける。
                pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )
    loaded = load_env_file()
    if loaded:
        logging.getLogger("jarvis").debug(".env から読み込んだ: %s", ", ".join(loaded))
    try:
        config = _load_config(args)
    except ConfigError as exc:
        print(f"設定エラー: {exc}", file=sys.stderr)
        return 2
    try:
        return args.handler(args, config)
    except KeyboardInterrupt:
        print("\n終了します。", file=sys.stderr)
        return 130
    except JarvisError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1


def _add_global_options(parser: argparse.ArgumentParser, *, after_command: bool) -> None:
    """全体に効くオプションを足す。

    サブコマンドの前後どちらに書いても通るよう、トップレベルと各サブコマンドの
    両方に同じものを定義する。サブコマンド側の既定を SUPPRESS にしてあるのは、
    指定しなかったときに属性を作らせないため。既定値を持たせると、後から走る
    サブコマンド側のパースが、前に置かれた指定を None で上書きしてしまう。
    """
    suppress = argparse.SUPPRESS if after_command else None
    parser.add_argument("-c", "--config", default=suppress, help="設定ファイル (既定: ./config.yaml)")
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=suppress,
        help="デバッグログを出す",
    )
    for stage in STAGE_OVERRIDES:
        parser.add_argument(
            f"--{stage}",
            metavar="ENGINE",
            default=suppress,
            help=f"{stage} のエンジンを設定より優先して指定する",
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jarvis", description="常時待受型の音声AIアシスタント")
    parser.add_argument("--version", action="version", version=f"jarvis {__version__}")
    _add_global_options(parser, after_command=False)

    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="各段が動く状態かを確認する")
    doctor.add_argument(
        "--offline", action="store_true",
        help="外部への問い合わせを省く（APIキーの有効性は確認されなくなる）",
    )
    doctor.set_defaults(handler=cmd_doctor)

    chat = sub.add_parser("chat", help="キーボードで対話する")
    chat.add_argument("--no-speak", action="store_true", help="読み上げずテキストだけ出す")
    chat.set_defaults(handler=cmd_chat)

    listen = sub.add_parser("listen", help="常時待受で音声対話する")
    listen.add_argument("--silence-ms", type=int, default=800, help="発話終了とみなす無音の長さ")
    listen.add_argument("--max-utterance-s", type=float, default=20.0, help="1発話の上限秒")
    listen.set_defaults(handler=cmd_listen)

    say = sub.add_parser("say", help="TTS だけを試す")
    say.add_argument("text", help="読み上げる文")
    say.set_defaults(handler=cmd_say)

    transcribe = sub.add_parser("transcribe", help="STT だけを試す")
    transcribe.add_argument("path", help="WAV ファイル")
    transcribe.set_defaults(handler=cmd_transcribe)

    devices = sub.add_parser("devices", help="入力デバイスを一覧する")
    devices.set_defaults(handler=cmd_devices)

    level = sub.add_parser("level", help="マイクの入力レベルを測り VAD のしきい値を提案する")
    level.add_argument("--seconds", type=float, default=8.0, help="測定時間")
    level.set_defaults(handler=cmd_level)

    tools = sub.add_parser("tools", help="LLM に渡されるツールを一覧する")
    tools.set_defaults(handler=cmd_tools)

    for subparser in (doctor, chat, listen, say, transcribe, devices, level, tools):
        _add_global_options(subparser, after_command=True)

    return parser


def _load_config(args: argparse.Namespace) -> Config:
    config = Config.load(args.config)
    for stage in STAGE_OVERRIDES:
        engine = getattr(args, stage, None)
        if engine:
            # エンジンを差し替えたら options は前のエンジン向けなので引き継がない。
            setattr(config, stage, StageConfig(engine=engine))
    return config


# -- サブコマンド ------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace, config: Config) -> int:
    print(f"設定: {config.source or '（既定値。config.yaml が見つからない）'}\n")
    memory = factory.build_memory(config)
    checks: list[tuple[str, Any]] = [
        (f"wake   [{config.wake.engine}]", lambda: factory.build_wake(config.wake)),
        (f"vad    [{config.vad.engine}]", lambda: factory.build_vad(config.vad)),
        (f"stt    [{config.stt.engine}]", lambda: factory.build_stt(config.stt)),
        (f"tts    [{config.tts.engine}]", lambda: factory.build_tts(config.tts)),
        ("tools", lambda: factory.build_tools(config, memory)),
    ]
    checks.append(
        (
            f"llm    [{config.llm.engine}]",
            lambda: factory.build_agent(config.llm, tools=factory.ToolRegistry(), memory=memory),
        )
    )
    failures = 0
    for label, build in checks:
        try:
            result = build()
            detail = ""
            if label == "tools":
                detail = f" ({len(result)}個: {', '.join(result.names()) or 'なし'})"
            elif label.startswith("llm") and not args.offline:
                # 組み立てられても、キーが通るとは限らない。ここで実際に問い合わせる。
                confirmed = result.check()
                if confirmed:
                    detail = f" ({confirmed})"
        except (EngineUnavailable, ConfigError) as exc:
            failures += 1
            print(f"  NG  {label}")
            for line in str(exc).splitlines():
                print(f"      {line}")
        else:
            print(f"  OK  {label}{detail}")
    print()
    if failures:
        print(f"{failures} 個の段が未整備。上の対処を済ませるか、--<段> で別エンジンに差し替えて試す。")
        return 1
    print("すべての段が利用可能。`jarvis listen` で待受を開始できる。")
    return 0


def cmd_chat(args: argparse.Namespace, config: Config) -> int:
    memory = factory.build_memory(config)
    tools = factory.build_tools(config, memory)
    agent = factory.build_agent(config.llm, tools=tools, memory=memory)
    tts = factory.build_tts(StageConfig("none")) if args.no_speak else factory.build_tts(config.tts)
    # 文字を出す TTS を使っているときに画面へも書くと、同じ応答が二度並ぶ。
    conversation = Conversation(
        agent, tts, memory=memory, on_reply_chunk=None if tts.displays_text else _print_chunk
    )
    print("テキストで対話します。空行か Ctrl-D で終了。\n")
    while True:
        try:
            user_text = input("> ").strip()
        except EOFError:
            print()
            break
        if not user_text:
            break
        conversation.handle(user_text)
        print()
    return 0


def _print_chunk(sentence: str) -> None:
    print(sentence, end="", flush=True)


def cmd_listen(args: argparse.Namespace, config: Config) -> int:
    memory = factory.build_memory(config)
    tools = factory.build_tools(config, memory)
    tts = factory.build_tts(config.tts)
    loop = VoiceLoop(
        wake=factory.build_wake(config.wake),
        vad=factory.build_vad(config.vad),
        stt=factory.build_stt(config.stt),
        conversation=Conversation(
            factory.build_agent(config.llm, tools=tools, memory=memory),
            tts,
            memory=memory,
            on_reply_chunk=None if tts.displays_text else (lambda s: print(f"< {s}", flush=True)),
        ),
        sample_rate=config.audio.sample_rate,
        frame_ms=config.audio.frame_ms,
        input_device=config.audio.input_device,
        settings=ListenSettings(silence_ms=args.silence_ms, max_utterance_s=args.max_utterance_s),
    )
    print(f"待受を開始しました（wake={config.wake.engine}）。Ctrl-C で終了。", file=sys.stderr)
    loop.run()
    return 0


def cmd_say(args: argparse.Namespace, config: Config) -> int:
    factory.build_tts(config.tts).speak(args.text)
    return 0


def cmd_transcribe(args: argparse.Namespace, config: Config) -> int:
    audio = Audio.from_wav_file(args.path)
    print(f"{audio.duration_s:.1f}秒 / {audio.sample_rate}Hz", file=sys.stderr)
    print(factory.build_stt(config.stt).transcribe(audio))
    return 0


def cmd_devices(args: argparse.Namespace, config: Config) -> int:
    del args, config
    devices = list_input_devices()
    if not devices:
        print("入力デバイスが見つかりません。")
        return 1
    for d in devices:
        mark = "*" if d["is_default"] else " "
        print(f" {mark} [{d['index']}] {d['name']}  {d['channels']}ch  {d['default_samplerate']}Hz")
    print("\n* が既定。別のものを使うなら audio.input_device に番号か名前を書く。")
    return 0


def cmd_level(args: argparse.Namespace, config: Config) -> int:
    """暗騒音と発話の実測値から、エネルギーVAD のしきい値を決める。

    しきい値は部屋とマイクで変わるので、既定値のまま使わずここで測る。
    """
    sample_rate, frame_ms = config.audio.sample_rate, config.audio.frame_ms
    frames_to_read = int(args.seconds * 1000 / frame_ms)
    print(f"{args.seconds:.0f}秒間測ります。前半は黙って、後半はいつもの声で話してください。\n", file=sys.stderr)

    levels: list[float] = []
    with Microphone(sample_rate, frame_ms, config.audio.input_device) as mic:
        for frame in mic.frames():
            level = rms(frame)
            levels.append(level)
            bar = "#" * min(40, int(level * 200))
            print(f"\r{level:.4f} |{bar:<40}|", end="", file=sys.stderr, flush=True)
            if len(levels) >= frames_to_read:
                break
    print("\n", file=sys.stderr)

    threshold = suggest_threshold(levels)
    print(f"暗騒音 (下位20%): {_pct(levels, 0.2):.4f}")
    print(f"発話   (上位5%) : {_pct(levels, 0.95):.4f}")
    print(f"\n提案するしきい値: {threshold}")
    print("config.yaml の vad.options.threshold に設定してください。")
    print("（拾いすぎるなら上げる。声を取りこぼすなら下げる）")
    return 0


def _pct(levels: list[float], fraction: float) -> float:
    from .audio import percentile

    return percentile(levels, fraction)


def cmd_tools(args: argparse.Namespace, config: Config) -> int:
    del args
    registry = factory.build_tools(config, factory.build_memory(config))
    if not len(registry):
        print("ツールはありません。")
        return 0
    for spec in registry.to_anthropic():
        print(f"- {spec['name']}: {spec['description'].splitlines()[0]}")
    return 0
