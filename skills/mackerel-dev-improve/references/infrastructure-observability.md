# DB・Web・ホストを条件付きで観測する

アプリの遅さや失敗をコードだけに帰属させず、DB、Webサーバー、OSの状態を同じ時間帯で確認する。対象が存在する経路だけを追加する。既存のmackerel-agentやプラグインが同じホスト・DBを収集している場合は、まずその系列を使い、二重収集しない。

## 追加する条件と送信経路

| 条件 | 同梱設定・収集対象 | Mackerel側 |
|---|---|---|
| Collectorが観測対象ホスト上で動く | [ホストoverlay](../assets/otel-host.example.yaml)：CPU、メモリ、filesystem、disk、network | ラベル付きメトリックス |
| PostgreSQLを利用し監視接続がある | [PostgreSQL overlay](../assets/otel-postgresql.example.yaml)：DB・サーバー統計 | ラベル付きメトリックス |
| Webサーバーが後述のJSONアクセスログを出せる | [Webログoverlay](../assets/otel-web-logs.example.yaml)：ファイル追従、時刻・重要度・有効なtrace contextの読取 | ログ検索、関連するAPMトレース |

これらは[基本Collector設定](../assets/otel-collector.example.yaml)への追加ファイルで、Contrib 0.163.2を対象とする。独立したpipeline名を使い、アプリのresourceや基本のOTLP入力を置換しない。

```sh
otelcol-contrib validate --config skills/mackerel-dev-improve/assets/otel-collector.example.yaml --config skills/mackerel-dev-improve/assets/otel-host.example.yaml --config skills/mackerel-dev-improve/assets/otel-postgresql.example.yaml --config skills/mackerel-dev-improve/assets/otel-web-logs.example.yaml
```

必要なoverlayだけを指定する。構文検証後、実際に収集・送信する場合は `validate` を外して起動する。基本設定の `MACKEREL_APIKEY`、`OTELCOL_INSTANCE_ID` に加えて、使用するoverlayの環境変数を非空で設定する。

| overlay | 必要な環境変数 |
|---|---|
| 共通 | `INFRA_SERVICE_NAMESPACE`、`INFRA_ENVIRONMENT`（例：development） |
| ホスト | `INFRA_HOST_ID`：観測対象ホストの安定した一意ID |
| PostgreSQL | `POSTGRESQL_MONITOR_ENDPOINT`（host:port）、`POSTGRESQL_MONITOR_USERNAME`、`POSTGRESQL_MONITOR_PASSWORD`、`POSTGRESQL_MONITOR_DATABASE`、`POSTGRESQL_MONITOR_INSTANCE_ID` |
| Webログ | `WEB_ACCESS_LOG_PATH`（対象ファイルの絶対パス）、`WEB_SERVICE_NAME`、`WEB_INSTANCE_ID` |

**MackerelのホストメトリックAPIと、OTLPのラベル付きメトリックスは別の保存経路。** `host_metrics` receiverの出力がホストメトリックAPIに現れるとは仮定しない。ラベル付き系列はMackerelのクエリグラフで確認し、必要なagent証拠は収集元の値や保存した成果物も併用する。既存mackerel-agentのホストメトリックスはそのホストIDとAPIで照合する。

## ホスト：飽和・欠測とアプリへの影響

CPU使用率だけでなく、メモリ使用状態、filesystem空き容量、disk I/O量・時間、network送受信・エラーを並べる。OS・権限により取れない指標は欠測とする。コア別使用率を合計して100%を超えた値をホスト使用率と呼ばない。ネットワーク・diskの累積counterはrate化し、再起動とcounter resetを考慮する。

設定例はホスト上のCollector用。コンテナ内のCollectorから見えるfilesystem・CPU上限はホスト全体とは限らない。コンテナ用のroot_path、必要なread-only mount、cgroup制限を確認し、host.idを実際の観測対象に対応させる。全プロセスの収集やホスト全体のmountを自動追加しない。

## PostgreSQL：接続・ロック・I/O

専用の読み取り用監視ユーザーを使い、対象DBへの接続と、必要な統計viewを読む権限を管理者と確認する。receiverは少なくとも `pg_stat_database` のSELECTを必要とする。アプリの書込みユーザーやsuperuserを流用せず、このSkillからユーザー作成・権限付与・拡張導入は行わない。

設定例はTLSと証明書検証を有効にする。独自CAが必要なら `tls.ca_file` を追加する。非TLSのローカルDBは対象接続がloopback等の意図した開発範囲であることを確認し、専用設定で変更する。例のまま接続できない理由を証明書検証の無効化で隠さない。

| 指標 | 判断と注意 |
|---|---|
| `postgresql.backends`、`postgresql.connection.max` | backend数と設定上限。backendsには非client workerも含まれるため、その単純比をclient pool使用率にしない |
| `postgresql.database.locks`、`postgresql.deadlocks` | ロック状況とdeadlock増分。ロック件数にはreceiver自身の接続も含む。待ち時間・ブロック件数とは別 |
| `postgresql.blks_hit`、`postgresql.blks_read` | 同じDB・区間でcounter増分を比較。hit比を作るなら分母は両者の増分和、0やreset時は欠測。OSのpage cacheヒット率ではない |
| `postgresql.commits`、`postgresql.rollbacks` | transactionの増分。rollbackには意図した処理もあるので、バグ件数と扱わない |
| `postgresql.temp.io`、`postgresql.temp_files`、`postgresql.db_size` | 一時ファイルへの書込み・件数・容量を、負荷とクエリの変化に合わせて確認 |

テーブル・index別の既定メトリックスは多くを無効化している。ロックのrelation等には複数の値が出るため、対象DBと系列数を確認する。接続poolの空き・待ち時間はDBサーバーのbackend数から推定せず、アプリのpool計装またはDB proxyから収集する。query sample/top queryイベント、`pg_stat_statements`依存の収集、SQL本文の収集はこの設定では有効にしない。

## Webアクセスログ：JSON契約と相関

Webサーバーに合わせてJSON出力を構成する。例は1行1JSON、UTF-8で、`timestamp` はtimezone付きRFC3339。`level` があれば重要度を設定する。以下は入力例であり、任意のNginx/Apacheの既定ログを自動解析する設定ではない。

```json
{"timestamp":"2026-10-26T01:02:03.123Z","level":"INFO","method":"GET","route":"/items/{id}","status":200,"request_time":0.125,"trace_id":"11111111111111111111111111111111","span_id":"2222222222222222"}
```

`trace_id` / `span_id` はWebサーバーが実際に伝播・生成したものを出す。設定は非ゼロの32桁・16桁hexをOTelの対応フィールドに写す。欠落・形式不正なら新しいIDを捏造せず、相関なしで残す。有効な形式でも対応トレースがMackerelに存在するとは限らない。Webのversionをアプリのrevisionで上書きしない。

元のJSON本文を保持するため、Mackerelの本文属性検索で `status` や `route` を絞り込める。URL query、Cookie、Authorization、個人情報を出さないフィールド選択をWeb側で行う。基本設定の属性削除では生ログ本文を無害化できない。`request_time` はこの例では秒、APM APIのlatencyはミリ秒なので、比較前に単位を合わせる。Web全体の処理時間とupstream処理時間も混ぜない。

成功を含む全対象requestのアクセスログを収集して初めて、同一条件のstatus別件数やエラー率の分母を検討できる。ERROR/FATALだけの検索結果、標本、ページ上限に達した結果からrequestエラー率を計算しない。1requestに複数のproxy行が出る構成では、ログ行数とrequest数を分ける。

`start_at: end` なので初回起動前のログは読み込まない。offsetはメモリ内のみで、停止中・再起動・rotationによる欠落や重複があり得る。継続性が必要ならfile_storage extension、保存先、rotation方式を別途構成して検証する。構文不正やtimestamp不正の行は削除せず送るため、Collectorのparserエラーも観測し、解析済みの全件とは数えない。元の発生時刻を確定できない行を比較試験の時間窓へ推測で割り当てない。

## 他のDB・Web製品とログ

MySQL/Redis/Nginx等は、その製品と版に対応するreceiverまたは既存Mackerelプラグインを選ぶ。このPRではそれらの接続・出力fixtureは未検証。Nginxのstatus endpointから取れる接続数・request数と、アクセスログのstatus分布を補完させる設計は可能だが、存在しないendpointを仮定して設定しない。

DBのslow logは閾値で選択された標本であり、全クエリの母数ではない。slow log件数を全queryの失敗率・遅延分布へ変換しない。DBログを読む場合は製品固有の時刻・multiline・SQL秘匿・rotationを検証した別のfile_logアダプターを追加する。上のWeb JSONアダプターへ形式の異なるDBログをそのまま投入しない。

## 一次資料・検証範囲

- [Contrib 0.163.0 Host Metrics receiver](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/v0.163.0/receiver/hostmetricsreceiver)
- [Contrib 0.163.0 PostgreSQL receiver](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/v0.163.0/receiver/postgresqlreceiver)・[指標の定義](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.163.0/receiver/postgresqlreceiver/metadata.yaml)
- [Contrib 0.163.0 File Log receiver](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/v0.163.0/receiver/filelogreceiver)
- [Mackerelログ検索API](https://mackerel.io/api-docs/entry/logs)

Contrib 0.163.2に同梱される0.163.0のreceiver定義を参照。任意の結合テストは `OTELCOL_CONTRIB` に0.163.2の実行ファイルを指定して実行する。各overlayと組合せの構文検証、Web JSONの時刻・重要度・trace context・不正行保持をローカルfile exporterで検証する。実DBへの接続、導入先OSの収集権限、実Mackerelへの送信は導入先で確認する。
