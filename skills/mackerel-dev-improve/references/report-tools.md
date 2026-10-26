# レポート変換・投稿ツール

Mackerelからの観測結果取得、品質bundle比較、ダッシュボード生成は[観測・比較・可視化CLI](observation-tools.md)を参照する。この文書はnormalize/publishの入力・投稿契約を扱う。

同梱スクリプトはPython 3.11以上の標準ライブラリだけを使う。CIではLinux/WindowsとPython 3.11/3.13を組み合わせる。各プロジェクトでランナーを実行し、生レポート、実行版、終了コード、測定時刻を収集した後に使う。スクリプトはテストやCCCC自体を実行しない。広い計測方針は[計測契約](measurement-contract.md)、現在の実装範囲はこの文書を正本とする。導入先の構成確認とCIへの接続は[導入手順](project-adoption.md)を使う。

以下の`python3`は環境に応じて`python`または`py -3`へ置き換えられる。run内のファイルパスはrun JSON基準であり、起動ディレクトリに依存しない。YAMLの設計例をCLIへ渡さない。設計契約・project設定・run・共通JSONは別の形式であり、`schema_version`の数値が同じだけでは交換できない。

## 対応する入力

| profile kind | 対応する生成元 | 完全性の根拠 |
|---|---|---|
| `junit` | PHPUnit 12.5.xのUTF-8 JUnit XML、単一のretryなし実行 | 全階層のsuite summaryとケース明細の一致、重複IDなし、完了したテスト計画、runner終了コード |
| `test_cases` | [test-cases/v1共通JSON](portable-tests.md)、単一のretryなし実行 | 独立した選択件数との一致、重複IDなし、生成元/converter版、完了したテスト計画、runner終了コード |
| `cccc` | moznion/cccc 1.8.3の全件`files/summary` JSON | 入力ファイル一覧との一致、再帰的な全関数の件数と分布、解析エラー情報、解析器終了コード |

別のJUnit生成元の直接読取、retry、複数shardのmerge、xfail/xpass、coverage、CRAP、期間内CI実行数は未対応。他ランナーでは生成元の意味を検証するconverterを実装し、`test_cases`へ変換する。名前の似た値から推定しない。ケース開始・試行回数・flakyも生成しない。テストIDは正規化明細ではハッシュにし、失敗本文・stdout・stderrを複製しない。生レポートにはそれらが残り得るため、CI成果物の閲覧範囲・保存期間は利用先で決める。

CCCCの集計では`--min 0`と全件JSONを使い、キャッシュと暗黙の設定を避ける。言語・除外・拡張子設定は固定profileで記録する。`top`形式、summaryより少ない関数明細、parse error、入力漏れは通常の分布系列へ投稿しない。関数0件の完全な解析ではfiles/functionsだけを出し、分位点や割合は欠測とする。

入力ファイルは各64MiB以下、件数・複雑度の整数は0〜2^53−1に限定する。JSONのNaN/Infinity・重複キー、XMLのDTD・外部実体宣言、過大な数値は拒否する。

## 設定とrun

[project.example.json](../assets/project.example.json)または[project.portable.example.json](../assets/project.portable.example.json)をプロジェクトの設定としてコピーし、サービス・環境・固定profileを設定する。profile名は`[a-z][a-z0-9_]{0,31}`、profile数は最大16。PR番号、SHA、任意の関数名からprofileを生成しない。各profileの`producer/version`は固定し、runに記録した実行版と照合する。全profileで`selection`は必須で、テスト選択や解析条件を記述する。CCCCの言語と2種類の整数閾値は必須で、超過判定は厳密な`>`を使う。

[run.example.json](../assets/run.example.json)は採取結果の形の例であり、記載されたSHA・URL・ファイル名・時刻は実データで置き換える。`report`とsidecarの`path`はrun JSONの親ディレクトリーを基準に解決する。CCCCの`expected_files`はリポジトリー相対パスの配列で、解析結果から逆算せず、採取対象ソースから独立に生成する。

runの必須項目は`revision`（完全な40桁または64桁commit ID）、`run_id`、`attempt_id`（文字列）、UTCの`started_at/finished_at`、`profiles` map。`base_revision`は完全IDまたはnull。`working_tree_dirty`と`artifact_uri`は投稿時の出所確認に使う。revision自体は比較キーへ入れず、profile設定、環境、アダプター版、任意の`ruleset_hash`を比較キーに含める。設定・除外ファイルの内容をまとめたSHA-256を`ruleset_hash`に保存すると、設定変更後の比較を区別できる。

テスト全体を実行するラッパーが事前チェックやcleanupも行う場合、任意の`test_wrapper_exit_code`をrun直下に記録する。非ゼロならテスト各profileの`run.success=0`に反映し、元ランナーのexitとは別の`wrapper_exit_code`として保持する。ランナー未開始の事前失敗や、成功後のcleanup失敗を成功・不明へ丸めない。

各profileには次を記録する。

- `report`, `version`, `exit_code`: 生レポート、実行版、元プロセスの終了コード。取得不能のexitはnull。POSIXのsubprocessが返す負のsignal終了も元値を保持し、既知の失敗として扱う。終了コードを正規化処理の成否で上書きしない。
- `gate_exit_code`: 既存の追加品質ゲートの終了コード。任意。ゲート失敗とレポートの完全性は別に残す。
- テストの`test_plan_complete`: fail-fast・中断・欠落がないと確認した場合だけtrue。レポートがあるだけではtrueにしない。`run_status`は`completed/aborted/running/unknown`。PHPUnitでは省略時に終了コードがあればcompleted、それ以外はunknownとする。PHPUnitのexit 0/1/2を正常な終了形として扱い、case失敗のexit 1/2でも完全な計測結果になり得る。`test_cases`では`run_status`と独立した`expected_cases`を明示し、正常な終了形を設定の`completed_exit_codes`と照合する。
- テストprofileの`started_at/finished_at`: 任意だが指定する場合は両方必須。UTCで開始≦終了、全runの期間内であることを検証し、profileの経過秒を算出する。並列ケースの所要時間の和や、別処理を含む全run時間で代用しない。
- CCCCの`expected_files`: 予定した全ファイル。exit 0/1を解析器の正常終了形として扱う。閾値超過のexit 1は解析失敗へ変換しない。
- `raw_artifacts`: 任意のsidecar map。例は`{"version.txt":{"path":"raw/version.txt","sha256":"64桁の実checksum"}}`。記録されたchecksumを読み直したファイルと照合する。

取得できなかったprofileはrunから省略できるが、configには残す。正規化はそのprofileをmissingとして記録する。run側だけに未知のprofileを追加した場合は設定エラーになる。

## 正規化の実行

Skillディレクトリーから実行する例。出力先は毎回新しいディレクトリーにする。

```sh
python3 scripts/normalize.py \
  --config /workspace/project.json \
  --run /workspace/artifacts/run-001/run.json \
  --output /workspace/artifacts/run-001/normalized
```

出力は`manifest.json`、`details.json`、`metrics.json`。最後のファイルがMackerel用の`[{"name":"...","time":123,"value":1}]`配列になる。時刻はrunの`finished_at`で固定する。過去の採取結果を再処理するとき、現在時刻へ書き換えない。

manifestの`collection` mapにはprofileごとの`status`（complete/partial/missing/error）、`report_complete`、`complete`、元exit、診断理由が入る。`profiles` mapは比較キー、実行版、設定、sidecarを保持する。`artifacts`は生レポートと正規化ファイルのSHA-256を保持し、生レポートURIの`relative_to=run_manifest_parent`はrun JSONの親基準を示す。`publication.payload_sha256`は`metrics.json`の生bytesのSHA-256。生成時点では`publication.status=not_published`である。

| exit | 意味 |
|---|---|
| 0 | 全profileの測定が完全。元のテスト・品質ゲートの合格とは別 |
| 1 | 成果物は保存したが、一部profileが不完全。manifestの診断を確認 |
| 2 | 設定・run・出力先のエラー。既存出力先の再利用も拒否 |

不完全なテスト結果からcanonicalケース件数・成功率を生成せず、report_complete/completeと、確認できた実行状態・経過時間だけを出す。runnerの最終失敗や中断を確認できればsuccess=0とし、取得不能だけで失敗を捏造しない。既知の失敗がなく完全性も確認できないときsuccessはnullとなり、その系列を省略する。CCCCも不完全な分布の代わりにanalysis.complete=0を出す。過去の成功値を新しい時刻で再送しない。

## Mackerelへの投稿

まずオフラインでbundleを検査する。APIキーも通信も不要で、結果はbundle内の`publication-result.json`へ保存する。

```sh
python3 scripts/publish.py --bundle /workspace/artifacts/run-001/normalized
```

実接続の読取り確認には次を使う。`MACKEREL_APIKEY`は実行環境の秘密管理から注入する。コマンド引数、設定JSON、ログへキーを記載しない。このモードはorganizationとservice一覧をGETするだけで、serviceがない場合も認証・organizationの照合成功を返し、存在しないことを表示する。

```sh
python3 scripts/publish.py --check-connection \
  --expected-org example-org --service orders-dev-quality
```

利用者が「このAPIキーの所属organizationを送信先にする」と明示した場合は、`--expected-org`の代わりに`--use-key-organization`を指定できる。両方は同時指定できない。所属先はGET応答の名前を検証してプロセス内部だけで保持し、同じclientで再照合する。所属名をログ、結果ファイル、service metadataへ書き出さない。

```sh
python3 scripts/publish.py --check-connection \
  --use-key-organization --service orders-dev-quality
```

この指定はAPIキーの所属先そのものを選択する。将来キーの所属organizationが変われば送信先も変わる。特定のorganizationへ固定する運用では従来の`--expected-org`を使う。指定がなければ所属先の自動選択は行わない。

実送信は`--send`を指定し、CI側が現在の基準ブランチ・最新世代と照合したrevision/run/attemptを渡す。bundleから値をコピーして一致させるだけでは古いrunを防げない。共有サービスへのwriterはCIのconcurrency設定などで直列化する。PRの計測は成果物として保存し、基準ブランチ専用publisherへ送信させる。

```sh
python3 scripts/publish.py --bundle /workspace/artifacts/run-001/normalized \
  --send --expected-org example-org \
  --expected-revision CURRENT_BASELINE_COMMIT \
  --expected-run-id CURRENT_RUN_ID --expected-attempt-id CURRENT_ATTEMPT_ID
```

明示的にキーの所属先を選んだ実送信でも、上の`--expected-org example-org`だけを`--use-key-organization`へ置き換える。revision/run/attemptの照合、測定時刻、cleanなソース、checksum、固定系列、完全性の検査はすべて維持する。`--send`を付けないdry-runでは、`--use-key-organization`があっても通信もキーの読取りも行わない。

`--create-service`は意図した新規サービスを作成するときだけ追加する。既存サービスを自動で作り直さない。送信は`working_tree_dirty=false`が明示された24時間未満のsnapshot、世代の一致、HTTPSの成果物リンク、payload checksum、固定series、計測完全性を確認する。送信先APIは`https://api.mackerelio.com`に固定し、redirectと環境proxyを使わない。

送信前にorganizationと既存の`dev-quality` service metadataを読む。より新しいrun、同じrunで異なる内容、別runとの120秒以内の重複を拒否する。profileの`comparison_key`も検証・記録し、同名profileの定義変更を送信前に拒否する。定義変更時は別の固定profile名で新しい基準を取得する。旧metadataにキーがない場合は次の成功時から記録するが、過去の定義との一致を保証するものではない。今回のnormalizerは版2であり、版1の結果と同条件の比較には新しい版での再計測が必要になる。

投稿後は各seriesを読み戻し、60秒未満に一致する測定点が一意にあることを確認する。この近傍一致だけではrunの帰属は証明できない。共有seriesを他のwriterと併用せず、測定時間帯を分離し、metadataのrevision/run/attemptと成果物も照合する。APIの読み取りだけを有限回再試行し、結果不明のPOSTを自動再実行しない。秒の丸め規則は[公式サービスメトリックAPI](https://mackerel.io/api-docs/entry/service-metrics)に明記されていないため、導入先で実際の読戻しを確認する。

読戻し成功後、他profileのmapを保持してservice metadataを更新し、再度読み戻す。途中でmetadataが変わった場合は上書きを止める。この検査は原子的なcompare-and-swapではないため、CI側の単一writer制御が引き続き必要になる。ダッシュボードは[ダッシュボード設計](mackerel-dashboard.md)を使って別途構成する。

`publication-result.json`の`dry_run`はローカル検証だけ、`verified`はメトリックとmetadataの読戻し完了を示す。`published_unverified`、`publication_unknown`、`metrics_verified_metadata_failed`などは途中まで反映した可能性を区別する。元のmanifestや測定時刻を変更せず結果を確認する。投稿CLIのexit 0は処理成功、1は検査拒否・API処理失敗、2は引数・bundle・ロック・ローカル保存のエラー。元のテスト終了コードは呼出し元が別途保持する。

## 検証

リポジトリー直下から実行する。

```sh
python3 -m unittest discover -s tests -v
```

正規化のfixtureは、正常、通常失敗、error、skipのみ、同名パラメーター、重複ケース、summary不一致、missing、完了未確認、中断、0件、未対応retry/xfail/xpass、DTD・UTF-16拒否を扱う。CCCCは親子関数・0値、閾値境界、関数なし、parse error、入力漏れ、top/filtered JSON、summary不整合、版変更を検証する。CLIの非ゼロ終了後の証跡保存、checksum、sidecar改変、既存出力先拒否も検証する。再試行やxfailのfixtureは「未対応として拒否する」試験であり、その意味を正規化できると主張するものではない。

実採用時は、固定したランナー版から生成した実レポートでも照合する。Mackerelの実APIへの送信・読戻し・表示は、ローカルのfixtureテストだけでは検証できない。
