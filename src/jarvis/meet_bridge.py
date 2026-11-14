"""Google Meet と jarvis を仮想音声デバイスでつなぐブリッジ (Phase A)。

jarvis の既存パイプラインは単一のマイクとスピーカーを前提にしているが、
Meet への bot 参加では「音の経路」を 2 本に分けないとエコーループが起きる。

    [Meet 側の相手音声] ─→ 仮想ケーブル #A ─→ jarvis STT        (下り)
    jarvis TTS         ─→ 仮想ケーブル #B ─→ [Meet 側のマイク]   (上り)

Windows では VB-Audio の Virtual Cable と Point の 2 系統が使える。
Chromium のメディア設定で入出力デバイスを 1 度だけ指定し、
ペルシステントプロファイルに保存して以後は自動で乗る。

Phase A の時点では接続の疎通確認が目的で、LLM パイプラインには繋がない。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .audio import SAMPLE_WIDTH, Audio
from .errors import EngineUnavailable

log = logging.getLogger("jarvis.meet")


# -- 音声デバイス解決 --------------------------------------------------------

#: Meet 側の音声を bot が受け取る入口（jarvis から見ると入力デバイス）。
#: VB-Cable の「CABLE Output」が該当する（名前が逆方向で紛らわしいが、
#: アプリ側は「送出するアプリの出力の受け口」という意味で Output）。
_DOWNLINK_CANDIDATES = (
    "CABLE Output (VB-Audio Virtual Cable)",
    "CABLE Output (VB-Audio Point)",
    "CABLE Output",
)

#: bot が TTS を書き込む出口（jarvis から見ると出力デバイス）。
#: 下りと同じケーブルを使うと TTS が自分で聞こえてループするので、
#: 別のケーブルを選ぶ。
_UPLINK_CANDIDATES = (
    "Speakers (VB-Audio Point)",
    "CABLE Input (VB-Audio Point)",
    "CABLE Input (VB-Audio Virtual Cable)",
    "CABLE Input",
)


@dataclass(frozen=True)
class AudioRoute:
    """仮想ケーブルの入出力ペア。"""

    downlink_input: int
    uplink_output: int
    downlink_name: str
    uplink_name: str

    def describe(self) -> str:
        return (
            f"下り (Meet→jarvis): #{self.downlink_input} {self.downlink_name}\n"
            f"上り (jarvis→Meet): #{self.uplink_output} {self.uplink_name}"
        )

    def chromium_speaker_hint(self) -> str:
        """Chromium 側のスピーカーに選ばせるべきデバイス名の目安。

        jarvis が『CABLE Output』から読んでいるなら、Chromium は同じケーブルの
        反対側『CABLE Input』に書き込まなければならない。
        """
        if "VB-Audio Virtual Cable" in self.downlink_name:
            return "CABLE Input (VB-Audio Virtual Cable)"
        if "VB-Audio Point" in self.downlink_name:
            return "CABLE Input (VB-Audio Point)"
        return "下り用ケーブルの反対側 (Input 側)"

    def chromium_mic_hint(self) -> str:
        """Chromium 側のマイクに選ばせるべきデバイス名の目安。

        jarvis が『Speakers (VB-Audio Point)』に書き込んでいるなら、Chromium は
        同じケーブルの反対側『CABLE Output (VB-Audio Point)』から読む。
        """
        if "VB-Audio Point" in self.uplink_name:
            return "CABLE Output (VB-Audio Point)"
        if "VB-Audio Virtual Cable" in self.uplink_name:
            return "CABLE Output (VB-Audio Virtual Cable)"
        return "上り用ケーブルの反対側 (Output 側)"


def _import_sounddevice():
    try:
        import sounddevice  # noqa: PLC0415
    except Exception as exc:  # ImportError だけでなく PortAudio 未導入の OSError も拾う
        raise EngineUnavailable(
            "meet",
            f"sounddevice を読み込めない ({exc})",
            'pip install "jarvis[meet]"（Chromium は playwright install chromium）',
        ) from exc
    return sounddevice


def _pick_device(
    devices: list[dict],
    candidates: tuple[str, ...],
    *,
    want_input: bool,
) -> tuple[int, str] | None:
    """候補名に部分一致するデバイスのうち、入出力の向きが合うものを拾う。

    優先順は `candidates` の並びどおり。同名が複数あるときは最初の 1 つ。
    """
    want_channels = "max_input_channels" if want_input else "max_output_channels"
    for needle in candidates:
        for index, info in enumerate(devices):
            if info.get(want_channels, 0) < 1:
                continue
            name = info.get("name", "")
            if needle in name:
                return index, name
    return None


def resolve_route() -> AudioRoute:
    """既定の仮想ケーブル 2 本を自動検出する。

    見つからない段や、同じケーブルを両向きに使おうとしている場合は
    EngineUnavailable を投げ、何を入れれば足りるかを指示する。
    """
    sd = _import_sounddevice()
    devices = list(sd.query_devices())
    downlink = _pick_device(devices, _DOWNLINK_CANDIDATES, want_input=True)
    uplink = _pick_device(devices, _UPLINK_CANDIDATES, want_input=False)
    if downlink is None:
        raise EngineUnavailable(
            "meet",
            "下り用の仮想ケーブル (CABLE Output) が見つからない",
            "VB-Audio Virtual Cable をインストールする: https://vb-audio.com/Cable/",
        )
    if uplink is None:
        raise EngineUnavailable(
            "meet",
            "上り用の仮想ケーブル (CABLE Input / VB-Audio Point) が見つからない",
            "VB-Audio Virtual Cable と VB-Audio Point の両方を入れる（2 本必要）",
        )
    # 同じデバイスを両向きに使うことは原理的にできないが、名前が似たデバイスを
    # 誤認しているケースを弾くためにガードしておく。
    if downlink[1].split(" (")[0] == uplink[1].split(" (")[0]:
        raise EngineUnavailable(
            "meet",
            f"下りと上りに同じケーブルを割り当てようとしている ({downlink[1]})",
            "2 本目の仮想ケーブル（VB-Audio Point など）を入れる",
        )
    return AudioRoute(
        downlink_input=downlink[0],
        uplink_output=uplink[0],
        downlink_name=downlink[1],
        uplink_name=uplink[1],
    )


# -- デバイス指定の録音・再生 ------------------------------------------------


def record_to_wav(
    device: int,
    seconds: float,
    sample_rate: int = 16000,
    channels: int = 1,
) -> Audio:
    """指定デバイスから一定秒数だけ録音して Audio を返す。

    疎通確認のための単発録音。Phase B 以降は VoiceLoop から継続録音に差し替える。
    """
    sd = _import_sounddevice()
    import numpy as np  # noqa: PLC0415

    frames = int(seconds * sample_rate)
    log.info("録音開始: device=%d, %.1fs @ %dHz, ch=%d", device, seconds, sample_rate, channels)
    buf = sd.rec(frames, samplerate=sample_rate, channels=channels, dtype="int16", device=device)
    sd.wait()
    if channels == 1:
        pcm = buf.tobytes()
    else:
        # モノラルに畳む（Whisper は 1ch 前提）。
        mono = buf.astype("int32").mean(axis=1).astype("int16")
        pcm = mono.tobytes()
    log.info("録音完了: %d バイト", len(pcm))
    return Audio(pcm=pcm, sample_rate=sample_rate)


def play_wav_to(data: bytes, device: int) -> None:
    """WAV バイト列を指定デバイスに流す。

    `audio.play_wav` は OS の既定デバイスに固定なので、上りケーブルに
    向けて鳴らすのにそのままは使えない。
    """
    sd = _import_sounddevice()
    import numpy as np  # noqa: PLC0415

    audio = Audio.from_wav_bytes(data)
    samples = np.frombuffer(audio.pcm, dtype="int16")
    log.info("再生開始: device=%d, %.2fs", device, audio.duration_s)
    sd.play(samples, samplerate=audio.sample_rate, device=device)
    sd.wait()


# -- Chromium 側 (Playwright) ------------------------------------------------


# bot の挙動が目立つ最小限のフラグだけ付ける。
# - use-fake-ui-for-media-stream: マイク/カメラの許可ダイアログを自動承認
# - disable-features=WebRtcHideLocalIpsWithMdns: Meet での接続安定性向上
# "--mute-audio" は入れない (スピーカーを仮想ケーブルに向けた上で鳴らしたいので)
_CHROMIUM_ARGS = (
    "--use-fake-ui-for-media-stream",
    "--disable-features=WebRtcHideLocalIpsWithMdns",
)


class MeetBrowser:
    """Chromium を永続プロファイルで起動し、Meet URL を開く。

    初回は手でログインと、Meet の設定からマイク/スピーカーに VB-Cable を
    選んでおくこと。選択はプロファイルに保存され、以後は自動で乗る。
    """

    def __init__(self, user_data_dir: str | Path, headless: bool = False) -> None:
        self.user_data_dir = Path(user_data_dir)
        self.headless = headless
        self._pw = None
        self._ctx = None

    def __enter__(self) -> "MeetBrowser":
        try:
            from playwright.sync_api import sync_playwright  # noqa: PLC0415
        except ImportError as exc:
            raise EngineUnavailable(
                "meet",
                "playwright が入っていない",
                'pip install "jarvis[meet]" のあと、`playwright install chromium`',
            ) from exc
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(
                str(self.user_data_dir),
                headless=self.headless,
                args=list(_CHROMIUM_ARGS),
                # "自動化で起動されたブラウザ" 感を薄める。Google の検知を
                # 完全には回避できないが、入室テストの範囲では十分。
                ignore_default_args=["--enable-automation"],
            )
        except Exception:
            self._pw.stop()
            self._pw = None
            raise
        return self

    def __exit__(self, *exc) -> None:
        if self._ctx is not None:
            try:
                self._ctx.close()
            except Exception as err:
                log.warning("Chromium コンテキストを閉じる際にエラー: %s", err)
            self._ctx = None
        if self._pw is not None:
            self._pw.stop()
            self._pw = None

    def open(self, url: str):
        """URL を開き、Page オブジェクトを返す。"""
        if self._ctx is None:
            raise RuntimeError("MeetBrowser は with 文の中で使うこと")
        page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        log.info("Chromium で URL を開く: %s", url)
        page.goto(url, wait_until="domcontentloaded")
        return page

    def wait_closed(self) -> None:
        """すべてのタブが閉じられるまで待つ。手動疎通テスト用。"""
        if self._ctx is None:
            raise RuntimeError("MeetBrowser は with 文の中で使うこと")
        while self._ctx.pages:
            try:
                self._ctx.pages[0].wait_for_event("close", timeout=0)
            except Exception:
                break


def iter_pcm_frames(
    device: int,
    frame_ms: int = 30,
    sample_rate: int = 16000,
) -> Iterator[bytes]:
    """指定デバイスからフレーム単位の PCM をストリーム出力する (Phase B 以降で使う)。

    `Microphone.frames()` と同じインタフェースを仮想ケーブル向けに揃えるためのもの。
    """
    sd = _import_sounddevice()
    frame_samples = int(sample_rate * frame_ms / 1000)
    stream = sd.RawInputStream(
        samplerate=sample_rate,
        blocksize=frame_samples,
        device=device,
        dtype="int16",
        channels=1,
    )
    stream.start()
    try:
        while True:
            data, overflowed = stream.read(frame_samples)
            del overflowed
            yield bytes(data)
    finally:
        stream.stop()
        stream.close()
