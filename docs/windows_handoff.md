# WindowsのChatGPTデスクトップ版への引き継ぎ手順

更新：2026-11-08（日本時間） / 段階：**DB書出し・Linuxの空DBへの復元検証は完了。Windowsへのデータ移送・復元は未実施**

最新の移行用ファイルは `keiba-handoff-20261108T122719-JST.zip`（76,320,379 bytes、約76.3MB）。**[本人限定のデータ保管庫](https://keiba-data-vault-20261107-b93d.relaxedbough1.chatgpt.site)へ入り、「移行用ZIPをダウンロード」を押して受け取る。** 前の開発環境内パスは配布URLではなく、受渡方法を修正した。DB本体はGitに含めない。[書出し記録](review/windows_db_export_20261108.json)と[HTTPSでの全件取得検証](review/windows_db_download_20261108.json)を参照。

## 1. 今回確定した方針

- 移行先はWindows PC。そのPCのChatGPTデスクトップ版Codexで開発を続ける。
- DBをPC内に保存する。引き継ぎではDB製品を変えず、現在と同じPostgreSQL 17系へ復元する。
- 完成形はWeb上で動くものを希望するが、必須要件ではない。デスクトップ専用アプリ化も確定していない。
- コードはGitの`work`ブランチをcloneして引き継ぐ。DB・原本はGitに含めず、別の非公開アーカイブで渡す。
- ご本人はフォルダー選択、ファイル受取、必要なインストール・本人認証を担当する。DBのコマンド、復元、検証、環境設定、続きの開発は開発担当のCodexが行う。
- ユーザーの「書き出してください」により、現在のCodex側の書出し・空DB復元・照合を実施済み。次は以下のPC操作に進む。Windowsへの復元・起動成功はまだ確認していない。

最終Web公開とローカルDBの位置づけは別の判断です。外部WebサーバーがPC内DBへ自動的につながるわけではありません。今回はローカルへの保管・開発再開を先に成立させ、公開WebのDB配置・PCの常時稼働・接続方式は引き継ぎ後に評価します。

## 2. 作業の分担と順序

| 順序 | 現在のCodexが行うこと | ご本人が行うこと | Windows側Codexが行うこと |
| --- | --- | --- | --- |
| 1 | 最新指定をコード・資料と照合し、`AGENTS.md`・`HANDOFF.md`・本書・データ所在をGitへ保存 | 手順と分担を確認する | まだ作業しない |
| 2 | 現在のDBから新しいダンプを作り、別の空DBへの復元と内容一致を検証する | まだDB操作は不要 | まだ作業しない |
| 3 | DBアーカイブ、検証基準、チェックサム、再開用指示文と受取方法を提供する | PCへコードをcloneし、アーカイブをGit外の指定フォルダーへ保存する | フォルダーへアクセス可能になったら環境を調べる |
| 4 | 必要な確認事項を引き継ぎ資料へ残す | ChatGPTでローカルフォルダーを開き、必要なアクセスを許可する | 資料・コード・PC環境・ファイルのSHAを確認する |
| 5 | 元DB・外部保管庫を保持する | 不足ソフトの導入で必要なWindowsの許可・本人入力を行う | Python環境・PostgreSQLを準備し、空DBへ復元する |
| 6 | こちらから新たな収集をしない | 検証結果を確認する | 原本・解析版・集計・進捗を照合し、Windows起動を確認する |
| 7 | 元環境を引き継ぎ完了前に破棄しない | 続きの開発を依頼する | ローカルの容量/検索方式を検証し、その後に停止解析の修正と収集再開を検討する |

この会話をデスクトップで開くだけでは、現在のクラウド環境のファイル・DB・実行プロセスがPCへ移ったことにはなりません。正式な引き継ぎ情報はGitとDBアーカイブへ残します。

現在のクラウド実行環境から、ご本人のWindowsディスクへ直接書き込むことはできません。このため、ファイル受取・ローカルフォルダー選択・Windowsの本人権限の操作をご本人の担当とし、以後のコマンド作業をPC上のCodexへ任せます。

## 3. 現在のデータの所在

[所在・件数の確認記録](review/windows_handoff_inventory_20261108.json) は12:11時点の記録です。その後の[書出し完了記録](review/windows_db_export_20261108.json)を最新とします。書出し直前のソースコードは`d4c60af`、アプリ実装版はv0.5.2です。

| 場所 | 内容 | 移行での扱い |
| --- | --- | --- |
| 公開GitHub `Amanecchi/keiba`、ブランチ`work` | ソース、設計、検証結果 | Windowsへcloneする。現在の開発環境の`.venv`はコピーしない |
| 開発環境のPostgreSQL 17.11、DB`keiba_preparation_20261107` | 実際の収集データ・原本・解析旧版・計画・再開位置 | **新しい移行用ダンプの取得元**。DB単位で書き出す |
| 本人限定の[環境外D1保管庫](https://keiba-data-vault-20261107-b93d.relaxedbough1.chatgpt.site) | 原本・解析版・進捗の環境外コピー | 移行完了まで維持する。Windowsの初回起動でサービス用秘密情報を必須にしない |
| 非公開Siteソースの`recovery/postgres-initial.dump` | 以前の30開催時点の退避 | 最新の移行用ファイルとして使わない |
| `work/`内の以前のダンプ・SQLite復元試験 | 過去時点の検証物 | 最新データと取り違えない。Linuxの絶対パスをPCへそのまま使わない |
| `keiba_capacity_20261108`等の容量試験DB | 模擬50万/100万件 | 実データ移行に含めない。必要ならコードから再生成する |

### 保存済みの実データ

| 項目 | 件数・状態 |
| --- | --- |
| 解析済みの最新開催 | 256開催 |
| 出走データ | 3,831件 |
| 原本本文 | 4,543種類、gzip合計62,751,753 bytes |
| 取得記録 | 4,790件 |
| 旧版込み解析 | 276版 |
| 対象開催一覧 | 2016〜2026年、計1,399開催 |
| 保存範囲の内訳 | 2016年128開催・1,917出走、2017年127開催・1,896出走、2025年1開催・18出走 |
| 初回ジョブ | `failed`、255/1,399。次対象は2017-12-28ホープフルS |

2018〜2024年等の対象一覧があることは、その年度の結果を収集済みという意味ではありません。補助走歴には主集計範囲外のページもあります。10年分の整備、全項目の充足、機能1の完成確認は未完了です。

## 4. 現在のCodexが準備するもの

### Gitへ保存するもの

- [AGENTS.md](../AGENTS.md)：最新要件・開発順序・停止条件・データ保全等の継続指示。
- [HANDOFF.md](../HANDOFF.md)：進捗、コードとDBの所在、未解決問題、再開時に読む順序。
- 本書：Windowsでの操作と担当の分担。
- 所在確認、移行用ファイルの作成時点・SHA・復元検証の結果。秘密情報や原本全体を含めない。

### Git外で提供するもの（作成・検証済み）

```text
keiba-handoff-作成日時/
  database/keiba_preparation.dump
  manifest.json
  checksums.sha256
  verification/baseline.json
  RESTORE.md
```

必要な保管用Workerのソース・失敗解析の記録がある場合は、秘密情報を除いた補助資料として加えます。仮想環境、`.git`の認証設定、DB接続パスワード、サービス用トークン、模擬データDBは含めません。

ダンプは`pg_dump --format=custom --no-owner --no-acl`による**単一DBの論理バックアップ**とします。原本がDB内に入っているため、別途JRAから取り直す必要はありません。PostgreSQL 17系の空DBへ`pg_restore --no-owner --no-acl --exit-on-error`で復元し、次を確認してから提供します。

1. 9テーブルの件数、開催・出走・原本・解析版、年度別保存範囲。
2. 原本4,543件の展開後SHA-256、解析版の本文と識別子。
3. 同じ保存データによる条件判定・集計とデータ版。
4. 固定した1,399開催の計画、255という再開位置、停止状態。
5. 移行用アーカイブのサイズ・SHAと、ソースコードのコミット。

現在の実DBから新しいダンプを作成し、9テーブル全内容・原本4,543件・解析276版・5通りの集計・計画/停止位置が一致することを確認しました。元DBの内容も変わっていません。ZIPのCRCと全同梱ファイルのSHAも確認済みです。Windows側では同梱のRESTORE.mdに従い、空DBへ復元後、scripts/verify_handoff.pyとverification/baseline.jsonで照合します。

ファイルは本人限定保管庫のR2へ保存し、認証付きHTTPSでダウンロードできるようにしました。実際に76,320,379 bytes全件を取得し、元ZIPとSHA-256が一致、ZIPのCRCも正常であることを確認しています。HEADのファイル名・サイズ、途中/末尾のRange取得、未ログイン時401も確認済み。Sitesの所有者1名・外部訪問者0名の設定を維持しています。ユーザーPCでの受信自体は未確認です。開発環境内の`/workspace/...`をWindowsのダウンロードURLとして使わないでください。

## 5. ご本人の具体的なWindows操作

書出しと空DBへの復元検証は完了しています。最新のZIPを受け取り、以下を実施します。

### 5.1 Gitを用意してコードをcloneする

1. 既にGitが使える場合はインストール不要。不明ならWindowsのスタートメニューで「PowerShell」を開き、`git --version`と入力する。
2. 未導入なら[Git for Windows公式](https://git-scm.com/install/windows)からPCに対応するインストーラーをダウンロードして起動する。コマンドラインからGitを使える設定を選ぶ。インストール後はPowerShellを開き直す。
3. PowerShellへ次の2行を貼り付ける。既に同じ場所に`keiba`がある場合は実行を止め、上書きせず担当Codexへ伝える。

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\keiba-work" | Out-Null
git clone --branch work --single-branch https://github.com/Amanecchi/keiba.git "$env:USERPROFILE\keiba-work\keiba"
```

4. 次で`work`が表示されることを確認する。

```powershell
git -C "$env:USERPROFILE\keiba-work\keiba" branch --show-current
```

公開リポジトリのclone自体に、新しいGitHubアカウントやDBアカウントの作成は不要です。後で変更をpushする場合の本人認証は別の操作です。

### 5.2 DBファイルを受け取る

1. [本人限定保管庫](https://keiba-data-vault-20261107-b93d.relaxedbough1.chatgpt.site)をブラウザで開く。サインインが出た場合は、この会話と同じChatGPTアカウントで入る。「WindowsへのDB移行」欄の**「移行用ZIPをダウンロード（76.3 MB）」**を押す。ファイル名が`keiba-handoff-20261108T122719-JST.zip`であることを確認する。
2. エクスプローラーのアドレス欄に`%USERPROFILE%`と入力して開き、`keiba-data`フォルダーを作る。
3. ZIPの場合は右クリック→「すべて展開」で、`keiba-data`の中へ展開する。
4. 展開したフォルダーのパスをWindows側Codexへ伝える。**DBの中身やパスワードをチャットへ貼る必要はない。**

DBファイルを`keiba-work\keiba`のGitフォルダーへ入れません。ダウンロードできない場合はその時点で知らせ、空DBのまま取得をやり直さないでください。

### 5.3 デスクトップ版でローカルの作業フォルダーを開く

1. ChatGPTデスクトップアプリを開き、サインインする。必要ならメニューから更新を確認する。
2. メニューで**Codex**を選ぶ。
3. **Add new project／新しいプロジェクトを追加**、または`Ctrl+O`で、`%USERPROFILE%\keiba-work\keiba`を選ぶ。
4. 実行場所は**This computer／このコンピューター（Local）**を選ぶ。Cloudは選ばない。最初の移行確認ではWorktreeも作らず、cloneしたフォルダーを使う。
5. ファイルを読めることを確認したら、DBファイルを置いた`keiba-data`も作業対象へ追加する。プロジェクトメニュー→**Edit project／プロジェクトを編集→Add folder／フォルダーを追加**。Git操作・`AGENTS.md`の基準になる主フォルダーは`keiba-work\keiba`のままにする。
6. 次節の再開用指示文を貼り付ける。ツールのファイルアクセス・Windowsの権限が必要と表示された場合は、対象がこの作業に必要なものか確認して許可する。

メニュー名はアプリの言語・版で変わることがあります。「通常のChat」「Cloud」のまま、PCのフォルダーを操作できると判断しないでください。該当項目がない場合は、アプリ名・版・表示されている選択肢を担当へ伝えます。[公式のWindows説明](https://learn.chatgpt.com/docs/app/windows)、[実行場所の選択](https://learn.chatgpt.com/docs/environments/modes)、[フォルダー追加](https://learn.chatgpt.com/docs/projects) を確認済みです。

### 5.4 不足ソフトの導入と認証

Windows側Codexに、まずGit・Python・PostgreSQL・空き容量を調べさせます。既存のソフトやDBへ無断で上書きしません。必要な場合に限り、次を導入します。

| ソフト | 理由 | ご本人の操作 |
| --- | --- | --- |
| Python 3.12系 | 既存の収集・分析コードを動かす | 担当が案内する公式インストーラーを起動し、Windowsの許可画面が出たら確認して進める |
| PostgreSQL 17系 | 今のDBを変換せずPCへ復元する | [公式Windows案内](https://www.postgresql.org/download/windows/)からEDBの対応する17系インストーラーを開く。Server・Command Line Tools、必要ならpgAdminを導入する |
| DBの本人認証 | ローカルDBを無認証で公開しないため | インストーラーのパスワード欄へPC上で入力して保管する。後の接続も担当が用意する保護したローカル入力・秘密設定を使い、チャットへ送らない |

PostgreSQLのデータ保存先はGitフォルダー外を使い、既存の別DBやOneDrive等の同期フォルダーへ不用意に変更しません。ポート等が競合する場合は担当が調べて別の設定を案内します。Stack Builderの追加製品や、新規有料サービスを導入する必要はありません。

Python依存のインストール、DBの作成・復元、認証情報の保護、コマンドの実行はWindows側Codexの担当です。ご本人へSQLの作成や手作業でのデータ投入を求めません。

## 6. Windows側Codexへ貼る再開用指示文

```text
競馬データ分析システムの開発を引き継いでください。

最初にAGENTS.md、HANDOFF.md、docs/windows_handoff.md、
docs/system_design_review.md、docs/database_feasibility.mdと実際のコードを読んでください。

最新方針はWindows PC内にDBを保存して開発を続けることです。
完成形のWeb上での利用は希望ですが必須ではありません。
Web公開やデスクトップ専用化、DB製品変更を最初から確定しないでください。

このPC上で実行していること、Gitブランチとコミット、データ受取フォルダー、
移行用manifestとSHA、Python/PostgreSQL/空き容量を確認してください。
現在のPostgreSQL 17系へ、同じ種類の空DBを新しく用意して復元してください。
既存DBへ上書きせず、元のダンプも残してください。

移行前の基準は256開催・3,831出走・原本4,543件・解析276版、
固定計画1,399開催、完了位置255、状態failedです。
manifestの作成時点の値を正とし、模擬データを実データへ混ぜないでください。
全原本SHA、解析版、代表集計、対象計画、再開位置を照合してください。
必要な認証はPC上で保護して扱い、チャットやGitへ秘密値を出さないでください。

Windows起動は未検証です。GunicornをWindowsへそのまま流用せず、
依存、Web配信、localhostの認証とCookie/HTTPSを検証してください。
移行・ローカル容量/速度/保全の確認まではJRAへの取得を再開しないでください。

初版はJRA平地重賞全体の最低10年以上、将来は未勝利戦等にも拡張します。
機能1の過去分析を完成して私が確認した後に、機能2の今回への適用へ進みます。
まず引き継いだ内容と復元・検証結果、未完了事項を報告してください。
```

## 7. 引き継ぎ完了の判定

- Windows上の対象フォルダーでGitの履歴・`work`ブランチ・最新資料を確認できる。
- DBと原本がPCの永続保存先にあり、アーカイブ・manifestのSHAが一致する。
- 空DBへの復元と、原本・解析版・集計・計画・停止位置の検証が通る。
- Windows上で既存機能を起動・確認できる。未対応があれば、復元完了とアプリ起動未完了を区別して記録する。
- PCの再起動後もデータが残り、通常検索でJRAへの通信を発生させない。
- 次の開発作業がローカル容量/検索の検証、停止解析の修正、全対象整備、機能1の完成提示の順に整理されている。

現在の資料作成だけで移行完了・機能1完成・DB全件整備とは扱いません。Linux内でダンプを復元できても、Windowsへの復元済みとは報告しません。

## 8. 公式資料

- [ChatGPTデスクトップのWorkとCodex](https://help.openai.com/en/articles/20001275-chatgpt-work-and-codex)
- [Windows版の操作](https://learn.chatgpt.com/docs/app/windows)、[Local/Cloudの選択](https://learn.chatgpt.com/docs/environments/modes)、[プロジェクトのフォルダー](https://learn.chatgpt.com/docs/projects)
- [PostgreSQL Windows版](https://www.postgresql.org/download/windows/)、[pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html)、[pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html)
- [Git for Windows](https://git-scm.com/install/windows)
