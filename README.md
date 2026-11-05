# keiba

過去の条件別成績を検索し、今回の単勝・複勝オッズから期待値を確認する個人PC向けツール。自動取得の検証CLIを作成済みです。検索・分析の本体は未実装です。

新しい開発タスクは [HANDOFF.md](HANDOFF.md) から再開してください。現在の実装・検証結果・接続障害の診断・次の手順をまとめています。

## 自動取得の検証

[実行手順・判定基準](docs/acquisition_probe.md) を参照してください。Python 3.12以降で実行します。

仮想環境で依存をインストールした後、次のコマンドが年度一覧から対象レースを探し、結果・払戻・馬情報を取得します。

```console
python -m pip install -r requirements-dev.lock -e .
jra-collect --database --years 2016,2021,2025
```

原本・抽出結果・所要時間と失敗理由は `work/acquisition/` に保存します。`--database` は公式の公開フォームを使い、保存版と詳細結果を開催キーで照合し、最終オッズを馬IDで結合します。省略時は既存の保存版取得を維持します。

11月5日22:22（日本時間）、JRAへのHTTPS・TLS検証・HTTP 200を確認しました。同一レースの2016〜2025年10開催・159頭で、結果・単複払戻・全馬の単勝最終オッズを自動取得できました。当時所属と発売中オッズ等は未検証・不足が残るため、作業1は部分完了です。[今回の実原本検証](docs/research/acquisition/2026-11-05/report.md) を参照してください。自動テスト64件とruffは合格しています。

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

履歴は最低10年以上、20年は目安。取得元はユーザー指定でJRA公式に確定しました。PDF/OCR・有料サービスは使いません。必要項目と発売中オッズを含む自動取得の実証を本体実装へ進む条件とし、成立しなければ取得方式の比較へ戻ります。同系列10年度の少量確認を全重賞10年分の網羅性とは扱いません。
