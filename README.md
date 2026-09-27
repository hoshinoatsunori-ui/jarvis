# jarvis

常時待受型の音声AIアシスタント。**ウェイクワード → VAD → STT → LLM(tool use) → TTS** のカスケード構成で、各段を設定1行で差し替えられる。

```
マイク ─→ ウェイクワード ─→ VAD ─→ STT ─→ LLM ─→ TTS ─→ スピーカー
         openwakeword      energy  faster   Claude   VOICEVOX
         porcupine         silero  whisper  (+MCP)   AivisSpeech
         manual(Enter)             openai            system / OpenAI
```

S2S（OpenAI Realtime / Gemini Live）ではなくカスケードを選んでいるのは、各段を個別に差し替えられるため。日本語の STT/TTS をローカルに置いたまま LLM だけクラウドにする、といった構成が取れる。代わりに割り込み（barge-in）は自前実装が要る（**未実装**）。

## 使いはじめ

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[llm]"                 # 最小構成: LLM だけ
cp config.example.yaml config.yaml
cp .env.example .env                    # APIキーを書く（起動時に自動で読まれる）

# マイク不要。キーボードで対話して、プロンプトとツールの挙動を先に確かめる
jarvis chat --tts none

# どの段が動く状態かを一覧する
jarvis doctor
```

`jarvis doctor` は各段を実際に組み立て、さらに「本当に使えるか」まで見る。APIキーが通るか、STT モデルが取得済みか、TTS エンジンが起動しているか。モデルの取得はここでは起こさない（初回の書き起こしまで遅らせてある）。`--offline` で外部への問い合わせを省ける。

```
  OK  wake   [manual]
  OK  vad    [energy]
  NG  stt    [faster_whisper]
      stt:faster_whisper: faster-whisper が入っていない
      対処: pip install "jarvis[stt-local]"
  OK  tools (4個: get_current_time, remember, recall, forget)
```

## 音声で動かすまで

段ごとに必要なものが違う。全部入れる必要はなく、使う段だけ入れればいい。

| 段 | 既定 | 入れるもの |
|---|---|---|
| マイク/スピーカー | – | `pip install -e ".[audio]"`（Linux は `apt install libportaudio2`） |
| ウェイクワード | openwakeword | `pip install -e ".[wake-oww]"` → `python -c "import openwakeword; openwakeword.utils.download_models()"` |
| VAD | energy | 追加なし（silero を使うなら `".[vad-silero]"` と `silero_vad.onnx`） |
| STT | kotoba-whisper | `pip install -e ".[stt-local]"`（初回はモデルを自動取得） |
| LLM | Claude | `pip install -e ".[llm]"` + `ANTHROPIC_API_KEY` |
| TTS | VOICEVOX | [VOICEVOX](https://voicevox.hiroshiba.jp/) か [AivisSpeech](https://aivis-project.com/) を起動しておく |

`--tts system` を使えば、TTS エンジンを入れずに音を出せる。品質は妥協になるが、最初の通し確認はこれで足りる。macOS は `say`、Linux は `espeak-ng`、Windows は PowerShell 経由の SAPI を使う。Windows の SAPI は PowerShell の起動ぶん（数百 ms）だけ発話開始が遅れるので、常用するなら VOICEVOX に移ること。

Windows では portaudio の別途導入は要らない（sounddevice の wheel に同梱されている）。日本語の出力が化けないよう、CLI は Windows でのみ標準入出力を UTF-8 に切り替える。

```bash
jarvis devices                          # 使うマイクを選ぶ
jarvis level                            # VAD のしきい値を実測する（部屋ごとに違う）
jarvis say "テストです"                  # TTS だけ試す
jarvis transcribe sample.wav            # STT だけ試す
jarvis listen                           # 常時待受を開始
```

`jarvis level` は8秒ほどマイクを聞いて、暗騒音と発話の実測値から `vad.options.threshold` の値を提案する。既定値のまま使うと、静かすぎる部屋では声を取りこぼし、うるさい部屋では常に喋っていると判定される。

うまく動かないときは、まず一番手前の段から潰す。

```bash
jarvis listen --wake manual             # ウェイクワードを飛ばして Enter で起動
jarvis listen --stt none --llm echo     # 録音の切り出しだけ確認する
jarvis chat                             # マイクを外して LLM と TTS だけ見る
```

## 設定

`config.yaml` の各段は `engine` と `options` の2つだけ。差し替えるときに触るのは `engine` の1行。

```yaml
tts:
  engine: aivisspeech      # voicevox から差し替え。ポート既定値も一緒に変わる
  options:
    speaker: 888753760
```

`${VAR}` は環境変数に展開される（`${VAR:-既定値}` も可）。APIキーを YAML に直接書かないため。

APIキーは `.env` に書いておけば起動時に読み込まれる（`.env` は `.gitignore` 済み）。既に環境変数が設定されていればそちらが優先される。読み込まれたかは `jarvis -v doctor` で確認できる。

> 「何もしないエンジン」の名前は `none`。YAML では `null` は文字列でなく null リテラルとして読まれるので、エンジン名には使えない。

## ツールと MCP

LLM が呼べるツールは2種類ある。どちらも LLM 側から見れば区別はない。

- **組み込み**: `get_current_time` / `remember` / `recall` / `forget`
- **MCP サーバ**: `config.yaml` の `tools.mcp_servers` に stdio 起動のコマンドを書く

```yaml
tools:
  mcp_servers:
    filesystem:
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "/home/me/notes"]
```

取り込まれたツールは `<サーバ名>__<ツール名>` という名前になる（サーバ間の名前衝突を避けるため）。`jarvis tools` で一覧できる。

MCP の Python SDK は asyncio 前提だが、パイプライン本体は同期で回っている。専用スレッドでイベントループを1本持ち、接続は常駐タスクが開いたまま保持する（`src/jarvis/mcp_bridge.py`）。

## 長期記憶

`remember` で保存した事実は `state/memory.json` に残り、次回以降システムプロンプトに差し込まれる。会話は `state/conversation.jsonl` に追記される。件数が増えて効かなくなったら、`Memory.as_prompt_block()` をベクトル検索に差し替える。

## レイテンシについて

体感を決めるのは「最初の音が出るまで」なので、そこを削る作りにしてある。

- LLM はストリーミングで受け、**1文できた時点で TTS に渡す**（`textutil.iter_sentences`）。文末が遠いときは読点で切る。
- `effort: low` で思考を浅くする。雑談や家電操作に深い推論は要らない。
- **thinking は切らない**。Claude Opus 5 で thinking を無効にすると、ツール呼び出しを `tool_use` ブロックではなく本文テキストに書いてしまい、呼び出しが実行されないまま会話が進む既知の失敗モードがある。速くしたいなら effort を下げる。

## 開発

```bash
pip install -e ".[dev,mcp]"
pytest                                  # 159件。ネットワークもマイクも使わない
```

テストはハードウェアに依存しない。パイプラインは偽のフレーム列を流し込んで検証し、LLM は SDK を差し替えて「ツールを実行して結果を正しい形で返すか」を見ている。MCP ブリッジだけは実際に子プロセスの MCP サーバを起動して確かめている（`tests/mcp_echo_server.py`）。

```
src/jarvis/
  interfaces.py   各段の抽象（WakeWordEngine / VAD / SpeechToText / TextToSpeech / Agent）
  factory.py      「エンジン名 → 実装」の対応表。段を足すときはここに1行
  pipeline.py     VoiceLoop（待受〜録音）と Conversation（LLM〜発話）
  llm.py          Claude のツールループ
  mcp_bridge.py   MCP サーバ → ToolSpec
  textutil.py     ストリームを文に切る
  audio.py        PCM 型・マイク・再生
```

## 現状と、これから

**Phase 0 は実機で一周した。** Windows 11 + VS Code で、マイクに話しかけてから
スピーカーが応答するまでを通しで確認している。

実機で通った構成:

| 段 | 実装 | 備考 |
|---|---|---|
| wake | `manual` | Enter 起動。openWakeWord は未検証 |
| vad | `energy` | `jarvis level` で実測したしきい値 |
| stt | `faster_whisper` / kotoba-whisper-v2.0 | CPU。日本語の認識を確認 |
| llm | `anthropic` / claude-opus-5 | tool use（`get_current_time`）まで実行された |
| tts | `system` | Windows SAPI。日本語で読み上げ |

応答が文単位で分割され、1文ずつ読み上げられることも確認できている。

**まだ実機で見ていないもの**:

- openWakeWord / Porcupine によるウェイクワード検出（`manual` で代替して確認した）
- silero VAD
- VOICEVOX / AivisSpeech（`system` で代替して確認した）
- macOS と Linux の経路。TTS の分岐が OS ごとに違うので、それぞれで確認が要る

**未実装**:

- 割り込み（barge-in）。TTS 再生中のマイク監視とエコーキャンセルが要る
- 話者識別、マルチルーム（音声サテライト）
- ネット断・API障害時のローカルLLMへのフォールバック

## 資料

- [docs/architecture.html](docs/architecture.html) — 構成と機能の全体像。信号の流れ、1ターンの処理、MCP 連携、レイテンシ設計、検証状況（ブラウザで開く）
- [docs/roadmap.md](docs/roadmap.md) — カスケードを選んだ理由と、Phase 0〜4 の進め方

## ライセンス

このリポジトリのコードは [MIT License](LICENSE)。

ただし **各段で使う外部エンジンのライセンスは別に適用される**。このリポジトリは
それらを同梱せず、利用者が任意で導入する形にしてあるが、導入した時点でそちらの
条件に従うことになる。とくに注意が要るもの:

| 対象 | ライセンス | 注意 |
|---|---|---|
| openWakeWord の同梱モデル | CC BY-NC-SA 4.0 | **非商用限定**。コード本体は Apache 2.0 |
| Porcupine（Personal プラン） | 独自 | **非商用かつ x86_64 限定**。商用や ARM は有償プラン |
| Style-Bert-VITS2 | AGPL v3 | 派生物の公開義務がある |
| VOICEVOX | キャラクターごとの利用規約 | 音声ごとにクレジット表記などの条件が異なる |
| kotoba-whisper-v2.0 | Apache 2.0 | 制約は緩い |

家庭内の非商用利用ならいずれも問題にならない。商用に転用する場合は、
ウェイクワードを microWakeWord（Apache-2.0）に替えるなどの検討が要る。
