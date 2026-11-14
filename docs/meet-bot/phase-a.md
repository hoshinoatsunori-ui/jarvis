# Phase A: Meet 参加 bot の疎通確認

Phase A は「音と Chrome が通る経路を物理的に敷く」ことだけを目的にする。
LLM/STT/TTS パイプラインには触らない。ここが通らなければ以降の計画は全部無効。

## 方針

- **参加方式**: ブラウザは手で開く (後述の理由で Playwright 自動化は保留)
- **ホスト**: Windows
- **下り**: WASAPI loopback でシステム既定スピーカーの出力を直接捕捉
- **上り**: VB-Audio Virtual Cable 1 本を挟んで Meet のマイクに書き込み

```
下り (Meet → jarvis):
    Chrome のスピーカー = 物理スピーカー (Realtek など、システムの既定)
    jarvis が WASAPI loopback でそのスピーカー出力をキャプチャ

上り (jarvis → Meet):
    jarvis TTS が CABLE Input (VB-Audio Virtual Cable) に書き込む
    Chrome の Meet マイクに CABLE Output (VB-Audio Virtual Cable) を選ばせる
```

当初は仮想ケーブル 2 本で分離する案だったが、2 本目に想定した VB-Audio Point が
portaudio の WDM-KS 対応で開けず (下記の [失敗記録](#失敗記録) 参照)、
loopback 方式に切り替えた経緯がある。
結果として **必要な外部依存が 1 ケーブルだけ** になり、他 PC でも再現しやすくなった。

## 依存の入れ方

```bash
pip install -e ".[meet,llm,audio]"
playwright install chrome   # Chrome 自動化は Phase B で使う
```

Windows 側は以下を一度だけ:

1. [VB-Audio Virtual Cable](https://vb-audio.com/Cable/) を入れる (無料)
2. 再起動

## 疎通確認の順序

```bash
jarvis meet-probe devices                   # 1. loopback とケーブルが解決できるか
jarvis meet-probe record --seconds 5        # 2. 下りを録音
jarvis meet-probe play PATH                 # 3. 上りに WAV を流す
jarvis meet-probe tone                      # (単純な正弦波は NS に消される)
jarvis meet-probe open URL                  # 4. Chrome で Meet URL を開く (Phase B で使う)
```

### 1. `meet-probe devices`

```
下り (Meet→jarvis): WASAPI loopback #22 スピーカー (Realtek High Definition Audio) [Loopback]
上り (jarvis→Meet): VB-Cable #15 CABLE Input (VB-Audio Virtual Cable)

Chrome 側の設定（Meet のミーティング設定で 1 度だけ選ぶ）:
  スピーカー: スピーカー (Realtek High Definition Audio)
             (既定のまま。ユーザーが聴きたいならヘッドホンを挿す)
  マイク    : CABLE Output (VB-Audio Virtual Cable)
```

デバイス番号は PC ごと・起動ごとに違う。番号を直接触るのは避け、名前ベースで解決している。

### 2. `meet-probe record`

既定スピーカーの出力を WASAPI loopback で N 秒ぶん拾って WAV にする。
Meet で相手が喋っている最中にこれを回せば、相手の声が入った WAV が取れる。

内部では pyaudiowpatch の `paWinWasapiLoopback` 機能を使っている。
sounddevice は 0.5.6 時点でこれを露出していない。

### 3. `meet-probe play`

WAV を上り (CABLE Input) に流す。Chrome の Meet マイクに CABLE Output を
選ばせておけば、相手端末の参加者に聞こえる。

### 4. `meet-probe tone`

440Hz を流すコマンド。**音声としては通るが、Meet のノイズサプレッションが
ピュアな正弦波をほぼ確実に消す**ので、疎通確認用途ではあまり当てにならない。
音声疎通の最終確認は `play` で実音声を流すこと。

## 実機で分かったこと (2026-11-14)

### 完全に疎通した

- 下り: Realtek スピーカーからの loopback を peak 73% / RMS 0.055 で捕捉できた
- 上り: TTS で合成した日本語音声を Meet 参加者に届けられた

### ブラウザ自動化は Phase B で持ち越し

当初 Playwright で Chrome を起動して Meet URL に遷移させる形にしていたが、
Google 側の bot 検知が強く、ログインが通らなかった。試した緩和策:

- Chromium 同梱 → 本物の Chrome (`channel="chrome"`) に変更 → 効かず
- `--no-sandbox` を除去 (Playwright の既定フラグ) → 警告バナーは消えたが NG
- `--use-fake-ui-for-media-stream` を除去 → バナー 1 個減っただけ
- accounts.google.com を先に開いてからログイン → ログインフォームに到達せず

Phase A の目的は音声経路なので、ここで止めて**普段使いの Chrome を手で開く運用**
にした。bot 的な常駐を Phase B で実現するなら:

- `--remote-debugging-port=9222` で Chrome を手で起動 → Playwright が CDP 接続
- あるいは Google Meet Media API に移る (Workspace 契約が要る)

### 2 本目の仮想ケーブル (VB-Audio Point) は portaudio で開けなかった {#失敗記録}

当初プラン: 下りと上りで別々の仮想ケーブルを使う (エコーループ回避のため)。
しかし、ユーザー環境にすでに入っていた VB-Audio Point は **WDM-KS でしか露出しておらず**
(MME/DirectSound/WASAPI のどれにも出てこない)、portaudio から開こうとすると:

- Blocking API (`sd.play()`): `"Blocking API not supported yet" [Windows WDM-KS error -9999]`
- Callback API (`sd.OutputStream`): `Invalid device [PaErrorCode -9996]`

で失敗する。portaudio の WDM-KS backend は blocking API を実装しておらず、
callback API も Meet 側がケーブルの反対端を掴んでいると排他で開けない。

これを機に方針転換:

| 当初 | 変更後 |
|---|---|
| 仮想ケーブル 2 本 | 仮想ケーブル 1 本 + WASAPI loopback |
| VB-Cable + VB-Audio Point が前提 | VB-Cable 1 本だけが前提 |
| `sounddevice` だけで完結 | 下りだけ `pyaudiowpatch` を使う |

変更後は他 PC への再現性が増した (WASAPI loopback はドライバ非依存で動く)。

### 録音と再生の同時実行は portaudio で不安定

sounddevice で異なる host API のデバイスを同時に扱うと、
`STATUS_ACCESS_VIOLATION` (`0xC0000005`) で落ちることがある。
特に MME + VB-Audio の組み合わせで出た。Phase B で VoiceLoop の継続録音と
TTS 再生を並行させる際は、**全部 pyaudiowpatch に寄せる** か、
sounddevice 側も host API を揃えて開く工夫が要る。

### ピュアな正弦波は Meet のノイズサプレッションに殺される

`meet-probe tone` で 440Hz を送っても相手には何も聞こえなかった。
同じ経路で TTS 音声 (`meet-probe play`) を送ったら問題なく届いた。
Meet の NS は「音声らしくない定常信号」を積極的に除去する設計。
疎通確認は音声系の信号で行うのが確実。

## 残課題 (Phase B への申し送り)

1. **ブラウザ自動化の方式確定**: CDP 接続 or Meet Media API or 手動運用の継続
2. **並行 I/O の安定化**: 上りに TTS を流しながら下りで STT の継続録音ができる形に。
   全部 pyaudiowpatch に寄せる案が有力
3. **自己音声のループ回避**: Meet 自身は自分の声をエコーバックしないので、
   loopback で拾った音には自分の TTS は乗らない (はず)。Phase B で実測確認
4. **ウェイクワード**: 「ジャービス」を STT 結果の文字列マッチで検出する
   `NameInTranscript` 実装
5. **VoiceLoop の loopback 対応**: `Microphone` を `iter_pcm_frames` 相当に差し替える
