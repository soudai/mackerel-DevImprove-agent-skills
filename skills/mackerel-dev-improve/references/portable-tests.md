# 言語に依存しないテスト入力

`test-cases/v1`はこのSkill独自の共通入力契約。JUnitの標準規格やOpenTelemetryの仕様ではない。各言語のランナーを変更せず、生成元に合わせたconverterを境界に置いて、同じ正規化・Mackerel投稿処理を使う。

```text
ランナーの生レポート + 独立したテスト計画 + 元exit
    → 生成元・版を固定したconverter
    → test-cases/v1 JSON + run.json
    → normalize.py → manifest/details/metrics → publish.py
```

対応単位は**単一の再試行なし実行における、選択された各ケースの最終結果**。pytest/Jest/Go等の生出力を直接受理する意味ではない。対応するPHPUnitのJUnitなら既存`kind=junit`をそのまま使う。retry・shardの単純連結、xfail/xpassのpass/skipへの丸め、未完了ケースの除去はこの契約では行わない。

## 三つの入力

- [project.portable.example.json](../assets/project.portable.example.json)：`kind=test_cases`、`contract=test-cases/v1`、ランナーの`producer/version`、converterの`name/version`、固定`selection`、正常に計画を完了し得る`completed_exit_codes`を指定する。exit 0を含め、通常のテスト失敗コードを生成元の仕様に従って加える。中断・起動失敗コードは含めない。
- [run.portable.example.json](../assets/run.portable.example.json)：実行版、元`exit_code`、`run_status`、`test_plan_complete`に加え、**実行結果とは独立に確認した選択ケース数**`expected_cases`を記録する。`len(report.cases)`から逆算しない。収集・列挙が動的で独立確認できない場合は、完全性を確定できないと報告する。
- [tests.example.json](../assets/tests.example.json)：下記の共通JSON。名前・パラメーター・失敗本文を含む生レポートは別成果物に保管し、runの`raw_artifacts`でchecksumを記録できる。

```json
{
  "schema_version": 1,
  "producer": {"name": "example-runner", "version": "1.0.0"},
  "converter": {"name": "example-converter", "version": "1.0.0"},
  "cases": [
    {"id": "module:suite:test-a:param-0", "status": "passed"},
    {"id": "module:suite:test-b", "status": "skipped"}
  ]
}
```

JSONの生成元とconverterはproject設定と一致させる。版番号が同じでも設定形式は交換可能とは限らない。未知のフィールド・状態・契約版を無視して受理せず、契約を更新して検証する。

| case項目 | 意味 |
|---|---|
| `id` | 空でない文字列。同じprofile内で一意、同じケース・parameterには安定したID。module/suiteを含め、表示名だけで同一視しない。正規化明細にはハッシュを保存 |
| `status=passed` | そのケースが成功して完了 |
| `status=failed` | アサーション等のテスト失敗で完了 |
| `status=error` | ランナーが当該ケースへ帰属させるエラー。suite全体の収集失敗を架空のケースへ変換しない |
| `status=skipped` | 選択されたがスキップされたケース。未完了・取得不能をskipにしない |

件数一致、重複IDなし、生成元・版の一致、完了した計画、正常な終了形が揃った場合だけcanonical件数を生成する。`expected_cases=0`や空レポートは現版では不完全となる。テスト不要なcomponentは事前にprofile対象から外し、「テスト合格」とは表現しない。

## 集計と比較

既存の`tests.<profile>.cases.{selected,passed,failed,error,skipped}`、`ratios.strict_pass`、`run.*`へ集計する。strict passの分母はpassed + failed + errorで、skipは含めない。全件skipなら割合を出さない。ケース開始回数、attempt、flaky、coverage、CI実行総数はこのJSONから生成しない。

profile設定のproducer・converter・選択条件・終了形とnormalizerの版は比較キーに含まれる。設定・除外・実行環境の違いも固定profileや`ruleset_hash`へ記録する。コンバーターの修正で結果の意味が変わった場合は版を上げ、別の固定profile名で新しい基準を作る。生レポートと元exitを残せばconverterの再検証ができる。

## converterを追加するときの検証

固定した生成元の実レポートで、通常成功、アサーション失敗、ケースエラー、全件skip、同名parameter、重複ID、列挙後の中断、欠落、0件を照合する。ランナー固有のretry/xfail/shardを検出して未対応として扱うことも確認する。ケース数だけ一致させて完全性の証拠を作らない。元exitとwrapper/gateのexitは別に保持し、converterのexitで置き換えない。

同梱例はリポジトリ直下からオフラインで試せる。出力先は未作成のディレクトリにする。

```sh
python skills/mackerel-dev-improve/scripts/normalize.py --config skills/mackerel-dev-improve/assets/project.portable.example.json --run skills/mackerel-dev-improve/assets/run.portable.example.json --output .quality/portable-example
python skills/mackerel-dev-improve/scripts/publish.py --bundle .quality/portable-example
```

例のrevision・時刻・URLは架空であり、このbundleを実投稿しない。実データへの切替とCI接続は[導入手順](project-adoption.md)、投稿前の条件は[レポートツール](report-tools.md)を使う。
