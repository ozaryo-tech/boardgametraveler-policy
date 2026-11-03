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

## 次にやること
`wh-clipper/HANDOFF.md` の「7. 既知の課題・次にやること」を参照。
