# ihealth-relay (日本語版 README)

> 英語版 README は [`../../README.md`](../../README.md) を参照。

iPhone 用アプリ **Health Auto Export — JSON+CSV** が iCloud Drive に書き出した
Apple Health のデータを、日々の記録先へ転記する macOS バッチツール。Python の
外部ライブラリは使わない。

- **Obsidian 日記** (`--publisher obsidian`): 健康データ・気象庁の天気・GitHub の
  活動を、日記ノート `YYYY-MM-DD.md` の YAML frontmatter に書き込む。担当する
  キー以外は 1 byte も変えない。
- **単日の pluggable Sink**: Notion / Markdown / stdout (JSON 1 行) / SQLite / Slack。

## ハイライト

- **Python 標準ライブラリのみ**: `requirements.txt` も venv も `pip install` も不要。
  Notion・GitHub・気象庁へのアクセスは `urllib.request`、気象庁ページの解析は
  `html.parser`、SQLite は同梱の `sqlite3`、TOML は自前の最小パーサ
  (`src/ihealth/_toml.py`) を使う。
- **Apple Developer Program ($99/yr) 不要**: HealthKit entitlement が要る処理を
  iPhone 側の Health Auto Export に任せることで回避している。
- **Obsidian 日記パイプライン**: 期間内の日付ごとに、健康 (Health Auto Export の
  オートメーション JSON。AutoSync `.hae` で欠けた値を補う)・気象庁の天気・GitHub
  活動を **独立に** 取得する。1 つの source が失敗しても他の source は書かれる。
- **手書きの日記を壊さない**: 書き換えるのは担当キーだけで、本文・他のキー・
  コメント・改行コード・BOM は byte 単位でそのまま残す。担当キーでも、entry に
  コメントが付いているものと、同じキーが 2 つ以上あるものは書き換えずに残す
  (実行レポートに残る)。書く前に、編集処理とは別に実装した検査で編集前後を
  比べ、担当キー以外が 1 byte でも変わるなら書かない。上書きの前には必ず
  バックアップを取り、Obsidian / Obsidian Sync / 他のツールによる同時編集を
  置換の直前に検知し、置換の後は読み直して値が入ったことを確かめる。
- **確かな値だけを書き、残りは追跡する**: まだ終わっていない日 (今日) と、日が
  終わる前に書き出されたままの日の値は暫定値として書く。取得が不完全な source は
  書かず、既存の値を残す。まだ確定していない値 (暫定値・気象庁の公開待ち・
  取りきれなかった GitHub など) は `data/diary-state.json` で追跡し、30 日まで処理し直す。
  失敗と、長く確定しない値は 1 回の実行につき 1 件の Reminders.app リマインダーに
  まとめる。
- **Pluggable Sinks**: `--publisher notion | markdown | stdout | sqlite | slack | dryrun | obsidian`。
  各 Sink は `src/ihealth/publishers/` 配下の独立モジュール。
- **冪等**: 同じ日付を再実行すると Notion ページ / Markdown ファイル / SQLite 行を
  上書きする。Obsidian の日記ノートは値が変わったときだけ書き換える。Notion 側の
  手入力値は保持される (`None` フィールドは update でスキップ)。
- **永続通知**: 失敗時は Reminders.app にリマインダーを作る (Notification Center と
  違って自動では消えず、確認するまで残る)。
- **macOS の dataless ファイル対応**: iCloud Drive の `SF_DATALESS` フラグを検知して
  `brctl download` を実行し、ダウンロード完了を待ってから `.hae` / オートメーション
  JSON を読む。headless な launchd 実行で起こりがちな silent EDEADLK を防ぐ
  ([詳細は case-study](../case-study/dataless-icloud.md))。

## アーキテクチャ

```
 iPhone                                    iCloud Drive (Health Auto Export)
┌────────────────────┐  automation (JSON) ┌────────────────────────────────────┐
│ Health.app         │───────────────────▶│ Documents/<オートメーション名>/    │
│ (HealthKit)        │                    │   HealthAutoExport-YYYY-MM-DD.json │
│ Health Auto Export │  AutoSync (.hae)   │ Documents/AutoSync/HealthMetrics/  │
│                    │───────────────────▶│   {metric}/YYYYMMDD.hae            │
└────────────────────┘                    └─────────────────┬──────────────────┘
                                                            │ iCloud 同期
 気象庁 1 時間ごとの値 ────┐              ┌─────────────────▼──────────────────┐
 GitHub GraphQL API ───────┴─────────────▶│ macOS: python3 -m ihealth          │
                                          │ (launchd 07:00 / 12:00 / 21:00)    │
                                          └────────┬──────────────────┬────────┘
                      --publisher obsidian         │                  │  その他の publisher
                      (期間 × 3 source)            ▼                  ▼  (1 日分, AutoSync .hae)
                                 ┌──────────────────────────┐ ┌──────────────────────────┐
                                 │ Obsidian vault           │ │ Notion / Markdown /      │
                                 │ 日記/**/YYYY-MM-DD.md    │ │ stdout / SQLite / Slack  │
                                 │ (frontmatter 部分更新)   │ │                          │
                                 └──────────────────────────┘ └──────────────────────────┘
```

詳細は [`docs/architecture.md`](../architecture.md) を参照。

## クイックスタート

完全な手順は [`docs/setup.md`](../setup.md) を参照。概要:

1. iPhone に Health Auto Export をインストールし、[取得データ](#取得データ) の
   メトリクスへのアクセスを許可する。そのうえで
   - **Obsidian 日記** なら、JSON を iCloud Drive に書き出す **オートメーション** を
     作る ([Obsidian のセットアップ](#obsidian-のセットアップ))
   - **Notion / Markdown / stdout / SQLite / Slack** なら **AutoSync** を有効にする
2. macOS でリポジトリを clone する
3. `cp .env.example .env` して `HEALTH_EXPORT_DIR` / `BIRTH_DATE` と、使う publisher の
   キー (Obsidian なら `OBSIDIAN_VAULT_DIR` など、Notion なら `NOTION_SECRET` /
   `DATABASE_ID`) を記入する (`chmod 600 .env` 推奨)
4. **(Notion の列名や日記のキー名が既定と違うなら)** `cp config.example.toml config.toml`
   して名前の override を設定する
5. dry-run で動作確認する (出力先には何も送らず、何も書き込まない):
   ```bash
   PYTHONPATH=src /usr/bin/python3 -m ihealth --dry-run --verbose
   ```
6. 本番実行 (単日の Sink は前日分。Obsidian は下記):
   ```bash
   PYTHONPATH=src /usr/bin/python3 -m ihealth
   ```
7. (任意) launchd に登録する:
   ```bash
   ./bin/install-launchd.sh
   ```
   installer は定期実行用の小さなランチャーをビルドし、最後に残りの手順を貼り付けて
   使えるコマンド付きで表示する: ランチャー
   `~/Library/Application Support/ihealth-relay/ihealth-launcher` に **フルディスク
   アクセス** を与え (`/bin/bash` ではない。与えないと定期実行が Health Auto Export の
   iCloud フォルダを読めない)、1 回試しに実行し、ihealth-launcher に **リマインダー** の
   操作を許可するダイアログを許可する ([定期実行 (launchd)](#定期実行-launchd))。

## Obsidian 日記 (`--publisher obsidian`)

期間内の日付ごと (既定は「昨日から 7 日分」+「今日」。値がまだ確定していない日は
30 日前まで加える。[下記](#まだ確定していない値)) に 3 つの source を独立に
取得し、その日の日記ノートの frontmatter に書く。

- **健康**: Health Auto Export の **オートメーション** JSON
  (`<オートメーションのフォルダ>/HealthAutoExport-YYYY-MM-DD.json`)。JSON に無い値は
  AutoSync `.hae` で補い、JSON が無い日 (オートメーションを作る前の日の backfill など)
  は `.hae` だけで集計する
- **天気**: 気象庁「過去の気象データ検索」の 1 時間ごとの値 (観測所 1 か所。既定は東京)
- **GitHub**: 自分の repo での Issue close / PR merge / default branch への commit
  ([GitHub の対象範囲](#github-の対象範囲))

日記ノートは日記ディレクトリの配下をサブフォルダまで探し、ファイル名が
`YYYY-MM-DD.md` に **完全一致** するものを使う (`日記/2026-10-12.md` も
`日記/2025/2025-03-01.md` も対象。`2025-09-07 週次振り返り.md` のようなノートには触れない)。

### 書き込む内容

既定の frontmatter キー (日本語。`config.toml` でキーごとに変えられる):

| キー (既定) | 内部名 | source | 規則 |
|---|---|---|---|
| `歩数 (歩)` | `step_count` | 健康 | 合計 (整数) |
| `睡眠時間 (分)` | `sleep_hours` | 健康 | 分 ([健康データの値についての注意](#健康データの値についての注意)) |
| `昼寝時間` | `nap_hours` | 健康 | 分。昼寝を分離できる日だけ |
| `消費カロリー (kcal)` | `active_energy_kcal` | 健康 | 合計 (小数 1 桁, kJ は換算) |
| `移動距離 (km)` | `distance_km` | 健康 | 合計 (小数 1 桁, mi / m は換算) |
| `安静時心拍数 (bpm)` | `heart_rate_resting` | 健康 | 平均 (小数 1 桁) |
| `平均心拍数 (bpm)` | `heart_rate_avg` | 健康 | 1 分ごとの平均値の平均 (小数 1 桁) |
| `最大心拍数 (bpm)` | `heart_rate_max` | 健康 | 最大 (小数 1 桁) |
| `酸素飽和度 (%)` | `oxygen_saturation` | 健康 | 平均 (小数 1 桁) |
| `運動強度スコア` | `exercise_intensity_score` | 健康 | Heart Points 近似 ([取得データ](#取得データ)) |
| `瞑想時間 (分)` | `mindful_minutes` | 健康 | 合計 (整数の分) |
| `瞑想回数 (回)` | `mindful_sessions` | 健康 | 回数 |
| `アクティビティ時間 (分)` | `exercise_minutes` | 健康 | Apple のエクササイズ時間 (整数の分) |
| `体重 (kg)` | `body_mass_kg` | 健康 | その日の最新値 (小数 1 桁, lb は換算) |
| `体脂肪率 (%)` | `body_fat_percentage` | 健康 | その日の最新値 (小数 1 桁) |
| `アクティビティ詳細` | `activity_detail` | 健康 | 瞑想、続けてその日のワークアウト (**Include Workouts** のとき) または Apple のエクササイズ時間。例: `瞑想10分、エクササイズ21分`。どちらも無く Watch を着けていた (心拍がある) 日は `なし`、心拍も無い日は書かない ([注意](#健康データの値についての注意)) |
| `気温` | `temperature` | 天気 | 朝・昼・夜の平均 + 1 日の最高・最低 |
| `湿度` | `humidity` | 天気 | 気温と同じ形 |
| `気圧` | `pressure` | 天気 | 海面気圧の 6 時間ごとの平均 + 急変マーク |
| `天気` | `weather` | 天気 | 3 時間ごとの天気 |
| `日照時間` | `sunshine` | 天気 | 1 日の合計 |
| `降水量` | `precipitation` | 天気 | 朝・昼・夜の合計 |
| `Github` | `github` | GitHub | 1 件 1 行の Markdown リンク。活動が無ければ `該当なし` |

書き込み例 (値はすべて合成):

```yaml
歩数 (歩): 8421
睡眠時間 (分): 412
消費カロリー (kcal): 512.3
平均心拍数 (bpm): 72.4
運動強度スコア: 24
アクティビティ詳細: "瞑想10分、ウォーキング31分"
気温: "朝:平均21.0℃, 昼:平均24.5℃, 夜:平均18.2℃（最高:26.1℃, 最低:15.3℃）"
湿度: "朝:平均70.4%, 昼:平均55.0%, 夜:平均81.3%（最高:90%, 最低:48%）"
気圧: "1-6時:平均1012.3hPa, 7-12時:平均1011.0hPa⤵️💣️, 13-18時:平均1005.2hPa, 19-24時:平均1004.8hPa"
天気: "3時: 晴れ☀️, 6時: 快晴☀️☀️, 9時: 薄曇⛅️, 12時: 曇☁️, 15時: 雨☔, 18時: 雷電, 21時: 雪❄️"
日照時間: "6.4時間"
降水量: "朝:0.0mm, 昼:3.5mm, 夜:0.5mm"
Github: "[🔀 my-app:PR #12: Add login form](https://github.com/your-name/my-app/pull/12)\n[📝 my-app:変更行数:+120-30 (3 commits)](https://github.com/your-name/my-app/commits/main)"
```

- 書式は前身 (GoogleFitNotionIntegration) が書いていた値と同じ。Notion から移行した
  日記でも過去の値と同じ形で並ぶ。
- 天気の区分: 気象庁の「N 時」の行は (N−1):00〜N:00 の 1 時間。朝 = 5〜11 時、
  昼 = 12〜18 時、夜 = 19〜24 時と 1〜4 時。`⤵️💣️` は次の 6 時間で 5 hPa 以上下がった、
  `⤴️⚠️` は 5 hPa 以上上がったことを表す。
- GitHub の 1 行: `[🎫 repo:Issue #n: タイトル](url)` / `[🔀 repo:PR #n: タイトル](url)` /
  `[📝 repo:変更行数:+追加-削除 (n commits)](url)`。Issue → PR → commit の順に並ぶ。
- 値が無いキーは **書かない** (既存の値をそのまま残す)。欠測を `0` で埋めることはしない。

### Obsidian のセットアップ

1. **iPhone — Health Auto Export のオートメーション**: **Health Metrics** を
   **iCloud Drive** に **JSON** (export version 2) で書き出すオートメーションを作る。
   データの集計 (aggregate) を有効にして **分 (minutes)** 単位にまとめ (運動強度スコアは
   1 分単位で数えるので、それより粗いまとめ方だと少なく出る)、睡眠の集計も
   有効にし (昼寝については[注意](#健康データの値についての注意)を参照)、**1 日 1 ファイル**
   にする。メトリクスは [取得データ](#取得データ) の表のものを含める。`アクティビティ詳細`
   は瞑想と Apple のエクササイズ時間から組む (`瞑想10分、エクササイズ21分`)。
   オートメーションの **Include Workouts** (ワークアウトを含める) を有効にすると、
   エクササイズ時間の代わりにその日のワークアウトを名前で並べる
   (`瞑想10分、ウォーキング31分`。ワークアウトの無い日は `エクササイズN分` のまま)。
   画面上の項目名はアプリのバージョンで多少違う。
   ファイルは
   `iCloud Drive › Health Auto Export › <オートメーション名> › HealthAutoExport-YYYY-MM-DD.json`
   に出る。フォルダ名は自由 (ファイル名のパターンで探す)。
2. **`.env`** (全キーの説明は `.env.example` にある。コメントは必ず独立した行に書く。
   `KEY=値 # メモ` と書くと `# メモ` まで値になる):
   ```bash
   # launchd は引数なしで起動するので、既定の publisher を obsidian にする
   IHEALTH_PUBLISHER=obsidian
   HEALTH_EXPORT_DIR=~/Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents
   # 自分の誕生日 (最大心拍数の計算に使う)
   BIRTH_DATE=1990-01-01
   OBSIDIAN_VAULT_DIR=~/obsidian/<vault>
   # vault 内の日記ディレクトリ (既定 日記)
   OBSIDIAN_DIARY_DIR=日記
   # 任意: ノートが無い日 (一昨日以前) をこのテンプレートから作る
   #OBSIDIAN_TEMPLATE=Templates/Daily.md
   ```
   `HEALTH_EXPORT_DIR` は Health Auto Export の `Documents` フォルダ (Obsidian 向けに
   推奨) と、その下の `AutoSync` フォルダ (他の publisher はこちらが必須。Obsidian は
   親フォルダからオートメーションのファイルを探す。AutoSync の `HealthMetrics` を
   消した後も同じ) のどちらでもよい。
3. **GitHub**: `gh auth login` を一度実行するか、`GITHUB_TOKEN` を設定する。
4. **確認してから書き込む**:
   ```bash
   ./bin/ihealth-run --publisher obsidian --dry-run   # "[dry-run] <ノート>: <キー>: 旧値 -> 新値" をログに出す
   ./bin/ihealth-run --publisher obsidian
   ```
5. **定期実行**: `./bin/install-launchd.sh` を実行し、最後に表示される手順に従う
   (ランチャーへのフルディスクアクセス (`/bin/bash` ではない)・試し実行・リマインダーの
   許可ダイアログ。[定期実行 (launchd)](#定期実行-launchd))。

#### Obsidian 用の `.env` キー

| キー | 既定 | 意味 |
|---|---|---|
| `IHEALTH_PUBLISHER` | `notion` | `--publisher` を省略したときの publisher。launchd で使うなら `obsidian` |
| `OBSIDIAN_VAULT_DIR` | — (必須) | vault のルート (`~/...` または絶対パス) |
| `OBSIDIAN_DIARY_DIR` | `日記` | vault 内の日記ディレクトリ。配下を再帰的に探す |
| `OBSIDIAN_TEMPLATE` | 未設定 | ノートが無い日に使う Templater テンプレート (vault 相対・`~/...`・絶対パス)。展開するのは先頭の `<%* … %>` ブロック (除去)・`<% dateStr %>`・`<% weekday %>` だけで、他の `<%` が残るテンプレートは使わない |
| `OBSIDIAN_CREATE_MISSING` | テンプレートがあれば `template`、無ければ `never` | `never` / `template` / `minimal` (テンプレートが使えなければ日付キーだけの最小ノートを作る) |
| `OBSIDIAN_CREATE_MIN_AGE_DAYS` | `2` | この日数以上前の日のノートだけ作る。今日・昨日のノートは利用者に任せる (別の端末で作った同じ日のノートと Obsidian Sync 上で衝突させないため)。`0` で今日も作る |
| `IHEALTH_LOOKBACK_DAYS` | `7` | 既定の期間: 昨日から何日分さかのぼるか (0〜366) |
| `IHEALTH_INCLUDE_TODAY` | `true` | 今日の暫定値も書くか |
| `IHEALTH_SOURCES` | `health,weather,github` | 処理する source。ここから外した source のキーは `data/diary-state.json` での追跡もやめる (`--sources` で一時的に外しただけなら追跡を続ける) |
| `IHEALTH_TZ_OFFSET` | `+09:00` | 日記の基準タイムゾーン。「今日 / 昨日」と GitHub 活動の日付の区切りに使う。健康データのレコードに UTC オフセットが無いとき (と AutoSync `.hae`) の「日の終わり」にも使う |
| `IHEALTH_NETWORK_GRACE_SEC` | `60` | スリープ復帰直後などにネットワーク接続を待つ秒数 (0〜600。`0` は待たない)。この間に解消した接続エラーは取り直し、失敗として通知しない |
| `JMA_PREC_NO` / `JMA_BLOCK_NO` | `44` / `47662` (東京) | 気象庁の観測所 ([天気の観測所を変える](#天気の観測所を変える)) |
| `GITHUB_TOKEN` | 未設定なら `gh auth token` | GitHub GraphQL API に使う token。ログには出さない |
| `GITHUB_OWNERS` | 自分 + 所属 org | 対象 owner の login (カンマ区切り) |
| `GITHUB_EXCLUDE_REPOS` | なし | 対象外の repo (`owner/name` のカンマ区切り) |

#### `config.toml` でキー名を変える

```toml
[publishers.obsidian]
date_property = "日付"        # OBSIDIAN_CREATE_MISSING=minimal で作る最小ノートの日付キー

[publishers.obsidian.fields.github]
property = "GitHub"          # GitHub 活動を "Github" ではなく "GitHub" に書く
```

内部名は [書き込む内容](#書き込む内容) の表のもの。2 つのフィールドを同じキーに
向けたり、`date_property` と同じキーにしたりすると、値が黙って消えないよう起動時に
設定エラー (終了コード 3) で止まる。

#### コマンドライン

```bash
./bin/ihealth-run --publisher obsidian                         # 既定の期間
./bin/ihealth-run --publisher obsidian --date 2026-10-01       # 1 日だけ
./bin/ihealth-run --publisher obsidian --from 2026-01-01 --to 2026-09-30 --fill-empty-only
./bin/ihealth-run --publisher obsidian --from 2026-09-01 --sources weather,github
```

- `--to` を省略した `--from` は昨日まで。`--date` と `--from` / `--to` は併用できない。
  未来の日付はエラーになる。
- `--fill-empty-only` は値が入っているキーを上書きしない (過去分の backfill 用)。
  残したキーは実行レポートに `kept` として既存値と今回の値の両方が載る。
  例外: 本ツールが書いた暫定値のまま変わっていないキーは確定値に置き換える。
  利用者が書き換えた値は残す ([まだ確定していない値](#まだ確定していない値))。
- `--dry-run` は日記ノートを書かず、vault にファイルも作らない (バックアップも取らない)。
  source の取得、予定した編集への書込前の検査、実行レポートの保存は通常どおり行うが、
  リマインダーは作らずにログに出すだけで、通知済みの記録も未確定の値の台帳
  (`data/diary-state.json`) も書き換えない (dry-run が後の本番の実行の通知を止めたり、
  追跡を変えたりしない)。
- `--from` / `--to` / `--sources` / `--fill-empty-only` は obsidian 専用
  (他の publisher で指定すると終了コード 2)。
- `.env` に `IHEALTH_PUBLISHER=obsidian` があれば `--publisher obsidian` は省略できる。

### 定期実行 (launchd)

`./bin/install-launchd.sh` は、小さなランチャー (下記) 経由で `bin/ihealth-run` を
**引数なしで** 起動する LaunchAgent を登録する。起動時刻は `.env` の `IHEALTH_PUBLISHER`
で決まる: `obsidian` なら
**07:00 / 12:00 / 21:00** (毎回既定の期間を処理するので、今日の暫定値が日中も更新され、
遅れて届いた再出力もその日のうちに拾う)、それ以外の publisher は従来どおり **07:00 だけ**
(Slack が 1 日 3 回投稿するのを防ぐ)。`IHEALTH_PUBLISHER` を変えたら installer を
再実行する。`./bin/install-launchd.sh --print-plist` で、何も変更せずに登録する plist を
確認できる。スリープ中に過ぎた起動は復帰後に 1 回だけ実行され、その直後は最大
`IHEALTH_NETWORK_GRACE_SEC` 秒 Wi-Fi の接続を待つ。

installer は最後に、次にやることを貼り付けて使えるコマンド付きで表示する。定期実行に
必要な macOS の許可を一度だけ与える:

- **ランチャーにフルディスクアクセス**
  (`~/Library/Application Support/ihealth-relay/ihealth-launcher`): macOS のプライバシー
  保護 (TCC) は、launchd ジョブのファイルアクセスを、ジョブに登録したプログラム
  (責任プロセス = responsible process) によるものとみなす。そのプログラムが別の
  プログラムを起動しても変わらない。launchd に bash スクリプト `bin/ihealth-run` を直接
  起動させるとそれは `/bin/bash` になり、許可を `/bin/bash` に与えるしかない (launchd が
  このユーザーで起動するすべての bash スクリプトに及ぶ)。そこで installer は
  `bin/launcher/ihealth-launcher.c` を Xcode Command Line Tools の C コンパイラでビルドし、
  ad-hoc 署名して、ジョブのプログラムとして登録する。ランチャーは
  `/bin/bash <clone>/bin/ihealth-run` を実行するだけで (パスはビルド時に埋め込む)、引数は
  受け付けない: launchd は引数を渡さないし、許可を持つプロセスに `--slack-webhook <URL>`
  や `--markdown-out <パス>` で別の送り先を指定させないため (引数付きで起動されたら何も
  実行せずに 64 で終わる。手で実行するときは `./bin/ihealth-run` を使う)。bash / Python /
  動的リンカに別のコードを読み込ませる環境変数も渡さず、自分自身も `DYLD_*` を無視する。
  許可が無いと Health Auto Export の iCloud フォルダ
  (`~/Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents`) の
  読み込みが `[Errno 1] Operation not permitted` で失敗し、健康データの source だけが
  失敗する (天気と GitHub は動き、終了コードは 12)。ダイアログは出ない。
  `open -R "$HOME/Library/Application Support/ihealth-relay/ihealth-launcher"` でファイルを
  表示し、`open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"`
  で開いたリストへドラッグしてオンにする。vault を `~/Documents` や `~/Desktop` に
  置いている場合も、この許可で足りる。許可が及ぶのは `bin/ihealth-run` と clone 内の
  コードがすることなので、clone は自分だけが書き込める場所に置く。
- **許可を保つ**: 許可はビルドしたそのバイナリに結び付く。installer を再実行しても
  ランチャーはそのまま残し、作り直すのはソース・clone の実際の場所 (symlink 経由や
  大文字小文字を変えて installer を実行しても、同じ場所とみなす)・ビルド設定が変わった
  ときだけ (作り直した結果が同じなら古いファイルを残す)。installer が新しいランチャーを
  ビルドしたと表示したら、リストの古い `ihealth-launcher` を「−」で消してから追加し直す。
- **C コンパイラが無い場合** (Command Line Tools 未導入) はランチャーをビルドできない。
  installer はその旨を表示して `bin/ihealth-run` を直接登録し、フルディスクアクセスは
  `/bin/bash` に与えることになる (おすすめしない。`xcode-select --install` を実行して
  installer を再実行する)。
- **リマインダー**: 定期実行が初めてリマインダーを作るとき、ihealth-launcher に
  リマインダーの操作を許可するか macOS が尋ねるので、許可する (後から
  システム設定 › プライバシーとセキュリティ › オートメーション › ihealth-launcher ›
  リマインダー でも許可できる)。許可するまでは通知がタイムアウトし、未送信のまま残って
  次の実行で再送される。
- 以前の版の手順で `/bin/bash` にフルディスクアクセスを与えた (bash にリマインダーの
  操作を許可した) 場合は、もう不要なので外してよい。

許可したら 1 回実行し、1〜2 分してから結果を確かめる (installer は同じコマンドを、uid と
パスを埋めて最後に表示する。最後のコマンドは clone の中で実行する):

```bash
launchctl kickstart -k "gui/$(id -u)/com.ihealthrelay.daemon"
launchctl print "gui/$(id -u)/com.ihealthrelay.daemon" | grep -E "^.(state|last exit code) = "
awk '/ihealth-relay 起動/{n=0} /ihealth-relay 起動|日記 [0-9-]+:|日記 完了:/{l[++n]=$0} END{for(i=1;i<=n;i++)print l[i]}' logs/run.log
```

`state = not running` で `last exit code = 0` なら成功。`awk` のコマンドは、run.log の
うち最後の実行の要約の行だけを出す (Obsidian): 最後の行 (`日記 完了: …`) が `exit=0` で、
各日の行が `health=complete` (今日の分は、まだデータが届いておらず `health=nodata` の
こともある)。`health=failed` (exit 12) なら、許可がまだ効いていない。`state = running`
ならまだ実行中なので (`last exit code` は前の回の値か `(never exited)`)、少し待って
確かめ直す。ほかの publisher では、installer は同じ `awk` を
`ihealth-relay 起動|集計完了|ERROR` の行について表示する: `集計完了` があり `ERROR` が
無ければ成功。

- アプリのログ: `logs/run.log` (日次ローテーション, 30 日保持)
- launchd の stdout / stderr: `~/Library/Logs/ihealth-relay/launchd.log`。起動失敗・
  警告・エラーだけが入る (このファイルはローテーションされない。`--verbose` なら全部出る)
- すぐ実行する: `launchctl kickstart -k gui/$(id -u)/com.ihealthrelay.daemon`

### いつ反映されるか

| source | 今日 | 昨日 | 2〜7 日前 |
|---|---|---|---|
| 健康 | **暫定値**: 最新のオートメーションのファイルにあるその時点までの値。実行のたびに置き換わる | Health Auto Export が昨日のファイルを日が終わった後に書き直した時点で **確定値** を書く。それまでは最新のファイル (日付が変わる前に書かれたもの) の値を **暫定値** として書き、再出力で置き換わるまで追跡する (下記) | 同じ規則。遅れて再出力されても期間内のどの実行でも拾い、期間を外れた後は下記の追跡で拾う |
| 天気 | 書かない (気象庁はその日が終わってから公開する)。公開待ちとして追跡する | 気象庁のページが完成したら書く。天気欄は数値より後から埋まるので、たいていはその日の後の回になる。それまでは公開待ちとして追跡する | 同じ。未完成のままの日は書かず、追跡を続ける ([失敗時の見え方](#失敗時の見え方と復旧手順)) |
| GitHub | その時点までの活動 (暫定値。健康と同じく追跡する) | 期間全体の収集が成功したときに書く。活動が無ければ `該当なし`。収集が不完全なら書かずに追跡する | 同じ |

- 健康データは、オートメーションのファイルがその日が終わった後に書かれたときだけ
  確定値として扱う。日の終わりは、その日のレコード自身の UTC オフセットでの翌日 0:00
  (レコードにオフセットが無ければ `IHEALTH_TZ_OFFSET`)。ファイルの更新時刻と日の終わりは
  絶対時刻で比べるので、Mac のタイムゾーンには左右されない。それより前に書かれた
  ファイル (20:39 の書き出しなど) はその日の途中までの値なので、
  日がすでに終わっていても暫定値として書き、Health Auto Export がファイルを書き直したら
  置き換える。
  Health Auto Export はオートメーションが動くたびに直近数日のファイルを書き直すが、
  iOS がアプリにバックグラウンドの実行時間を与えたときにしか動かない。値が遅れるときは
  iPhone で Health Auto Export を一度開く。
- AutoSync `.hae` (補完と fallback。日の終わりは `IHEALTH_TZ_OFFSET` の翌日 0:00):
  日が終わる前に最後に書かれたファイルの値は、日が終わった後は **保留** (書かない) に
  する。止めた AutoSync はそのファイルを二度と書き直さないため。今日の分は暫定値。
- 睡眠の集計を無効にした場合、睡眠時間 (分) には前日のファイルも要る。前日のファイルが
  無い・読めない・その夜より前の書き出ししか無い間は、睡眠時間だけを暫定値として書く
  (他の値は確定値)。
- 健康データは各レコード自身の UTC オフセットの日付で数える (海外で記録した日も現地の
  日付になる)。天気は観測所の日付 (日本時間)。GitHub の日付と「今日」は
  `IHEALTH_TZ_OFFSET` で決める。
- 今日・昨日のノートは、すでにある場合だけ書く (本ツールは作らない。
  `OBSIDIAN_CREATE_MIN_AGE_DAYS` を参照)。一昨日以前のノートは `OBSIDIAN_TEMPLATE`
  (または `OBSIDIAN_CREATE_MISSING`) が許す場合だけ作る。作る場所は前後 7 日以内で
  いちばん近い既存の日記ノートと同じフォルダ (無ければ日記ディレクトリの直下)。

#### まだ確定していない値

次の理由でまだ確定していないキーは、日付・理由・最初に見つけた日時とともに
`data/diary-state.json` (台帳) に記録する。暫定値なら本ツールが書いた値も記録する
(健康と天気の取得失敗はここでは追跡しない。失敗として毎回通知し、期間内で取り直す)。

| 理由 | 何が起きたか | 確定するとき |
|---|---|---|
| `provisional` | 後で変わりうる値 (暫定値) を書いた: 今日の健康・`アクティビティ詳細`・`Github` / 日が終わる前に書かれたオートメーションのファイル / 前日のファイルが無い夜の睡眠 | 確定値を書いたとき |
| `withheld` | 日が終わる前に書かれた AutoSync `.hae` の値なので書かなかった (保留) | 利用者が手で記入したとき (AutoSync は書き直されない) |
| `weather_pending` | 気象庁がまだその日を公開していない | 気象庁のページが完成したとき |
| `weather_incomplete` | 完成しているはずの日の気象庁のページが未完成 | ページが完成したとき |
| `github_incomplete` | GitHub の収集が不完全だった | 全部を取りきれたとき |
| `unwritable` | 本ツールが書いた暫定値の entry にコメントが付いた・キーが重複したため、書き換えられない | コメントを移す・重複をまとめたとき |

- 定期実行 (日付の指定なし) は、台帳にある日を 7 日の期間から外れても **30 日まで**
  処理し直す。期間の外では、空のキーを埋めることと、本ツールが書いた暫定値のまま
  変わっていないキーを置き換えることだけをする (利用者が書いた値は残す)。
- 次の場合も台帳から外す: 本ツールが上書きしない利用者の値がキーに入っている
  (期間の外の日、または本ツールに書く値が無いキー) / その日の source のデータが
  確定していて、そのキーの値が無い (気象庁の欠測など) / その source を
  `IHEALTH_SOURCES` から外した / ノートが無くなった。
- 最初に見つけてから **3 日を超えて** 確定しないキーは、その回のリマインダーに
  「要確認」として載る (件名は `日記の暫定値が確定していません (要確認)`、暫定値以外の
  理由を含むときは `日記の値が確定していません (要確認)`。理由ごとの対処も載る)。
  通知は 1 日 1 回まで。`withheld` と `weather_incomplete` は自動ではまず確定しないので、
  1 回だけ知らせて、その後は黙って追跡する。
- 30 日たっても確定しない日は、最後に 1 回知らせて台帳から外す (後で `--date` で
  処理し直せる)。30 日より前の日付 (backfill など) で見つかった未確定のキーは追跡せず、
  リマインダーで 1 回知らせて、実行レポートの `provisional.untracked` に残す。
- それ以外で 7 日の期間から外れた日は自動では更新されない。`--date` / `--from` で実行する。

#### 健康データの値についての注意

- **睡眠時間 (分)** は Health Auto Export の睡眠の集計設定に従う。集計を有効にした場合は
  その日の `totalSleep` で、18 時より前に終わった **昼寝を含む** (18 時以降に終わった
  睡眠は翌日に入る)。このとき **昼寝時間** は、同じ日の AutoSync `.hae` で昼寝を分離
  できる場合だけ書く。集計を無効にした場合は、0〜14 時に終わった睡眠を睡眠時間、
  14〜20 時に終わった睡眠を昼寝時間にする (AutoSync と同じ規則)。
- **平均心拍数 (bpm)** は 1 分ごとの平均値の平均 (データのある 1 分を同じ重みで数える)。
- オートメーションのファイルが無い日 (オートメーションを作る前の日の backfill など) は
  AutoSync `.hae` だけで集計し、AutoSync の規則 (夜の主睡眠と昼寝を分ける、平均心拍数は
  個々の心拍レコードの平均) になる。
- **アクティビティ詳細** は、瞑想 (その日の合計が 1 分以上なら `瞑想N分`) に続けて
  - オートメーションがワークアウトを書き出す設定 (**Include Workouts**) で、その日に
    1 分以上のワークアウトがあれば (1 分未満のワークアウトは無いものとして扱う)、
    各ワークアウトを名前で (`ウォーキング31分` など。英語名は日本語にし、知らない名前は
    そのまま)
  - そうでなければ、Apple のエクササイズ時間を `エクササイズN分` (1 分以上のとき)。
    AutoSync `.hae` だけで集計する日もこの規則

  を `、` でつないだもの (例: `瞑想10分、エクササイズ21分`)。どれも無い日は、心拍の
  データがあれば (Watch を着けていて運動しなかった日。Health Auto Export はそうした日に
  エクササイズ時間そのものを書き出さない) `なし` を書く。心拍のデータが無い日は、
  エクササイズ時間が 0 分と記録されていても書かず、既存の値を残す。材料のどれかを
  読めなかった日 (補完に使う AutoSync `.hae` が読めない・日の途中までの出力の日を含む) も、
  心拍があっても書かず、既存の値を残す。分の丸めは 瞑想時間 (分)・アクティビティ時間 (分)
  と同じ。

  既定の期間 (昨日から `IHEALTH_LOOKBACK_DAYS` = 7 日分 + 今日) の日では、このキーも
  他の担当キーと同じく本ツールのもので、本ツールが組んだ値 (`なし`・`エクササイズN分` を
  含む) がキーの値を置き換える。直近 7 日に手で書いたアクティビティ詳細も置き換わる。
  残したい値は、その行にコメントを付ければ書き換えない
  (`アクティビティ詳細: "ヨガ30分" # 手入力`。実行レポートでは `kept`)。
  ノートにワークアウト名を並べている (前身から移行した日記など) なら、オートメーションの
  **Include Workouts** を有効にしてワークアウト名を書かせること。無効のままだと、
  期間内の日は `エクササイズN分` に書き換わる。
- **運動強度スコア** の最大心拍数には `BIRTH_DATE` を使う。

### 失敗時の見え方と復旧手順

**失敗の見え方**

- **1 回の実行につき 1 件の Reminders.app リマインダー** に、source と日付ごとに
  まとめて載る。例: タイトル `ihealth-relay: 日記の一部取得失敗 (github)`、本文に
  `[github] 2026-10-05〜2026-10-12 (期間全体)` と理由。同じ内容の失敗は 1 日 1 回しか
  通知しない (`logs/.failure_notified`)。1 日 3 回の実行で同じリマインダーが積み上がる
  ことはない。設定エラー (終了コード 3) と想定外のエラー (終了コード 1) も同じ。
  リマインダーを作れなかった通知 (`osascript` の失敗・タイムアウトなど) は通知済みに
  数えず、次の実行で改めて通知する。
- **実行レポート** `logs/diary-report-<UTC 時刻>.json` を毎回作る (`--dry-run` でも。
  30 日保持)。
  - 実行の開始時に、`status: "running"` と処理する予定の日付 (`planned_dates`。
    台帳から加えた日は `revisit_dates`) を入れて作る。日付を 1 つ処理するたびに、
    その結果を journal `logs/diary-report-<UTC 時刻>.jsonl` に追記してディスクに
    書き出す (途中でプロセスが落ちても、処理済みの日の結果が残る)。
  - 最後に全日付の結果と `status` (`finished`、処理しきれなかったら `interrupted` と
    未処理の `remaining_dates`)・`processed`・`exit_code` を入れて確定し、journal を消す。
  - 日付ごとに、ノートの状態とパス、バックアップのパス、各 source の状態
    (`complete` / `partial` / `failed` / `nodata` / `skipped`)、まだ確定していないキー
    (`unresolved`)、キーごとの action (`insert` / `update` / `unchanged` / `kept` /
    `missing` / `failed` / `skipped`) と理由 (`not_final` / `not_published` /
    `incomplete_day` / `no_value` / `source_failed` / `source_incomplete` / `too_recent` /
    `create_disabled` など) が入る。コメント・重複キーのため残したキーには `warning`
    (`comment present …` / `duplicate key …`)、台帳から外れたキーには `resolved`
    (それまでの理由) と、確定値を書かずに外れたときは `resolution` (`user_value` /
    `no_value_final` / `source_deselected`) が付く。
  - `provisional` には実行後の台帳 (`unresolved`)、3 日を超えたもの (`overdue`)、
    30 日で外したもの (`expired`)、30 日より前の日付で追跡しないもの (`untracked`) が、
    どれも 日付 → キー → 理由 の形で入る。
  - レポートを作れなければ、日記ノートに一切触れずに止める。日付の結果を追記できなければ
    残りの日付は処理しない。最後のレポートを書けなければ journal を残す。どれも失敗
    (終了コード 12。途中経過の場所をリマインダーに載せる) になる。
- **データの鮮度** (その回のリマインダーにまとめる。終了コードは変えない): 最新の
  オートメーションのファイルが「今日 − 3 日」より古い → `HAE オートメーション停止の疑い`。
  オートメーションのファイルが 1 つも無く、どの日の健康データも取れなかった →
  `HAE オートメーション出力が見つかりません`。最初に見つけてから 3 日を超えて確定しない
  値がある → `日記の暫定値が確定していません (要確認)` / `日記の値が確定していません (要確認)`
  ([まだ確定していない値](#まだ確定していない値))。これらは 1 日 1 回まで
  (`logs/.stale_notified`)。1 回きりの項目 (`withheld` / `weather_incomplete` の 1 回だけの
  要確認、30 日で台帳から外した日、30 日より前の日付の未確定のキー) は 1 回だけ送る:
  `logs/.freshness_notified` が今日送った内容を覚えているので、同じ日の後の実行
  (backfill など) で見つかった項目も知らせる。リマインダーを作れなかったときは、台帳から
  外した日と古い日付の項目を `data/diary-state.json` (`unreported`) に残し、次の実行で送る。
- **終了コード**: `0` 失敗なし (まだデータが無いだけの日は失敗ではない)、`12` 一部の
  source の失敗、利用者の対応が要るノートがある、または実行レポート・
  `data/diary-state.json` を保存できない、`11` 日記ノートを書けなかった (書いた後の確認が
  取れなかった場合と、書く前の検査で書き込みを止めた場合を含む)、または全日付で全 source が
  失敗した、`3` 設定エラー (vault や日記ディレクトリが無い、`config.toml` が壊れている、
  キーの衝突)。失敗した source は他の source の書き込みを止めず、そのキーには触れない。

**復旧手順**

| 症状 | 意味 | 対処 |
|---|---|---|
| リマインダー「HAE オートメーション停止の疑い」 | iPhone のオートメーションが 3 日以上書き出していない | iPhone で Health Auto Export を開き、オートメーションが有効か、iCloud Drive が同期しているかを確認して一度実行する。直近 7 日分 (と台帳にある日) は次の実行で埋まる。それより長く空いたら `./bin/ihealth-run --publisher obsidian --from <最初の欠けた日> --sources health` |
| 昨日の健康データが暫定値のまま (レポートで `provisional`) | 昨日の最新のファイルが日付の変わる前に書かれたもので、その値を暫定値として書いた | 通常は待てばよい。Health Auto Export がその日を書き直せば、定時実行が確定値に置き換える (アプリを開くと早まる)。最初に書いてから 3 日を超えても暫定値のままなら、下の「要確認」のリマインダーが届く |
| リマインダー「日記の暫定値が確定していません (要確認)」/「日記の値が確定していません (要確認)」 | 3 日を超えて確定しない値がある。リマインダーに日付・キー・理由と、理由ごとの対処が載る | `provisional`: iPhone で Health Auto Export を開いてオートメーションを実行し、書き出し期間に直近数日が含まれているか確認する。`weather_pending` / `weather_incomplete`: 気象庁の欠測の可能性がある。必要なら手で記入する。`github_incomplete`: 下の GitHub の行。`withheld`: 必要なら手で記入する。`unwritable`: 下のコメントの行。30 日までは自動で処理し直す。それを過ぎたら `./bin/ihealth-run --publisher obsidian --date <日付>` |
| 健康のキーが書かれない (レポートで `not_final`) | 日が終わる前に書かれた AutoSync `.hae` の値で、止めた AutoSync は書き直さない (台帳の理由は `withheld`) | 必要なら手で記入する (記入すると追跡も終わる) |
| キーが `kept` で、warning が `comment present …` / `duplicate key …` | 担当キーの entry にコメントがある (key 行の行末、entry の中の独立したコメント行、継続行の行末、`\|` / `>` の header の後ろ)、または同じキーが 2 つ以上ある。そのキーは書き換えない (他のキーは書く) | コメントをキーの上の独立した行に移すか、重複した entry を 1 つにまとめる。次の実行で書かれる |
| リマインダー「HAE オートメーション出力が見つかりません」 | `HEALTH_EXPORT_DIR` (`AutoSync` を指しているならその親) の下に `HealthAutoExport-YYYY-MM-DD.json` が無い | `HEALTH_EXPORT_DIR` を Health Auto Export の `Documents` フォルダにし、iPhone のオートメーション (iCloud Drive / JSON) を確認する |
| ノートが更新されない (`note.status: missing`) | `YYYY-MM-DD.md` が無く、作成も許されていない (`too_recent` / `create_disabled`) | Obsidian でノートを作る (または `OBSIDIAN_TEMPLATE` を設定する) → `./bin/ihealth-run --publisher obsidian --date <日付>` |
| リマインダー「日記ノートの確認が必要」 | frontmatter が無いノートに Templater の `<% … %>` が残っている、またはテンプレートが使えない | Obsidian でノートを開いてテンプレートを適用する (または `OBSIDIAN_TEMPLATE` を直す) → その日付を再実行 |
| GitHub の失敗 (`[github]`、token / 一部 repo の取得失敗) | その実行の期間の `Github` は書かれていない (不完全な一覧は書かない)。その日は台帳で追跡し (`github_incomplete`)、取りきれた実行で埋まる | `gh auth login` / `GITHUB_TOKEN` を確認し、`GITHUB_OWNERS` / `GITHUB_EXCLUDE_REPOS` の書式を見直す → `--sources github --from <日付1> --to <日付2>` で再実行 |
| 天気の失敗 (`[weather]`) | 気象庁に接続できない / ページの形式が変わった / 過去の日が 2 日以上続けて未完成 (1 時間ごとの行が 1 つも無いページを含む: 観測所の休止や `JMA_PREC_NO` / `JMA_BLOCK_NO` の誤り) | 時間をおいて `--sources weather --date <日付>` で再実行。ブラウザでページを確認する |
| 終了コード 11「日記の書き込み失敗」 | ノートを書けなかった: 権限 / 閉じ `---` が無いなど Obsidian も読めない frontmatter / 書き込み中も変更が続いた / 書いた後に読み直すと別の内容に変わっていることが続いた (書いた直後に他のプログラムが保存した。リマインダーに書く前のバックアップが載る) / 書く前の検査で担当キー以外の変更が見つかった (何も書いていない。不具合なので報告してほしい) | ノートを確認し (必要ならバックアップから戻す)、その日付を再実行 |
| 終了コード 12「日記の実行レポートを保存できません」/「日記の未確定の値の台帳を保存できません」 | `logs/` か `data/` に書けない (容量不足・権限)。開始時にレポートを作れなかった場合、ノートには触れていない | 空き容量や権限を直して再実行する (定時実行が期間を処理し直す) |
| ノートが意図しない形に変わった | — | `data/obsidian-backup/<UTC 日付>/<ノート名>.<UTC 時刻>.md` (上書きの前に毎回保存。30 日保持) から戻す。または Obsidian の「ファイル復元」や Obsidian Sync のバージョン履歴から戻す |

何かを書く前 (バックアップや一時ファイルを作る前。`--dry-run` でも) に、frontmatter の
編集処理とは別に実装した検査で編集前後のノートを比べる。閉じ `---` より後ろの本文、
`---` の行、書き換えるキー以外の frontmatter の行 (他の entry・書き換えずに残した
entry・コメント行・空行) が byte 単位で同じで同じ順序であること、書き換えたキーの entry が
ちょうど 1 つであることを確かめ、どれかが崩れていれば何も書かずに失敗にする (終了コード 11)。

本ツールは置換の直前にノートを読み直し、Obsidian や他のプログラムが変更していたら
読み直してやり直す。置換 (ノートの新規作成も) の後にもノートを読み直し、書いた内容と
違えば (書いた直後に他のプログラムが保存した)、書けたとは数えずに最新の内容を読み直して
値を書き直す。この 2 種類のやり直しは合わせて最大 3 回 (最初の 1 回を含む) で、それでも
値が入ったことを確かめられなければ失敗 (終了コード 11) にし、書く前に取ったバックアップの
場所をリマインダーに載せる。ただし、置換の直前の確認から置換までの μs 単位の間に
書かれた変更は検知できない (Obsidian と共有できるロックが無いため)。バックアップに
入っているのは本ツールが編集の基にした内容で、この間の変更は含まれない。その場合は
Obsidian の「ファイル復元」コアプラグインか Obsidian Sync のバージョン履歴から戻す。

### GitHub の対象範囲

日付 (`IHEALTH_TZ_OFFSET`) ごとに、`Github` キーには次のものが並ぶ。

- その日に **close された Issue**、**merge された PR**、**default branch への commit**
  (committer date がその日のもの。追加・削除行数を repo ごとに合計)
- 対象 repo は **自分が owner の repo と、所属する org の repo** すべて。private repo は
  token で読める場合に含まれる (classic PAT なら `repo` と `read:org`。`gh auth login`
  ならこの権限が付く)。archive 済みの repo (backfill のため) と fork も含む
- 作者は問わない (自分の repo で起きたことの記録)。ただし bot (`…[bot]`)、merge commit、
  PR の merge commit 経由でだけ default branch に入った commit (代わりに PR が並ぶ) は
  除く。squash merge / rebase merge された commit は直接の commit として数える
- **fork**: commit は自分 (GitHub の login) が作者のものだけ数える。fork を同期すると
  upstream の commit が元の日付のまま入ってくるため。fork 自身の Issue / PR は作者を問わない
- `GITHUB_OWNERS` で owner を絞る (例: `your-name,your-org`)。`GITHUB_EXCLUDE_REPOS` で
  repo (`owner/name`) を除く

活動は 1 回の実行で期間全体をまとめて集める。どれか 1 つの repo でも取り切れなければ、
その実行ではどの日の `Github` も書かない (不完全な一覧は「活動なし」と区別できないため)。

### 天気の観測所を変える

既定の観測所は東京 (`JMA_PREC_NO=44`, `JMA_BLOCK_NO=47662`)。

1. 気象庁の [過去の気象データ検索](https://www.data.jma.go.jp/stats/etrn/index.php) で
   都道府県と地点、日付を選び、「1時間ごとの値」を表示する。
2. URL は
   `https://www.data.jma.go.jp/stats/etrn/view/hourly_s1.php?prec_no=NN&block_no=NNNNN&year=...`
   のような形になる。2 つの番号を `.env` に写す:
   ```bash
   JMA_PREC_NO=NN
   JMA_BLOCK_NO=NNNNN
   ```
3. `./bin/ihealth-run --publisher obsidian --date <過去の日付> --sources weather --dry-run` で確かめる。

対応するのは 1 時間ごとの値のページが `hourly_s1.php` になる地点 (気象台など) だけ。
アメダスだけの地点 (`hourly_a1.php`) は別のページで天気欄が無いため使えない。
天気欄 (天気の観測) がある地点を選ぶこと。24 時の行に気温と海面気圧があり、天気の
観測が 1 件以上ある日だけを「完成した日」として扱う。気象庁へのリクエストは 1 秒以上
間隔を空ける。

## プライバシーと健康データの取り扱い

> 本ツールは Apple Health 由来の健康データ (歩数, 距離, 心拍, SpO₂, 睡眠, 体重,
> 体脂肪率, 瞑想記録) を扱う。データは iCloud Drive からローカルに読み込み、
> `--publisher` で選んだ sink に転送する。注意点:
>
> - **Notion / Slack publisher は健康データを HTTPS 経由で第三者サービスに送信する**。
>   Slack Free プランの retention は 90 日、Notion はあなたの private DB に保存される。
>   Webhook URL や Notion token が漏れると 1 日のサマリが第三者に読まれる。
> - **Markdown / SQLite / stdout / Obsidian publisher はデータをローカルに保持する** が、
>   出力先のセキュリティ (FS パーミッション, バックアップ暗号化) は利用者の責任。
>   Obsidian の日記は vault の同期先 (Obsidian Sync, iCloud, git など) にも届く。
> - **Obsidian のバックアップ**: ノートを上書きする前に `data/obsidian-backup/` に
>   コピーを取る (パーミッション 0600, gitignore 済, 30 日保持)。日記本文のコピーである。
>   台帳 `data/diary-state.json` と `logs/` の実行レポートにも、日記に書いた値が入る。
> - **Obsidian 日記の通信**: 気象庁へのリクエストに含まれるのは観測所と日付だけ。
>   GitHub GraphQL API は token 付きで呼ぶが、token をログ・リマインダー・実行レポートには
>   出さない。
> - **`BIRTH_DATE` は必須** (Tanaka 式の最大心拍数算出)。`.env` は秘密ファイルとして
>   扱うこと。将来的に直接 `MAX_HEART_RATE` を渡す方式へ移行を検討中。
> - **GDPR / HIPAA / 個人情報保護法**: 本ソフトウェアは規制対象の医療機器では
>   ない。規制環境での利用は運用者の責任 (MIT 免責条項適用)。
> - `dryrun` publisher は HTTPS を発射せずにログ出力のみ。動作検証用。

## Sink 一覧

`--publisher` で選ぶ。省略時は `.env` の `IHEALTH_PUBLISHER`、それも無ければ `notion`。
`--dry-run` は単日の Sink を `dryrun` に置き換える。`obsidian` は独自の dry-run
(差分の表示だけ) を持つ。

| Sink | CLI | 出力 | 必要な設定 |
|---|---|---|---|
| `notion` (既定) | `python3 -m ihealth` | Notion の日記ページ | `.env` の `NOTION_SECRET`, `DATABASE_ID` |
| `obsidian` | `--publisher obsidian` | 既存の日記ノート `<vault>/<日記ディレクトリ>/**/YYYY-MM-DD.md` の frontmatter (健康 + 天気 + GitHub, 複数日) | `.env` の `OBSIDIAN_VAULT_DIR` |
| `markdown` | `--publisher markdown` | `output_dir` に 1 日 1 つ生成する `YYYY-MM-DD.md` (毎回ファイル全体を書き直す) | `.env` の `MARKDOWN_OUTPUT_DIR` か `--markdown-out PATH` |
| `stdout` | `--publisher stdout` | stdout に JSON 1 行 | なし |
| `sqlite` | `--publisher sqlite` | `daily_health` テーブルへ UPSERT | `.env` の `SQLITE_DB_PATH` か `--sqlite-path PATH` |
| `slack` | `--publisher slack` | Incoming Webhook で日次サマリ | `.env` の `SLACK_WEBHOOK_URL` か `--slack-webhook URL` |
| `dryrun` | `--dry-run` か `--publisher dryrun` | Notion の payload をログに出す (HTTP なし) | なし |

`notion` / `markdown` / `stdout` / `sqlite` / `slack` / `dryrun` は 1 日分 (既定は前日) の
AutoSync `.hae` だけを読むので、`HEALTH_EXPORT_DIR` は `AutoSync` ディレクトリにする。

## 取得データ

健康データのフィールド、既定の名前、元になる Health Auto Export のメトリクス。
名前は `config.toml` で変えられる。

| データ | Health Auto Export のメトリクス | Notion の既定プロパティ | Obsidian の既定キー | 単位 |
|---|---|---|---|---|
| 日付 | — | `Date` (日本語 DB の例: `日付`) | (ファイル名) | date |
| 歩数 | Step Count | `Step Count` | `歩数 (歩)` | 歩 |
| 移動距離 | Walking + Running Distance | `Distance (km)` | `移動距離 (km)` | km |
| 消費カロリー | Active Energy | `Active Energy (kcal)` | `消費カロリー (kcal)` | kcal |
| アクティビティ時間 | Apple Exercise Time | `Exercise Minutes` | `アクティビティ時間 (分)` | 分 |
| 運動強度スコア | Heart Rate | `Exercise Intensity Score` | `運動強度スコア` | pt |
| 平均心拍 | Heart Rate | `Heart Rate Avg (bpm)` | `平均心拍数 (bpm)` | bpm |
| 最大心拍 | Heart Rate | `Heart Rate Max (bpm)` | `最大心拍数 (bpm)` | bpm |
| 安静時心拍 | Resting Heart Rate | `Heart Rate Resting (bpm)` | `安静時心拍数 (bpm)` | bpm |
| 酸素飽和度 | Blood Oxygen Saturation | `Oxygen Saturation (%)` | `酸素飽和度 (%)` | % |
| 睡眠時間 | Sleep Analysis | `Sleep Minutes` | `睡眠時間 (分)` | 分 |
| 昼寝 | Sleep Analysis | `Nap Minutes` | `昼寝時間` | 分 |
| 瞑想回数 | Mindful Minutes | `Mindful Sessions` | `瞑想回数 (回)` | 回 |
| 瞑想時間 | Mindful Minutes | `Mindful Minutes` | `瞑想時間 (分)` | 分 |
| 体重 | Weight & Body Mass | `Body Mass (kg)` | `体重 (kg)` | kg |
| 体脂肪率 | Body Fat Percentage | `Body Fat (%)` | `体脂肪率 (%)` | % |

Notion の既定プロパティ名は v0.1.0 から英語。日本語の列名の Notion DB を使うなら
`cp config.example.toml config.toml` で override する (例は `config.example.toml` に
ある)。Obsidian publisher はこれに加えて `アクティビティ詳細`・天気の 6 キー・`Github`
を書く ([書き込む内容](#書き込む内容))。単日の Sink では、睡眠時間は 0〜14 時に終わった
夜の主睡眠、昼寝は 14〜20 時に終わった睡眠 (AutoSync `.hae`)。

> **運動強度スコア** は Google Fit の Heart Points に倣った近似値 ("Heart Points" は
> Google LLC の商標。本実装は独自のもので Google とは無関係)。心拍レコードを 1 分ごとの
> bucket に分け、bucket 内の平均 `avg` が最大心拍数の 70% 以上なら 2pt、50% 以上なら
> 1pt として合計する。最大心拍数は Tanaka 式 (`205.8 − 0.685 × 年齢`) で、年齢は `.env`
> の `BIRTH_DATE` から求める。心拍データがあってどの 1 分も zone に届かなかった日は
> `0` を書く (心拍データ自体が無い日は空のまま)。Health Auto Export の心拍は間隔の
> 空いたサンプル (数百秒おきのことも多い) なので、Google Fit の「連続 1 分」判定は
> 厳密には再現できず、公式値よりやや多めに出る (issue #13)。

## 開発

```bash
# テスト一式 (unittest, 外部依存なし, ネットワークなし)
PYTHONPATH=src /usr/bin/python3 -m unittest discover tests

# 手動実行ラッパー (launchd と同じエントリ)
./bin/ihealth-run --date 2026-04-22
```

`tests/test_e2e_diary.py` は、合成の fixture (`tests/fixtures/e2e/`: オートメーション JSON・
気象庁のページ・GitHub GraphQL の応答・日記ノート) で Obsidian 日記のパイプライン全体を通す
テスト。差し替えるのは外部 I/O (HTTP・`gh`・iCloud の materialize・時計・Reminders) だけで、
通常日・日本時間 0 時の日付の境界・日の途中の書き出し・欠測の 4 つの条件を確かめる。

CI は [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) で
macOS + Linux × Python 3.9-3.12 を回す (macOS arm64 では 3.9/3.10 セルは Apple ランナーが
提供しないため exclude)。

## 終了コード

| Code | Class | 原因 |
|---|---|---|
| 0 | — | 正常終了 |
| 1 | (catch-all) | 想定外例外 |
| 2 | argparse / `--*` validation | CLI 引数の形式エラー |
| 3 | `ConfigAppError` | `.env` / `config.toml` 設定不備 (Obsidian の vault / 日記ディレクトリ不在を含む) |
| 4 | `HealthExportDirError` | `HEALTH_EXPORT_DIR` 不在 / 非ディレクトリ |
| 5 | `SourceAppError` | `.hae` 取得 / 解凍失敗 |
| 6 | `NotionAppError` | Notion API 失敗 / ページ未作成 / 列名不一致 |
| 7 | `MarkdownAppError` | Markdown 書き込み失敗 |
| 8 | `StdoutAppError` | stdout 書き込み失敗 |
| 9 | `SQLiteAppError` | SQLite I/O / SQL / lock 競合 |
| 10 | `SlackAppError` | Slack Webhook HTTP 失敗 |
| 11 | `ObsidianAppError` | Obsidian: 日記ノートを書けなかった (書いた後の読み直しで確認が取れなかった場合と、書く前の検査で止めた場合を含む) / 全日付で全 source が失敗 |
| 12 | `DiaryPartialFailureError` | Obsidian: 一部の source・日付で取得に失敗 / 利用者の対応が要るノートがある / 実行レポート・`data/diary-state.json` を保存できない (開始時にレポートを作れなければノートは書かない) |

終了コード 1 と 3〜12 は Reminders.app にリマインダーを立て、確認するまで残す
(引数エラーの 2 は stderr に出すだけ)。obsidian は 1 回の実行を 1 件のリマインダーに
まとめ、同じ内容のリマインダー (`.env` / `config.toml` の設定エラー = 3 と想定外の
エラー = 1 を含む) は 1 日 1 回だけ作る。`obsidian --dry-run` はログに出すだけ。

## ドキュメント

- [`README.md` (英語)](../../README.md) — メイン
- [`architecture.md`](../architecture.md) — モジュール構成
- [`setup.md`](../setup.md) — 詳細セットアップ手順 (Obsidian の vault / Notion DB を含む)
- [`case-study/dataless-icloud.md`](../case-study/dataless-icloud.md) —
  silent EDEADLK の調査記録
- [`CHANGELOG.md`](../../CHANGELOG.md) — 変更履歴

## ライセンス

[MIT](../../LICENSE) — Atsuro Murata, 2026.
