# mackerel-DevImprove-agent-skills

Mackerelを活用した開発改善のためのagent Skillsを保存するリポジトリです。OpenTelemetry・[moznion/cccc](https://github.com/moznion/cccc)を組み合わせ、開発環境の性能、テスト結果、コード複雑度を観測し、改善と効果検証を進めます。

最初のSkillとして、導入から検証までを扱う `mackerel-dev-improve` を収録しています。

テストケース数・試行数・CI実行数を区別し、認知複雑度・循環的複雑度、カバレッジ、変更履歴、不具合を照合します。指標の低下だけで改善と判定せず、測定の完全性と変更前後の挙動を確認します。

## 内容

- [Skill本体](skills/mackerel-dev-improve/SKILL.md)：導入・調査・改善・検証の手順
- [計測契約](skills/mackerel-dev-improve/references/measurement-contract.md)：系列、母数、測定条件、成果物の対応
- [テスト結果](skills/mackerel-dev-improve/references/test-results.md)：成否、実行数、再試行、カバレッジ、CRAP
- [CCCCアダプター](skills/mackerel-dev-improve/references/cccc-adapter.md)：moznion/ccccのJSONと認知・循環的複雑度
- [Mackerelダッシュボード](skills/mackerel-dev-improve/references/mackerel-dashboard.md)：投稿、可視化、agentの参照手順
- [OTel導入](skills/mackerel-dev-improve/references/otel-setup.md)：APM・ログ・実行時メトリック
- [改善判断](skills/mackerel-dev-improve/references/decision-policy.md)：閾値、優先順位、効果検証
- [プロジェクト設定例](skills/mackerel-dev-improve/assets/project.example.yaml)

## 利用

`skills/mackerel-dev-improve`フォルダー全体を、利用するagentが対応するSkill配置先へ登録してください。`SKILL.md`から必要な参照資料を読み、対象プロジェクトの言語・CI・テストランナーに合わせて導入します。

依頼例：

```text
mackerel-dev-improveを使い、このプロジェクトのテスト結果とmoznion/ccccの
複雑度をMackerelで可視化する構成を作成してください。
既存のCI・テストコマンドを確認し、OTelのAPM・ログと照合できるようにしてください。
```

`project.example.yaml`はこのSkill独自の設定契約を示す例です。CollectorやCCCCがそのまま読み込む設定でも、自動実行するプログラムでもありません。サービス名、解析対象、計測プロファイル、閾値は利用先で設定します。

## データの流れ

```mermaid
flowchart TD
    App[開発環境のアプリ] --> OTel[OpenTelemetry Collector]
    OTel --> Runtime[Mackerel APM・ログ・ラベル付きメトリック]
    CI[テスト・CCCC・その他の解析] --> Normalize[正規化・集計]
    Normalize --> Metrics[Mackerel サービスメトリック]
    Normalize --> Evidence[詳細成果物・測定条件]
    Runtime --> Agent[改善agent]
    Metrics --> Agent
    Evidence --> Agent
    Agent --> Verify[修正・同条件での効果検証]
```

## 実装状況

このリポジトリはSkillの手順・計測仕様・設定例を収録しています。共通publisherや各レポート変換スクリプトは未実装です。対象アプリの実解析、Mackerelへの実データ投稿、ダッシュボード表示の検証は利用先で行ってください。

製品仕様の確認日は2026-10-26です。利用時は採用するツールの版を固定し、リンク先の公式仕様と実出力を確認してください。
