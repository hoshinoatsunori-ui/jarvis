"""Google Meet と jarvis を音声でつなぐブリッジ (Phase A)。

jarvis の既存パイプラインは単一のマイクとスピーカーを前提にしているが、
Meet への bot 参加では「音の経路」を 2 本に分けないとエコーループが起きる。
二つの方向で違う仕組みを使う:

    下り (Meet → jarvis):
        Chrome は普段どおりシステムの既定スピーカーに音を出す。
        jarvis は WASAPI の loopback 機能でそのスピーカー出力を直接拾う。
        仮想ケーブルを挟まないので、VB-Audio の相性問題に左右されない。

    上り (jarvis → Meet):
        jarvis TTS を CABLE Input (VB-Audio Virtual Cable) に書き込む。
        Chrome 側で Meet のマイクを CABLE Output (VB-Audio Virtual Cable) に
        切り替えておく。仮想ケーブル 1 本で済む。

当初は仮想ケーブル 2 本構成を試したが、2 本目として想定した VB-Audio Point が
portaudio の WDM-KS 対応で開けず、loopback 方式に切り替えた経緯がある。

Phase A の時点では接続の疎通確認が目的で、LLM パイプラインには繋がない。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .audio import Audio
from .errors import EngineUnavailable

log = logging.getLogger("jarvis.meet")


# -- 音声デバイス解決 --------------------------------------------------------


#: bot が TTS を書き込む仮想ケーブル (jarvis から見ると出力デバイス)。
#: Chrome 側で Meet のマイクに「CABLE Output (VB-Audio Virtual Cable)」を
#: 選ばせておく。
_UPLINK_CANDIDATES = (
    "CABLE Input (VB-Audio Virtual Cable)",
    "CABLE Input",
)


@dataclass(frozen=True)
class AudioRoute:
    """下り (loopback) と上り (仮想ケーブル) のデバイスペア。"""

    #: pyaudiowpatch の loopback デバイス番号 (スピーカー出力をキャプチャする)。
    downlink_input: int
    #: sounddevice の出力デバイス番号 (TTS を仮想ケーブルに流し込む)。
    uplink_output: int
    downlink_name: str
    uplink_name: str

    def describe(self) -> str:
        return (
            f"下り (Meet→jarvis): WASAPI loopback #{self.downlink_input} {self.downlink_name}\n"
            f"上り (jarvis→Meet): VB-Cable #{self.uplink_output} {self.uplink_name}"
        )

    def chrome_speaker_hint(self) -> str:
        """Chrome 側のスピーカーに選ばせるべきデバイス。

        loopback 方式ではシステムの既定スピーカーをそのまま使う。bot 向けの
        仮想ケーブルを挟む必要がない。ユーザーが Meet 音声を聴きたいなら
        ヘッドホンやイヤホンを挿せばよい (physical mic への回り込みを避ける)。
        """
        # pyaudiowpatch は loopback 面を "[Loopback]" 付きで返す。
        # Meet の UI 上では Loopback 付きの名前は見えないので剥がす。
        return self.downlink_name.replace(" [Loopback]", "").strip()

    def chrome_mic_hint(self) -> str:
        """Chrome 側のマイクに選ばせるべきデバイス名の目安。"""
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
            'pip install "jarvis[meet]"（Chrome は playwright install chrome）',
        ) from exc
    return sounddevice


def _import_pyaudiowpatch():
    try:
        import pyaudiowpatch  # noqa: PLC0415
    except Exception as exc:
        raise EngineUnavailable(
            "meet",
            f"pyaudiowpatch を読み込めない ({exc})",
            'pip install "jarvis[meet]" (Windows 限定)',
        ) from exc
    return pyaudiowpatch


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
    """既定スピーカー (loopback) と仮想ケーブル (上り) を自動検出する。"""
    sd = _import_sounddevice()
    pya = _import_pyaudiowpatch()

    # 下り: WASAPI の既定再生デバイスを loopback モードで開ける番号を取る。
    p = pya.PyAudio()
    try:
        loopback_info = p.get_default_wasapi_loopback()
    except Exception as exc:
        p.terminate()
        raise EngineUnavailable(
            "meet",
            f"WASAPI loopback デバイスが見つからない ({exc})",
            "Windows の『サウンド』で既定の再生デバイスを一つ確定させる",
        ) from exc
    else:
        p.terminate()

    # 上り: 仮想ケーブル (CABLE Input) を sounddevice 側で見つける。
    devices = list(sd.query_devices())
    uplink = _pick_device(devices, _UPLINK_CANDIDATES, want_input=False)
    if uplink is None:
        raise EngineUnavailable(
            "meet",
            "上り用の仮想ケーブル (CABLE Input) が見つからない",
            "VB-Audio Virtual Cable をインストールする: https://vb-audio.com/Cable/",
        )

    return AudioRoute(
        downlink_input=loopback_info["index"],
        uplink_output=uplink[0],
        downlink_name=loopback_info["name"],
        uplink_name=uplink[1],
    )


# -- 録音・再生 --------------------------------------------------------------


def record_to_wav(
    device: int,
    seconds: float,
    sample_rate: int = 16000,
    channels: int = 1,
) -> Audio:
    """WASAPI loopback でスピーカー出力を録る。

    pyaudiowpatch 経由で開く。デバイスのネイティブ設定 (48kHz ステレオが普通)
    で録ったあと、jarvis の既定 (16kHz モノラル int16) に揃えて返す。
    """
    del channels  # loopback は常にデバイスのネイティブチャネル数を使う
    pya = _import_pyaudiowpatch()
    import numpy as np  # noqa: PLC0415

    p = pya.PyAudio()
    try:
        info = p.get_device_info_by_index(device)
        native_sr = int(info["defaultSampleRate"])
        native_ch = int(info["maxInputChannels"])
        chunk = 1024
        total_frames = int(seconds * native_sr)
        log.info(
            "loopback 録音開始: device=%d (%s), %.1fs @ %dHz %dch → %dHz 1ch",
            device, info["name"], seconds, native_sr, native_ch, sample_rate,
        )
        stream = p.open(
            format=pya.paFloat32,
            channels=native_ch,
            rate=native_sr,
            frames_per_buffer=chunk,
            input=True,
            input_device_index=device,
        )
        try:
            buf = bytearray()
            read = 0
            while read < total_frames:
                data = stream.read(chunk, exception_on_overflow=False)
                buf.extend(data)
                read += chunk
        finally:
            stream.stop_stream()
            stream.close()
    finally:
        p.terminate()

    samples = np.frombuffer(bytes(buf), dtype=np.float32)
    if native_ch > 1:
        samples = samples.reshape(-1, native_ch).mean(axis=1)
    # 線形補間でダウンサンプル。音質は多少落ちるが、Whisper の前段としては十分。
    if native_sr != sample_rate:
        new_len = int(len(samples) * sample_rate / native_sr)
        samples = np.interp(
            np.linspace(0, len(samples) - 1, new_len),
            np.arange(len(samples)),
            samples,
        )
    pcm = (samples * 32767).clip(-32768, 32767).astype("int16").tobytes()
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


# -- Chrome 側 (Playwright) -------------------------------------------------


# bot の挙動が目立つ最小限のフラグだけ付ける。
# - disable-features=WebRtcHideLocalIpsWithMdns: Meet での接続安定性向上
# "--use-fake-ui-for-media-stream" は入れない: Chrome が「サポートされていない
#   コマンドライン フラグ」と警告バナーを出し、Google 側の bot 検知にも使われる。
#   マイク/カメラの許可は初回だけ手で承認して、永続プロファイルに保存する。
# "--mute-audio" は入れない: Meet 音声をスピーカーに出させて loopback で拾う。
_CHROME_ARGS = (
    "--disable-features=WebRtcHideLocalIpsWithMdns",
)


class MeetBrowser:
    """ブラウザを永続プロファイルで起動し、Meet URL を開く。

    既定で Google Chrome Stable (`channel="chrome"`) を使う。Playwright 同梱の
    Chromium は Google の bot 検知に「安全でないブラウザ」と判定されて
    ログインが通らないため、本物の Chrome を選ぶ。`playwright install chrome`
    で入る。

    初回は手でログインと、Meet のマイク設定の切り替えを済ませておくこと。
    選択はプロファイルに保存され、以後は自動で乗る。
    """

    def __init__(
        self,
        user_data_dir: str | Path,
        headless: bool = False,
        channel: str = "chrome",
    ) -> None:
        self.user_data_dir = Path(user_data_dir)
        self.headless = headless
        self.channel = channel
        self._pw = None
        self._ctx = None

    def __enter__(self) -> "MeetBrowser":
        try:
            from playwright.sync_api import sync_playwright  # noqa: PLC0415
        except ImportError as exc:
            raise EngineUnavailable(
                "meet",
                "playwright が入っていない",
                'pip install "jarvis[meet]" のあと、`playwright install chrome`',
            ) from exc
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(
                str(self.user_data_dir),
                channel=self.channel,
                headless=self.headless,
                args=list(_CHROME_ARGS),
                # "自動化で起動されたブラウザ" 感を薄める。Google の検知を
                # 完全には回避できないが、入室テストの範囲では十分。
                # --no-sandbox は Playwright が既定で付けるが、Chrome が
                # 警告バナーを出して Google 側にも bot と判定される要因になる。
                ignore_default_args=["--enable-automation", "--no-sandbox"],
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
                log.warning("Chrome コンテキストを閉じる際にエラー: %s", err)
            self._ctx = None
        if self._pw is not None:
            self._pw.stop()
            self._pw = None

    def open(self, url: str):
        """URL を開き、Page オブジェクトを返す。"""
        if self._ctx is None:
            raise RuntimeError("MeetBrowser は with 文の中で使うこと")
        page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        log.info("Chrome で URL を開く: %s", url)
        page.goto(url, wait_until="domcontentloaded")
        return page

    def open_new_tab(self, url: str):
        """新しいタブで URL を開き、Page オブジェクトを返す。"""
        if self._ctx is None:
            raise RuntimeError("MeetBrowser は with 文の中で使うこと")
        page = self._ctx.new_page()
        log.info("Chrome で新タブを開く: %s", url)
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
    """loopback デバイスからフレーム単位の PCM をストリーム出力する (Phase B 以降で使う)。

    `Microphone.frames()` と同じインタフェースを揃えるためのもの。
    """
    pya = _import_pyaudiowpatch()
    import numpy as np  # noqa: PLC0415

    p = pya.PyAudio()
    try:
        info = p.get_device_info_by_index(device)
        native_sr = int(info["defaultSampleRate"])
        native_ch = int(info["maxInputChannels"])
        # jarvis 側のフレーム長に合わせるため、native サンプル数で読む。
        native_frame_samples = int(native_sr * frame_ms / 1000)
        stream = p.open(
            format=pya.paFloat32,
            channels=native_ch,
            rate=native_sr,
            frames_per_buffer=native_frame_samples,
            input=True,
            input_device_index=device,
        )
        try:
            while True:
                data = stream.read(native_frame_samples, exception_on_overflow=False)
                samples = np.frombuffer(data, dtype=np.float32)
                if native_ch > 1:
                    samples = samples.reshape(-1, native_ch).mean(axis=1)
                if native_sr != sample_rate:
                    new_len = int(len(samples) * sample_rate / native_sr)
                    samples = np.interp(
                        np.linspace(0, len(samples) - 1, new_len),
                        np.arange(len(samples)),
                        samples,
                    )
                yield (samples * 32767).clip(-32768, 32767).astype("int16").tobytes()
        finally:
            stream.stop_stream()
            stream.close()
    finally:
        p.terminate()
