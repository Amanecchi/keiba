# keiba

過去の条件別成績を検索し、今回の単勝・複勝オッズから期待値を確認する個人PC向けツール。自動取得の検証CLIを作成済みです。検索・分析の本体は未実装です。

## 自動取得の検証

[実行手順・判定基準](docs/acquisition_probe.md) を参照してください。Python 3.12以降で実行します。

仮想環境で依存をインストールした後、次のコマンドが年度一覧から対象レースを探し、結果・払戻・馬情報を取得します。

```console
python -m pip install -r requirements-dev.lock -e .
jra-collect --years 2016,2021,2025
```

原本・抽出結果・所要時間と失敗理由は `work/acquisition/` に保存します。合成HTMLの自動テスト44件は合格しました。11月5日のクラウド実行は接続プロキシで停止し、JRA原本0件です。現行サイトでの自動取得の成否・全件取得時間は未判定です。

## 設計・実装計画

- [基本設計書](docs/basic_design.md)
- [実装計画・番号付き作業フロー](docs/implementation_plan.md)
- [JRA公式：HTML以外の取得形式の再調査](docs/jra_official_formats.md)
- [無料データ取得方式：選択前の5案比較](docs/data_source_options.md)
- [作業1：データ取得元調査](docs/data_source_research.md)
- [項目対応表](docs/data_field_mapping.md)
- [取得範囲一覧](docs/data_coverage.md)
- [作業進捗](docs/progress.md)
- [初期受け入れテスト仕様](tests/analysis_test_cases.md)

履歴は最低10年以上、20年は目安。取得元はユーザー指定でJRA公式に確定しました。PDF/OCR・有料サービスは使いません。自動取得の実証を本体実装へ進む条件とし、成立しなければ取得方式の比較へ戻ります。原本・発売中オッズの実証は未完了です。
