# このリポジトリについて

- ルートの `index.md` / `README.md` は「ボードゲーム・トラベラー」のプライバシーポリシー（公開用）。**動画制作の作業では触らない。**
- `wh-clipper/` は個人学習用の世界史解説動画づくりのツール一式（公開しない・PC内完結）。
  詳細な経緯と仕様は `wh-clipper/HANDOFF.md`、使い方は `wh-clipper/README.md`。

## 動画制作の方針（ユーザーの希望）
- Claude API は使わない。要約が必要なら Ollama か本文そのまま。
- 一気通貫にせず、段階（plan → voice → render）に分け、途中で確認・修正できるようにする。
- `scenes.json` は手編集前提。ユーザーの指示はここに反映する。
- 参考YouTube動画は後日共有予定。それをもとに「形式」を作り、`make_video.py --format` を実装する。
- ユーザーの環境は Windows。Python/ffmpeg の有無は未確認。

## 動画の作り方（現行）
- 画面録画方式（make_video.py）より、台本からの「歴史画像＋動き」方式（`wh-clipper/tools/make_doc_video.py`）を優先する。使い方は `wh-clipper/README.md` の末尾。
- 台本: `wh-work/scripts/<テーマ>/ch*.md`（ナレーション／左下注記／画面メモ）。「説」や未確認の内容は、左下注記として画面に小さく出す。
- 画像はパブリックドメインを使い、出典を `visuals.json` と `*_credits.txt` に残す。
- 資料が無い場面は、画像生成AIのイラスト（PC内のStable Diffusion系 → Gemini の二段構え）＋2.5D視差で動かす。図形を組み合わせた絵は使わない。`make_illustrations.py` と `illustrations.json`。必要な数カットだけAI動画（画像→動画）に差し替える。
- サムネイル: `wh-clipper/tools/make_thumbnail.py`、型は `wh-work/formats/thumbnail_style_mond.md`（案A採用）。

## 次にやること
`wh-clipper/HANDOFF.md` の「7. 既知の課題・次にやること」を参照。
