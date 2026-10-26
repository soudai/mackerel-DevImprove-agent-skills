# Mackerelへの集計投稿とダッシュボード

対象は、開発環境のテスト・静的解析・負荷試験の結果を、サービス単位で比較する運用。以下のパネル・系列・投稿ルールはこのSkillの設計であり、Mackerelの組み込み品質評価機能ではない。製品仕様は2026-10-26に公式資料で確認した。

## 保存先を使い分ける

- 実行時のトレース・ログはOpenTelemetry Collectorから対応するMackerel機能へ送る。OTel Metricsのラベル付きメトリックと、ここで使うサービスメトリックは別の保存モデル。
- サービスメトリックには固定名の数値集計を送る。関数名、ファイル、PR番号、commit、run IDを系列名として増やさない。
- 正規化JSON、JUnit等の原レポート、`moznion/cccc` のJSON、測定条件はCI成果物などに保持する。詳細を復元できないグラフだけで改善を決定しない。
- サービス対応表に `service.namespace / service.name / deployment.environment.name -> Mackerel serviceName` を持たせる。複数言語・テスト種別は、必要なものだけ固定プロファイルとして定義する。

## 投稿・取得の契約

ベースURLは `https://api.mackerelio.com`。APIキーは実行環境のシークレットから `X-Api-Key` に渡し、ファイル・コマンド表示・ログに値を残さない。調査用Read権限と投稿用Read/Write権限は用途に合わせて扱う。

| 処理 | APIと入力 |
|---|---|
| 集計値投稿 | `POST /api/v0/services/<serviceName>/tsdb` に `[{"name":"tests.cases.executed","time":<実測Unix秒>,"value":<数値>}, ...]` |
| 集計値取得 | `GET /api/v0/services/<serviceName>/metrics?name=<系列名>&from=<Unix秒>&to=<Unix秒>` |
| 最新成果物の対応表 | `GET/PUT /api/v0/services/<serviceName>/metadata/dev-quality` |
| 改善・解析器変更の注記 | `GET/POST /api/v0/graph-annotations`。対象service、from/to、title、短いdescriptionを使う |

サービスメトリックAPIは `name/time/value` の数値時系列。24時間より古い投稿はHTTP 200でも保存されず、同じ過去データへの再投稿は上書きになり得る。429では頻度を落とし、複数系列をまとめて投稿する。[API仕様](https://mackerel.io/api-docs/entry/service-metrics)

投稿処理には次の条件を実装する。

1. CIでテストが失敗しても、可能なレポートを保存・集計する。テストの終了状態は維持し、アップロード成功でテスト成功に置き換えない。
2. 一つの共有系列は、基準ブランチの一つのpublisherが管理する。並行PR・個人実行は同じ系列へ混ぜない。CIの順序制御で旧revisionの遅いジョブが最新結果を置き換えないようにする。同一系列・同一時間バケットに複数runが入る場合は、事前に決めた代表runのみ投稿し、全runは成果物に残す。
3. 送信前に有限数・単位・分母・集計完全性を検証し、未対応や欠測を0に置換しない。測定時刻はrun manifestの値を使う。
4. GETの通信失敗は上限付きで再試行する。POST/PUTの結果が不明な場合は自動再送せず、payloadとrun ID、送信結果を保存し、APIの読戻しと既存metadataを調査してから再開を判断する。再開時も測定時刻を現在時刻へ付け替えない。24時間を過ぎた結果は成果物として保持し、時系列に偽の新測定を作らない。同梱CLIの途中状態は [投稿手順](report-tools.md#mackerelへの投稿) を参照する。
5. 投稿成功後に測定前後を含む短い区間の系列を読み戻し、値と時間バケットをmanifestと照合する。APIの粒度を確認し、投稿のエポック秒と返却時刻の完全一致を前提にしない。同じバケットに別runがある、集約されて区別できない、または値が確認できない場合は `published_unverified` と記録する。
6. 最新metadataは値の投稿・照合が済んでから更新する。数値投稿とmetadata更新は一つのトランザクションではないため、途中失敗も記録する。

取得APIは指定期間に応じて粒度が変わり、その粒度での平均値を返す。公式の1分粒度取得条件は20時間以下の区間。導入時に短い区間で読み戻し、実際のタイムバケットを確認する。秒から分へのfloor/round/ceil規則は参照したAPI仕様では確認できないため、独自に決めつけない。[取得粒度の公式説明](https://support.mackerel.io/hc/en-us/articles/900006365223-Is-it-possible-to-download-metrics-and-monitors)

## 実行単位の値と鮮度

`tests.cases.*`、`tests.attempts.*`、複雑度、カバレッジは原則「そのrunの測定値」というゲージとして投稿する。テストケース数の系列を時間積分して総実行数にしない。CIの実行回数を日単位などで表示したい場合は、重複除去済みrun manifestから固定期間の件数を別途集計し、ケース数とは区別する。

イベント駆動のCIでは点のない時間は通常あり得る。グラフの空白を0件・正常と解釈しない。`tests.run.complete`、`tests.run.report_complete`、`tests.run.success` と最終完了時刻を別に示し、完走した失敗run、レポート欠損、aborted/interrupted/unknownを区別する。判定不能なsuccessを成功・失敗のどちらかに強制変換しない。`quality.analysis.last_success_timestamp` は測定成功時刻であり、公開成功時刻ではない。

鮮度はagentが `現在時刻 - manifestの測定時刻` で判定する。常時表示が必要なら専用監視がageゲージを更新してもよいが、それを新しいテスト・解析の実行として数えない。データの期待更新間隔はCI頻度から決める。

## パネル構成

| 順 | 表示する情報 | グラフ・数値・根拠 |
|---|---|---|
| 1. 計測状態 | 対象service/environment/revision、測定時刻、完全性、解析対応範囲、欠落shard | 状態値とMarkdownの最新manifestリンク。古い・不完全な結果を先に示す |
| 2. テスト結果 | `tests.cases.passed/failed/error/skipped/xfail/xpass/not_run` | 最終状態の内訳。合計・executed・flakyを内訳と一緒に積み上げて二重計数しない |
| 3. 実行量・再試行 | selected、executed、attempts、retries、flaky、run時間 | ケース数とattempt数を別グラフにする。件数増を品質改善と断定しない |
| 4. テストの保護範囲 | 行・分岐カバレッジ、変更箇所カバレッジ、任意のmutation/CRAP | 分母と対象範囲へのリンク。異なるcoverage定義を同じ系列で混ぜない |
| 5. CCCC・複雑度 | `cccc.<profile>.cognitive.{p95,max,over_limit_count,over_limit_ratio}`、`cccc.<profile>.cyclomatic.{p95,max,over_limit_count,over_limit_ratio}`、`cccc.<profile>.analysis.{files,functions,parse_errors}` | 関数単位の認知・循環的複雑度を別々に表示し、解析エラーと母集団を併記する |
| 6. 改善候補 | 高複雑度かつ変更頻度が高い箇所、依存違反、既知の回帰不具合 | 集計件数＋詳細一覧へのリンク。未知のバグ数として表示しない |
| 7. 実行時の効果 | 負荷試験の遅延、エラー率、処理量、資源使用量 | APM・ログ・試験結果へのリンク。同じ条件の前後比較を掲載 |

このSkillのCCCCは [moznion/cccc](https://github.com/moznion/cccc)。既定出力はJSONで、`files` と `summary` を持ち、認知・循環的複雑度、関数数・ファイル数・解析エラー数を読み取れる。関数の `children` を含む分布とファイル合計は区別する。閾値超過集計には省略のない関数明細を使う。依存違反・結合度・循環依存は別アダプターの `quality.*` としてパネル6に追加し、CCCCから得た値として表示しない。

カスタムダッシュボードの `graph`、`value`、`markdown` ウィジェットを使う。グラフウィジェットは `graph` オブジェクト、数値ウィジェットは `metric` オブジェクトを持ち、両者は異なる。サービスグラフでは `type=service/serviceName/name` を設定し、生成済みグラフの実際のnameを確認してから使用する。`legendList` で表示系列を選べる。[Dashboard API](https://mackerel.io/api-docs/entry/dashboards)

命名はグラフのまとまりと単位を意識する。Mackerelのカスタムメトリックは末尾のドット以前が共通の系列をまとめる仕様があるため、命名だけで期待したサービスグラフになると決めつけず、初回投稿後に実際のまとまりを確認する。[メトリック仕様](https://mackerel.io/docs/entry/spec/metrics)

- `tests.run.*` の0/1状態、秒、Unix時刻を同じ縦軸に表示しない。`legendList` と別ウィジェットで分離する。
- ratioの正本は0〜1。表示も0〜1なら軸・表記をそのまま使う。%表示する場合は100倍した式または明示的な表示用系列を使う。`suffix="%"` だけでは値の変換にならない。
- 時間・件数・比率・複雑度は別軸または別パネルとする。基準値はプロジェクト設定に対応し、全言語共通の合格線を引かない。
- 広い期間のグラフは傾向確認用。p95同士の平均や、グラフの丸め値による厳密な採否判定をしない。

## revision・根拠リンク

service metadataの `dev-quality` に `schema_version / service / environment / profiles` を持つ小さな最新manifestを置く。`profiles` は固定プロファイル名をキーとするmapで、各値に `revision / run_id / measured_at / artifact_uri / report_completeness / published_status` を持たせる。あるプロファイルだけ更新するときも既存mapを読み、他プロファイルを保持した全体JSONを一つのpublisherから直列更新する。複数writerのread-modify-writeは更新欠落を起こすため使わない。metadataは更新可能なので、履歴の正本はrunごとの不変成果物とする。Mackerelのservice metadataは任意JSONを扱える。[Metadata API](https://mackerel.io/api-docs/entry/metadata)

改善を適用した区間、解析器・ルール変更、テスト対象変更にはgraph annotationを付ける。descriptionには短いrevision/run IDと成果物リンクを載せ、秘密情報やログ本文を入れない。annotationにはserviceと時間範囲を指定できる。[Graph annotations API](https://mackerel.io/api-docs/entry/graph-annotations)

agentは次の順で読む。

1. サービス対応表とmetadataの `profiles` mapから対象環境・revision・プロファイル・成果物を確認する。各プロファイルが別revisionを指す場合、同一runの測定結果として結合しない。
2. 対象時間帯のservice metricsをAPIで取得し、欠測と鮮度を判定する。グラフ画像の見た目を数値の代替にしない。
3. manifestのrun IDから原レポート・関数別明細を読み、指標の分母と解析完全性を検証する。
4. 同じrevisionのAPM/ログ・実験条件と照合する。改善前後で測定定義が違えば比較を保留する。
5. 結論に、対象revision、run ID、測定値、明細へのリンク、残る不確実性を含める。

## 設定を反映する前後の確認

既存ダッシュボードがあればGETして内容を保持し、必要な差分だけを作る。POSTは新規、PUTは既存IDへの更新。今回のタスクで許可されたサービス・環境を確認して適用する。適用後はAPIで設定を読み戻し、可能なら表示も確認する。サービスメトリック名、表示単位、空データ、manifestリンクが正しいことを確かめてから可視化完了とする。
