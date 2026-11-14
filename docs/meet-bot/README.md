# Meet bot への方向転換 (実験ブランチ: `feat/meet-bot`)

ジャービスを **Google Meet に参加して会話できる bot** に転用する実験。
ホームオートメーション用途からの派生で、`main` には未マージ。

## いまどこ

| 段 | 状態 | 文書 |
|---|---|---|
| Phase A: 音声経路の疎通 | **完了** (実機で双方向疎通) | [phase-a.md](phase-a.md) |
| Phase B: STT/LLM/TTS を Meet に接続 | **計画のみ・未着手** | [phase-b.md](phase-b.md) |
| Phase C: ブラウザ自動化・etiquette | 未計画 | — |

## 次に触るとき

1. ブランチを出す

   ```bash
   git fetch origin
   git checkout feat/meet-bot
   ```

2. 開発依存を入れ直す

   ```bash
   pip install -e ".[meet,llm,audio,dev]"
   playwright install chrome   # Phase C 以降で使う。Phase B は手動 Chrome のみ
   ```

3. 疎通が壊れていないか確認 (環境依存の値を見るだけ)

   ```bash
   jarvis meet-probe devices
   ```

4. 着手するタスクは [phase-b.md](phase-b.md) の **B-1 (音声 I/O 抽象)** から

## 環境の前提 (Phase A で揃っている)

- Windows 11
- [VB-Audio Virtual Cable](https://vb-audio.com/Cable/) がインストール済み
- Google Chrome Stable (bot 動作は普段使いの Chrome で)
- 設定済みの Meet: スピーカー = 既定スピーカー、マイク = CABLE Output (VB-Audio Virtual Cable)

> VB-Audio Point は入っていても使わない (WDM-KS 限定で portaudio から開けない、
> Phase A の失敗記録に詳細)

## 状態ファイル (`state/` 配下、gitignore 済み)

- `state/meet-profile/` — Playwright 用 Chrome プロファイル。Phase A の試行で作られた
  可能性あり。Phase B は使わないので削除してよい
- `state/conversation.jsonl`, `state/memory.json` — ホームオートメーション側と共有

## 既知のトラップ (繰り返さないため)

- Playwright 同梱の Chromium では Google ログインが通らない → `channel="chrome"` に
- `--no-sandbox` / `--use-fake-ui-for-media-stream` は Chrome の警告バナー + Google
  の bot 検知のタネ → 両方外す
- `sd.play()` + `sd.rec()` を異なる host API で並行させると `0xC0000005` で落ちる
- VB-Audio Point は WDM-KS でしか露出しない + portaudio の WDM-KS 対応は blocking 不可
- Meet のノイズサプレッションはピュア正弦波をほぼ確実に消す。疎通確認は実音声で
