# 設計の背景と、段階的な進め方

実装の全体像（信号の流れ、1ターンの処理、コードの地図）は [architecture.html](architecture.html) にある。
このドキュメントは「なぜその構成にしたか」と「次に何をするか」を扱う。

## なぜカスケードなのか

2026年時点で現実的な構成は2系統ある。

| | S2S（OpenAI Realtime / Gemini Live） | カスケード（このリポジトリ） |
|---|---|---|
| レイテンシ | 最小。中間ハンドオフが無く1秒未満 | 各段の合算。ストリーミングで短縮 |
| 割り込み(barge-in) | ネイティブ対応 | 自前実装が要る |
| カスタマイズ性 | 低い。LLM も音声もベンダー固定 | 高い。各段を差し替え自由 |
| LLM 切替 | 不可（音声パスに固定される） | 自由 |
| コスト | 音声トークン課金 | 各段個別。ローカル化すれば電気代のみ |

カスケードを選んだのは、**日本語の STT/TTS をローカルに置いたまま LLM だけクラウドにする**構成が取れるため。音声が家庭外に出るのはウェイクワード検出後だけ、という設計にできる。代償は barge-in の自前実装（未実装）と、各段の合算レイテンシ。

「S2S の手軽さ」を取るなら、このリポジトリは土台にならない。`pipeline.py` を Realtime API のセッション管理に置き換えることになる。

## フェーズ

出典の記事が示す5段階に、このリポジトリの現状を対応させたもの。

### Phase 0 — まず喋らせる ✅ 実機で一周済み

ウェイクワード → STT → LLM → TTS のミニマルなループを通す。

```bash
pip install -e ".[llm]"
cp config.example.yaml config.yaml
jarvis chat --tts none          # マイク不要。LLM とツールだけ先に確認
```

音声まで通すなら、手前の段から順に潰す。

```bash
pip install -e ".[audio,stt-local,wake-oww]"
jarvis devices                  # 使うマイクを決める
jarvis level                    # VAD のしきい値を実測する（部屋ごとに違う）
jarvis say "テストです"          # TTS 単体
jarvis transcribe sample.wav    # STT 単体
jarvis listen --wake manual     # Enter 起動で通す
jarvis listen                   # ウェイクワードを有効化
```

実機で通った構成（Windows 11 + VS Code）。ウェイクワードと TTS は、
追加インストールの要らない `manual` と `system` で代替して確認した:

| 段 | 実装 | 根拠 |
|---|---|---|
| ウェイクワード | openWakeWord `hey_jarvis` | 無料・ローカル。同梱モデルは非商用ライセンス |
| VAD | エネルギー | 依存ゼロ。静かな部屋なら実用になる |
| STT | kotoba-whisper-v2.0 (faster-whisper) | 日本語 CER が large-v3 を上回り、かつ約6.3倍速いとされる |
| LLM | Claude (`claude-opus-5`, effort: low) | tool use / MCP を前提にできる |
| TTS | VOICEVOX / AivisSpeech | 日本語の自然さ。ローカル完結 |

### Phase 1 — S2S で自然な会話にする（未着手）

Gemini Live か OpenAI Realtime に置き換えて、barge-in と1秒未満の応答を体感する。**このリポジトリの構成とは排他**なので、比較検討のための寄り道になる。カスケードのまま barge-in だけ欲しいなら、Phase 4 の項を参照。

### Phase 2 — 常時待受のハードウェア化（未着手）

Raspberry Pi 5 + ReSpeaker マイクアレイ、または Home Assistant Voice Preview Edition。遠距離集音とエコーキャンセルを詰める段階。

このリポジトリ側で必要になる変更は少ない。`audio.input_device` を合わせ、`jarvis level` でしきい値を測り直し、systemd で常駐させるくらい。Pi 5 は GPU が無いので、STT が重ければ `compute_type: int8` か、STT だけクラウドに出す。

### Phase 3 — スマートホーム連携（部分的に土台あり）

LLM の tool use から家電を操作する。このリポジトリでは **MCP サーバを足すだけ**で届く。

```yaml
tools:
  mcp_servers:
    home-assistant:
      command: uvx
      args: ["mcp-server-home-assistant"]
      env:
        HA_TOKEN: ${HA_TOKEN}
```

Home Assistant 経由が王道。赤外線家電は Nature Remo などのスマートリモコンを HA に統合する。応答速度を重視するなら Cloud API ではなく Local API を使う。

### Phase 4 — Jarvis 化（未着手）

- **割り込み (barge-in)**: TTS 再生中もマイクを監視し、発話を検出したら再生を止める。エコーキャンセルが要る（スピーカーの音を自分で拾ってしまうため）。`pipeline.Conversation.handle` が同期再生しているので、ここを非同期化するところから。
- **長期記憶の強化**: 現状は `memory.json` の全件をシステムプロンプトに入れている。件数が増えたら `Memory.as_prompt_block()` をベクトル検索に差し替える。
- **話者識別**: WhisperX + pyannote-audio。
- **マルチルーム**: 各部屋に ESP32-S3 等のサテライトを置き、STT/LLM/TTS はハブに集約する。
- **フォールバック**: ネット断・API障害時にローカル LLM へ落とす。`factory.build_agent` の前段に切替を挟む。

## 判断を変える閾値

- 月間利用が10時間を大きく超え、API 費が月数千円を超える → ローカル STT/TTS の比率を上げる
- 日本語 TTS の自然さに不満 → Style-Bert-VITS2 のファインチューニング、またはクラウド TTS を試す
- Pi 5 でローカル STT が遅い（応答2秒超が常態） → Mini PC + GPU、または STT をクラウドへ
- 商用転用の可能性が出た → openWakeWord 同梱モデル（CC BY-NC-SA 4.0）と Porcupine Personal（非商用・x86_64限定）はどちらも使えない。microWakeWord (Apache-2.0) 等へ切り替える

## 出典について

本ドキュメントの数値（CER、速度比、レイテンシの目安、ライセンス条件）は、この実装の設計判断のもとにした調査記事の記述による。**この環境からは元記事にも各一次ソースにもアクセスできていないため、独立に検証していない。** 料金・モデル名・ライセンス条件は変動が激しいので、導入前に各公式ページで確認すること。
