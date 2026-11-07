# keiba

JRA公式データを使い、過去の同一レースの条件別成績を検索し、今回の出走馬と単勝・複勝オッズから期待値を確認するWebシステムを開発しています。利用者はURLからブラウザで操作し、取得・保存・分析はサーバー側で行います。

現在は取得検証CLIと解析処理を実装・検証した段階です。Web画面・分析API・永続DB・本番環境はこれから実装します。11/7の追加指定により、計画をWeb運用へ変更しました。[Web構成・配備方針](docs/web_architecture.md) を参照してください。

新しい開発タスクは [HANDOFF.md](HANDOFF.md) から再開してください。現在の実装・検証結果・接続障害の診断・次の手順をまとめています。

## 自動取得の検証（開発・サーバー組み込み向け）

[実行手順・判定基準](docs/acquisition_probe.md) を参照してください。Python 3.12以降で実行します。

以下は開発環境での検証用です。Webサービスの利用者は、最終的にブラウザ上の更新操作を使います。

仮想環境で依存をインストールした後、次のコマンドが年度一覧から対象レースを探し、結果・払戻・馬情報を取得します。

```console
python -m pip install -r requirements-dev.lock -e .
jra-collect --database --trainer-history --years 2016,2021,2025
```

原本・抽出結果・所要時間と失敗理由は `work/acquisition/` に保存します。`--database` は公式の公開フォームを使い、保存版と詳細結果を開催キーで照合し、最終オッズを馬IDで結合します。`--trainer-history` は公式の年付き調教師所属歴から当時所属を補います。省略時は既存の取得範囲を維持します。

11月7日、同一レースの2016〜2025年10開催・159出走で、結果・払戻・最終オッズ・種牡馬・前走・当時所属の検証が通過しました。発売中の出馬表と単複18頭も取得し、提供時刻と値の更新を確認しました。[11/7の実原本検証](docs/research/acquisition/2026-11-07/report.md) を参照してください。自動テスト83件・ruff・pip checkが合格しています。全重賞の網羅性と本体の完成を意味しません。

今回だけを検証する例（入口URLは実行日の公式案内で確認）：

```console
jra-collect --current-only --upcoming-url "https://www.jra.go.jp/keiba/race/101/syutsuba.html"
```

原本と抽出JSONは版別に保持し、再取得や失敗で以前の成功版を失いません。

## 設計・実装計画

- [基本設計書](docs/basic_design.md)
- [Web構成・配備方針](docs/web_architecture.md)
- [分析条件・時点・データ品質の仕様草案](docs/analysis_rules.md)
- [実装計画・番号付き作業フロー](docs/implementation_plan.md)
- [JRA公式：HTML以外の取得形式の再調査](docs/jra_official_formats.md)
- [無料データ取得方式：選択前の5案比較](docs/data_source_options.md)
- [作業1：データ取得元調査](docs/data_source_research.md)
- [項目対応表](docs/data_field_mapping.md)
- [取得範囲一覧](docs/data_coverage.md)
- [作業進捗](docs/progress.md)
- [初期受け入れテスト仕様](tests/analysis_test_cases.md)

履歴は最低10年以上、20年は目安。取得元はユーザー指定でJRA公式に確定しました。PDF/OCR・有料データサービスは使いません。Webの配備先・公開範囲・運用費は比較して決めます。必要項目と発売中オッズを含む自動取得の実証を本体実装へ進む条件とし、成立しなければ取得方式の比較へ戻ります。同系列10年度の少量確認を全重賞10年分の網羅性とは扱いません。
