"""TTS（テキスト→音声）。

日本語の自然さを最優先するなら、記事の比較どおりローカルの
VOICEVOX / AivisSpeech（Style-Bert-VITS2 ベース）が最有力。
AivisSpeech は VOICEVOX 互換の REST API なので、`base_url` を差し替えるだけで移れる。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request

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
    """OS 付属の音声合成。macOS の `say`、Linux の `espeak-ng`。

    品質は妥協になるが、依存ゼロでパイプラインを通せるので Phase 0 の確認に向く。
    """

    def __init__(self, voice: str | None = None) -> None:
        self.voice = voice
        self._command = self._detect()

    @staticmethod
    def _detect() -> str:
        for cmd in ("say", "espeak-ng", "espeak"):
            if shutil.which(cmd):
                return cmd
        raise EngineUnavailable(
            "tts:system",
            "say / espeak-ng が見つからない",
            "macOS なら標準の say、Linux なら `apt install espeak-ng`",
        )

    def synthesize(self, text: str) -> bytes:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "speech.wav"
            if self._command == "say":
                args = ["say", "-o", str(out), "--data-format=LEI16@22050"]
                if self.voice:
                    args += ["-v", self.voice]
                args.append(text)
            else:
                args = [self._command, "-w", str(out)]
                if self.voice:
                    args += ["-v", self.voice]
                args.append(text)
            subprocess.run(args, check=True, capture_output=True)
            return out.read_bytes()


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
