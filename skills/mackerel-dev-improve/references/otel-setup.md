# 開発環境のOTel導入

実装時には対象言語・SDK・Collectorの現行対応と既存設定を確認する。以下は2026-10-26確認の構成であり、固定の全言語共通設定ファイルではない。

実行できるContrib設定とローカル検証は[observability-setup.md](observability-setup.md)、APIによる観測結果取得は[observation-tools.md](observation-tools.md)を参照する。

## 信号と送信先

| 信号 | 収集方法 | Mackerel側 |
|---|---|---|
| traces | 対応する自動計装と必要な手動span | APM。OTLP/HTTP exporterのbase endpointは `https://otlp-vaxila.mackerelio.com` |
| logs | ログブリッジ、またはCollectorのfilelog等 | logs pipelineから同HTTP endpoint。exporterが信号別pathを付ける設定を確認 |
| metrics | OTel metrics / 対応receiver | ラベル付きメトリック。公式例はOTLP/gRPC `otlp.mackerelio.com:4317` |
| テスト・コード品質の集計 | CIの正規化・publisher | 従来のサービスメトリックAPI。OTLPとは別経路 |

Collectorの実配布物に必要なreceiver/processor/exporterが含まれるか確認する。MDOTとContribの構成・OS対応を混同しない。設定例をそのまま異なる配布物へ投入しない。

## 相関とデータ量

- `service.namespace`, `service.name`, `service.version`, `deployment.environment.name=development` を設定する。instanceを区別する必要があれば正しい発生元でinstance IDを付ける。
- HTTP・非同期処理のtrace context伝播を確認する。ログ中のtrace/span IDはOTelの対応フィールドに変換する。文字列を本文に入れただけで相関済みとしない。
- test/run IDを試験トレース・ログの独自属性に付ける場合、命名規則と保持範囲を決める。ラベル付きメトリックへrun ID・SHA・raw URL・user IDを無制限に付けない。
- routeは可能ならテンプレートを使う。認証情報・個人情報・SQLの生パラメーター等は送信前に必要最小限へ絞る。
- batching、queue、retry、メモリ上限を対象環境に合わせる。送信失敗・drop・queue詰まりも観測する。

## 送信確認

代表的な成功処理と意図した失敗処理を開発環境で実行し、service/environment/version、traceの親子関係、ログ相関、実行時間、errorの意味を確認する。HTTP 4xx/5xxと業務上の失敗は同義にせず、負荷試験のerror条件を明示する。

APMのトレース由来の件数・分布はsamplingの影響を受ける。母数が必要な比較には独立したrequest metricまたは負荷試験器の結果を使う。サンプリング率とCollectorの欠落を記録する。

Mackerelのログは確認日時点でβ版。検証に必要なテスト結果・ログ抜粋はartifactにも保持する。ラベル付きヒストグラムは代表値保存であり、保存済みp95を平均して全区間p95を作らない。比較試験の標本または合成可能な分布を別に保持する。

## 一次資料

- [Mackerel Collector導入](https://mackerel.io/ja/docs/entry/apm/getting-started/otel-collector-setup)
- [Mackerelログ送信](https://mackerel.io/ja/docs/entry/log/sending)
- [ラベル付きメトリック・ヒストグラム制約](https://mackerel.io/ja/docs/entry/howto/labeled-metrics/post-metrics)
- [OTel Collector architecture](https://opentelemetry.io/docs/collector/architecture/)
- [OTel service属性](https://opentelemetry.io/docs/specs/semconv/resource/service/)
- [OTel logs data model](https://opentelemetry.io/docs/specs/otel/logs/data-model/)
