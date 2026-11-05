> 引き継ぎ保存用の複製。以下は原因調査時点の記録で、「コミットを行っていない」はその時点を指す。今回の引き継ぎで証跡をGit保存する。runtime-status.jsonは不要な環境・設定識別子を省略した。作業用の元ファイルは変更していない。

# JRA HTTPS接続の原因切り分け

通信試験日時：2026-11-05 21:51（日本時間）。対象：`https://www.jra.go.jp/`。既存のHTTPSプロキシ・CA設定を保持した読み取り調査のみ実施した。

## 結論

今回観測した失敗は、Codex実行環境の外向きプロキシがCONNECT要求を拒否したもの。JRAのHTTPSサーバーとのTLS接続は始まっておらず、JRAからのHTTP応答は受信していない。JRA側のアクセス拒否を示す証拠ではない。

実行環境の状態APIは `observations_current=true`、`mode=restricted`、`state=enforced`、`presets=[package_managers]` と返した。JRAドメインは有効な許可一覧に含まれず、起動時の `/etc/codex/network-policy.json` も制限モードだった。調査の前後で状態APIを再確認したが同じ値だった。

ユーザーが設定した「すべてのパブリックインターネットへのアクセス許可」と、接続中の実行環境で報告・観測された状態に不一致がある。設定画面そのものと適用処理の履歴は取得できていないため、反映待ち、稼働中インスタンスへの適用漏れ等の具体的な理由は断定できない。

## 段階別の観測

| 段階 | 結果 | 根拠 |
| --- | --- | --- |
| プロキシのDNS解決 | 成功 | `proxy` → `172.31.1.221` |
| プロキシへのTCP接続 | 成功 | `proxy:8080` に接続しHTTP応答を受信 |
| JRA宛てCONNECT | 拒否 | `CONNECT www.jra.go.jp:443 HTTP/1.1` に `HTTP/1.1 403 Forbidden` |
| プロキシのエラー本文 | `Domain forbidden` | 通常のCONNECT要求に対する応答本文16バイトを読み取り |
| JRAとのTLS | 未開始 | CONNECTで停止。curlログにJRAへのClientHello・証明書交換なし、`time_appconnect=0.000000` |
| JRAのHTTPステータス | 未受信 | curlの `http_code=000`。これは実際のHTTPステータスコードではない |

プロキシ応答の `server` は `envoy`。curlの終了コードは56、エラーは `CONNECT tunnel failed, response 403`。`http_connect=403` と、未受信の `http_code=000` を区別する。`ssl_verify_result=0` はTLS未実行のケースでも出るため、JRA証明書の検証成功を意味しない。

## DNS解決の切り分け

OSの `socket.getaddrinfo` では、`www.jra.go.jp`、`jra.go.jp`、比較先の `pypi.org` がすべて `EAI_AGAIN`（errno -3、`Temporary failure in name resolution`）になった。一方、プロキシの名前は解決できた。

HTTPSプロキシを使用する通信では、クライアントはまずプロキシへ接続して宛先ホスト名をCONNECTで渡す。したがってローカルのJRA名前解決失敗と、プロキシでの宛先接続拒否を混同しない。比較先のpypi.orgもローカルDNSでは失敗したが、同じプロキシ経由では接続できた。今回のHTTPS失敗をローカルDNS障害だけに帰すことはできない。プロキシ内部のJRA DNS照会結果は未取得。

## 同じ経路の比較試験

| 測定値 | JRA | pypi.org |
| --- | --- | --- |
| `http_connect` | 403 | 200 |
| TLS | 未開始 | TLS 1.3、証明書検証成功 |
| `http_code` | 000（未受信） | 200 |
| curl終了コード | 56 | 0 |

一般的なプロキシ疎通・TLS機能・CA信頼設定がすべて壊れているという結果ではなく、今回のJRA宛てCONNECTが拒否されている。JRAサーバー自身のTLS設定や、接続許可後のHTTP受け入れ可否はこの試験では評価できない。

## 調査方法と証跡

- `curl -q --verbose --head --connect-timeout 10 --max-time 20 https://www.jra.go.jp/` を既存プロキシ経由で実行し、接続段階・応答ヘッダー・時間を記録。同じ条件でpypi.orgを比較した。
- 通常のHTTPプロキシCONNECTを同じ `HTTPS_PROXY` 宛てに送り、403応答の本文を取得した。200の場合だけTLSへ進む診断処理だったが、今回403のためJRAへのTLS処理は実行されていない。
- OSの名前解決を各ホストについて行い、結果・エラー・所要時間を記録した。
- 環境状態APIと起動時ポリシーを読み取った。保存されたプロバイダー診断記録は `history_status=not_found` で、内部監査ログを追加取得できなかった。
- プロキシ解除、直接接続、IP直指定、別DNS、別プロキシ、TLS検証無効化、設定変更は行っていない。アプリの変更・収集処理・コミットも行っていない。

証跡：

- [JRA curlログ](jra-curl-verbose.txt) / [計測値JSON](jra-curl.json)
- [CONNECT応答と本文](connect.json)
- [DNS確認](dns.json)
- [比較先curlログ](control-pypi-curl-verbose.txt) / [計測値JSON](control-pypi-curl.json)
- [稼働環境の状態API抜粋](runtime-status.json)

追加確認が必要なのは、全許可の設定がこのタスクに接続されている稼働中インスタンスへ適用されているか、その保存・適用履歴。今回は原因調査のみの指定のため、設定変更や迂回は実施していない。
