# Phase A: Meet 参加 bot の疎通確認

Phase A は「音と Chromium が通る経路を物理的に敷く」ことだけを目的にする。
LLM/STT/TTS パイプラインには触らない。ここが通らなければ以降の計画は全部無効。

## 方針

- **参加方式**: ブラウザ自動化（Playwright + Chromium）
- **ホスト**: Windows
- **音声経路は 2 本の仮想ケーブルで分離**。1 本だと TTS がそのまま自分の STT に戻ってループする

```
下り (Meet → jarvis):
    Chromium のスピーカー出力 ─→ CABLE Input  (VB-Audio Virtual Cable)
    jarvis が                    CABLE Output (VB-Audio Virtual Cable) から読む

上り (jarvis → Meet):
    jarvis TTS が                Speakers     (VB-Audio Point) に書く
    Chromium のマイクに          CABLE Output (VB-Audio Point) を選ばせる
```

## 依存の入れ方

```bash
# jarvis 本体 + Playwright
pip install -e ".[meet,llm,audio]"
playwright install chromium
```

Windows 側は以下を一度だけ:

1. [VB-Audio Virtual Cable](https://vb-audio.com/Cable/) を入れる
2. [VB-Audio Point](https://vb-audio.com/Point/) を入れる（2 本目のケーブルが要る）
3. 再起動

## 疎通確認の順序

```bash
jarvis meet-probe devices                    # 1. 仮想ケーブルが見えているか
jarvis meet-probe record --seconds 3         # 2. 下りから録音できるか
jarvis meet-probe tone --seconds 1           # 3. 上りに音を流せるか
jarvis meet-probe open https://meet.google.com/xxx-yyyy-zzz
                                              # 4. Chromium が Meet ページに遷移できるか
```

### 1. `meet-probe devices`

```
下り (Meet→jarvis): #10 CABLE Output (VB-Audio Virtual Cable)
上り (jarvis→Meet): #31 Speakers (VB-Audio Point)

Chromium 側の設定（Meet のミーティング設定で 1 度だけ選ぶ）:
  スピーカー: CABLE Input (VB-Audio Virtual Cable)
  マイク    : CABLE Output (VB-Audio Point)
```

デバイス番号は PC ごとに違う。番号を直接触るのは避け、名前ベースで解決している。

### 2. `meet-probe record`

下りの仮想ケーブルから WAV を書き出す。Meet 側で誰かが喋った音をファイルに
落とせれば Phase A の 半分は終わり。

### 3. `meet-probe tone`

440Hz を 1.5 秒、上りに流す。相手端末の Meet 側で聞こえれば配線成立。
TTS を使わない分、音声合成の失敗と経路の失敗を切り分けられる。

### 4. `meet-probe open`

Chromium を永続プロファイルで起動し、Meet URL に遷移する。初回に手で:

- Google にログイン
- Meet の設定からスピーカー/マイクを VB-Cable に（上の表のとおり）
- 「参加をリクエスト」

を済ませておくと、プロファイルに保存されて以降は自動で乗る。

## 実機で分かったこと (2026-11-14)

### 片道は問題なく通る

- `meet-probe devices`: VB-Cable と VB-Audio Point の両方が正しく解決された
- `meet-probe record --seconds 2`: 64000 バイトの PCM を取得、WAV 保存成功
- `meet-probe tone --seconds 1`: 上り (device=31) に 440Hz を流せた
- Chromium 永続プロファイル起動も成功

### 録音・再生の同時実行で portaudio がクラッシュする

当初は「TTS を上りに流しながら下りで録音する」ループバックテストを入れていたが、
Windows + MME + VB-Audio の組み合わせで exit code `0xC0000005`
（STATUS_ACCESS_VIOLATION）が出た。再生単独・録音単独は問題ない。

Phase B で VoiceLoop を Meet 向けに差し替えるときは、**入出力の host API を揃える**
（できれば WASAPI、無理なら MME 側に統一）か、**sounddevice ではなく pyaudiowpatch
など WASAPI loopback に強いバインディングに切り替える**ことを検討する必要がある。
Phase A の段階では「並行動作が未検証」として先送りする。

### Chromium のデバイス選択は自動化しない

Playwright から Meet のサウンド設定を触るのは可能だが、Meet の UI 変更に弱く、
初回だけ手で設定して永続プロファイルに保存する運用で十分。

## 残課題 (Phase B への申し送り)

1. **並行 I/O の安定化**: 上りに TTS を流しながら下りで STT の継続録音ができる形にする
2. **自己音声のループ回避**: TTS 発話中は STT をミュートする仕組みを `Conversation` に追加
3. **ウェイクワード**: 「ジャービス」を STT 結果の文字列マッチで検出する `NameInTranscript` 実装
4. **VoiceLoop の仮想デバイス対応**: `Microphone` / `play_wav` に `device` パラメータを通す
