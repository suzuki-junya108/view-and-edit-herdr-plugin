# View and Edit — herdr プラグイン

> A [herdr](https://herdr.dev) plugin that previews, edits, and browses files in a popup:
> text, Markdown, HTML, images, video, audio, PDF, Office documents, CSV/JSON, SQLite and more.
> **macOS only. The interface is in Japanese.**

herdr の画面に出ているファイルのパスから、そのファイルをポップアップで開いて
**見る・編集する・フォルダを見て回る** ためのプラグインです。
テキストエディタや Finder に切り替えずに、herdr の中で完結します。

- **ファイルブラウザ** — 左に一覧、右にプレビュー。絞り込み・新規作成・名前変更・ゴミ箱への移動
- **ビューア** — 画像や動画も端末の中に表示。表・スライド・ページは ← → で切り替え
- **内蔵エディタ** — メモ帳と同じキー操作（Ctrl+S で保存、Ctrl+Z で元に戻す）。
  Shift_JIS などの元の文字コードと改行のまま保存し、ほかで書き換えられたファイルは確認してから保存します

キーボードでもマウスでも使えます。画面のいちばん下に今使える操作が出ていて、クリックでも押せます。

**はじめての方は [操作ガイド](docs/guide.md) をどうぞ。** 開き方・画面の見方・やりたいことごとの手順・困ったときの対処をまとめています。

## 必要なもの

| もの | 用途 | 備考 |
|---|---|---|
| macOS | 必須 | Linux / Windows は未対応 |
| [herdr](https://herdr.dev) 0.9.0 以上 | 必須 | |
| Python 3.9 以上 | 必須 | macOS 標準の `python3` で動きます。初めて使うときに「コマンドライン・デベロッパ・ツール」のインストールを求められたら入れてください（`xcode-select --install`） |
| 画像を表示できる端末 | 画像・動画・PDF などの表示 | Kitty graphics に対応した端末（Ghostty、kitty、WezTerm など）。動作確認は Ghostty で行っています |
| Google Chrome | HTML の見た目の表示 | 無ければ macOS のクイックルックで代用（文字コード指定のない日本語 HTML は文字化けすることがあります） |
| ffmpeg | 動画の再生、音声の早送り | `brew install ffmpeg`。無くても動画の 1 コマ目の表示と音声の再生はできます |
| poppler | PDF のページ送り・テキスト抽出 | `brew install poppler`。無ければ 1 ページ目だけ表示 |

追加のツールは任意です。入っているものを自動で使います。

## インストール

```bash
herdr plugin install suzuki-junya108/view-and-edit-herdr-plugin
```

インストール前に、実行されるコマンドの一覧が表示されます。内容を確認してから進めてください。
このプラグインにビルド手順はなく、Python の追加パッケージも使いません。

### キーの割り当て（推奨）

ダブルクリックで選んだパスを開くには、`~/.config/herdr/config.toml` に次を書き足します。

```toml
[[keys.command]]
key = "prefix+f"
type = "plugin_action"
command = "view-and-edit.open"
description = "view or edit selected path"
```

保存したら `herdr server reload-config` を実行します（herdr のメニューの `reload config` でも同じです）。
`prefix+f` がほかの操作と重なる場合は、好きなキーに変えてください。

## 使い方

| やりたいこと | 操作 |
|---|---|
| 画面上のパスを開く | パスをダブルクリックで選んでから `prefix+f` |
| リンクになっているファイルを開く | `file://` のリンクを **Ctrl+クリック**（`eza --hyperlink` などが出力するリンク） |
| 今いるフォルダを見て回る | 何も選ばずに `prefix+f` |

`src/main.rs:120:5` のように行番号付きのパスを選ぶと、その行を開きます。
`a/src/main.rs`（git diff の表記）や、引用符・括弧で囲まれたパスもそのまま使えます。

ファイルを開いたあとは `e` で編集、`o` で Finder などの既定のアプリで開きます。
どの画面でも `?`（エディタでは `F1`）でキー操作の一覧が出ます。
手順つきの説明は [操作ガイド](docs/guide.md) にあります。

### 主なキー

**ファイルブラウザ**

| キー | 動作 |
|---|---|
| ↑↓ / j k | 移動（PgUp PgDn / g G でページ・先頭末尾） |
| Enter / → | 開く（フォルダなら中へ） |
| ← / Backspace | ひとつ上のフォルダへ |
| / | 名前で絞り込み（Esc で解除） |
| e / o | 内蔵エディタで編集 / 既定のアプリで開く |
| n / m / r | 新しいファイル / 新しいフォルダ / 名前を変更 |
| d | ゴミ箱に移動（確認あり。Finder から元に戻せます） |
| c / . / ~ | パスをコピー / 隠しファイルの表示切替 / ホームフォルダへ |
| q / Esc | 閉じる |

マウスでは、クリックで選択、ダブルクリックで開く、一覧の先頭の `..` で上のフォルダへ、ホイールで移動できます。

**ビューア**

| キー | 動作 |
|---|---|
| ↑↓ / PgUp PgDn / g G | スクロール |
| ← → | ページ・シート・スライドの切り替え（動画・音声は 5 秒移動） |
| Space | 動画・音声の再生 / 一時停止 |
| Tab | 表示の切り替え（表示 ⇄ ソース など） |
| / n N | 検索 / 次 / 前 |
| e / o / c / r | 編集 / 既定のアプリで開く / パスをコピー / 再読み込み |
| q / Esc | 戻る |

マウスでは、2 行目の表示の名前（`表` `プレビュー` など）と `◀` `▶` をクリックで切り替え、再生の行のクリックで再生・一時停止、ホイールでスクロールできます。

**エディタ**

| キー | 動作 |
|---|---|
| Ctrl+S | 保存 |
| Ctrl+Q / Ctrl+W | 閉じる（未保存なら確認） |
| Ctrl+Z / Ctrl+Y | 元に戻す / やり直し |
| Ctrl+C / X / V | コピー / 切り取り / 貼り付け（選択なしなら行ごと） |
| Ctrl+A | すべて選択 |
| Ctrl+F / Ctrl+G | 検索 / 指定行へ移動 |
| Shift+矢印 | 選択（マウスのドラッグでも可） |
| Tab / Shift+Tab | 字下げ / 字下げ解除 |
| F1 | キー操作の一覧（`?` は文字として入力されます） |

マウスでは、クリックでカーソル移動、ドラッグで選択、ホイールでスクロールできます。

**どの画面でも**、いちばん下の行の案内（`Enter 開く`、`^S 保存` など）と、確認の質問のボタン（`はい` `いいえ` など）はクリックで押せます。

## 対応している形式

| 種類 | 表示のしかた |
|---|---|
| テキスト・ソースコード | 行番号と色分け（Python、JavaScript/TypeScript、Go、Rust、C/C++、Java、シェル、Ruby、SQL、CSS、設定ファイルなど）。UTF-8 / Shift_JIS |
| Markdown | 整形表示（見出し・リスト・チェックボックス・表・コード）⇄ ソース |
| HTML | 見た目 ⇄ テキスト ⇄ ソース |
| 画像 | PNG / JPEG / GIF / WebP / HEIC / TIFF / BMP / SVG など |
| 動画 | 端末内で再生（音声付き） |
| 音声 | 再生・一時停止・早送り |
| PDF | ページ送り ⇄ テキスト |
| Word / Excel / PowerPoint | docx の見た目・テキスト、xlsx の表（シート切り替え）、pptx の見た目・スライドごとのテキスト |
| CSV / TSV | 表 ⇄ ソース |
| JSON / JSON Lines / Jupyter Notebook | 整形表示 |
| SQLite | テーブルごとの表（大きな表も必要な分だけ読み込み） |
| zip / tar | 中身の一覧 |
| そのほか | 16 進表示 |

編集できるのはテキスト系の形式だけです。

## プライバシーと安全性

- ファイルの中身をネットワークに送ることはありません。
- HTML の表示では、ページが外部の画像やスクリプトを読み込もうとしても**すべて遮断**します（Chrome をオフライン状態で起動します）。
- SQLite は読み取り専用で開きます。Office 文書は、悪意のあるファイルでメモリを使い果たさないよう、XML の DTD や極端に大きい中身を読み込みません。
- 削除は完全削除ではなくゴミ箱への移動です。

## 制約

- 画面上のただの文字列（リンクになっていないパス）は、herdr の仕様でクリックだけでは開けません。ダブルクリックで選んでからキーを押してください。
- herdr のポップアップは同時に 1 つだけです。開いている間に別のパスを開こうとすると、その旨の通知が出ます。
- HTML の見た目は、ページの最初の 1 画面ぶんです（全体はテキスト表示やソースで確認できます）。
- Keynote / Numbers / Pages はクイックルックのプレビュー画像だけを表示します。

## 更新・削除

herdr には更新専用のコマンドがないため、同じコマンドでもう一度インストールすると最新版になります。
特定の版を使いたいときは `--ref v0.2.0` のように指定します。

```bash
herdr plugin install suzuki-junya108/view-and-edit-herdr-plugin      # 更新
herdr plugin uninstall view-and-edit                                  # 削除
```

## 開発

```bash
herdr plugin link .                          # 手元のコードを herdr に登録
bin/view-and-edit open [パス]                # herdr を通さずに起動
python3 -m unittest discover -s tests -t .   # テスト
uvx ruff check . && uvx mypy                 # lint / 型チェック
```

## ライセンス

[MIT](LICENSE)
