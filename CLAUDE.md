# CLAUDE.md - ihealth-relay

## プロジェクト概要

iPhone の Health Auto Export アプリが Apple Health のデータを iCloud Drive に書き出し、macOS 側の Python バッチがそれを読んで日次に集計・転記するツール。

- **主経路: Obsidian 日記** (`--publisher obsidian`): 健康 (Health Auto Export の **オートメーション JSON** が主、AutoSync `.hae` が補完・fallback) + **気象庁の天気** + **GitHub 活動** を、日記ノート `<vault>/<日記ディレクトリ>/**/YYYY-MM-DD.md` の frontmatter に書く。複数日 × 3 source を独立に取得する (`src/ihealth/diary.py`)。
- **単日 Sink**: AutoSync `.hae` の 1 日分を pluggable sink (Notion / Markdown / stdout / SQLite / Slack / dryrun) に転記する従来経路 (`src/ihealth/workflow.py`)。

前身の GoogleFitNotionIntegration (Python / Google Fit / Cloud Functions → Notion) からの移行プロジェクト。日記は Notion から Obsidian に移行済みで、天気・GitHub・アクティビティ詳細の **書式は前身が Notion に書いていた値と完全互換** にしている。

> **実装状況 (2026-10-12, v0.2.0)**: Obsidian 日記化が完了。`--publisher obsidian` / `.env` の `IHEALTH_PUBLISHER` で既定 publisher を切替、`--from` / `--to` / `--sources` / `--fill-empty-only`、既定は「昨日から `IHEALTH_LOOKBACK_DAYS` (7) 日分の確定値 + 今日の暫定値」。暫定値は `data/diary-state.json` (台帳) で確定するまで追跡し、引数なしの実行で 30 日まで処理し直す。launchd は `IHEALTH_PUBLISHER=obsidian` なら 07:00 / 12:00 / 21:00 の 1 日 3 回、それ以外の publisher は従来どおり 07:00 のみ (install-launchd.sh が `.env` を見て決める)。実行レポート `logs/diary-report-<UTC>.json`、失敗は 1 run 1 通の Reminders 通知 (同一内容は 1 日 1 回。設定エラーも)、データの鮮度の通知 (オートメーション停止 / 出力なし / 暫定値が確定しない)、書込前バックアップ `data/obsidian-backup/`、終了コード 11 / 12 を追加。あわせて AutoSync 側の修正: HAE1 コンテナ形式の `.hae` 解凍、妥当性検証の巻き添え drop 修正 (`parser.drop_invalid_fields`)、日跨ぎ睡眠の二重計上修正。
>
> **それ以前 (2026-06-28)**: Sink プラガブル化 (`--publisher` で notion / markdown / stdout / sqlite / slack / dryrun)。Notion PROPERTY_MAP は英語 Title Case 名が既定 (英語以外の DB 列名は `config.toml` で override)。主要メトリクス欠落時の retry (120s × 5 回, `step_count`/`heart_rate` のみ critical)、Reminders.app 永続通知、dataless `.hae` の自動 materialize (`brctl download` + polling)。`exercise_minutes` 追加 + `exercise_intensity_score = 0pt` を None に潰さず 0 で記録 + materialize timeout 120s (`IHEALTH_MATERIALIZE_TIMEOUT_SEC` で上書き可)。
>
> 残作業: Issue #11 (時間プロパティの hours 表記移行) と #13 (Heart Points 厳密 Google Fit 準拠)。

**設計上の重要な選択**: Swift + HealthKit 直叩きは `com.apple.developer.healthkit` entitlement のために Apple Developer Program (年 99 USD) への加入が必須だったため断念し、iPhone 側の Health Auto Export にデータ抽出を委任する構成に変更した。

**オートメーション JSON について**: Health Auto Export の **オートメーション** (iCloud Drive / JSON / ExportVersion.v2 / aggregateData=true (1 分集計) / aggregateSleep=true / exportAggregation=Minutes / exportFileLength=day) は `<HAE Documents>/<オートメーション名>/HealthAutoExport-YYYY-MM-DD.json` に 1 日 1 ファイルを書く。フォルダ名は利用者が付けるので固定パスにできず、`HEALTH_EXPORT_DIR` 以下をファイル名パターンで **自動探索** する (`src/ihealth/automation.py`, 深さ 4, 隠しディレクトリと symlink は除外)。同じ日付が複数フォルダにあれば **mtime が最新** (tie は size 大 → path 順) を採用し、壊れていれば次の候補へ。オートメーションが動くたびに直近数日のファイルが書き直され、当日のファイルは書き出し時点までの途中経過になる。

**`.hae` について (2026-04-24 再調査で判明)**: Health Auto Export が AutoSync で出力する `.hae` は独自バイナリではなく、**LZFSE (Apple 公開の圧縮アルゴリズム) で圧縮された JSON** である。macOS 標準の `/usr/bin/compression_tool` で解凍できるため、外部依存ゼロ原則を維持したまま Python から扱える。新しい HAE は **HAE1 コンテナ** (`b"HAE1"` + `[uint32 big-endian 長][LZFSE stream]` の繰り返し) で書くことがあり、segment ごとに解凍して `data` を連結する (`source._decompress_hae1`)。

## 技術スタック

- **言語**: Python 3.9+（macOS 標準同梱の `/usr/bin/python3` が 3.9.6 で凍結されているため、互換性を確保するのが最優先）
- **ランタイム**: `/usr/bin/python3`（macOS 14/15 いずれも Command Line Tools 経由で 3.9.x を提供、追加インストール不要）
- **プラットフォーム**: macOS 14+ (Sonoma), Apple Silicon (M4)
- **依存ライブラリ**: なし（Python 標準ライブラリのみ、`requirements.txt` も venv も不要）。HTTP は `urllib.request`、気象庁 HTML は `html.parser`
- **iPhone 側**: Health Auto Export - JSON+CSV (App Store, App ID 1115567069, 買い切り Premium)
- **外部サービス (Obsidian 日記)**: 気象庁「過去の気象データ検索」(`hourly_s1.php`)、GitHub GraphQL API (token は `GITHUB_TOKEN` か `gh auth token`)
- **実行方式**: `python3 -m ihealth` → launchd で定時実行 (obsidian は 07:00 / 12:00 / 21:00、他の publisher は 07:00)
- **通知**: AppleScript 経由で Reminders.app にリマインダーを作成（自動消滅しない、iPhone にも同期される）

## 開発コマンド

`PYTHONPATH=src` が必要 (`./bin/ihealth-run` は設定済み)。

```bash
python3 -m ihealth                                    # 既定 publisher (IHEALTH_PUBLISHER → notion) で実行
python3 -m ihealth --publisher obsidian               # 日記: 昨日から 7 日分 + 今日
python3 -m ihealth --publisher obsidian --dry-run     # 日記: 差分をログに出すだけ (vault に書かない)
python3 -m ihealth --publisher obsidian --date 2026-10-10 --sources weather,github
python3 -m ihealth --publisher obsidian --from 2026-01-01 --to 2026-09-30 --fill-empty-only  # backfill
python3 -m ihealth --date 2026-04-10                  # 単日 Sink: 指定日のデータを処理
python3 -m ihealth --dry-run                          # 単日 Sink: Notion に書き込まない動作確認
python3 -m ihealth --verbose                          # 詳細ログ
PYTHONPATH=src /usr/bin/python3 -m unittest discover -s tests   # テスト実行
./bin/ihealth-run [--publisher obsidian] [--date YYYY-MM-DD]     # 手動実行ラッパー (launchd と同じエントリ)
./bin/install-launchd.sh                              # launchd へ登録 (obsidian は 07/12/21 時、他は 07 時)
./bin/install-launchd.sh --print-plist                # 登録する plist を表示するだけ
```

## ディレクトリ構成

- `src/ihealth/` - メインコード
  - `__main__.py` - エントリポイント (`python3 -m ihealth`)。publisher の決定 (`--publisher` → `IHEALTH_PUBLISHER` → `notion`) と、obsidian なら `diary`、それ以外は `workflow` への振り分け
  - `config.py` - `.env` 読み込み (`PUBLISHER_KINDS` / `SOURCE_NAMES` の定義元)
  - `config_file.py` / `_toml.py` - `config.toml` 読み込み (Notion / Obsidian のキー名 override)
  - `logger.py` - ログ設定
  - `errors.py` - `AppError` 階層 → 終了コード
  - `models.py` - `DailyHealthData` dataclass (date + 健康 15 フィールド)
  - `diary.py` - Obsidian 日記パイプライン (日付の決定・3 source の取得・実行レポート・通知・停止検知)
  - `automation.py` / `automation_parser.py` - オートメーション JSON の探索・読込・日次集計
  - `health_loader.py` - 1 日分の健康データ (オートメーション JSON 優先 + AutoSync `.hae` 補完、確定 / 暫定の判定)
  - `source.py` - AutoSync `.hae` の検出・materialize・LZFSE / HAE1 解凍・アーカイブ
  - `parser.py` - AutoSync `.hae` のメトリクス別集計 + `drop_invalid_fields`
  - `weather.py` - 気象庁 hourly_s1 の取得・解析・日次サマリ
  - `github_activity.py` - GitHub GraphQL での活動収集と書式化
  - `records.py` - `DiaryRecord` (1 日分の日記書き込み値)
  - `frontmatter.py` - frontmatter の byte 保存 upsert (純関数)
  - `workflow.py` - 単日パイプライン + composition root (`build_default_deps` / `build_obsidian_publisher`)
  - `publishers/obsidian.py` - 日記ノートの frontmatter 更新 (バックアップ・競合検知・不在ノート作成)
  - `publishers/notion.py` ほか - 単日 Sink (`urllib.request` ベースの Notion クライアント等)
  - `notifier.py` - Reminders.app 連携
- `bin/` - 実行ラッパー・セットアップスクリプト
- `data/health/` - 受信した JSON のアーカイブ（`YYYY-MM-DD/{metric}.json` 形式、`.gitignore` 対象）
- `data/obsidian-backup/` - 日記ノートの書込前バックアップ (`<UTC 日付>/<ノート名>.<UTC 時刻>.md`, 0600, 30 日保持, `.gitignore` 対象)
- `data/diary-state.json` - 暫定値の台帳 (日付 → 暫定値として書いたキーと値。確定したら消える, `.gitignore` 対象)
- `logs/` - `run.log` (30 日ローテーション)、`diary-report-<UTC>.json` (実行レポート, 30 日)、`.failure_notified` / `.stale_notified` (通知の重複抑止)
- `tests/` - ユニットテスト (`unittest`)。`tests/fixtures/` は合成データ (`sample_health_export/` = AutoSync、`automation/` = オートメーション JSON、`jma/` = 気象庁ページ)
- `LaunchAgent/` - `com.ihealthrelay.daemon.plist`

## データフロー

### Obsidian 日記 (`--publisher obsidian`, 主経路)

1. **iPhone (常時)**: Health Auto Export の **オートメーション** が日次 JSON を iCloud Drive の `<HAE Documents>/<オートメーション名>/` に書く (動くたびに直近数日を書き直す。当日分は途中経過)。AutoSync (`.hae`) は止めていても過去分の補完・backfill に使う
2. **iCloud 同期**: ほぼ全ファイルが dataless (placeholder) のまま見える
3. **macOS (07:00 / 12:00 / 21:00)**: launchd が `bin/ihealth-run` を引数なしで起動 → `.env` の `IHEALTH_PUBLISHER=obsidian` で日記パイプライン
4. **対象日の決定** (`diary.resolve_dates`): 昨日から `IHEALTH_LOOKBACK_DAYS` 日分 (確定値) + 今日 (暫定値, `IHEALTH_INCLUDE_TODAY`)。`--date` / `--from` / `--to` で上書き。「今日」は `IHEALTH_TZ_OFFSET` (既定 +09:00)。引数なしの実行では、暫定値の台帳 (`data/diary-state.json`) にある日も 30 日前まで加える (`DiaryOptions.revisit_unresolved`)。window の外から加えた日は fill-only (空のキーを埋める + 台帳の暫定値のままのキーを置き換える) で書き、利用者の値は上書きしない
5. **ネットワーク待ち** (`NetworkGate`): スリープ復帰直後に備え最大 `IHEALTH_NETWORK_GRACE_SEC` (60) 秒。猶予内に解消した接続エラーは取り直して失敗扱いしない
6. **GitHub**: 期間全体を 1 回で収集 (`collect_github_activity`)。不完全なら期間中どの日も `Github` を書かない
7. **日付ごとに独立取得**:
   - 健康 (`HealthLoader.load`): オートメーション JSON を `_ensure_materialized` してから読み、JSON で None のフィールドを同日の AutoSync `.hae` で補う。JSON が無い日は `.hae` だけ
   - 天気 (`fetch_weather`): 気象庁 hourly_s1 (1 リクエスト / 秒以下)。今日 (JST) は未公開なので取らない
   - GitHub: 手順 6 の結果からその日の文字列
8. **書き込み**: `DiaryRecord` → `ObsidianPublisher.publish_record` → `frontmatter.upsert_frontmatter` (担当キーだけ置換・追記。`--fill-empty-only` でも台帳の暫定値のままのキーは `replaceable` で置き換える) → 書込前バックアップ → 隠し tmp + fsync → 置換直前の再確認 → `os.replace`
9. **台帳の更新**: 暫定の source (今日の健康・アクティビティ詳細・GitHub) で書いたキーを値とともに記録し、確定値を書けた / 利用者が書き換えたキーを外す (保存直前に読み直して処理した日だけ反映。`--dry-run` は書かない)
10. **仕上げ**: 天気の未完成が 2 日以上続けば失敗に、オートメーションの最新出力日が今日 − 3 日より古ければ停止の疑い、オートメーションの出力が 1 件も無く健康データを 1 日も取れなければ「出力が見つからない」、3 日を過ぎても確定しない暫定値があれば「暫定値が確定していない」→ 実行レポート `logs/diary-report-<UTC>.json` → 失敗とデータの鮮度を 1 通の Reminders 通知に集約 (同一内容は 1 日 1 回。作れなかった通知は記録せず次回再送。`--dry-run` は送らない) → 終了コード 0 / 11 / 12

### 単日 Sink (notion / markdown / stdout / sqlite / slack / dryrun)

1. **iPhone (常時)**: Health Auto Export の **AutoSync** (Premium) が HealthKit のデータを **メトリクス別 1 日 1 ファイル**の `.hae` (LZFSE 圧縮 JSON) として iCloud Drive 上の専用フォルダに書き出す。パスは `~/Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents/AutoSync/HealthMetrics/{metric_snake}/{YYYYMMDD}.hae`
2. **iCloud 同期**: `.hae` が macOS 側の iCloud Drive 配下から見えるようになる（Health Auto Export の iCloud Drive 専用領域なので、常に上記の固定パス）
3. **macOS**: launchd (07:00。`IHEALTH_PUBLISHER` が obsidian 以外のとき) または手動で `python3 -m ihealth` を起動 (既定は前日)
4. **Python**: `HEALTH_EXPORT_DIR`（= `AutoSync` ディレクトリ）配下から対象日の 11 メトリクス分の `.hae` を検出
5. **Python**: `/usr/bin/compression_tool -decode -a lzfse` で各ファイルを解凍 (HAE1 は segment ごと) し、`./data/health/YYYY-MM-DD/{metric}.json` にアーカイブ（以降の処理はコピー先が正）
6. **Python**: メトリクスごとに JSON をパース → 単位フィルタ・重複除去・日次集計（SUM / MEAN / MAX） → `drop_invalid_fields` → `DailyHealthData` dataclass に変換
7. **Python**: 選択された publisher で出力. Notion なら日付検索 → 該当ページを PATCH で更新, Markdown なら frontmatter ファイルを atomic write, SQLite なら UPSERT, Slack なら Webhook 投稿, stdout なら JSON 1 行を標準出力
8. **失敗時**: Reminders.app にリマインダーを追加（ユーザーが完了するまで残り、iPhone 側にも iCloud 同期で届く）

## 重要な設計判断

1. **外部依存ゼロ**: サブスク・外部サービス依存なしが要件。HTTP は `urllib.request`、JSON は `json`、HTML は `html.parser`、ファイル操作は `pathlib` で処理
2. **Apple Developer Program 回避**: HealthKit 直叩きをやめ、iPhone 側 Health Auto Export に委任
3. **CLI ファースト**: GUI なし。バックグラウンドで静かに動くことが優先
4. **`.env` ベースの設定**: `HEALTH_EXPORT_DIR` / `BIRTH_DATE` は常時必須。`NOTION_SECRET` / `DATABASE_ID` は実際に notion を使うときだけ必須 (`IHEALTH_PUBLISHER` を読むため、まず Notion なしで `.env` を読み、notion に決まった時点で `ensure_notion_settings` で検証)。`OBSIDIAN_VAULT_DIR` は obsidian のとき必須。他の Obsidian 用キーはすべて optional。`BIRTH_DATE` は運動強度スコア (Heart Points) の最大心拍数 (Tanaka 式) 算出に使う
5. **冪等実装**: 同じ日付で何度実行しても Notion ページ上書き / Markdown ファイル再生成 / SQLite UPSERT / Slack 再投稿になるだけ。Obsidian は値が変わったキーがあるときだけノートを書き換える (1 日 3 回 × 7 日の再処理でも無駄な書込・バックアップ・Sync を起こさない)
6. **エラー時の通知は永続的**: 通知センター（自動消滅する）ではなく Reminders.app を採用
7. **成功時は静か**: 成功ログのみ残し通知は出さない、ユーザー介入ゼロ運用
8. **担当キー以外は 1 byte も変えない**: 日記は利用者が手で書くノート (他のツールが本文に追記したり、日付キーだけの最小ノートを作ったりする運用もある)。`frontmatter.py` は担当 entry (key 行 + 継続行) の置換と閉じ `---` 直前への追記しかしない。本文・担当外 entry・コメント・空行・BOM・改行コード・末尾改行は不変。Obsidian 本体と解釈が食い違う frontmatter (閉じ `---` 欠落など) には書かない (`FrontmatterError` → 書込失敗)
9. **確実な値だけを書く (完全性ステータス)**: 各 source は `complete` / `partial` / `failed` / `nodata` を返し、publisher には確実に取れたキーだけ渡す。渡さないキーは既存値保持。欠測を 0 で埋めない (前身の天気は欠測区分を 0℃ / 0% で書いていた)
10. **確定値 / 暫定値**: HAE のファイルの mtime が対象日の終了 (Mac のローカル時刻で翌日 0:00) 以降なら確定。今日は暫定 (`provisional`) として書き、後の実行で上書きする。終わった日なのに終了前の出力しか無ければ保留 (`withheld_fields`, レポートの `not_final`)。暫定値は台帳 (`data/diary-state.json`) で確定するまで追跡する: 7 日の window から外れても引数なしの実行が 30 日まで処理し直し、3 日を過ぎても確定しなければ 1 日 1 回通知 (HAE の再出力が window より遅れると暫定値が確定値の顔で残り、`--fill-empty-only` の backfill でも直せなくなるため)。天気は当日・公開待ち (今日 − 2 日まで) を `nodata` として書かない (それより古い日は観測行が 0 件でも `partial`)。GitHub は収集が完全なときだけ書き、活動なしが確定した日だけ `"該当なし"`
11. **書込前バックアップ + 競合検知**: 既存ノートを書き換える前に `data/obsidian-backup/` へ保存 (失敗したら書かない)。読込時の `(st_mtime_ns, st_size, st_ino)` + 内容を置換直前に再確認し、不一致なら読み直してやり直す (試行は最初の 1 回を含めて最大 3 回)。3 回とも不一致なら書かずに失敗。再確認から `os.replace` までの μs 窓の競合は原理的に排除できない (Obsidian とロックを協調する仕組みが無い)。この窓で他のプログラムが書いた内容はバックアップに入らないので、Obsidian の「ファイル復元」コアプラグインか Obsidian Sync のバージョン履歴から戻す
12. **ノートの新規作成は慎重に**: 今日・昨日のノートは作らない (`OBSIDIAN_CREATE_MIN_AGE_DAYS`=2。Obsidian Sync は Obsidian 起動中しか同期しないので、別端末で作られた同じ日のノートと「共通の祖先が無い同名ファイル」として衝突しうる)。作るのは `OBSIDIAN_TEMPLATE` があるとき (または `OBSIDIAN_CREATE_MISSING`) だけで、Templater は限定展開 (先頭 `<%* … %>` 除去 / `<% dateStr %>` / `<% weekday %>`)、それ以外の `<%` が残れば作らない。作成先は前後 7 日で最も近い既存ノートと同じフォルダ。`os.link` による排他作成で他者のノートを上書きしない
13. **3 source は独立**: 1 source の失敗は他の source・他の日付を止めない。終了コードは書込失敗 / 全 source 全日付の失敗 → 11、それ以外の失敗 → 12。設定不備 (vault / 日記 dir 不在、`config.toml` 不正、キー衝突) は 3
14. **通知は集約**: 1 run 1 通 (失敗とデータの鮮度の通知が両方あれば 1 件にまとめる)。同じ失敗内容 (日付・source・メッセージの sha256) は 1 日 1 回 (`logs/.failure_notified`。`.env` / `config.toml` の設定エラーと想定外エラーも内容の sha256 で 1 日 1 回)。データの鮮度 (オートメーション停止の疑い / 出力が見つからない / 暫定値が確定しない) も 1 日 1 回 (`logs/.stale_notified`)。リマインダーを作れなかった (`notify_failure` が False) 通知は記録しない (次回再送)。`--dry-run` は通知せず記録もしない。`nodata` (未出力・公開待ち) と、ノートが無く作らなかった日 (`too_recent` / `create_disabled`) は通知しない。利用者の対応が要るノート (`unprocessed_template` / `template_unavailable`。書く値があった日だけ) は通知 + 12
15. **前身互換の書式**: 気温 / 湿度 / 気圧 / 天気 / 日照時間 / 降水量 / `Github` / アクティビティ詳細は前身が Notion に書いていた文字列と同一書式。`Github` は Obsidian Bases の formula (例: `note["Github"].contains("変更行数")`) から参照されうるので書式を崩さない

## 日記 frontmatter / Notion のプロパティマッピング

Obsidian の既定キーは日本語 (`publishers/obsidian.py` の `DEFAULT_PROPERTY_MAP`、並びは日記テンプレートの順 = 新規キーの追記順)。Notion の既定プロパティは英語 (`publishers/notion.py` の `PROPERTY_MAP`)。どちらも `config.toml` の `[publishers.<obsidian|notion>.fields.<内部名>] property` で上書きでき、2 つの内部名が同じ名前に向くと設定エラー (exit 3)。

### 健康 (15 フィールド)

| 内部フィールド | オートメーション JSON の metric | AutoSync (`HealthMetrics/<dir>/`) | 集計ルール | Obsidian キー (既定) | Notion プロパティ (既定) | 書き込み形式 |
|---|---|---|---|---|---|---|
| step_count | `step_count` | `step_count` | SUM(qty) | `歩数 (歩)` | `Step Count` | 整数 |
| distance_km | `walking_running_distance` | `walking_running_distance` | SUM(qty)。JSON は km / mi / m を換算、`.hae` は km のみ | `移動距離 (km)` | `Distance (km)` | round 1 桁 |
| active_energy_kcal | `active_energy` | `active_energy` | SUM(qty)。JSON は kJ ÷ 4.184 を換算、`.hae` は **kcal のみ** (kJ は捨てる) | `消費カロリー (kcal)` | `Active Energy (kcal)` | round 1 桁 |
| exercise_minutes | `apple_exercise_time` | `apple_exercise_time` | SUM(qty) (min) | `アクティビティ時間 (分)` | `Exercise Minutes` | 分 (round 整数) |
| exercise_intensity_score | `heart_rate` | `heart_rate` | Heart Points 近似: record を 1 分 bucket (JSON は `epoch // 60`、`.hae` は `int(start // 60)`) に分け、bucket 内 Avg 平均が ≥ 70% max_hr → 2pt, ≥ 50% → 1pt を総和。**心拍データがあって zone 未到達なら 0** (「未測定」と「運動ゼロ」を区別)。心拍データが無ければ None | `運動強度スコア` | `Exercise Intensity Score` | round 1 桁 (整数値は整数表記) |
| heart_rate_avg | `heart_rate` | `heart_rate` | JSON: **1 分集計の Avg の平均** (データのある 1 分を同じ重み) / `.hae`: MEAN(avg) (レコード単位) | `平均心拍数 (bpm)` | `Heart Rate Avg (bpm)` | round 1 桁 |
| heart_rate_max | `heart_rate` | `heart_rate` | MAX(Max) | `最大心拍数 (bpm)` | `Heart Rate Max (bpm)` | round 1 桁 |
| heart_rate_resting | `resting_heart_rate` | `resting_heart_rate` | MEAN(qty) (`.hae` は unit="count/min" のみ) | `安静時心拍数 (bpm)` | `Heart Rate Resting (bpm)` | round 1 桁 |
| oxygen_saturation | `blood_oxygen_saturation` | `blood_oxygen_saturation` | MEAN。JSON は 1.0 以下の値 (fraction 表記) を ×100 | `酸素飽和度 (%)` | `Oxygen Saturation (%)` | round 1 桁 |
| sleep_hours | `sleep_analysis` | `sleep_analysis` | JSON aggregateSleep: その日の `totalSleep` (**昼寝込み**。sleepEnd が 18 時以降のセッションは翌日付)、複数ソースは最大 / JSON segment 形式・`.hae`: **夜主睡眠** = 終了 hour ∈ [0,14) の asleep をソース別 SUM → MAX | `睡眠時間 (分)` | `Sleep Minutes` | **分** (hr × 60, 整数) |
| nap_hours | `sleep_analysis` | `sleep_analysis` | JSON aggregateSleep: None (分離不能。同日の `.hae` で補完) / segment 形式・`.hae`: **昼寝** = 終了 hour ∈ [14,20) をソース別 SUM → MAX | `昼寝時間` | `Nap Minutes` | **分** (hr × 60, 整数) |
| mindful_sessions | `mindful_minutes` | `mindful_minutes` | COUNT (JSON で `source` が `\|` 区切りの record はソース数で数える) | `瞑想回数 (回)` | `Mindful Sessions` | 回 |
| mindful_minutes | `mindful_minutes` | `mindful_minutes` | SUM(qty) (qty が無ければ end − start) | `瞑想時間 (分)` | `Mindful Minutes` | 分 (round 整数) |
| body_mass_kg | **`weight_body_mass`** | **`weight_body_mass`** | 最新 qty。JSON は kg / lb / g を換算、`.hae` は **kg のみ** (lb は捨てる) | `体重 (kg)` | `Body Mass (kg)` | round 1 桁 |
| body_fat_percentage | `body_fat_percentage` | `body_fat_percentage` | 最新 qty。JSON は 1.0 以下を ×100 | `体脂肪率 (%)` | `Body Fat (%)` | round 1 桁 |

### Obsidian 日記だけのキー

| 内部名 | source | Obsidian キー (既定) | 書式 (前身互換) |
|---|---|---|---|
| activity_detail | 健康 (mindful_minutes + JSON の `data.workouts` / apple_exercise_time + 「なし」の根拠に heart_rate。AutoSync `.hae` だけの日も同じ規則) | `アクティビティ詳細` | `automation_parser.compose_activity_detail`: ① 瞑想 (合計 round ≥ 1) `"瞑想N分"` → ② 当日のワークアウト (round ≥ 1 分のもの。1 分未満はワークアウト無しと同じ) があれば各 `"{日本語名}N分"` (英→日、未知名はそのまま。エクササイズ時間は足さない)、無ければ (ワークアウト未出力 / 当日 0 件) `apple_exercise_time` が round ≥ 1 なら `"エクササイズN分"`、を `"、"` で連結 (例 `"瞑想3分、エクササイズ21分"`)。③ 何も無い日は心拍データがある (`has_heart_rate`: 補完・妥当性検証後の平均 / 最大心拍数・運動強度スコアのどれかに値 = Watch 装着) なら `"なし"`、無ければ書かない (qty 0 のエクササイズ record があっても心拍が無ければ書かない)。④ 依存する入力 (瞑想・ワークアウト・使う場合のエクササイズ時間) が集計失敗 / 妥当性違反 / 補完元 `.hae` の保留・読込失敗なら、心拍があっても書かない |
| temperature | 気象庁 | `気温` | `"朝:平均{:.1f}℃, 昼:平均{:.1f}℃, 夜:平均{:.1f}℃（最高:{:.1f}℃, 最低:{:.1f}℃）"` |
| humidity | 気象庁 | `湿度` | `"朝:平均{:.1f}%, 昼:平均{:.1f}%, 夜:平均{:.1f}%（最高:{}%, 最低:{}%）"` (最高・最低は整数) |
| pressure | 気象庁 (海面気圧) | `気圧` | `"1-6時:平均{:.1f}hPa{mark}, 7-12時:…, 13-18時:…, 19-24時:…"`。span の最大 − 次 span のいずれか ≥ 5 → `⤵️💣️`、次 span のいずれか − span の最小 ≥ 5 → `⤴️⚠️` (最後の span は無印) |
| weather | 気象庁 (天気欄 img alt) | `天気` | `"3時: 快晴☀️☀️, 6時: 晴れ☀️, …"` (絵文字は 快晴 → 晴 → 薄曇 → 曇 → 雨 → 雪 の順に部分一致) |
| sunshine | 気象庁 | `日照時間` | `"{:.1f}時間"` |
| precipitation | 気象庁 | `降水量` | `"朝:{:.1f}mm, 昼:{:.1f}mm, 夜:{:.1f}mm"` |
| github | GitHub GraphQL | `Github` | 1 件 1 行 (`\n` 連結): `[🎫 {repo}:Issue #{n}: {title}]({url})` / `[🔀 {repo}:PR #{n}: {title}]({url})` / `[📝 {repo}:変更行数:+{add}-{del} ({n} commits)]({commits url})`。活動なし (収集完全) は `"該当なし"` |

天気の区分: 気象庁の「N 時」の行は (N−1):00〜N:00。朝 = 5〜11 時、昼 = 12〜18 時、夜 = 19〜24 時 + 1〜4 時。区分が全欠測のキーは書かない。降水量・日照時間は欠測が 1 時間でもあれば書かない (合計が過小になるため)。`--` (現象なし) と夜間の日照の空欄は 0。

GitHub の対象: 自分が owner の repo + 所属 org の repo (owner ごとに `repositoryOwner(login).repositories(ownerAffiliations: [OWNER])` で列挙。archived も含む)、`max(pushedAt, updatedAt)` が期間開始以降のもの。Issue close (`closedAt`) / PR merge (`mergedAt`) / default branch の直コミット (committer date)。作者は問わないが bot・merge commit・PR ブランチ上の commit は除外、squash / rebase merge された commit は直コミットとして数える。**fork の commit は viewer 本人が作者のものだけ** (fork 同期で upstream の commit が元の日付のまま入るため)。`GITHUB_OWNERS` / `GITHUB_EXCLUDE_REPOS` で絞る。

📌 **Notion プロパティ名の表記ルール** (Phase 1 B2 で 2026-04-29 に英語化):
- 既定は `Step Count` / `Distance (km)` / `Body Mass (kg)` 等の **Title Case + 単位 (km, kcal, bpm, %)**. Notion API で文字列完全一致が必要なので、空白・括弧の幅 (半角) も含めて正確に揃える.
- 既存の Notion DB が英語以外の列名 (例: 日本語 `歩数 (歩)`) の場合は `config.toml` でフィールドごとに override する. `config.example.toml` に日本語マッピング例を記載済み.
- 日付列の名前 (`find_page_by_date` の filter キー) も `Date` を既定とし、別名なら `[publishers.notion] date_property = "日付"` 等で override する.
- Notion API で DB に存在しないプロパティは `publishers/notion.py` が**警告ログだけ残してスキップ**する設計なので、追加プロパティ (例: `Nap Minutes`) が DB に無くても他の書き込みは壊れない.

📌 **Obsidian キーのルール**:
- テンプレート由来のノートでは `"歩数 (歩)":` のような quoted key、Obsidian が書き直したノートでは `歩数 (歩):` の plain key になる。`frontmatter.py` は両方を同じキーとして扱う (NFC 正規化して完全一致)。
- ノートに無いキーは閉じ `---` の直前に、property map の順で追記する。
- `[publishers.obsidian] date_property` (既定 `日付`) は `OBSIDIAN_CREATE_MISSING=minimal` の最小ノートの日付キーにだけ使い、値は書かない。書き込み対象のキーと同名にはできない。

📌 **将来 TODO**: 当面は `Sleep Minutes` / `Nap Minutes` / `Mindful Minutes` のように分単位で書き込むが、小数 2 桁の時間表記 (例: `7.2h`) のほうが直感的なので、プロパティ名ごと後日リネーム予定 (Issue #11)。

⚠️ **罠ポイント**:
- `body_mass_kg` のディレクトリ / metric は `weight_body_mass` で、`body_mass_index` (BMI) とは**別物**。誤って BMI を書き込まないよう、明示的ホワイトリスト (`METRIC_DIRS` / metric 名の完全一致) で紐付ける
- `active_energy` / `weight_body_mass` / `apple_sleeping_wrist_temperature` などは **同じ qty を複数単位で重複出力**するため、メトリクスごとに優先単位を固定し、それ以外を捨てる。オートメーション JSON で同名 metric が複数回出た場合も優先単位 (kcal > kJ, kg > lb > g, km > m > mi) の 1 つだけ採用する (加算しない)
- `sleep_analysis` は AutoSleep と Apple Watch から**同じ期間の睡眠データが重複**して来る。ソースごとに合計 → 最大ソースを採用（単純 SUM は二重計上 / 単純 MAX は 1 フェーズしか拾えない）
- AutoSync の `sleep_analysis` の `.hae` は「その日に**重なる**レコード」を含むので、日付をまたぐセッションは前日ファイルと当日ファイルの**両方**に同じレコードで入る。前日 + 当日の結合では `(source, start, end, asleep)` が同じレコードを 1 回だけ数える
- AutoSync の `sleep_analysis` は**開始日基準**で分割されるので、当日ファイルに「前夜→当日朝」の睡眠と「当日夜→翌朝」の未完就寝が混在する。`end.hour` JST で切り分けて `sleep_hours` (夜主睡眠) と `nap_hours` (昼寝) に振り分ける
- オートメーション JSON の睡眠は aggregateSleep=true だと 1 日 1 レコードの集計値で、**昼寝を分離できない** (`sleep_hours` = 昼寝込みの総睡眠、`nap_hours` = None)。HAE 側で aggregateSleep=false にすると segment 形式になり、AutoSync と同じ規則で分離できる (前日ファイルも併読)
- オートメーション JSON の record の日時 (`"2026-10-10 05:02:00 +0900"`) は **record 自身の offset の暦日**で対象日と比較する (JST 固定にしない)
- オートメーションの当日ファイルは途中経過。**mtime が対象日の終了より前のファイルを確定値として書かない** (過去日なら保留、今日なら暫定)。今日の暫定値は台帳で追跡する (HAE の再出力は数週間遅れることがある)
- HAE は運動 0 分の日に `apple_exercise_time` 自体を出力しない (qty 0 の record は出ない)。アクティビティ詳細の `"なし"` を「エクササイズ時間 0 分」で判定すると実データでは永久に書けないので、**心拍の有無** (Watch を着けていたか) で「運動 0 分」と「記録なし」を分ける。心拍は補完・妥当性検証後の最終値で見る (JSON に無く AutoSync `.hae` で補えた心拍も根拠になり、心拍の集計失敗・保留・読込失敗の日は `"なし"` と断定しない)
- 実際のオートメーション設定は `includeWorkouts=false` (JSON に `data.workouts` が無い) なので、アクティビティ詳細は `"エクササイズN分"` になる。既定 window (昨日から `IHEALTH_LOOKBACK_DAYS` = 7 日 + 今日) では `"なし"` / `"エクササイズN分"` もキーの既存値を上書きする (直近 7 日に手で書いたアクティビティ詳細や、前身から移行した `"…、フィットネスウォーク52分"` も置き換わる。この挙動はリーダー決定で維持)。守りたい値は entry にコメントを付ければ書き換えない (`kept`)。ワークアウト名で書きたいなら HAE のオートメーションで **Include Workouts** を有効にする
- `HEALTH_EXPORT_DIR=…/AutoSync` は `HealthMetrics` が無くても (フォルダ名 `AutoSync` で) 親からオートメーションを探す
- iCloud の競合コピー (`HealthAutoExport-2026-10-10 2.json`) は対象外 (ファイル名の完全一致)
- 秒単位タイムスタンプ (`start`/`end`) は **Mac absolute time** (2001-01-01 UTC 起点) なので、必要に応じて `datetime(2001,1,1) + timedelta(seconds=...)` で変換
- 気象庁: 前身は見出し判定 `if i > 2` の off-by-one で **1 時の行を読み捨てていた**。本実装は 1〜24 時を全て使う (`weather._FIRST_HOUR = 1`) ので、1-6 時の気圧平均・夜の気温 / 湿度平均などが前身の値 (移行済みの過去ノート) と 0.1〜0.8 程度違いうる。`_FIRST_HOUR = 2` で前身と完全一致
- 気象庁: 当日・未来日のページには表が無い (「閲覧可能な日は…」の案内だけ)。前日分は数値の行が先に出て天気欄が後から埋まる → 今日 − 2 日までの未完成は「公開待ち」(`nodata`)。それより古い日の未完成は `partial` で、2 日以上続いたら通知
- GitHub: `viewer.repositories` は org の private repo を返さないことがある → owner ごとに列挙。`history(since:, until:)` は全 parent をたどり committer date 基準。その日の items の並びは「その日のその repo の最終イベント時刻」の降順 (現在の pushedAt 順にすると後日の push で過去日の並びが変わり、毎回ノートが書き換わる)
- 日記ノートは `日記/` 直下だけでなくサブフォルダ (`日記/2025/` など) にもある → 再帰探索でファイル名 `YYYY-MM-DD.md` の**完全一致** (`2025-09-07 週次振り返り.md` のようなノートを掴まない)

## コーディング規約

- **Python 3.9 互換を維持**（`/usr/bin/python3` がこのバージョンのため）
- 各モジュール先頭に `from __future__ import annotations` を置き、`dict[str, str]` / `Path | None` などの新しい型ヒント構文を文字列として保持する（runtime では評価されないので 3.9 でも動く。`isinstance` や `typing.get_type_hints` で `X | Y` を評価しない）
- **使ってはいけない機能**: `match` 文 (3.10+), `dataclass(slots=True)` (3.10+), `typing.Self` (3.11+), ExceptionGroup (3.11+)
- PEP 8 準拠（`snake_case`, 4 スペースインデント）
- 型ヒント (type hints) 必須
- エラーハンドリングは `try...except` で明示的に
- `@dataclass(frozen=True)` と `pathlib.Path` を積極利用
- コメントは日本語 OK
- 標準ライブラリ以外は使わない（`requests` ですら使わない、`urllib.request` で統一）
- **テスト**: ネットワークに出ない (opener / runner / clock を注入してモック)。実 vault・実 iCloud・実 `gh` / keychain に触れない (tempdir を使う)。fixture は合成データ (実健康値・実誕生日・private repo 名・日記本文を入れない)
- **秘密情報**: GitHub token / Notion secret / Slack Webhook URL をログ・例外メッセージ・通知・実行レポートに出さない (`Config` の repr にも出さない)

## 注意事項

- **`/usr/bin/python3` のバージョン確認**: Python 3.9 未満では動かない。起動時に `sys.version_info` でチェックして明示的に失敗させる。macOS の標準 Python は長年 3.9.x で凍結されているため、通常このチェックは通過する
- **iPhone ロック中は AutoSync / オートメーションが走らない可能性**: iOS の制約。就寝中は充電器接続＋AOD ON 運用が推奨。ロック解除が確実でない場合は、起床後に iPhone 側で Health Auto Export を一度開いて同期を確実にさせ、macOS 側で `./bin/ihealth-run` を手動実行する動線を使う。Obsidian 日記は 1 日 3 回 × 7 日分を再処理するので、遅れて届いた分は後の回で自動的に埋まる
- **`HEALTH_EXPORT_DIR` は `.env` で指定**: 絶対パス (`~/` 始まり可)。obsidian は HAE の **Documents root** (`~/Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents`) と **AutoSync** (その下の `AutoSync`) のどちらでも動く (AutoSync を指すと、オートメーション JSON はその親から探す)。単日 Sink は AutoSync ディレクトリ必須 (直下に `HealthMetrics/` が無いと exit 5)。パスに iCloud 系の `iCloud~com~ifunography~HealthExport` が含まれるが、中間の `~` は iCloud フォルダ名の一部なので先頭の `~/` だけを展開する
- **iCloud の dataless ファイル**: オートメーション JSON も `.hae` もほぼ全て placeholder。`open()` は File Provider の download 待ちで長時間 block しうるので、読む前に必ず `source._ensure_materialized` (`brctl download` + polling) を通す
- **`.hae` の解凍には `/usr/bin/compression_tool` を使う**: Xcode Command Line Tools に含まれる macOS 標準ツール。Python からは `subprocess.run(["/usr/bin/compression_tool", "-decode", "-a", "lzfse", "-i", src, "-o", dst])` で呼び出す (HAE1 は segment ごとに stdin / stdout で)。LZFSE は Apple が公開しているアルゴリズム（[GitHub: lzfse/lzfse](https://github.com/lzfse/lzfse)）
- **`.hae` の実体は JSON**: 「独自バイナリ」ではなく LZFSE で圧縮された UTF-8 JSON。解凍後のスキーマはメトリクスによって異なる（秒サンプル / 事前集計済み avg-min-max / 睡眠ステージなど）ので、メトリクスごとに集計戦略を分岐する
- **Notion API のレート制限**: 平均 3 リクエスト/秒。429 応答時は指数バックオフでリトライ（`publishers/notion.py` に実装）
- **気象庁への配慮**: リクエスト間隔は 1 秒以上 (`diary.WEATHER_MIN_INTERVAL_SEC`)、User-Agent に repo URL。5xx / 429 / 通信エラーは指数バックオフで再試行、4xx は即失敗。見出しから列を特定できない (レイアウト変更) ときは黙って誤パースせず `WeatherError`
- **GitHub API**: GraphQL のサーバ側 timeout (約 10 秒) に備え、history の page 件数を 100 → 50 → 25 → 10 と縮めて取り直す。403 / 429 の Retry-After は最大 60 秒まで待つ (それ以上は失敗)
- **launchd の起動時刻は `IHEALTH_PUBLISHER` で決まる**: `install-launchd.sh` が `.env` を読み、obsidian なら 07:00 / 12:00 / 21:00、それ以外 (未設定 = notion を含む) は 07:00 だけにする (テンプレートの `IHEALTH_OBSIDIAN_EXTRA_RUNS_BEGIN`〜`END` を消す)。`IHEALTH_PUBLISHER` を変えたら再実行する。`--print-plist` で登録内容だけ確認できる
- **launchd.log はローテーションされない**: launchd 経由 (標準エラーが端末でない) では標準エラーへのログを WARNING 以上に絞る (`__main__._quiet_stderr_when_not_tty`)。`--verbose` と dry-run (`--dry-run` / dryrun publisher = `--publisher dryrun` か `IHEALTH_PUBLISHER=dryrun`) では絞らない (dry-run は起動行と変更内容のログが出力そのものなので、`> out.txt 2>&1` やパイプの先にも INFO を出す)。launchd は `--dry-run` を付けないので、`IHEALTH_PUBLISHER=dryrun` にしていない限り定時実行の launchd.log は絞られたまま。全ログは `logs/run.log`
- **`.env` の必須キー**: 常時必須キーは `HEALTH_EXPORT_DIR` / `BIRTH_DATE`。`NOTION_SECRET` / `DATABASE_ID` は notion を使うときだけ必須 (obsidian / markdown / stdout / sqlite / slack / dryrun では空でも起動できる)。`OBSIDIAN_VAULT_DIR` は obsidian のとき必須。`.env` には ihealth-relay が参照しないキーを残さないこと (`gitignore` 済とはいえ、誤コミット時の漏洩リスクを最小化する)。`.env` は行末コメントに対応しない (`KEY=値 # メモ` は `# メモ` まで値になる)
- **プロセス環境変数でだけ効く調整値**: `IHEALTH_MATERIALIZE_TIMEOUT_SEC` / `IHEALTH_RETRY_WAIT_SEC` / `IHEALTH_RETRY_MAX` は `os.environ` から読む (`.env` に書いても効かない)
- **実 vault を触る作業の前にはバックアップ**: 実データでの検証は vault のコピーか `--dry-run` で行い、書き込み後は「担当キー以外が byte 不変か」を機械的に確認する
