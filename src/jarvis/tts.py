"""TTS（テキスト→音声）。

日本語の自然さを最優先するなら、記事の比較どおりローカルの
VOICEVOX / AivisSpeech（Style-Bert-VITS2 ベース）が最有力。
AivisSpeech は VOICEVOX 互換の REST API なので、`base_url` を差し替えるだけで移れる。
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .errors import EngineUnavailable
from .interfaces import TextToSpeech


class VoicevoxTts(TextToSpeech):
    """VOICEVOX / AivisSpeech の HTTP エンジン。

    既定ポート: VOICEVOX = 50021、AivisSpeech = 10101。
    `/audio_query` で読み・韻律のクエリを作り、`/synthesis` で WAV を得る2段構え。
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:50021",
        speaker: int = 1,
        speed_scale: float | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.speaker = speaker
        self.speed_scale = speed_scale
        self.timeout = timeout

    def synthesize(self, text: str) -> bytes:
        query = self._post_json("/audio_query", {"text": text, "speaker": self.speaker})
        if self.speed_scale is not None:
            query["speedScale"] = self.speed_scale
        return self._post_raw("/synthesis", {"speaker": self.speaker}, json.dumps(query).encode("utf-8"))

    def check(self) -> str:
        """エンジンが起動しているかを確かめる。合成はしない。"""
        try:
            with urllib.request.urlopen(f"{self.base_url}/version", timeout=self.timeout) as resp:
                version = resp.read().decode("utf-8", "replace").strip().strip('"')
        except urllib.error.URLError as exc:
            raise EngineUnavailable(
                "tts:voicevox",
                f"{self.base_url} に繋がらない ({exc})",
                "VOICEVOX(既定 50021) か AivisSpeech(既定 10101) を起動し、"
                "tts.options.base_url を合わせる",
            ) from exc
        return f"{self.base_url} が応答 (version {version})"

    def _url(self, path: str, params: dict) -> str:
        return f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"

    def _post_json(self, path: str, params: dict) -> dict:
        return json.loads(self._request(self._url(path, params), b"", "application/json"))

    def _post_raw(self, path: str, params: dict, body: bytes) -> bytes:
        return self._request(self._url(path, params), body, "application/json")

    def _request(self, url: str, body: bytes, content_type: str) -> bytes:
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.URLError as exc:
            raise EngineUnavailable(
                "tts:voicevox",
                f"{self.base_url} に繋がらない ({exc})",
                "VOICEVOX(既定 50021) か AivisSpeech(既定 10101) のエンジンを起動し、"
                "tts.options.base_url を合わせる",
            ) from exc


class OpenAITts(TextToSpeech):
    """OpenAI TTS。ローカルエンジンを立てられない環境のフォールバック。"""

    def __init__(self, model: str = "tts-1", voice: str = "alloy", api_key: str | None = None) -> None:
        self.model = model
        self.voice = voice
        from .stt import openai_client

        self._client = openai_client(api_key, "tts:openai")

    def synthesize(self, text: str) -> bytes:
        response = self._client.audio.speech.create(
            model=self.model, voice=self.voice, input=text, response_format="wav"
        )
        return response.read()


class SystemTts(TextToSpeech):
    """OS 付属の音声合成。

    macOS は `say`、Linux は `espeak-ng`、Windows は PowerShell 経由の SAPI
    (System.Speech)。品質は妥協になるが、**追加インストールなしでパイプラインを
    最後まで通せる**ので Phase 0 の実機確認に向く。

    Windows の SAPI は PowerShell の起動ぶん（数百 ms）だけ発話開始が遅れる。
    常用するなら VOICEVOX / AivisSpeech に差し替えること。
    """

    def __init__(self, voice: str | None = None, timeout: float = 60.0) -> None:
        self.voice = voice
        self.timeout = timeout
        self._command = self._detect()

    @staticmethod
    def _detect() -> str:
        """使える合成手段を探す。Windows だけ外部コマンドではなく SAPI を使う。"""
        if platform.system() == "Windows":
            for shell in ("powershell", "pwsh"):
                if shutil.which(shell):
                    return shell
            raise EngineUnavailable(
                "tts:system",
                "powershell が見つからない",
                "PATH を確認するか、tts.engine を voicevox にして VOICEVOX を使う",
            )
        for cmd in ("say", "espeak-ng", "espeak"):
            if shutil.which(cmd):
                return cmd
        raise EngineUnavailable(
            "tts:system",
            "say / espeak-ng が見つからない",
            "macOS なら標準の say、Linux なら `apt install espeak-ng`",
        )

    def synthesize(self, text: str) -> bytes:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "speech.wav"
            if self._command in ("powershell", "pwsh"):
                return self._synthesize_sapi(text, Path(tmp), out)
            return self._synthesize_cli(text, out)

    def _synthesize_cli(self, text: str, out: Path) -> bytes:
        if self._command == "say":
            args = ["say", "-o", str(out), "--data-format=LEI16@22050"]
        else:
            args = [self._command, "-w", str(out)]
        if self.voice:
            args += ["-v", self.voice]
        args.append(text)
        subprocess.run(args, check=True, capture_output=True, timeout=self.timeout)
        return out.read_bytes()

    def _synthesize_sapi(self, text: str, workdir: Path, out: Path) -> bytes:
        """Windows SAPI で合成する。

        読み上げ文もスクリプトもファイル経由で渡す。コマンドラインに日本語を
        載せるとコードページ次第で壊れるうえ、引用符の扱いが PowerShell と
        subprocess の間で食い違うため。
        """
        text_path = workdir / "text.txt"
        script_path = workdir / "speak.ps1"
        # Windows PowerShell 5.1 は BOM の無いファイルを ANSI とみなすので BOM を付ける。
        text_path.write_text(text, encoding="utf-8-sig")
        script_path.write_text(_sapi_script(text_path, out, self.voice), encoding="utf-8-sig")

        result = subprocess.run(
            [self._command, "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
            capture_output=True,
            timeout=self.timeout,
        )
        if result.returncode != 0 or not out.exists():
            detail = result.stderr.decode("utf-8", "replace").strip() or "原因不明"
            raise EngineUnavailable(
                "tts:system",
                f"SAPI での合成に失敗した ({detail})",
                "日本語音声が入っているか確認する（設定 → 時刻と言語 → 音声認識）。"
                "入っていなければ tts.engine を voicevox にして VOICEVOX を使う",
            )
        return out.read_bytes()


def _sapi_script(text_path: Path, out_path: Path, voice: str | None) -> str:
    """SAPI を叩く PowerShell スクリプトを組み立てる。

    出力形式は 16bit モノラルに固定する。既定のままだと環境によって
    ビット深度が変わり、再生側（wave モジュール）が受け取れないことがある。
    """
    select = (
        f"$s.SelectVoice('{voice}')"
        if voice
        else (
            "$jp = $s.GetInstalledVoices() | "
            "Where-Object { $_.VoiceInfo.Culture.Name -eq 'ja-JP' } | Select-Object -First 1\n"
            "if ($jp) { $s.SelectVoice($jp.VoiceInfo.Name) }"
        )
    )
    return f"""$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
{select}
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
  22050,
  [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
  [System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile('{out_path}', $fmt)
$s.Speak((Get-Content -Raw -Encoding UTF8 '{text_path}'))
$s.Dispose()
"""


class NullTts(TextToSpeech):
    """合成せず標準出力に書くだけ。スピーカーの無い環境やテスト用。"""

    displays_text = True

    def __init__(self, prefix: str = "[jarvis] ") -> None:
        self.prefix = prefix

    def synthesize(self, text: str) -> bytes:
        from .audio import Audio

        return Audio.silence(0.0, 16000).to_wav_bytes()

    def speak(self, text: str) -> None:
        print(f"{self.prefix}{text}", flush=True)
