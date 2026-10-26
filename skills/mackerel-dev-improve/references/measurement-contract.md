# 計測契約

このファイルはSkill独自の広い計測設計。OTel Semantic ConventionsやMackerelの既製機能と区別する。同梱CLIが受理・出力する厳密な形式と対応範囲は[report-tools.md](report-tools.md)を参照する。以下の設計表をそのままCLIのJSONスキーマとして使わない。

## 二つの経路

```text
アプリ → OTel SDK / ログ収集 → Collector → Mackerel APM・ログ・ラベル付きメトリック
CI → テスト / coverage / CCCC等 → アダプター → 共通結果 → サービスメトリックAPI
                                                    └→ 詳細成果物・manifest
```

OTelは静的複雑度やテスト結果を自動計算しない。両経路を `service.namespace / service.name / environment / revision` と測定時間で照合する。従来Mackerel serviceNameへの対応表を持つ。

## run manifest

| 項目 | 内容 |
|---|---|
| schema_version | この正規化仕様の版。初版は `1` |
| service, environment, revision, base_revision | 論理サービス、環境、対象と比較基準の完全なcommit ID |
| run_id, attempt_id, started_at, finished_at | CI run、job再試行、時刻（UTC ISO 8601） |
| profile, comparison_key | 固定プロファイルと比較条件の識別子 |
| tools | tool名、実バージョン、アダプター版、ruleset_hash、設定・除外のhash |
| scope | 対象ファイル・言語・テストsuite・entity粒度、選択条件、予定shard一覧 |
| collection | 各入力のcomplete/partial/missing/unsupported/error、exit code、診断理由 |
| artifacts | 生レポート、正規化結果、テスト明細、静的解析明細、比較表のURIとchecksum |
| publication | 投稿対象service、系列、測定時刻、payload checksum、API結果、読戻し結果 |

`comparison_key` は言語・測定器・ルール・除外・テスト選択条件・実行環境・負荷シナリオなどから作る。revision自体を含めて前後比較不能にしない。コードの変更に伴う対象数の変化は明細でレビューし、設定変更による母集団差と区別する。

## 測定レコード

共通項目は `metric_definition_id, entity_kind, entity_id, file, value, unit, status, numerator, denominator, evidence_ref`。service/revision/run/toolはmanifestを参照する。

- `metric_definition_id` に式、版、対象粒度、数え方を対応させる。同じ名称でも定義が異なれば別ID。
- `status=measured` だけが数値を持つ。unknown/unsupported/invalidは値をnullとし、Mackerelへ値0を投稿しない。
- entityはproject/module/class/function/test_caseなど。安定IDに言語、完全修飾名、signature、parameter、実行profileを含め、表示名だけで重複排除しない。
- ratioは0〜1、時間は明示したsecondsまたはms、件数は非負整数。割合の分子・分母を保存する。
- XMLは外部実体・DTDの外部解決を無効にして読む。HTMLしかない場合は集計精度の限界を残す。

## 系列の設計

一つのMackerelサービスに複数の計測profileを置く場合、`<family>.<profile>.<group>.<field>` を使う。profileは設定で許可した少数の固定名に限定する。単一profileでは省略可。すべての投稿元で同じ規則にする。

| 系列群 | 定義・分母 |
|---|---|
| `tests.cases.*`, `tests.attempts.*`, `tests.run.*` | [テスト契約](test-results.md)を使用。1回のrunのスナップショットであり累積カウンターではない |
| `cccc.<profile>.cognitive.{p95,max,over_limit_count,over_limit_ratio}` | moznion/ccccの関数認知複雑度。[CCCC契約](cccc-adapter.md)を使用 |
| `cccc.<profile>.cyclomatic.{p95,max,over_limit_count,over_limit_ratio}` | 同ツールの関数循環的複雑度。cognitiveとは別に評価 |
| `cccc.<profile>.analysis.{files,functions,parse_errors,parse_error_files,complete}` | 実出力の集計と入力manifestの照合。completeはSkill側で判定する0/1状態 |
| `quality.<profile>.cognitive.{p95,max,over_limit_count,over_limit_ratio}` | 同一解析器の関数値。超過関数数 / 測定済み対象関数数 |
| `quality.<profile>.npath.{p95,max,over_limit_count}` | 対応する解析器が出力した関数値。未対応は欠測 |
| `quality.<profile>.coverage.{line_ratio,branch_ratio,changed_line_ratio}` | covered / coverable。line/branch/conditionを交換しない |
| `quality.<profile>.mutation.score` | toolが定義する検出変異 / 有効変異。timeout・equivalent等の扱いを記録 |
| `quality.<profile>.crap.{p95,max,over_limit_count}` | 同一関数・revisionのCCとcoverageから計算。詳細はテスト契約 |
| `quality.<profile>.dependency.{cyclic_module_count,cyclic_module_ratio,boundary_violations}` | 頂点・辺の定義を固定。2頂点以上の強連結成分と自己循環に属するmodule数 / 全module数 |
| `quality.<profile>.duplication.ratio` | 同一検出器の重複行 / 対象行。最低一致長等を固定 |
| `quality.<profile>.analysis.{function_count,coverage_ratio,last_success_timestamp}` | 計測済み関数数、解析した対象ファイル数 / 入力対象ファイル数、実際の最終成功時刻 |
| `quality.<profile>.hotspots.{candidate_count,changed_candidate_count}` | 判定ポリシーに一致した対象数。詳細スコアでバグ確率を装わない |
| `quality.<profile>.defects.{confirmed_open,regressions_90d}` | 重複除去した確認済み不具合。Issue等の確認できた情報のみ。未連携は欠測 |
| `benchmark.<scenario>.latency.{p50_ms,p95_ms,p99_ms}` | 同じ負荷試験の全標本、または合成可能な分布から計算 |
| `benchmark.<scenario>.outcomes.{requests,errors,dropped_iterations}` | 負荷生成器の実績。error率だけでなく母数を残す |

Mackerelは最後のドットまで同じ名前の系列を同一グラフにまとめる。単位の異なる系列はダッシュボードで分けて表示する。`p95` と `over_limit_count` を同じ軸で比較させない。グラフ配置の詳細は [ダッシュボード設計](mackerel-dashboard.md)。

## 集計と比較

- 初版の分位点はnearest-rank（昇順n個の `ceil(p*n)` 番目）とし、n・max・超過件数を併記する。他手法の既存データとは定義を揃える。n=0は欠測。
- p95同士を平均・加算しない。言語やmoduleの集計を関数の分布と混ぜない。
- ratioの統合は同一定義・重複しない母集団の分子と分母を合算して行う。profile別の割合を単純平均しない。
- 前後で完全性・revision・comparison_keyを確認する。異なる設定でしか測れない場合は比較不能とし、新しい基準を取得する。
- 静的解析成功とテスト成功を独立に保持する。テスト失敗でも正常に取得した解析値は使えるが、欠落したテスト結果から成功率は作らない。parse errorや読取漏れのある静的解析はpartialとし、完全な解析用のcanonical分布系列へ投稿しない。計測状態と部分明細を保存し、参考値にはpartialと明示する。
- 関数分割による母集団変化を個別差分で確認する。関数数、LOC、結合度、重複も併記し、総和の減少だけを目標にしない。

## 保存と公開

生レポート → 正規化 → 同一runの集計 → artifact保存 → 投稿候補選別 → API投稿 → 読戻しの順にする。CIの失敗時にも収集処理を実行し、元のテスト終了コードを保持する。

基準ブランチの指定CIを共有系列の単一publisherとする。job完了順で「最新」を決めず、現在の公開対象revisionとgenerationをCI側で照合する。古いrunは成果物だけを残す。PRの比較は個別artifactで行い、固定枠の検証用serviceを使う場合も同時利用を制御する。

イベント時に測定値を投稿する。過去の値を現在時刻で再送して鮮度を偽らない。runが来ない期間の欠測を0と補間しない。経過時間を継続表示したい場合のみ、別のcollectorが `now - last_success_timestamp` を計算する。スナップショットのテスト件数を積分して期間中の実行総数にしない。期間集計はrun台帳から重複除去して求める。

出典: [MackerelサービスメトリックAPI](https://mackerel.io/api-docs/entry/service-metrics)、[メトリック仕様](https://mackerel.io/docs/entry/spec/metrics)、[Sonarメトリック定義](https://docs.sonarsource.com/sonarqube-server/user-guide/code-metrics/metrics-definition)。製品仕様確認日: 2026-10-26。
