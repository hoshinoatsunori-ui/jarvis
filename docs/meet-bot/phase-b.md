# Phase B: 既存の STT / LLM / TTS を Meet に乗せる

Phase A で音声経路は通った (loopback 下り + VB-Cable 上り)。
Phase B は **「Meet の中で『ジャービス、今何時？』と呼びかけたら 2.5 秒以内に返答する」** まで持っていく。
LLM パイプラインを Meet 用途に接続し、ブラウザは当面手動運用のまま進める。

## ゴール

- 別端末の Meet 参加者が「ジャービス、今何時？」と呼びかける
- jarvis が 2.5 秒以内に発話開始
- `get_current_time` ツール呼び出しが実行され、時刻を日本語で読み上げる
- 発話は Meet 相手側で聞こえる

**非ゴール (Phase C 以降)**:

- ブラウザ自動化 (CDP 接続 / Meet Media API)
- 発話権制御・barge-in・話者ダイアライゼーション
- 入室時の挨拶・会議サマリ出力
- 複数 Meet の同時対応

## タスク分解

### B-1. 音声 I/O 抽象の整理

既存 `jarvis.audio` は「マイク 1 本、スピーカー 1 本」前提。Meet では
下りが loopback、上りが仮想ケーブルで、API も違う (pyaudiowpatch vs sounddevice)。
`VoiceLoop` から呼べる形に揃える。

- [ ] `AudioSource` 抽象 (フレーム列を返す) を interfaces に追加
  - `MicSource`: 既存 `Microphone` のラッパ
  - `LoopbackSource`: `meet_bridge.iter_pcm_frames` のラッパ
- [ ] `AudioSink` 抽象 (バイト列を鳴らす) を追加
  - `DefaultSink`: 既存 `audio.play_wav`
  - `DeviceSink`: 既存 `meet_bridge.play_wav_to`
- [ ] 既存 `VoiceLoop.__init__` の `input_device` パラメータを `AudioSource` に差し替える
- [ ] 既存 `TextToSpeech.speak` の経路を `AudioSink` 経由に変える (既定は今まで通り)

**成功基準**: 既存 159 件のテストが落ちない。`jarvis listen` の挙動が変わらない。

### B-2. 並行 I/O の安定化

Phase A で判明したとおり、sounddevice で録音と再生を異なる host API で同時に
開くとクラッシュする (`STATUS_ACCESS_VIOLATION`)。
Meet 対話は「下りを常時聞きながら、ときどき上りに TTS を流す」ので、
並行動作が前提になる。

- [ ] まず現状の pyaudiowpatch loopback 録音と sounddevice 再生の同時実行が落ちるかを実測する
- [ ] 落ちるなら、**TTS 再生も pyaudiowpatch に寄せる**  
      (sounddevice のまま WASAPI 共有モードに固定する手もあるが、
      ひとつの API に統一するほうが保守が楽)
- [ ] 落ちないなら現状で進む

**成功基準**: 30 秒の loopback 録音を回しながら、その最中に 3 回 TTS を鳴らしても
プロセスが落ちない。

### B-3. ウェイク層: 書き起こしから名前を拾う

Meet 対話では Enter や openWakeWord は使えないので、
STT 結果に名前が入ったら会話モードに切り替える方式にする。

- [ ] `wake.NameInTranscript` を実装 (`WakeWordEngine` インタフェース)
  - 入力: STT の中間結果 (または直近のフレーズ)
  - 検出: 「ジャービス」「じゃーびす」等のバリエーションに文字列マッチ
  - 検出位置より後ろを「質問本文」として取り出せるようにする
- [ ] 既存 `WakeWordEngine` インタフェースとは呼び出し粒度が違う (フレームでなく文字列)
  ので、別インタフェース `TranscriptWakeDetector` を切るのが素直

**成功基準**: ユニットテストで「ジャービス、今何時？」から「今何時？」が取れる。
「サービスが…」等の偽陽性を弾ける。

### B-4. MeetVoiceLoop

常時 STT を回し、ウェイク検出と発話切り出しを書き起こし側でやるループ。
既存 `VoiceLoop` とは制御フローが別物なので、拡張ではなく新規に書く。

- [ ] `MeetVoiceLoop` クラス (`pipeline.py` に追加)
  - 常時 `AudioSource` からフレーム読み出し
  - VAD でフレーズ単位に区切る (既存 `VoiceActivityDetector` を再利用)
  - フレーズが切れたら STT にかけて書き起こす
  - `TranscriptWakeDetector` でウェイク検出 → 本文抽出 → `Conversation.handle` に流す
  - 検出しなかった書き起こしは捨てる (メモリは `jsonl` に追記しておく)
- [ ] TTS 発話中は STT をスキップするフラグ (B-5 でエコー戻りが観測されたら有効化)

**成功基準**: モックの `AudioSource` と `SpeechToText` で `MeetVoiceLoop` が
「ジャービス、今何時？」→ `get_current_time` ツール呼び出し → TTS 発話、まで通る。

### B-5. 自己音声のループ回避

Meet は自分の声を自分の耳に戻さない (AEC が効く) はずなので、
loopback で拾った音には jarvis の TTS が乗らない見込み。
ただし Chrome の AEC の挙動は信用しきれないので実測で確認する。

- [ ] 実機で `MeetVoiceLoop` を起動、TTS 発話中に loopback の RMS を記録
- [ ] RMS が有意に上がるようなら、TTS 発話中は STT をミュートする処理を B-4 に入れる

**成功基準**: 自己 TTS が自分の STT で拾われて会話が暴走するのが起きない。

### B-6. CLI: `jarvis meet-listen`

- [ ] 新サブコマンド `meet-listen` を追加
  - 事前条件: Chrome で Meet に入室済み、デバイス設定済み (`meet-probe open` 等は使わない)
  - 起動すると MeetVoiceLoop が走り、ウェイクを待つ
  - Ctrl-C で終了
- [ ] `--wake-word` で名前を上書き可 (既定: `ジャービス`)
- [ ] `--silence-ms` など既存 `listen` と揃える
- [ ] `--no-echo-guard` で TTS 中ミュートを無効化 (デバッグ用)

**成功基準**: 実機で `jarvis meet-listen` を起動 → 別端末から「ジャービス、今何時？」→
2.5 秒以内に時刻を読み上げる。

### B-7. ドキュメント

- [ ] docs/meet-bot/phase-b.md (このファイル) に実機で分かったことを追記
- [ ] README に Meet モードの動かし方 (Chrome を手で開く → `jarvis meet-listen`) を追加

## リスクと撤退条件

| リスク | 対処 |
|---|---|
| pyaudiowpatch で再生も安定しない | sounddevice を WASAPI 共有モードに固定して戻す |
| Meet の AEC が効かず自分の TTS を STT が拾う | B-5 のミュート処理を既定で有効化 |
| 常時 STT で CPU が持たない | kotoba-whisper を small に落とす、または VAD トリガで STT を遅延実行 |
| 「ジャービス」の誤検知が頻発 | 検出閾値を上げる・前後の無音を要件に入れる |

## 見積もり

実機確認含めて **2〜3 日**。B-2 (並行 I/O) と B-5 (自己音声回避) が実測依存なので
ブレ幅あり。
