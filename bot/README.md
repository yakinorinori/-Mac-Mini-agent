# bot/ — Mac mini用 人間らしい自動操作Bot(基盤ライブラリ)

macOS上で、画面のスクリーンショットを元にUI要素を認識し、人間らしい不規則な動作で
マウス・キーボードを操作するための再利用可能な基盤ライブラリです。特定サービス向けの
ロジックは含みません。

## ディレクトリ構成

```
bot/
├── config.yaml          # 全設定・乱数レンジ
├── requirements.txt
├── core/
│   ├── screen.py         # スクショ取得・Retinaスケール補正・権限チェック
│   ├── vision.py          # 要素認識(Claude Vision / OpenCV / Dummy)
│   ├── mouse.py            # HumanMouse: ベジェ軌跡+イージング+オーバーシュート
│   ├── click.py            # HumanClick: 許容範囲内ガウス分布クリック
│   └── keyboard.py        # HumanTyping: ローマ字IME入力+誤打ち+訂正
├── planner/
│   ├── plan.py             # Plan生成(乱数を確定してステップに記録)
│   └── confirm.py         # 表形式表示 + y/n確認
├── safety.py                # 緊急停止・権限チェック・後始末
├── runner.py                 # CLIエントリ(Plan→Confirm→Execute)
├── logs/ , debug/
└── tests/                     # 乱数ロジックの統計的単体テスト
```

## セットアップ

```bash
cd bot
python3 -m pip install -r requirements.txt
```

### macOSの権限設定(必須)

初回実行時に自動チェックされ、未許可の場合はエラーメッセージが表示されます。

1. **システム設定 > プライバシーとセキュリティ > 画面収録** — ターミナル(またはPythonを実行するアプリ)を許可
2. **システム設定 > プライバシーとセキュリティ > アクセシビリティ** — 同上を許可

### Claude Vision APIキー(任意)

環境変数 `ANTHROPIC_API_KEY` を設定すると `ClaudeVisionRecognizer` が有効になります。
未設定の場合は `DummyRecognizer`(常に未検出)にフォールバックし、動作確認はできますが
実際のクリックは行われません。

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

## 使い方

```bash
# 計画のみ表示(実行しない)
python3 runner.py --dry-run

# 通常実行(y/n確認あり)
python3 runner.py

# 乱数だけ再抽選して計画を作り直す
python3 runner.py --replan

# 実行前にアクティブアプリ名を確認し、想定と違えば中止する
python3 runner.py --expected-app "TextEdit"
```

サンプルフロー(`runner.py` 内の `actions`)は TextEdit の本文エリアをクリックし、
打ち間違えを2箇所含めてテキストを入力するデモです。

## 設定項目(`config.yaml`)

| セクション | 説明 |
|---|---|
| `screen` | スクショ保存先など |
| `vision` | 認識プロバイダ(`claude`/`template`/`dummy`)・リトライ回数・信頼度閾値 |
| `mouse` | 移動時間ジッター・ベジェ制御点の揺らぎ・オーバーシュート確率など |
| `click` | クリック許容範囲の割合・ガウス分布のsigma・押下時間 |
| `typing` | 基準WPM・考え込みポーズ確率・打ち間違えの通知遅延範囲 |
| `safety` | 緊急停止(Esc長押し秒数)・想定アプリチェックの有無 |
| `planner` | Plan JSONの保存先・ステップ間待機秒数 |

## Plan → Confirm → Execute の流れ

1. `build_plan()` が乱数(シード指定可)を元に、各ステップの移動時間・クリック許容範囲・
   打ち間違え内容などをすべて確定させた `Plan` を生成する
2. `render_table()` で人が読める表を表示し、同時に `logs/plans/plan_<seed>.json` に保存する
3. `confirm()` で y/n を尋ね、承認された場合のみ **同じPlanとシードで** 実行する
4. クリック対象の実座標は、実行直前のスクリーンショット認識結果から確定する
   (Plan段階では「許容範囲の割合」までしか決まっていないことに注意)

## 安全機能

- `Esc` を1秒以上長押しすると緊急停止する(`SafetyMonitor`)
- 例外発生時は `released_inputs_on_exit()` によりマウスボタン・修飾キーを必ず解放する
- `--expected-app` 指定時、アクティブアプリが想定と異なれば実行前に中止する
- 各ステップの開始・終了・クリック座標・打ち間違え数などを `logs/run_<seed>.jsonl` に記録する

## テスト

乱数ロジック(移動軌跡・クリック座標・打ち間違え・Plan生成)の統計的な単体テストを用意しています。
実機の画面操作は行わないため、GUI権限がない環境でも実行できます。

```bash
cd bot
PYTHONPATH=. pytest tests/ -q
```

## 既知の簡略化・今後の拡張ポイント

- `to_romaji()` のひらがな→ローマ字テーブルは主要な文字のみを収録(拗音・カタカナ・記号の一部は未対応)
- 打ち間違えの `SWAP`(文字入れ替え)はテキスト末尾の文字では発生させず、代替として無処理にフォールバックする
- `scroll` ステップ種別は `Plan` の型定義のみ用意し、実行ロジックは未実装
- `TemplateMatchRecognizer` はテンプレート画像パスの辞書を呼び出し側で用意する必要がある
- 現在のカーソル位置取得(`runner.py` の `current_pos`)は仮実装。実機では実際の取得APIに置き換えること
