"""Gemini Live API による S2S の最小 PoC。

カスケード (src/jarvis) との比較用。barge-in と1秒未満応答が目的で、
音声は Google にそのまま流れる（カスケードの前提とは排他）。

パイプライン:
    マイク(16kHz PCM) → Gemini Live(サーバ側 VAD / 応答生成 / 音声合成)
    → 応答音声(24kHz PCM) → スピーカー

実行:
    export GEMINI_API_KEY=...
    python -m experiments.s2s.gemini_live
    # Ctrl+C で終了

依存:
    pip install -e ".[s2s-gemini]"
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from typing import Any

try:
    import sounddevice as sd
except ImportError as e:
    sys.stderr.write(
        f"音声ライブラリが入っていない: {e}\n"
        '  pip install -e ".[s2s-gemini]"\n'
    )
    sys.exit(1)

try:
    from google import genai
    from google.genai import types
except ImportError:
    sys.stderr.write(
        'google-genai が入っていない:\n'
        '  pip install -e ".[s2s-gemini]"\n'
    )
    sys.exit(1)


log = logging.getLogger("s2s")

# Gemini Live の仕様で固定されている値
MIC_SAMPLE_RATE = 16000
SPEAKER_SAMPLE_RATE = 24000
CHANNELS = 1
SAMPLE_WIDTH = 2  # int16

# マイクのチャンクサイズ。小さいほど低レイテンシだが CPU 負荷が増える
MIC_CHUNK_MS = 20


async def mic_to_api(
    session: Any,
    stop: asyncio.Event,
    input_device: int | str | None,
) -> None:
    """マイク音声を 20ms ごとに Gemini Live に送り続ける。"""
    chunk_frames = int(MIC_SAMPLE_RATE * MIC_CHUNK_MS / 1000)
    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
    loop = asyncio.get_running_loop()

    def callback(indata, frames, time, status):
        if status:
            log.warning("入力ストリーム: %s", status)
        try:
            loop.call_soon_threadsafe(queue.put_nowait, bytes(indata))
        except asyncio.QueueFull:
            log.warning("マイクのキューが詰まった。古いフレームを捨てる")

    with sd.RawInputStream(
        samplerate=MIC_SAMPLE_RATE,
        blocksize=chunk_frames,
        dtype="int16",
        channels=CHANNELS,
        device=input_device,
        callback=callback,
    ):
        log.info("マイク開始 (%d Hz, %d ms チャンク)", MIC_SAMPLE_RATE, MIC_CHUNK_MS)
        while not stop.is_set():
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue
            await session.send_realtime_input(
                audio=types.Blob(data=chunk, mime_type=f"audio/pcm;rate={MIC_SAMPLE_RATE}")
            )


async def api_to_speaker(
    session: Any,
    stop: asyncio.Event,
    output_device: int | str | None,
) -> None:
    """Gemini Live からの音声/テキストを受け取り、音声は即スピーカーに流す。

    文字起こしは単語ごとに partial で届くので、ターン終端まで溜めて
    1行で吐く。モデルが model_turn.parts.text に英語チェイン思考を
    混ぜてくることがあるが、字幕としての実体は output_transcription
    のほうなので、ここでは part.text は無視する。
    """
    play_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def callback(outdata, frames, time, status):
        if status:
            log.warning("出力ストリーム: %s", status)
        needed = frames * SAMPLE_WIDTH
        buf = bytearray()
        while len(buf) < needed:
            try:
                chunk = play_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if chunk is None:
                break
            buf.extend(chunk)
        if len(buf) < needed:
            buf.extend(b"\x00" * (needed - len(buf)))  # 無音で埋める
        outdata[:] = bytes(buf[:needed])

    def enqueue(data: bytes) -> None:
        loop.call_soon_threadsafe(play_queue.put_nowait, data)

    user_buf: list[str] = []
    model_buf: list[str] = []

    def flush_user() -> None:
        if user_buf:
            log.info("認識: %s", "".join(user_buf).strip())
            user_buf.clear()

    def flush_model(label: str = "応答") -> None:
        if model_buf:
            log.info("%s: %s", label, "".join(model_buf).strip())
            model_buf.clear()

    with sd.RawOutputStream(
        samplerate=SPEAKER_SAMPLE_RATE,
        dtype="int16",
        channels=CHANNELS,
        device=output_device,
        callback=callback,
    ):
        log.info("スピーカー開始 (%d Hz)", SPEAKER_SAMPLE_RATE)
        while not stop.is_set():
            try:
                async for response in session.receive():
                    if stop.is_set():
                        return
                    server = getattr(response, "server_content", None)
                    if server is None:
                        continue

                    # ユーザー発話検出 → 再生中の応答を止める (barge-in)
                    if getattr(server, "interrupted", False):
                        _drain(play_queue)
                        flush_model("応答(barge-in で中断)")

                    user_tx = getattr(server, "input_transcription", None)
                    if user_tx is not None and getattr(user_tx, "text", None):
                        user_buf.append(user_tx.text)

                    model_turn = getattr(server, "model_turn", None)
                    if model_turn is not None:
                        # 応答が始まった = ユーザーのターンは終わった
                        flush_user()
                        for part in getattr(model_turn, "parts", []) or []:
                            inline = getattr(part, "inline_data", None)
                            if inline is not None and inline.data:
                                enqueue(inline.data)
                            # part.text は native-audio モデルが
                            # 英語チェイン思考を漏らしてくる窓口。
                            # 字幕は output_transcription を使う

                    output_tx = getattr(server, "output_transcription", None)
                    if output_tx is not None and getattr(output_tx, "text", None):
                        model_buf.append(output_tx.text)

                    if getattr(server, "turn_complete", False):
                        flush_user()
                        flush_model()
            except Exception:  # noqa: BLE001
                if stop.is_set():
                    return
                log.exception("受信ループで例外")
                await asyncio.sleep(0.5)


def _drain(q: asyncio.Queue) -> None:
    try:
        while True:
            q.get_nowait()
    except asyncio.QueueEmpty:
        return


async def run(
    model: str,
    api_key: str,
    system_instruction: str,
    input_device: int | str | None,
    output_device: int | str | None,
) -> None:
    client = genai.Client(api_key=api_key, http_options={"api_version": "v1beta"})

    config = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=types.Content(parts=[types.Part(text=system_instruction)]),
        # 文字起こしは PoC のログ可読性のためだけに有効化。
        # 不要なら削って帯域を節約できる。
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in ("SIGINT", "SIGTERM"):
        if hasattr(signal, sig):
            try:
                loop.add_signal_handler(getattr(signal, sig), stop.set)
            except NotImplementedError:
                # Windows は add_signal_handler を実装していない。
                # KeyboardInterrupt は asyncio.run が投げてくれる。
                pass

    log.info("Gemini Live に接続中 (model=%s)", model)
    async with client.aio.live.connect(model=model, config=config) as session:
        log.info("接続完了。話しかけると応答が返る。Ctrl+C で終了")
        send_task = asyncio.create_task(mic_to_api(session, stop, input_device))
        recv_task = asyncio.create_task(api_to_speaker(session, stop, output_device))
        try:
            await stop.wait()
        finally:
            send_task.cancel()
            recv_task.cancel()
            for t in (send_task, recv_task):
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass


def _parse_device(value: str | None) -> int | str | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Gemini Live S2S の最小 PoC")
    parser.add_argument(
        "--model",
        default=os.environ.get("GEMINI_LIVE_MODEL", "gemini-2.5-flash-native-audio-latest"),
        help="Gemini Live のモデル名 (環境変数 GEMINI_LIVE_MODEL でも指定可)",
    )
    parser.add_argument(
        "--system",
        default="あなたは家庭用の音声アシスタントです。簡潔な日本語で答えてください。",
        help="システムプロンプト",
    )
    parser.add_argument(
        "--input-device",
        default=None,
        help="マイクのデバイス番号または名前 (jarvis devices で一覧)",
    )
    parser.add_argument(
        "--output-device",
        default=None,
        help="スピーカーのデバイス番号または名前",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug ログを出す")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # 本線 CLI と同じく .env を読む (既設の env が優先)
    try:
        from jarvis.config import load_env_file

        loaded = load_env_file()
        if loaded:
            log.debug(".env から読み込んだ: %s", ", ".join(loaded))
    except Exception:  # noqa: BLE001
        pass

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        sys.stderr.write(
            "GEMINI_API_KEY が設定されていない。\n"
            "  https://aistudio.google.com/apikey からキーを取り、環境変数に入れる\n"
        )
        return 1

    try:
        asyncio.run(
            run(
                model=args.model,
                api_key=api_key,
                system_instruction=args.system,
                input_device=_parse_device(args.input_device),
                output_device=_parse_device(args.output_device),
            )
        )
    except KeyboardInterrupt:
        log.info("終了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
