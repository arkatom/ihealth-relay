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
  コメント・改行コード・BOM は byte 単位でそのまま残す。上書きの前には必ず
  バックアップを取り、Obsidian / Obsidian Sync / 他のツールによる同時編集を
  置換の直前に検知する。
- **確かな値だけを書く**: まだ終わっていない日 (今日) の値は暫定値として書き、
  確定値に置き換わるまで追跡する。取得が不完全な source は書かず、既存の値を残す。
  失敗は 1 回の実行につき 1 件の Reminders.app リマインダーにまとめる。
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

## Obsidian 日記 (`--publisher obsidian`)

期間内の日付ごと (既定は「昨日から 7 日分」+「今日」) に 3 つの source を独立に
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
| `アクティビティ詳細` | `activity_detail` | 健康 | 瞑想 + ワークアウト。無ければ `なし`。オートメーションがワークアウトも書き出すときだけ ([注意](#健康データの値についての注意)) |
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
   も書くなら **ワークアウト** も書き出す設定にする (ワークアウトが無いと運動の有無を
   判断できないので、そのキーには触れない)。画面上の項目名はアプリのバージョンで多少違う。
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
5. **定期実行**: `./bin/install-launchd.sh` ([定期実行 (launchd)](#定期実行-launchd))。

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
| `IHEALTH_SOURCES` | `health,weather,github` | 処理する source |
| `IHEALTH_TZ_OFFSET` | `+09:00` | 日記の基準タイムゾーン。「今日 / 昨日」と GitHub 活動の日付の区切りに使う |
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
  例外: 本ツールが書いた暫定値のまま変わっていないキー ([いつ反映されるか](#いつ反映されるか))
  は確定値に置き換える。利用者が書き換えた値は残す。
- `--dry-run` は日記ノートを書かず、vault にファイルも作らない (バックアップも取らない)。
  source の取得と実行レポートの保存は通常どおり行うが、リマインダーは作らずに
  ログに出すだけで、通知済みの記録も暫定値の台帳 (`data/diary-state.json`) も
  書き換えない (dry-run が後の本番の実行の通知を止めたり、追跡を変えたりしない)。
- `--from` / `--to` / `--sources` / `--fill-empty-only` は obsidian 専用
  (他の publisher で指定すると終了コード 2)。
- `.env` に `IHEALTH_PUBLISHER=obsidian` があれば `--publisher obsidian` は省略できる。

### 定期実行 (launchd)

`./bin/install-launchd.sh` は `bin/ihealth-run` を **引数なしで** 起動する LaunchAgent を
登録する。起動時刻は `.env` の `IHEALTH_PUBLISHER` で決まる: `obsidian` なら
**07:00 / 12:00 / 21:00** (毎回既定の期間を処理するので、今日の暫定値が日中も更新され、
遅れて届いた再出力もその日のうちに拾う)、それ以外の publisher は従来どおり **07:00 だけ**
(Slack が 1 日 3 回投稿するのを防ぐ)。`IHEALTH_PUBLISHER` を変えたら installer を
再実行する。`./bin/install-launchd.sh --print-plist` で、何も変更せずに登録する plist を
確認できる。スリープ中に過ぎた起動は復帰後に 1 回だけ実行され、その直後は最大
`IHEALTH_NETWORK_GRACE_SEC` 秒 Wi-Fi の接続を待つ。

- アプリのログ: `logs/run.log` (日次ローテーション, 30 日保持)
- launchd の stdout / stderr: `~/Library/Logs/ihealth-relay/launchd.log`。起動失敗・
  警告・エラーだけが入る (このファイルはローテーションされない。`--verbose` なら全部出る)
- すぐ実行する: `launchctl kickstart -k gui/$(id -u)/com.ihealthrelay.daemon`

### いつ反映されるか

| source | 今日 | 昨日 | 2〜7 日前 |
|---|---|---|---|
| 健康 | **暫定値**: 最新のオートメーションのファイルにあるその時点までの値。実行のたびに置き換わる | Health Auto Export が昨日のファイルを日付が変わった後に書き直した時点で **確定値** を書く。それまでは何も書かないので、ノートには最後の暫定値が残る (本ツールはそれを覚えていて、その日を処理し直し続ける。下記) | 同じ規則。遅れて再出力されても期間内のどの実行でも拾う |
| 天気 | 書かない (気象庁はその日が終わってから公開する) | 気象庁のページが完成したら書く。天気欄は数値より後から埋まるので、たいていはその日の後の回になる | 同じ。未完成のままの日は書かない ([失敗時の見え方](#失敗時の見え方と復旧手順)) |
| GitHub | その時点までの活動 (暫定値。健康と同じく追跡する) | 期間全体の収集が成功したときに書く。活動が無ければ `該当なし` | 同じ |

- 健康データは、その日が終わった後 (Mac のタイムゾーンで翌日 0:00 以降) に書かれた
  ファイルだけを確定値として扱う。Health Auto Export はオートメーションが動くたびに
  直近数日のファイルを書き直すが、iOS がアプリにバックグラウンドの実行時間を与えた
  ときにしか動かない。値が遅れるときは iPhone で Health Auto Export を一度開く。
- 健康データは各レコード自身の UTC オフセットの日付で数える (海外で記録した日も現地の
  日付になる)。天気は観測所の日付 (日本時間)。GitHub の日付と「今日」は
  `IHEALTH_TZ_OFFSET` で決める。
- 今日・昨日のノートは、すでにある場合だけ書く (本ツールは作らない。
  `OBSIDIAN_CREATE_MIN_AGE_DAYS` を参照)。一昨日以前のノートは `OBSIDIAN_TEMPLATE`
  (または `OBSIDIAN_CREATE_MISSING`) が許す場合だけ作る。作る場所は前後 7 日以内で
  いちばん近い既存の日記ノートと同じフォルダ (無ければ日記ディレクトリの直下)。
- **暫定値は確定するまで追跡する。** 暫定値として書いたキー (今日の健康・アクティビティ
  詳細・Github) は値とともに `data/diary-state.json` に記録する。定期実行 (引数なし) は
  その日が 7 日の期間から外れても **30 日まで** 処理し直し、確定値を書けた (または
  利用者が値を書き換えた) キーから記録を外す。期間の外では、空のキーを埋めることと
  本ツールが書いた暫定値のままのキーを置き換えることだけをする (利用者が書いた値は残す)。
  3 日を過ぎても確定しない日があれば、1 日 1 回
  リマインダー「日記の暫定値が確定していません」を作る。
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
- **アクティビティ詳細** は瞑想 (`瞑想N分`) と各ワークアウト (`ウォーキング31分` など) を
  並べ、どちらも無ければ `なし`。ただし書くのはオートメーションの JSON にワークアウトが
  含まれている (ワークアウトも書き出す設定の) ときだけ。含まれていない日と、AutoSync
  `.hae` だけで集計する日は書かない (`なし` や `瞑想N分` だけを書くと、ノートにすでにある
  ワークアウトの記録を消してしまうため)。
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
- **実行レポート** `logs/diary-report-<UTC 時刻>.json` を毎回保存する (`--dry-run` でも。
  30 日保持)。日付ごとに、ノートの状態とパス、バックアップのパス、各 source の状態
  (`complete` / `partial` / `failed` / `nodata` / `skipped`)、キーごとの action
  (`insert` / `update` / `unchanged` / `kept` / `missing` / `failed` / `skipped`) と理由
  (`not_final` / `not_published` / `incomplete_day` / `no_value` / `source_failed` /
  `source_incomplete` / `too_recent` / `create_disabled` など) が入る。
- **データの鮮度** (1 日 1 回, `logs/.stale_notified`。終了コードは変えない。その回の
  リマインダーがあればそれにまとめる): 最新のオートメーションのファイルが「今日 − 3 日」
  より古い → `HAE オートメーション停止の疑い`。オートメーションのファイルが 1 つも無く、
  どの日の健康データも取れなかった → `HAE オートメーション出力が見つかりません`。
  暫定値が 3 日を過ぎても確定しない (30 日で追跡をやめる日も最後に 1 回) →
  `日記の暫定値が確定していません`。
- **終了コード**: `0` 失敗なし (まだデータが無いだけの日は失敗ではない)、`12` 一部の
  source の失敗、または利用者の対応が要るノートがある、`11` 日記ノートを書けなかった、
  または全日付で全 source が失敗した、`3` 設定エラー (vault や日記ディレクトリが無い、
  `config.toml` が壊れている、キーの衝突)。失敗した source は他の source の書き込みを
  止めず、そのキーには触れない。

**復旧手順**

| 症状 | 意味 | 対処 |
|---|---|---|
| リマインダー「HAE オートメーション停止の疑い」 | iPhone のオートメーションが 3 日以上書き出していない | iPhone で Health Auto Export を開き、オートメーションが有効か、iCloud Drive が同期しているかを確認して一度実行する。直近 7 日分は次の実行で埋まる。それより長く空いたら `./bin/ihealth-run --publisher obsidian --from <最初の欠けた日> --sources health` |
| 昨日の健康データが入らない / 暫定値のまま (レポートで `not_final`) | 昨日のファイルが日付の変わる前に書かれたまま | 通常は対処不要。定期実行が、Health Auto Export がその日を書き直すまで処理し直す (アプリを開くと早まる)。3 日たっても確定しなければ下のリマインダーが届く |
| リマインダー「日記の暫定値が確定していません」 | Health Auto Export がその日のファイルを、日付が変わった後に書き直していない (オートメーションの停止、または書き出し期間に過去の日が入っていない) | iPhone で Health Auto Export を開いてオートメーションを実行し、書き出し期間に直近数日が含まれているか確認する。30 日までは自動で処理し直す。それを過ぎたら `./bin/ihealth-run --publisher obsidian --date <日付>` |
| リマインダー「HAE オートメーション出力が見つかりません」 | `HEALTH_EXPORT_DIR` (`AutoSync` を指しているならその親) の下に `HealthAutoExport-YYYY-MM-DD.json` が無い | `HEALTH_EXPORT_DIR` を Health Auto Export の `Documents` フォルダにし、iPhone のオートメーション (iCloud Drive / JSON) を確認する |
| ノートが更新されない (`note.status: missing`) | `YYYY-MM-DD.md` が無く、作成も許されていない (`too_recent` / `create_disabled`) | Obsidian でノートを作る (または `OBSIDIAN_TEMPLATE` を設定する) → `./bin/ihealth-run --publisher obsidian --date <日付>` |
| リマインダー「日記ノートの確認が必要」 | frontmatter が無いノートに Templater の `<% … %>` が残っている、またはテンプレートが使えない | Obsidian でノートを開いてテンプレートを適用する (または `OBSIDIAN_TEMPLATE` を直す) → その日付を再実行 |
| GitHub の失敗 (`[github]`、token / 一部 repo の取得失敗) | その実行の期間の `Github` は書かれていない (不完全な一覧は書かない) | `gh auth login` / `GITHUB_TOKEN` を確認し、`GITHUB_OWNERS` / `GITHUB_EXCLUDE_REPOS` の書式を見直す → `--sources github --from <日付1> --to <日付2>` で再実行 |
| 天気の失敗 (`[weather]`) | 気象庁に接続できない / ページの形式が変わった / 過去の日が 2 日以上続けて未完成 (1 時間ごとの行が 1 つも無いページを含む: 観測所の休止や `JMA_PREC_NO` / `JMA_BLOCK_NO` の誤り) | 時間をおいて `--sources weather --date <日付>` で再実行。ブラウザでページを確認する |
| 終了コード 11「日記の書き込み失敗」 | ノートを書けなかった (権限、閉じ `---` が無いなど Obsidian も読めない frontmatter、書き込み中も変更が続いた) | ノートを直してからその日付を再実行 |
| ノートが意図しない形に変わった | — | `data/obsidian-backup/<UTC 日付>/<ノート名>.<UTC 時刻>.md` (上書きの前に毎回保存。30 日保持) から戻す。または Obsidian の「ファイル復元」や Obsidian Sync のバージョン履歴から戻す |

本ツールは置換の直前にノートを読み直し、Obsidian や他のプログラムが変更していたら
読み直してやり直す (試行は最初の 1 回を含めて最大 3 回。やり直しは 2 回まで。それでも
変更が続けば書かずに諦める)。ただし、その確認から置換までの μs 単位の間に
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
| 11 | `ObsidianAppError` | Obsidian: 日記ノートを書けなかった / 全日付で全 source が失敗 |
| 12 | `DiaryPartialFailureError` | Obsidian: 一部の source・日付で取得に失敗 / 利用者の対応が要るノートがある / `data/diary-state.json` を保存できない |

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
