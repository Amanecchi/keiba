# JRAから環境外DBまでの自動取得手順

更新：2026-11-07 / 実装v0.5.1。手作業のブラウザ巡回・コピーではなく、PythonプログラムによるHTTP通信とHTML解析で取得する。

## 実行しているプログラム

`Runner.run` → `Probe.database_result` → `Client.fetch` の順で、DBに固定した開催一覧を自動処理する。通信は `requests.Session.get()` と `post()`、HTML解析はBeautifulSoup。JRAのログイン・有料データ・PDF/OCRは使わない。

| 段階 | HTTP通信と自動処理 |
| --- | --- |
| 年度一覧 | `GET https://www.jra.go.jp/datafile/seiseki/replay/YYYY/jyusyo.html`。HTMLの表から平地重賞の名称・日付・開催場・距離と結果リンクを抽出する |
| 保存版結果 | 一覧に公開された結果リンクへGET。開催日・競馬場・開催回/日・レース番号を読み、照合用の開催キーを作る |
| 過去結果検索 | 保存版の「過去のレース結果」にあるリンク/`doAction`から公式フォーム値を抽出。`POST /JRADB/accessS.html`、本文 `cname=抽出した値` で送信 |
| 該当年月 | 年/月の選択肢、ページ内の `objParam`、公開された遷移テンプレートを読み、対象年月のフォーム値を決定。同じ `accessS.html` に `cname` をPOST。チェック値を推測せず、既知のテンプレートを認識できなければ停止 |
| 詳細結果 | 年月一覧のリンクから、保存版と日付・競馬場・開催回/日・レース番号が一致する1件を自動選択してGET。着順・馬番・馬ID・払戻等を解析 |
| 確定オッズ | 詳細結果内の対象レースのオッズリンクから公式フォーム値を抽出。`POST /JRADB/accessO.html`、本文 `cname=抽出値`。結果と馬番/馬IDが一致することを確認して結合 |
| 当時所属 | 必要な場合だけ、結果にある調教師ID/公式リンクから `POST /JRADB/accessC.html`。年付き所属歴を解析し、対象日の所属を確認 |
| 馬プロフィール | 結果にある馬IDのプロフィールリンクへGET。当該レースより前の走歴だけを選ぶ。原本が対象日より古い場合は再取得 |
| 直近3走 | プロフィールの公開リンクから各過去走のフォーム値を読み、`POST /JRADB/accessS.html`。開催キー・馬ID・着順を照合し、前走クラス・通過順位からの脚質等を作る |

フォーム通信の実体は、既存の `Client.fetch(..., post_cname=True)` が行う次の処理。値は取得した公式HTMLから抽出したものを使い、手入力しない。

```python
session.post(
    "https://www.jra.go.jp/JRADB/accessS.html",
    data={"cname": published_cname},
    timeout=15,
    allow_redirects=False,
    stream=True,
)
```

通信は直列、JRAへの要求開始間隔は最低2秒。TLS検証を有効にし、403/429では停止する。原本がDBにあれば本文ハッシュを検証して再利用し、JRAへ再要求しない。通常の検索・条件変更・再読込・CSVはDBのみを使用する。

## 保存までの自動化

初回収集は `MirroredStore` を使って実行している。

```text
JRA公式HTML
  → Pythonで自動取得・照合
  → 原本gzip/SHA・出典URL・取得日時を保存
  → 本人限定の環境外D1 DBへHTTPSで自動保存・応答確認
  → 解析結果・旧版を残したスナップショットを同DBへ保存
  → 保存件数を照合し、完了開催のカーソルを同DBへ保存
  → 次の開催へ進む
```

PostgreSQLは現在のワーカーの作業用保存先でもあるが、環境外DBへの確認を済ませず次の取得を続けない。環境外保存の通信・件数確認に失敗したら取得を停止する。再開前に、以前の失敗時に作業用DBへ残った分を環境外DBへ同期してからJRAへの通信を許可する。

- 原本：`source_blobs`。本文のSHA-256で重複排除し、gzipから展開してもSHAを確認。
- 出典：`source_fetches`。URL・GET/POST・取得時刻・本文SHAを保持。
- 開催一覧：`race_catalog`、`catalog_runs`。年度一覧と確認範囲を保持。
- 解析：`analysis_snapshots`、`analysis_races`。旧版を保持し、最新ポインタを更新。
- 進捗：`acquisition_jobs`、`vault_checkpoints`。固定対象・再開位置と環境外保存の確認件数。

個人の認証情報はソース・ブラウザ・公開Gitへ入れない。環境外DBの接続情報は秘密設定/保護した標準入力からプロセスのメモリへ渡す。

## 保存先と復元

外部の本人限定保管先は [競馬データ保管庫](https://keiba-data-vault-20261107-b93d.relaxedbough1.chatgpt.site)。ChatGPTの既存認証にひもづくSites/D1を使用し、Render・Neonの新規アカウント登録は現在のDB保存には不要。

30開催・458出走・原本1,303件の時点で、環境外DBから別の空DBへ全件を読み戻した。原本1,303件のSHA、解析50版の本文、出走データと集計結果が一致した。以後の収集も同じDBへ自動保存している。[検証報告](research/storage/2026-11-07/report.md) を参照。

開発環境が失われると、その環境の収集プロセスは止まる。環境外DBに保存済みのデータ・対象計画・再開位置は残る。新しい実行環境で `RemoteVault.restore_to()` により空DBへ復元し、未完了の開催から再開する。利用者のPCでコマンドを実行する手順にはしない。

この保管先はデータ保護のための稼働コンポーネント。全1,399開催の収集完了、過去分析Webの完成、機能1のユーザー完成確認を意味しない。
