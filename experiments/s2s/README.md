# S2S 寄り道 PoC — Gemini Live

Phase 1 (S2S) の検証用。カスケード (`src/jarvis`) とは**排他**で、音声は Google にそのまま流れる。
目的は「barge-in と1秒未満の応答がどのくらい自然か」を自分の耳で確かめることだけ。本線に取り込む予定は無い。

## これは何をするか

```
マイク(16kHz PCM) → Gemini Live(サーバ側 VAD / 応答生成 / 音声合成) → スピーカー(24kHz PCM)
```

- マイクを常時 Gemini に流し続ける（ウェイクワード無し、PoC なので）
- 発話の切れ目はサーバ側 VAD が判定する
- 応答が返っている最中にこちらが話し始めると、サーバがそれを検出して応答を打ち切る（barge-in）
- 認識テキスト（input_transcription）と応答テキスト（output_transcription）はログに出す

## 動かし方

```bash
pip install -e ".[s2s-gemini]"
export GEMINI_API_KEY=...   # https://aistudio.google.com/apikey で取得
python -m experiments.s2s.gemini_live
# Ctrl+C で終了
```

マイクやスピーカーが複数あるなら、カスケード側の `jarvis devices` で番号を確認してから指定:

```bash
python -m experiments.s2s.gemini_live --input-device 1 --output-device 3
```

モデルを変える:

```bash
python -m experiments.s2s.gemini_live --model gemini-2.5-flash-preview-native-audio-dialog
# 既定は gemini-live-2.5-flash-preview
```

## カスケードとの比較で見るところ

| | カスケード (本線) | S2S (この PoC) |
|---|---|---|
| 「ジャービス」からの初音までの遅延 | 複数段の合算。実機で体感2〜3秒 | ネイティブ VAD + ストリーミング合成で1秒前後の想定 |
| 割り込み | 未実装 (Phase 4) | ネイティブに動く |
| 日本語の自然さ (音声合成) | VOICEVOX / AivisSpeech / SAPI の素性次第 | Gemini のネイティブ音声合成 |
| プライバシ | 認識テキストだけが外に出る | 生音声が外に出る |
| LLM 切り替え | 自由 | 不可 (Gemini に固定) |
| ツール/MCP | 動く (本線で実装済み) | Live API の tool-use に書き直しが要る (未実装) |

## 実装の割り切り

- **ウェイクワード無し**。常時接続。家で使うには当然まずい前提だが、PoC の最小構成として
- **ツール呼び出し無し**。カスケード側の `jarvis.tools` と MCP は使わない。応答生成は Gemini 単体
- **エラー復旧は雑**。受信ループは例外を飲んで続行するが、認証/モデル名のミスはそのまま終了
- **テスト無し**。本線のテスト方針（マイクもネットも使わず数秒で通す）とは相容れないため

## 既知の宿題 (このブランチで深追いしない)

- モデル名の既知バージョン表記は 2026/10 時点のもの。SDK/モデル名はまだ動く
- `signal.add_signal_handler` が Windows で未対応 → KeyboardInterrupt 経由で終了する経路に任せている
- エコーキャンセルは無い。ノート PC 内蔵スピーカーだとフィードバックで barge-in が暴発する可能性
- `response.server_content` の属性名は SDK バージョンで変わりやすい。`getattr` でガードはしてある
