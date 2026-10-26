# CCCCアダプター（moznion/cccc）

このSkillでいうCCCCは[moznion/cccc](https://github.com/moznion/cccc)を指す。Rust製のCLI／ライブラリーで、共通エンジンと言語別フロントエンドにより**認知的複雑度と循環的複雑度の両方**を測る。JSON出力を取り込み、関数単位の改善候補とMackerelの推移を結び付ける。

## 対応と計測プロファイル

採用するrelease／commit、バイナリーのハッシュ、ビルドfeature、言語、解析対象、除外、拡張子の振り分け、閾値を固定する。対応言語は採用版のREADME・`--help`・実際の入力で確認する。確認時のmainはTypeScript/JavaScript、Rust、Go、PHP、Ruby、Python、Java、C/C++、C#など多数の言語を扱うが、インストール経路で搭載言語が異なる場合がある。例えば確認時のcrates.io版ではKotlinが含まれない。[上流README](https://github.com/moznion/cccc/blob/5222b790b32eb4fd2ec012f1b6462551a0120dfd/README.md)

共通エンジンでも、言語構文からの変換・関数相当の単位・解析対応は異なる。プロファイルを言語・採用版・規則・対象範囲に結び付け、異言語の値を品質ランキングにしない。SonarSourceの考え方に沿う認知複雑度でも、Sonar製品の各言語解析器と完全一致することは保証しない。独自の`cccc.*`系列で追い、解析器更新時はbaselineを再測定する。

NPath、カバレッジ、テスト結果、依存結合度、循環依存、コード変更頻度は別の供給元を使う。CCCCだけから生成しない。

## 全関数のJSONを取得する

集計用の入力は通常の`{files, summary}`形式に固定する。既定ではJSONだが、自動探索された`cccc.toml`／`.cccc.toml`が表示形式や対象を変えるため、暗黙の設定を使わない。

次はPowerShell 7で`src`を対象とする採取例。実行ごとに新しい出力先を使い、対象パス・言語・除外をプロファイルに合わせる。別のshellではstdoutをUTF-8で保存し、stderrと混在させない。上流CLIの構文を確認した例であり、このSkillで実行済みという意味ではない。

```powershell
New-Item -ItemType Directory -Path '.quality/cccc/run-001' | Out-Null
cccc --version > '.quality/cccc/run-001/version.txt'
cccc --no-config --no-cache --min 0 --pretty src > '.quality/cccc/run-001/report.json' 2> '.quality/cccc/run-001/stderr.txt'
$ccccExitCode = $LASTEXITCODE
$ccccExitCode | Set-Content -LiteralPath '.quality/cccc/run-001/exit-code.txt'
```

- `--no-config`で自動探索を止める。プロジェクト固有の設定が必要なら、監査した専用設定を`--config <path>`で指定し、`table=false`、`min=0`、言語と除外条件を固定する。`--no-config`と併用しない。
- `--min 0`で全関数の明細を保持する。`--min N`は、認知または循環のどちらかがN以上の関数と、その祖先を残す表示フィルター。表示件数を母数と見なさない。
- 集計用の採取では`--top-cognitive`、`--top-cyclomatic`、`--table`を使わない。top指定時は`{metric, top, summary}`形式になり、全関数・全ファイルの明細が失われる。
- summaryは表示フィルター前に計算されるが、除外・言語選択・入力パス・解析失敗の影響は受ける。「topのsummaryが全体」でも、閾値超過数や詳細照合をtopの行から作らない。
- このSkillの通常投稿でいう「検証済みsummary」は、省略のない`{files, summary}`と入力manifestを照合したものに限る。top出力のsummaryは調査の参考にできるが、通常系列への投稿前に全明細を再採取する。独立した確認情報があっても、この既定契約を暗黙に変更しない。
- ディレクトリー探索は既定でignore規則を尊重し、`node_modules`を除く。採取した入力manifestと設定を記録し、除外変更で値が下がったことを改善にしない。
- JSON、stderr、終了コード、入力manifestをそれぞれ保存する。JSON出力が空・不正なら成功扱いしない。

[CLIの実装](https://github.com/moznion/cccc/blob/5222b790b32eb4fd2ec012f1b6462551a0120dfd/crates/cccc-cli/src/lib.rs)、[引数定義](https://github.com/moznion/cccc/blob/5222b790b32eb4fd2ec012f1b6462551a0120dfd/crates/cccc-cli/src/cli.rs)

## JSONの正規化契約

| 入力 | 意味と扱い |
|---|---|
| `files[].path` | 対象ファイル。リポジトリー相対パスに正規化する |
| `files[].cognitive`、`.cyclomatic` | 全深さの関数とmodule-level codeを含むファイル合計。関数の分位数として扱わない |
| `files[].functions[]`と再帰的な`children[]` | 全関数相当単位。子関数の値は親の値に含めて二重加算しない |
| 関数の`name`、`kind`、`line` | 名称・種類・開始行。lineは1始まり。署名・終了行・言語フィールドを出力にあるものとして仮定しない |
| 関数の`cognitive`、`cyclomatic` | 非負整数の個別値。`children`は空なら省略され得る |
| `files[].parse_errors[]` | 解析エラーメッセージの配列。エラーがなければ省略され得る |
| `summary.file_count`、`.function_count` | レポートに載ったファイル数、全深さの関数数。期待入力数と一致するとは限らない |
| `summary.cognitive`、`.cyclomatic` | それぞれ`sum/max/median/p90/p95`。全関数が母集団でmodule-level codeを除く |
| `summary.parse_error_count`、`.parse_error_file_count` | メッセージ数と影響ファイル数 |
| `summary.parse_error_files[]` | 影響パス。空なら省略され得る |

分位数は上流実装のnearest-rank方式。関数数ゼロの場合、上流は分位数等をゼロで出すが、正規化側では「関数なし」を記録して分位数・超過率を欠測扱いにする。関数ゼロと複雑度ゼロは別物。認知複雑度ゼロの実在関数は有効値として保持する。

標準のJSONには計測条件や恒久的な関数IDが揃っていない。ラッパー側で`tool.repository=moznion/cccc`、版／commit、バイナリーハッシュ、ruleset/config hash、言語プロファイル、revision、run ID、測定時刻、入力manifest、元レポートへの参照を付ける。run内の識別には相対パス、関数の階層、名前、kind、開始行、必要な衝突識別子を用いる。行移動・関数分割後の前後対応は差分で確認し、同名だけで結合しない。

[結果型と集計実装](https://github.com/moznion/cccc/blob/5222b790b32eb4fd2ec012f1b6462551a0120dfd/crates/cccc-core/src/report.rs)

## 終了コードと解析の有効性

| 終了コード | 上流での意味 | 収集側の対応 |
|---|---|---|
| 0 | 通常終了。対応入力が0件の場合も含む | JSON、対象範囲、解析エラーを別途検証する |
| 1 | `--max-cognitive`／`--max-cyclomatic`の閾値超過 | 有効なJSONなら結果は保存し、品質ゲート失敗として分類する |
| 2 | 引数・設定・パスなどのエラーで続行不可 | 計測失敗として記録し、品質値をゼロ投稿しない |
| その他 | プロセス異常・中断など | 計測失敗として保存し原因を確認する |

parse errorのあるファイルも回復できた部分が測定されることがあり、parse error自体ではexit 1/2にならない。ファイル読取失敗もstderrに出てレポートから除外され得る。対応ファイルが0件なら、exit 0でもJSONを出さず終了する実装である。

既定ではparse error、入力漏れ、空・不正なJSONがあるrunを「完全な比較としては無効」にする。得られた明細は調査用に保存し、Mackerelには`analysis.complete=0`などの計測状態だけを投稿する。認知・循環の分布や超過率を通常の品質系列へ投稿しない。例外として一部範囲を比較する場合は、その範囲を前後とも明示し別プロファイルにする。最新の失敗runを以前の成功値で覆い隠さない。

採用版のJSONに必須のsummary件数・エラー情報が存在しない場合は、その版用の定義と検証方法を用意する。未確認の欠落項目を自動的に0として扱わない。

## Mackerelへの投稿

プロファイルは固定allowlistにし、同じ系列の定義を途中で変えない。以下はrunごとのスナップショット値で、累積カウンターではない。

| 系列 | 元データ・計算 |
|---|---|
| `cccc.<profile>.cognitive.p95`、`.max` | 検証済みsummaryまたは同一母集団の全関数明細 |
| `cccc.<profile>.cognitive.over_limit_count`、`.over_limit_ratio` | 関数の認知値がプロファイル閾値を厳密に超える件数／全関数数 |
| `cccc.<profile>.cyclomatic.p95`、`.max` | 循環的複雑度について同様 |
| `cccc.<profile>.cyclomatic.over_limit_count`、`.over_limit_ratio` | 関数の循環値が閾値を厳密に超える件数／全関数数 |
| `cccc.<profile>.analysis.files`、`.functions` | summaryのfile_count、function_count |
| `cccc.<profile>.analysis.parse_errors`、`.parse_error_files` | summaryのparse_error_count、parse_error_file_count |
| `cccc.<profile>.analysis.complete` | ラッパーが全条件を検証した場合1、比較無効なら0。完全性が不明なら欠測 |
| `cccc.<profile>.analysis.last_success_timestamp` | 最後に比較可能な測定が完了した時刻。品質警告の有無とは別 |

混在言語からプロファイル別に分ける場合、JSONにlanguageフィールドがあるとは仮定せず、採用版の拡張子dispatchと明示設定を記録するか、言語別に別runで採取する。混在summaryを各言語のsummaryとして複製しない。

agentはまず測定の有効性・鮮度・対象件数を確認し、悪化した認知／循環指標から関数明細へ進む。同一revisionの変更履歴・失敗テスト・カバレッジ・APMを照合して改善候補を選ぶ。関数名やrun IDをメトリック名に展開せず、元JSONと前後比較をCI成果物として残す。複雑度低下だけで性能改善や残存バグ数を判定しない。

## アダプター実装時の検証

- 親・子関数、無名関数、認知値0の関数、関数なしのファイルを含む実レポートで、再帰的な関数数とsummaryを照合する。
- `--min`、`--top-*`、設定ファイルのtable指定による明細の欠落を検出する。
- parse error、読取失敗、対象0件、閾値超過、設定エラーを区別し、終了コードだけで成功を判定しない。
- 閾値境界は`>`、ratioは0〜1、p95はnearest-rankとして確認する。定義を変える場合はプロファイルを更新してbaselineを再取得する。

確認日: 2026-10-26。参照した上流mainのcommitは`5222b790b32eb4fd2ec012f1b6462551a0120dfd`。READMEのmainと採用releaseの対応・出力が同じとは仮定しない。これは仕様確認を反映したアダプター設計であり、CCCCのインストール・対象コードの解析・Mackerel投稿を実行済みという意味ではない。
