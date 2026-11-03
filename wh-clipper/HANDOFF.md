# 引き継ぎメモ：世界史ノート取り込み → 解説動画づくり

Claude（チャット）での作業内容を、Claude Code に移すためのまとめ。最終更新: 2026-11-03。

## 1. 目的と前提（ユーザーの希望）
- 世界史の勉強用。**個人利用・PC内で完結**。作った動画は公開しない。
- Webページ（世界史の窓 https://www.y-history.net/appendix/wh0101-003.html が出発点）の**文章と画像を抽出**し、**画面も録画**して、その両方から解説動画を作る。
- **Claude API は使わない**。要約が必要ならローカルLLM（Ollama）か、本文そのまま。
- 動画生成は**一気通貫にしない**。段階に分け、途中で確認・修正できること。
- 参考のYouTube動画を**あとで共有**してもらい、それをもとに複数の「形式（フォーマット）」を作る予定。まだ未共有。
- 抽出対象は世界史の窓だけでなく、**Wikipedia や他のYouTube動画**も想定（実装済みのアダプタ参照）。
- ユーザーのPCは **Windows（デバイス名 58605）**。Python / ffmpeg が入っているかは**未確認**。

## 2. 成果物の全体像
```
wh-clipper/
  README.md           使い方（ユーザー向け）
  HANDOFF.md          このファイル
  extension/          Chrome拡張 (Manifest V3) v0.2.0
    manifest.json
    popup.html / popup.js        操作画面・経過表示
    background.js                サービスワーカー：状態機械（抽出→録画→保存・連続取り込み）
    offscreen.html / offscreen.js  tabCapture録画(MediaRecorder)・Blob生成・生存信号
    extractor.js                 ページ内注入：サイト別アダプタで抽出＋要素の表示時刻の記録
    picker.js                    本文範囲の手動指定（Shadow DOMのUI）
  tools/make_video.py  動画生成（plan / voice / render の3段階）
```
別に、アーティファクト「動画フォーマット管理」（形式の管理画面）がある（後述）。

## 3. Chrome拡張の仕様
### 3.1 モード
- 抽出＋録画（自動スクロール）：抽出 → 指定速度でスクロールしながら録画 → 最下部で自動停止・保存
- 抽出＋録画（手動）：自分でスクロール、「停止して保存」で終了（連続取り込みは不可）
- 抽出のみ
- 連続取り込み：「次へ」リンクをたどって最大N（既定10、上限50）ページ。**同一オリジンのみ**。初回にそのサイトの `optional_host_permissions` を要求。結果は1フォルダ・録画1本にまとめる。
- YouTubeの動画ページは常に「抽出のみ」（録画しない）。

### 3.2 抽出（extractor.js）— サイト別アダプタ
| id | 本文root | 備考 |
|---|---|---|
| y-history | `div.card` | 本文は `<p class="midasi">`（見出し）＋ `<br>` 区切りの素テキスト＋`<a>`/`<b>`。`.prev-next .next a` が「次へ」 |
| wikipedia | `#mw-content-text .mw-parser-output` | 脚注・navbox・infobox等を除外、「脚注/関連項目/外部リンク/参考文献」の見出しで終了。画像は srcset の最大、`sourcePage`（ファイルページURL）を記録 |
| generic | article/main 等から最長のもの | `a[rel~=next]` を「次へ」に |
- 走査方法：root配下をDOM順に歩き、ブロック要素の境界・`<br>`で段落を切る。`h1-6`/`.midasi`→heading、`figcaption`/`.komoji`/`.thumbcaption`→caption、`li`→list、`.lead`→paragraph(lead)。画像は幅・高さとも100px未満を無視。lazy画像は読み込みを待つ。
- 手動指定（picker.js）：クリック＝root、Alt+クリック＝除外、↑↓＝範囲の広げ縮め、Esc＝終了。`chrome.storage.local.wh_overrides[hostname] = {root, exclude[]}` に保存し、extractorが優先して使う。
- **時刻記録**：各ブロックが画面の上から70%より上に入った最初の時刻（epoch ms）を100msごとに記録（Range.getBoundingClientRect）。保存時に録画開始からのmsへ変換して `t` に入れる。

### 3.3 YouTube（background.js の `ytCollectMain` / `buildYouTubePage`）
- ページのMAINワールドで `#movie_player.getPlayerResponse()` から title/author/shortDescription/captionTracks、`ytInitialData` を再帰探索して `macroMarkersListItemRenderer`（チャプター）を取得。概要欄の「0:00 タイトル」形式の行もチャプターとして解釈。
- 字幕は `captionTracks[].baseUrl + '&fmt=json3'` をfetch。**実ページ（jNQXAC9IVRw）では本文が空で返った**（YouTube側の制限の可能性）。補助として画面の「文字起こし」ボタンを開いて読む処理を入れたが**未検証**。空なら概要欄とチャプターのみ保存し、`pages[].meta.transcriptStatus` に結果を入れる。
- チャプターごとの見出し（level2、`videoTime`付き）＋字幕を約200字の段落に分割。

### 3.4 録画（offscreen.js / popup.js / background.js）
- **`chrome.tabCapture.getMediaStreamId` は popup 内で呼ぶ**（activeTab権限のため）。streamIdをbackgroundへ渡し、offscreen文書の `getUserMedia({chromeMediaSource:'tab',...})`→`MediaRecorder`（vp9/opus、6Mbps、1秒ごとに分割）。
- タブ録画中は元のタブが無音になるため、AudioContextで音を流し続ける。
- ワーカーが眠らないよう、offscreenが20秒ごとに `hb` を送信。オフスクリーンは `reasons: ['USER_MEDIA','BLOBS']`。
- 異常時（映像トラック終了・MediaRecorderエラー・タブ閉鎖）は `rec-error`/`finishJob` で**そこまでを保存**。
- ジョブ状態は `chrome.storage.session.job`（phase: starting/extracting/scrolling/between/loading/settling、`finishing` フラグ）。次ページ移動は `tabs.onUpdated(complete)` で再開。バッジ：REC / OK / !。
- 保存：`chrome.downloads.download` で `ダウンロード/WorldHistoryClips/<スラッグ>[_Np]/` に、`images/NNN_名前.拡張子`、`recording.webm`、`page.json`。page.jsonはoffscreenで作ったBlob URL（失敗時はdata URL）。SVGは拡張子をpngにしている（要検証：実体はSVGのまま保存される可能性）。

### 3.5 page.json のスキーマ
```jsonc
{
  "title": "...", "url": "...", "capturedAt": "ISO",
  "viewport": {"width":1280,"height":720} | null,
  "recording": {"file":"recording.webm","durationMs":123456,"mode":"auto","speed":120} | null,
  "pages": [{"url","title","site","blockFrom","blockTo","meta"?}],   // 連続取り込み時は複数
  "blocks": [
    {"id":"p1-b001","type":"heading","level":3,"text":"...","t":1234},   // t=録画開始からのms（録画時のみ）
    {"id":"p1-b002","type":"paragraph","text":"...","lead":true,"t":1234},
    {"id":"p1-b007","type":"image","src":"https://...","alt":"","width":718,"height":771,"file":"images/001_x.jpg","sourcePage":"...","t":...},
    {"type":"caption"|"list", "text":"..."}
    // YouTubeのみ: "videoTime"(動画内ms), "fromDescription"
  ]
}
```
複数ページ時は、見出しで始まらないページの先頭に `pageStart: true` の合成見出し（ページタイトル）を挿入。

## 4. 動画生成（tools/make_video.py）
Python 3 + Pillow + ffmpeg。3段階：
1. `plan <folder> [--max-chars 90] [--ollama MODEL --summary-sentences 3] [--prefer-images]` → `scenes.json`
   - 見出しごとに場面。本文を約90文字（文末「。」優先、長文は「、」）のカットに分割。
   - 各カットに録画区間 `start_ms/end_ms` を割り当て。見出しの `t` が欠けた所は前後から補間、時刻が近い（300ms以内）見出しは次に時刻が変わるまでの区間を文字量で按分（実データで重なりが出たための修正）。
   - `source`: recording（録画あり）/ image / card。
2. `voice <folder> [--speaker 3] [--voicevox http://127.0.0.1:50021] [--no-voice]` → `_work/audio/*.wav`、`scenes.json` に `duration` と `audio_hash`（本文を直したカットだけ再生成）。VOICEVOXに繋がらなければ無音（6文字/秒、最低2.5秒）。
3. `render <folder> [--scene N] [--out x.mp4] [--font ...]` → `video.mp4`（1920x1080/30fps）。
   - 録画は初回に30fpsのmp4へ変換（MediaRecorderのwebmは可変フレームレート・シーク不安定のため）。
   - recordingカット：録画の該当区間を音声長に合わせて速度調整（0.6〜2.5倍の比率で、足りなければ最終コマ保持）、上に見出し帯、下に字幕帯をPILで作ったRGBA PNGでoverlay。imageカット：抽出画像を中央、注釈付き。
   - 日本語フォントは Windows（meiryo, YuGothM）/ Mac / Noto CJK の順に探索、`--font` で指定。
- `scenes.json` は**手で編集する前提**（text/source/start_ms/end_ms の変更、カットの削除・並べ替え）。

## 5. アーティファクト「動画フォーマット管理」
- URL: https://claude.ai/artifact/A9vj1kGR1iKLHCwZMtej3J / slug `4a214e91-d88d-4a72-be59-4b9eab4650e5`
- 宣言した機能：`db`（既定のルール）、`downloads`。形式は db の `formats/<id>` に保存。初期データ `formats/fstandard01`（「標準（録画＋字幕）」）。
- 形式の項目：name, description, references[{url,title,at,note,mimic[]}], layout(rec-main/image-main/split/card), aspect(16:9/9:16/1:1), sourceDefault, headingSize, subtitleSize, subtitlePos, maxChars, bgColor/accentColor/textColor, speaker, speakerLabel, summarize(none/ollama), ollamaModel, summarySentences, notes。
- **現スクリプトが反映できるのは**：maxChars(`--max-chars`)、sourceDefault=image(`--prefer-images`)、speaker(`--speaker`)、summarize(`--ollama`)。レイアウト・縦横比・色・文字サイズ・字幕位置は**未対応**（画面に「参考動画をもとに今後対応」タグ）。
- 今後：JSON書き出し → `make_video.py --format 形式.json` で読み込む機能を追加予定。アプリ化する場合も、このJSONが引き継ぎ用のデータ形式になる。

## 6. 検証状況
確認済み
- 世界史の窓「メソポタミア」で抽出（見出し・本文・地図画像・注釈が正しい順序）。Wikipedia「メソポタミア」で抽出（見出し17・段落47・画像7・キャプション7、脚注混入なし）。
- YouTube実ページでタイトル・概要・チャプター3件の取得。
- Playwright（`--load-extension` + `--headless=new`）で拡張を読み込み、3ページの擬似サイトで連続取り込み（抽出のみ／自動スクロール）、時刻 `t` が全ブロックに付き単調増加、画像保存の呼び出しまで確認。範囲指定のクリック・Alt除外・↑キー・Esc・outline復元・抽出への反映を確認。
- make_video.py：サンプル（実ページ由来JSON＋ダミー録画）で plan→voice(--no-voice)→render（1場面／全体）まで、フレームの目視確認。文章を直して voice 再実行で差分のみ再生成も確認。

**未検証（要・実機のChrome）**
- `tabCapture` による実際の録画（権限・ストリーム・音声・webmの再生可否）。サンドボックスのChromiumでは再現できない。テストでは offscreen の録画部分をモックしている。
- 連続取り込みで実在サイト（世界史の窓の「Next▶」）をたどる一連の動作。
- YouTubeの字幕テキスト取得（実ページでは空）。「文字起こし」ボタン経由の補助処理。
- VOICEVOXでの読み上げ、Ollamaでの要約。
- PCにPython/ffmpegがあるか、Windowsでの `make_video.py` 実行（フォントパス・パス区切り）。
- popupのUI（開いた状態での見た目・操作）。

## 7. 既知の課題・次にやること
1. 実機のChromeに拡張を読み込み、世界史の窓のページで自動スクロール録画を1回通す（`chrome://extensions` → デベロッパーモード → パッケージ化されていない拡張機能を読み込む）。録画が再生できるか、`page.json` の `t` と映像のずれを確認。
2. その出力で `plan → voice → render --scene 1` を試し、字幕の長さ・録画区間の割り当てを調整。
3. ユーザーが共有する参考YouTube動画をもとに形式を作り、アーティファクトに登録 → make_video.py に `--format` を実装（レイアウト・色・サイズ・字幕位置・縦横比）。
4. SVG画像の保存（Wikipedia）：拡張子をpngにしているが中身はSVGのまま。PNG変換または `.svg` 保存＋render側でラスタライズが必要。
5. 範囲指定の自動セレクタは、クラス/IDが無いページで脆い（`:nth-of-type` 依存）。
6. 録画音声（タブの音）は動画に使っていない（ナレーションのみ）。
7. 複数ページ録画時の巨大ファイル対策（現状メモリ上のBlob）。長時間録画の分割保存は未実装。

## 8. 作業環境メモ（再現用）
- 拡張のテスト：Playwright で `chromium.launchPersistentContext(dir, {headless:false, args:['--disable-extensions-except=EXT','--load-extension=EXT','--headless=new']})`。サービスワーカーは `ctx.serviceWorkers()`。テスト時は manifest に `host_permissions:["<all_urls>"]` を足し、background の `const toOffscreen` を `var` に変えて、`toOffscreen` / `ensureOffscreen` / `chrome.downloads.download` を差し替えた（本番コードには入れていない）。
- 擬似サイト：`/tmp/site/p1..p3.html`（article、`rel="next"`、画像、見出し）を `python3 -m http.server` で配信。（セッション限りで保存していない）
- フォーマット管理画面のテスト：`window.claude` をスタブして Playwright で表示確認。
- 注意：データベースの snapshot の値は凍結されている（編集前に structuredClone）。`artifact` 機能（自己再公開）ではなく `db` を採用した理由は、Claude側からも形式を読めるため。

## 9. 補足（最初の雑談）
- 最初に、YouTubeチャンネル「名探偵の反応集」の月間収益の概算を相談された（登録者1.86万人、動画278本、直近30本の合計約209万再生。RPM100〜300円と仮定し、活発期で月8〜30万円、投稿停止中の現在で月1〜6万円という目安）。本作業とは無関係。
