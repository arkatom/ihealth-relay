# ihealth-relay

A macOS batch tool that relays Apple Health data exported by the iPhone app
**Health Auto Export — JSON+CSV** to where you keep your days — with zero
external Python dependencies:

- **Obsidian daily notes** (`--publisher obsidian`): writes health metrics,
  the day's weather from the Japan Meteorological Agency (JMA) and your GitHub
  activity into the YAML frontmatter of your `YYYY-MM-DD.md` diary notes,
  touching nothing but its own keys.
- **Pluggable single-day sinks**: Notion, Markdown files, stdout (JSON line),
  SQLite, Slack.

[日本語版 README はこちら](docs/ja/README.ja.md)

## Highlights

- **Zero external Python dependencies**: standard library only — no
  `requirements.txt`, no `venv`, no `pip install`. Notion, GitHub and JMA are
  called via `urllib.request`, the JMA page is parsed with `html.parser`,
  SQLite uses the bundled `sqlite3`, and TOML parsing uses a minimal in-tree
  parser (`src/ihealth/_toml.py`).
  *(This refers to PyPI packages only. Network access to Notion / Slack /
  GitHub / JMA and iCloud Drive / Health Auto Export are obvious external
  dependencies of the features you enable.)*
- **No Apple Developer Program ($99/yr) required**: avoids the
  `com.apple.developer.healthkit` entitlement by delegating data extraction
  to Health Auto Export on the iPhone.
- **Obsidian diary pipeline**: for every day in a window, health (Health Auto
  Export automation JSON, with AutoSync `.hae` as supplement and fallback),
  JMA weather and GitHub activity are fetched independently — one failing
  source never blocks the others.
- **Safe edits to notes you write by hand**: only the tool's own frontmatter
  keys change; the body, other keys, comments, line endings and BOM stay
  byte-identical. Every overwrite is preceded by a backup, and concurrent
  edits (Obsidian, Obsidian Sync, other tools) are detected before the file is
  replaced.
- **Writes only values it is sure about**: a day that has not ended yet is
  written as provisional and tracked until the final values replace it,
  incomplete sources are skipped (the note keeps its existing values), and
  failures are summarized in one Reminders.app reminder per run.
- **Pluggable Sinks**: `--publisher notion | markdown | stdout | sqlite | slack | dryrun | obsidian`.
  Each sink is an independent module under `src/ihealth/publishers/`.
- **Idempotent**: re-running for the same date overwrites the existing
  Notion page / Markdown file / SQLite row; Obsidian notes are only rewritten
  when a value actually changes. Manually-entered values in Notion are
  preserved (`None` fields are skipped on update).
- **Persistent failure notifications**: failures create a Reminder in
  Reminders.app (no auto-dismiss like Notification Center).
- **macOS dataless-file aware**: detects iCloud Drive's `SF_DATALESS`
  flag, triggers `brctl download`, and waits for materialization before
  reading `.hae` / automation JSON files. Fixes silent EDEADLK in headless
  launchd autoruns.

## Architecture

```
 iPhone                                    iCloud Drive (Health Auto Export)
┌────────────────────┐  automation (JSON) ┌────────────────────────────────────┐
│ Health.app         │───────────────────▶│ Documents/<automation name>/       │
│ (HealthKit)        │                    │   HealthAutoExport-YYYY-MM-DD.json │
│ Health Auto Export │  AutoSync (.hae)   │ Documents/AutoSync/HealthMetrics/  │
│                    │───────────────────▶│   {metric}/YYYYMMDD.hae            │
└────────────────────┘                    └─────────────────┬──────────────────┘
                                                            │ iCloud sync
 JMA hourly observations ──┐              ┌─────────────────▼──────────────────┐
 GitHub GraphQL API ───────┴─────────────▶│ macOS: python3 -m ihealth          │
                                          │ (launchd 07:00 / 12:00 / 21:00)    │
                                          └────────┬──────────────────┬────────┘
                      --publisher obsidian         │                  │  other publishers
                      (window of days, 3 sources)  ▼                  ▼  (one day, AutoSync .hae)
                                 ┌──────────────────────────┐ ┌──────────────────────────┐
                                 │ Obsidian vault           │ │ Notion / Markdown /      │
                                 │ <diary dir>/**/DATE.md   │ │ stdout / SQLite / Slack  │
                                 │ (frontmatter upsert)     │ │                          │
                                 └──────────────────────────┘ └──────────────────────────┘
```

See [`docs/architecture.md`](docs/architecture.md) for module-level details.

## Quick Start

### Requirements

- macOS 14+ (Sonoma) on Apple Silicon (also tested on Intel)
- Python 3.9+ (`/usr/bin/python3` is bundled with macOS as 3.9.x)
- iPhone with iCloud syncing on, and **Health Auto Export — JSON+CSV**
  (App ID 1115567069) Premium one-time purchase
- For the GitHub part of the Obsidian diary: a GitHub token, or the
  [GitHub CLI](https://cli.github.com/) logged in (`gh auth login`)

### Setup

1. **iPhone**: install Health Auto Export and grant HealthKit permissions for
   the metrics in [Collected Data](#collected-data). Then
   - for **Obsidian daily notes**: create an *automation* that exports JSON to
     iCloud Drive — see [Setup for Obsidian](#setup-for-obsidian);
   - for **Notion / Markdown / stdout / SQLite / Slack**: enable **AutoSync**
     to iCloud Drive (Premium feature).
2. **macOS**: clone this repository.
3. Copy `.env.example` to `.env` and fill in (`chmod 600 .env`):
   ```bash
   HEALTH_EXPORT_DIR # Health Auto Export folder in iCloud Drive (see .env.example)
   BIRTH_DATE        # YYYY-MM-DD; used to compute max heart rate (Tanaka formula)
   # Publisher-specific:
   IHEALTH_PUBLISHER     # default publisher when --publisher is omitted (e.g. obsidian)
   OBSIDIAN_VAULT_DIR    # required for --publisher obsidian (more keys below)
   NOTION_SECRET         # Notion integration token (only if --publisher notion)
   DATABASE_ID           # Notion DB ID (URL last 32 chars; only if notion)
   MARKDOWN_OUTPUT_DIR   # required if --publisher markdown
   SQLITE_DB_PATH        # required if --publisher sqlite
   SLACK_WEBHOOK_URL     # required if --publisher slack
   ```
4. **(Optional) Name overrides**: if your Notion DB uses non-default column
   names (e.g., legacy Japanese names like `歩数 (歩)`), or your diary uses
   different frontmatter keys, copy the template:
   ```bash
   cp config.example.toml config.toml
   # Edit `config.toml` to map internal field names → your column / key names.
   # `config.toml` is gitignored (per-user setting).
   ```
5. Try a dry-run first (nothing is sent to or written into the destination):
   ```bash
   PYTHONPATH=src /usr/bin/python3 -m ihealth --dry-run --verbose
   ```
6. Real run (yesterday for single-day sinks; for Obsidian, see below):
   ```bash
   PYTHONPATH=src /usr/bin/python3 -m ihealth
   ```
7. **Schedule via launchd** (optional):
   ```bash
   ./bin/install-launchd.sh   # installs LaunchAgent/com.ihealthrelay.daemon.plist
   ```

See [`docs/setup.md`](docs/setup.md) for detailed instructions, including
the Obsidian vault, Notion DB schema setup and per-sink configuration.

## Obsidian daily notes (`--publisher obsidian`)

For every day in a window (by default yesterday back 7 days, plus today), the
tool collects three sources independently and writes them into the
frontmatter of that day's diary note:

- **Health** — Health Auto Export *automation* JSON
  (`<automation folder>/HealthAutoExport-YYYY-MM-DD.json`), supplemented by
  AutoSync `.hae` files where the JSON lacks a value (and used alone for days
  without JSON, e.g. backfills of older days).
- **Weather** — the JMA "過去の気象データ検索" hourly page of one station
  (default: Tokyo).
- **GitHub** — issues closed, pull requests merged and default-branch
  commits in your repositories (see [GitHub scope](#github-scope)).

Notes are found anywhere under the diary directory by exact file name
`YYYY-MM-DD.md` (so `日記/2026-10-12.md`, `日記/2025/2025-03-01.md` and other
subfolders all work; a note named `2025-09-07 Weekly review.md` is never
touched).

### What it writes

Default frontmatter keys (Japanese, matching the diary template the tool was
built for — rename any of them via `config.toml`):

| Key (default) | Internal name | Source | Rule |
|---|---|---|---|
| `歩数 (歩)` | `step_count` | Health | sum, integer |
| `睡眠時間 (分)` | `sleep_hours` | Health | minutes (see [notes](#notes-on-health-values)) |
| `昼寝時間` | `nap_hours` | Health | minutes; only when naps can be separated |
| `消費カロリー (kcal)` | `active_energy_kcal` | Health | sum, 1 decimal (kJ converted) |
| `移動距離 (km)` | `distance_km` | Health | sum, 1 decimal (mi / m converted) |
| `安静時心拍数 (bpm)` | `heart_rate_resting` | Health | mean, 1 decimal |
| `平均心拍数 (bpm)` | `heart_rate_avg` | Health | mean of per-minute averages, 1 decimal |
| `最大心拍数 (bpm)` | `heart_rate_max` | Health | max, 1 decimal |
| `酸素飽和度 (%)` | `oxygen_saturation` | Health | mean, 1 decimal |
| `運動強度スコア` | `exercise_intensity_score` | Health | Heart-Points-style score (see [Collected Data](#collected-data)) |
| `瞑想時間 (分)` | `mindful_minutes` | Health | sum, integer minutes |
| `瞑想回数 (回)` | `mindful_sessions` | Health | count |
| `アクティビティ時間 (分)` | `exercise_minutes` | Health | Apple Exercise Time, integer minutes |
| `体重 (kg)` | `body_mass_kg` | Health | latest, 1 decimal (lb converted) |
| `体脂肪率 (%)` | `body_fat_percentage` | Health | latest, 1 decimal |
| `アクティビティ詳細` | `activity_detail` | Health | mindfulness + workouts, or `なし`; only when the automation exports workouts (see [notes](#notes-on-health-values)) |
| `気温` | `temperature` | Weather | morning / afternoon / night means + daily max / min |
| `湿度` | `humidity` | Weather | same shape as 気温 |
| `気圧` | `pressure` | Weather | sea-level pressure, four 6-hour means + trend marks |
| `天気` | `weather` | Weather | 3-hourly weather observations |
| `日照時間` | `sunshine` | Weather | daily total |
| `降水量` | `precipitation` | Weather | morning / afternoon / night totals |
| `Github` | `github` | GitHub | one Markdown link per item, or `該当なし` |

Example (all values synthetic):

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

- The formats match the values the predecessor (GoogleFitNotionIntegration)
  wrote, so diaries migrated from Notion keep a uniform history.
- Weather periods: JMA's "N時" row covers (N−1):00–N:00; 朝 = 5–11時,
  昼 = 12–18時, 夜 = 19–24時 and 1–4時. `⤵️💣️` marks a drop of 5 hPa or more
  into the next 6-hour span, `⤴️⚠️` a rise of 5 hPa or more.
- GitHub items: `[🎫 repo:Issue #n: title](url)`, `[🔀 repo:PR #n: title](url)`,
  `[📝 repo:変更行数:+added-deleted (n commits)](url)`, ordered issues → PRs →
  commits.
- Values that are missing are **not** written (the key keeps whatever it had);
  nothing is ever written as a fake `0`.

### Setup for Obsidian

1. **iPhone — Health Auto Export automation**: create an automation that
   exports **Health Metrics** to **iCloud Drive** as **JSON** (export version
   2), with data aggregation on, grouped by **minutes** (the exercise
   intensity score counts minutes, so coarser grouping undercounts it), sleep
   aggregation on (see [notes](#notes-on-health-values) for naps) and **one
   file per day**.
   Include the metrics listed in [Collected Data](#collected-data). To get
   `アクティビティ詳細`, also let the automation export **workouts** (without
   them the tool cannot tell whether you exercised, so it leaves that key
   alone). Labels differ slightly between app versions. The files appear as
   `iCloud Drive › Health Auto Export › <automation name> › HealthAutoExport-YYYY-MM-DD.json`;
   the folder name is free — the tool searches for the file pattern.
2. **`.env`** (see `.env.example` for every key with comments; comments must
   be on their own line — `KEY=value # note` would make `# note` part of the
   value):
   ```bash
   # launchd runs without arguments, so make obsidian the default publisher
   IHEALTH_PUBLISHER=obsidian
   HEALTH_EXPORT_DIR=~/Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents
   # your own birth date (used for the maximum heart rate)
   BIRTH_DATE=1990-01-01
   OBSIDIAN_VAULT_DIR=~/obsidian/<vault>
   # vault-relative diary directory (default 日記)
   OBSIDIAN_DIARY_DIR=日記
   # optional: create missing notes (older than yesterday) from this template
   #OBSIDIAN_TEMPLATE=Templates/Daily.md
   ```
   `HEALTH_EXPORT_DIR` may point at the Health Auto Export `Documents` folder
   (recommended for Obsidian) or at its `AutoSync` subfolder (required by the
   other publishers; Obsidian then searches the parent folder for automation
   files, also after AutoSync's `HealthMetrics` folder has been removed).
3. **GitHub**: run `gh auth login` once, or set `GITHUB_TOKEN`.
4. **Preview**, then write:
   ```bash
   ./bin/ihealth-run --publisher obsidian --dry-run   # logs "[dry-run] <note>: <key>: old -> new"
   ./bin/ihealth-run --publisher obsidian
   ```
5. **Schedule**: `./bin/install-launchd.sh` (see [Schedule](#schedule-launchd)).

#### `.env` keys for Obsidian

| Key | Default | Meaning |
|---|---|---|
| `IHEALTH_PUBLISHER` | `notion` | Publisher when `--publisher` is omitted. Set `obsidian` for launchd. |
| `OBSIDIAN_VAULT_DIR` | — (required) | Vault root (`~/...` or absolute). |
| `OBSIDIAN_DIARY_DIR` | `日記` | Diary directory inside the vault; searched recursively. |
| `OBSIDIAN_TEMPLATE` | unset | Templater template used to create missing notes (vault-relative, `~/...` or absolute). Only a leading `<%* … %>` block (removed), `<% dateStr %>` and `<% weekday %>` are expanded; a template with any other `<%` is not used. |
| `OBSIDIAN_CREATE_MISSING` | `template` if a template is set, else `never` | `never` / `template` / `minimal` (fall back to a note holding only the date key). |
| `OBSIDIAN_CREATE_MIN_AGE_DAYS` | `2` | Create notes only for days at least this old, so today's and yesterday's notes are left to you (avoids Obsidian Sync conflicts with a note created on another device). `0` allows today. |
| `IHEALTH_LOOKBACK_DAYS` | `7` | Default window: yesterday back this many days (0–366). |
| `IHEALTH_INCLUDE_TODAY` | `true` | Also write today's provisional values. |
| `IHEALTH_SOURCES` | `health,weather,github` | Sources to process. |
| `IHEALTH_TZ_OFFSET` | `+09:00` | Diary time zone: decides "today"/"yesterday" and GitHub day boundaries. |
| `IHEALTH_NETWORK_GRACE_SEC` | `60` | Seconds to wait for the network right after wake (0–600, `0` = don't wait). Connection errors that clear within this time are retried, not reported. |
| `JMA_PREC_NO` / `JMA_BLOCK_NO` | `44` / `47662` (Tokyo) | Weather station, see [Changing the JMA station](#changing-the-jma-station). |
| `GITHUB_TOKEN` | unset → `gh auth token` | Token used for the GitHub GraphQL API. Never logged. |
| `GITHUB_OWNERS` | you + your organizations | Comma-separated owner logins to include. |
| `GITHUB_EXCLUDE_REPOS` | none | Comma-separated `owner/name` to skip. |

#### `config.toml` overrides

```toml
[publishers.obsidian]
date_property = "日付"        # date key of notes created with OBSIDIAN_CREATE_MISSING=minimal

[publishers.obsidian.fields.github]
property = "GitHub"          # write GitHub activity to "GitHub" instead of "Github"
```

Internal names are the ones in [What it writes](#what-it-writes). Mapping two
fields to the same key, or a field to the `date_property`, is rejected at
startup (exit 3) instead of silently losing a value.

#### Command line

```bash
./bin/ihealth-run --publisher obsidian                         # default window
./bin/ihealth-run --publisher obsidian --date 2026-10-01       # one day
./bin/ihealth-run --publisher obsidian --from 2026-01-01 --to 2026-09-30 --fill-empty-only
./bin/ihealth-run --publisher obsidian --from 2026-09-01 --sources weather,github
```

- `--from` without `--to` runs through yesterday. `--date` cannot be combined
  with `--from` / `--to`; future dates are rejected.
- `--fill-empty-only` keeps every key that already has a value (for
  backfills); the run report lists those keys as `kept` with both values.
  Exception: a provisional value the tool wrote itself and that is still
  unchanged (see [When values appear](#when-values-appear)) is replaced by the
  final value; a value you edited is kept.
- `--dry-run` writes no note and creates no file in the vault (no backups
  either), but still reads the sources and saves the run report. Reminders
  it would create are only logged, and nothing is recorded as notified or
  tracked (`data/diary-state.json` is not changed), so a dry run never
  silences or alters the next real run.
- `--from` / `--to` / `--sources` / `--fill-empty-only` are Obsidian-only
  (exit 2 with other publishers).
- With `IHEALTH_PUBLISHER=obsidian` in `.env`, `--publisher obsidian` can be
  omitted.

### Schedule (launchd)

`./bin/install-launchd.sh` installs a LaunchAgent that runs `bin/ihealth-run`
**without arguments**. The installer picks the times from `IHEALTH_PUBLISHER`
in `.env`: **07:00, 12:00 and 21:00** for `obsidian` (each run processes the
default window, so today's provisional values stay current and late
re-exports are picked up the same day), and **07:00 only** for every other
publisher (as before — Slack would otherwise post three times a day). Re-run
the installer after changing `IHEALTH_PUBLISHER`;
`./bin/install-launchd.sh --print-plist` shows the plist it would install
without changing anything.
Runs missed while the Mac was asleep are run once after wake; the tool then
waits up to `IHEALTH_NETWORK_GRACE_SEC` seconds for Wi-Fi.

- Application log: `logs/run.log` (daily rotation, 30 days).
- launchd stdout/stderr: `~/Library/Logs/ihealth-relay/launchd.log` — start-up
  failures, warnings and errors only (this file is not rotated; `--verbose`
  writes everything).
- Run now: `launchctl kickstart -k gui/$(id -u)/com.ihealthrelay.daemon`.

### When values appear

| Source | Today | Yesterday | 2–7 days ago |
|---|---|---|---|
| Health | **Provisional**: values so far, from the latest automation file; replaced on every run | **Final** once Health Auto Export has re-written yesterday's file after midnight. Until then nothing is written, so the note keeps the last provisional values — the tool remembers them and keeps re-processing that day (see below) | Same rule; a late re-export is picked up by any run within the window |
| Weather | Never (JMA publishes a day only after it ends) | Once JMA's page is complete. The weather column (天気) is filled in later than the numbers, so this is usually a later run of the day | Same; a day that is still incomplete is not written ([failures](#failures-and-recovery)) |
| GitHub | Activity so far (provisional, tracked like health) | Written whenever the whole collection succeeded; `該当なし` if there was no activity | Same |

- A health day counts as final only when its file was written after the day
  ended (midnight in the Mac's time zone). Health Auto Export re-writes the
  files of recent days whenever the automation runs, but iOS runs it only
  when the app gets background time — if values lag, open Health Auto Export
  on the iPhone once.
- Each health record is dated by its own UTC offset (days recorded abroad stay
  on their local date). Weather days are the station's days (JST). GitHub days
  and "today" use `IHEALTH_TZ_OFFSET`.
- Today's and yesterday's notes are written only if they already exist (the
  tool does not create them, see `OBSIDIAN_CREATE_MIN_AGE_DAYS`). A note for an
  older day is created only when `OBSIDIAN_TEMPLATE` (or
  `OBSIDIAN_CREATE_MISSING`) allows it, in the same folder as the nearest
  existing diary note within ±7 days (otherwise the diary root).
- **Provisional values are tracked until they are final.** Every key written
  as provisional (today's health, アクティビティ詳細 and Github) is recorded
  with its value in `data/diary-state.json`. Scheduled runs (no arguments)
  re-process those days even after they leave the 7-day window — for up to
  30 days — and drop a key from the list once its final value has been
  written (or you changed the value yourself). Outside the window such a
  day is only completed: empty keys are filled and the tool's own
  provisional values are replaced; values you wrote are kept. If a day is
  still provisional 3 days later, you get one reminder per day
  ("日記の暫定値が確定していません").
- Apart from that, a day that leaves the 7-day window is no longer updated
  automatically; use `--date` / `--from` for it.

#### Notes on health values

- **睡眠時間 (分)** follows Health Auto Export's sleep aggregation: with sleep
  aggregation on, it is the daily `totalSleep`, which **includes naps** that
  ended before 18:00 (sleep ending after 18:00 counts toward the next day),
  and **昼寝時間** is filled only when the same day's AutoSync `.hae` can
  separate naps. With sleep aggregation off, sleep ending 0:00–14:00 goes to
  睡眠時間 and naps ending 14:00–20:00 to 昼寝時間 (the AutoSync rule).
- **平均心拍数 (bpm)** is the mean of per-minute averages (each minute with
  data weighs the same).
- Days that have no automation file (e.g. backfills of days before you
  created the automation) are computed from AutoSync `.hae` alone, with the
  AutoSync rules: main night sleep and naps separated, average heart rate
  over the individual heart rate records.
- **アクティビティ詳細** lists mindfulness (`瞑想N分`) and each workout
  (`ウォーキング31分`), or `なし` if neither — but only when the automation
  JSON contains workouts (the automation exports them). Without workouts in
  the JSON, and on days computed from AutoSync `.hae` alone, the key is not
  written (writing `なし` or only `瞑想N分` could erase a workout that is
  already in the note).
- **運動強度スコア** uses `BIRTH_DATE` for the maximum heart rate.

### Failures and recovery

**How failures show up**

- **One Reminders.app reminder per run**, grouping failures by source and
  date, e.g. title `ihealth-relay: 日記の一部取得失敗 (github)` with lines like
  `[github] 2026-10-05〜2026-10-12 (期間全体)` and the reason. An identical
  set of failures is reminded at most once per day (`logs/.failure_notified`),
  so three runs a day don't pile up duplicates. The same applies to
  configuration errors (exit 3) and unexpected errors (exit 1). A reminder
  that could not be created (e.g. `osascript` failed or timed out) is not
  counted as sent; the next run tries again.
- **Run report** `logs/diary-report-<UTC timestamp>.json` after every run
  (including `--dry-run`, kept 30 days): for each date the note's status and
  path, the backup path, each source's status (`complete` / `partial` /
  `failed` / `nodata` / `skipped`) and, per key, the action (`insert` /
  `update` / `unchanged` / `kept` / `missing` / `failed` / `skipped`) with a
  reason such as `not_final`, `not_published`, `incomplete_day`, `no_value`,
  `source_failed`, `source_incomplete`, `too_recent`, `create_disabled`.
- **Data freshness** (once per day, `logs/.stale_notified`; the exit code is
  unaffected; added to the run's reminder if there is one):
  `HAE オートメーション停止の疑い` when the newest automation file is older
  than today − 3 days; `HAE オートメーション出力が見つかりません` when no
  automation file exists at all and no day of the run got any health data;
  `日記の暫定値が確定していません` when provisional values are still not final
  after 3 days (and, once, when a day is dropped after 30 days).
- **Exit code**: `0` nothing failed (data that is simply not there yet is not
  a failure), `12` some source failed or a note needs your attention, `11` a
  note could not be written or every source failed for every date, `3`
  configuration error (vault or diary directory missing, broken
  `config.toml`, key collision). A failed source never blocks the others, and
  its keys are left untouched.

**What to do**

| Symptom | Meaning | Fix |
|---|---|---|
| Reminder "HAE オートメーション停止の疑い" | The iPhone automation has not exported for more than 3 days | On the iPhone, open Health Auto Export, check the automation is enabled and iCloud Drive is syncing, and run it once. The next run fills the last 7 days; for a longer gap run `./bin/ihealth-run --publisher obsidian --from <first missing day> --sources health`. |
| Yesterday's health values missing or still provisional (`not_final` in the report) | Yesterday's file was last written before midnight | Usually nothing — scheduled runs re-process the day until Health Auto Export re-exports it (open the app to speed it up). If it is not final within 3 days you get the reminder below. |
| Reminder "日記の暫定値が確定していません" | Health Auto Export has not re-written that day's file after the day ended (automation stopped, or its export range does not include past days) | On the iPhone, open Health Auto Export, run the automation and check that its export range covers the last few days. The day is re-processed automatically for 30 days; after that run `./bin/ihealth-run --publisher obsidian --date <day>`. |
| Reminder "HAE オートメーション出力が見つかりません" | No `HealthAutoExport-YYYY-MM-DD.json` under `HEALTH_EXPORT_DIR` (or its parent when it points at `AutoSync`) | Point `HEALTH_EXPORT_DIR` at the Health Auto Export `Documents` folder and check the automation (iCloud Drive / JSON) on the iPhone. |
| Note not updated (`note.status: missing`) | No `YYYY-MM-DD.md`, and the tool may not create it (`too_recent`, `create_disabled`) | Create the note in Obsidian (or set `OBSIDIAN_TEMPLATE`), then `./bin/ihealth-run --publisher obsidian --date <day>`. |
| Reminder "日記ノートの確認が必要" | The note has no frontmatter and still contains Templater `<% … %>`, or the template cannot be used | Open the note in Obsidian and apply the template (or fix `OBSIDIAN_TEMPLATE`), then re-run that date. |
| GitHub failure (`[github]`, token / partial) | `Github` was not written for the window (incomplete lists are never written) | `gh auth login` or fix `GITHUB_TOKEN`; check `GITHUB_OWNERS` / `GITHUB_EXCLUDE_REPOS` format; re-run `--sources github --from <d1> --to <d2>`. |
| Weather failure (`[weather]`) | JMA unreachable / page layout changed, or two or more past days in a row are still incomplete (including pages with no hourly rows — a closed station or wrong `JMA_PREC_NO` / `JMA_BLOCK_NO`) | Re-run later with `--sources weather --date <day>`; check the page in a browser. |
| Exit 11, "日記の書き込み失敗" | The note could not be written (permissions, frontmatter Obsidian cannot read either such as a missing closing `---`, or it kept changing during the write) | Fix the note, then re-run that date. |
| A note changed in a way you don't want | — | Restore it from `data/obsidian-backup/<UTC date>/<note>.<UTC time>.md` (saved before every overwrite, kept 30 days), or from Obsidian's File recovery / Obsidian Sync version history. |

The tool re-checks the note immediately before the atomic replace and
starts over if Obsidian or another program changed it meanwhile (up to 3
attempts in total, then it gives up without writing).
A change made in the microseconds between that check and the replace cannot
be detected (Obsidian offers no lock to share). The backup holds the content
the tool edited, not such a last-moment change — recover that from
Obsidian's File recovery core plugin or Obsidian Sync's version history.

### GitHub scope

For each day (`IHEALTH_TZ_OFFSET`), the `Github` key lists:

- **issues closed** that day, **pull requests merged** that day, and
  **commits on the default branch** committed that day (committer date,
  added/deleted lines summed per repository);
- in every repository **owned by you or by an organization you belong to**
  — private ones when the token can read them (a classic PAT needs `repo`
  and `read:org`; `gh auth login` grants these), archived ones included (for
  backfills), forks included;
- by **any author** (it is a log of what happened in your repositories),
  except bots (`…[bot]`), merge commits and commits that reached the default
  branch only through a pull request's merge commit (the PR is listed
  instead). Squash- and rebase-merged commits count as direct commits.
- **Forks**: only commits authored by you (GitHub login) are counted, because
  syncing a fork brings in upstream commits with their original dates. The
  fork's own issues / PRs count for every author.
- `GITHUB_OWNERS` narrows the owners (e.g. `your-name,your-org`);
  `GITHUB_EXCLUDE_REPOS` skips repositories (`owner/name`).

Activity is collected once per run for the whole window. If any repository
cannot be read completely, `Github` is not written for any day of that run
(an incomplete list would look like "no activity").

### Changing the JMA station

The default station is Tokyo (`JMA_PREC_NO=44`, `JMA_BLOCK_NO=47662`).

1. Open JMA's [過去の気象データ検索](https://www.data.jma.go.jp/stats/etrn/index.php),
   choose the prefecture and the station, a date, and the hourly table
   ("1時間ごとの値").
2. The URL looks like
   `https://www.data.jma.go.jp/stats/etrn/view/hourly_s1.php?prec_no=NN&block_no=NNNNN&year=...`.
   Copy the two numbers into `.env`:
   ```bash
   JMA_PREC_NO=NN
   JMA_BLOCK_NO=NNNNN
   ```
3. Check with `./bin/ihealth-run --publisher obsidian --date <a past day> --sources weather --dry-run`.

Only stations whose hourly page is `hourly_s1.php` (meteorological
observatories) are supported; AMeDAS-only points (`hourly_a1.php`) use a
different page without the weather column. The station must report the
weather (天気) column: a day counts as complete only when the 24時 row has
temperature and sea-level pressure and at least one weather observation
exists. The tool waits at least 1 second between requests to JMA.

## Privacy & Health Data Handling

> **This tool processes Apple Health data** (steps, distance, heart rate,
> SpO₂, sleep stages, weight, body fat, mindfulness sessions). Data is
> read locally from iCloud Drive and forwarded to whichever sink you
> configure. Be aware:
>
> - **Notion / Slack publishers transmit health data over HTTPS to those
>   third-party services.** Slack messages are retained per Slack's data
>   policy (90 days on the Free tier). Notion stores them in your private
>   database. Webhook URL or Notion token leakage exposes your daily
>   health summary.
> - **Markdown / SQLite / stdout / Obsidian publishers keep data local**, but
>   the destination is your responsibility to secure (filesystem permissions,
>   full-disk backup encryption, etc.). Obsidian notes travel wherever your
>   vault syncs (Obsidian Sync, iCloud, git, ...).
> - **Obsidian backups**: before overwriting a note the tool copies it to
>   `data/obsidian-backup/` (mode 0600, gitignored, kept 30 days) — these are
>   copies of your diary.
> - **Obsidian diary requests**: the JMA request contains only the station and
>   date; the GitHub GraphQL API is called with your token, which is never
>   written to logs, reminders or run reports.
> - **`BIRTH_DATE` is required** to compute max heart rate (Tanaka
>   formula). Treat `.env` as a secret file. A future release may switch
>   to direct `MAX_HEART_RATE` configuration to avoid storing date of
>   birth (PRs welcome).
> - **GDPR / HIPAA / 個人情報保護法**: This software is **not** a
>   regulated medical device. Use in regulated environments is the
>   operator's responsibility. The MIT license disclaimer applies.
> - The `dryrun` publisher prints the would-be Notion payload to logs
>   without HTTPS — useful for verifying behavior without external
>   transmission.

## Sinks

Pick one with `--publisher`. Without it, `IHEALTH_PUBLISHER` from `.env` is
used, and `notion` if that is unset too. `--dry-run` overrides the
single-day sinks to `dryrun`; `obsidian` has its own dry-run (diff only).

| Sink | CLI | Output | Required config |
|---|---|---|---|
| `notion` (default) | `python3 -m ihealth` | Daily journal page in Notion | `NOTION_SECRET`, `DATABASE_ID` in `.env` |
| `obsidian` | `--publisher obsidian` | Frontmatter of your existing diary notes `<vault>/<diary dir>/**/YYYY-MM-DD.md` — health + weather + GitHub, window of days | `OBSIDIAN_VAULT_DIR` in `.env` |
| `markdown` | `--publisher markdown` | One generated `YYYY-MM-DD.md` per day in `output_dir` (rewritten as a whole) | `MARKDOWN_OUTPUT_DIR` in `.env` or `--markdown-out PATH` |
| `stdout` | `--publisher stdout` | Single JSON line on stdout | (none) |
| `sqlite` | `--publisher sqlite` | UPSERT into `daily_health` table | `SQLITE_DB_PATH` in `.env` or `--sqlite-path PATH` |
| `slack` | `--publisher slack` | Daily summary via Incoming Webhook | `SLACK_WEBHOOK_URL` in `.env` or `--slack-webhook URL` |
| `dryrun` | `--dry-run` or `--publisher dryrun` | Logs the Notion payload (no HTTP) | (none) |

`notion` / `markdown` / `stdout` / `sqlite` / `slack` / `dryrun` read only
AutoSync `.hae` files for one day (yesterday by default), so for them
`HEALTH_EXPORT_DIR` must be the `AutoSync` directory.

Examples:

```bash
# Pipe to jq for ad-hoc queries
PYTHONPATH=src /usr/bin/python3 -m ihealth --publisher stdout --date 2026-04-22 \
  | jq '.step_count'

# Build a local SQLite warehouse
PYTHONPATH=src /usr/bin/python3 -m ihealth --publisher sqlite \
  --sqlite-path ~/health.db --date 2026-04-22

# Daily Slack summary
PYTHONPATH=src /usr/bin/python3 -m ihealth --publisher slack \
  --slack-webhook https://hooks.slack.com/services/T0/B0/secret
```

## Collected Data

Health fields, their default names, and the Health Auto Export metric they
come from. Customize names via `config.toml`.

| Field | Health Auto Export metric | Default Notion property | Default Obsidian key | Unit |
|---|---|---|---|---|
| Date (filter key) | — | `Date` | (file name) | date |
| Step count | Step Count | `Step Count` | `歩数 (歩)` | count |
| Distance | Walking + Running Distance | `Distance (km)` | `移動距離 (km)` | km |
| Active energy | Active Energy | `Active Energy (kcal)` | `消費カロリー (kcal)` | kcal |
| Exercise minutes | Apple Exercise Time | `Exercise Minutes` | `アクティビティ時間 (分)` | min |
| Exercise intensity (Heart Points equivalent) | Heart Rate | `Exercise Intensity Score` | `運動強度スコア` | pt |
| Heart rate (avg) | Heart Rate | `Heart Rate Avg (bpm)` | `平均心拍数 (bpm)` | bpm |
| Heart rate (max) | Heart Rate | `Heart Rate Max (bpm)` | `最大心拍数 (bpm)` | bpm |
| Heart rate (resting) | Resting Heart Rate | `Heart Rate Resting (bpm)` | `安静時心拍数 (bpm)` | bpm |
| Oxygen saturation | Blood Oxygen Saturation | `Oxygen Saturation (%)` | `酸素飽和度 (%)` | % |
| Sleep | Sleep Analysis | `Sleep Minutes` | `睡眠時間 (分)` | min |
| Nap | Sleep Analysis | `Nap Minutes` | `昼寝時間` | min |
| Mindful sessions | Mindful Minutes | `Mindful Sessions` | `瞑想回数 (回)` | count |
| Mindful minutes | Mindful Minutes | `Mindful Minutes` | `瞑想時間 (分)` | min |
| Body mass | Weight & Body Mass | `Body Mass (kg)` | `体重 (kg)` | kg |
| Body fat | Body Fat Percentage | `Body Fat (%)` | `体脂肪率 (%)` | % |

The Obsidian publisher additionally writes `アクティビティ詳細`, the six
weather keys and `Github` (see [What it writes](#what-it-writes)).
For single-day sinks, Sleep is the main night sleep (ending 0:00–14:00) and
Nap the sleep ending 14:00–20:00, from AutoSync `.hae`.

> **Exercise Intensity Score** is a *Heart-Points-style* approximation
> (the term "Heart Points" is a trademark of Google LLC; this implementation
> is independent and not affiliated with or endorsed by Google). Each
> heart rate record is bucketed into a 1-minute window, and each bucket's
> mean `avg` selects a zone: `≥ 70% max_hr → 2 pt`, `≥ 50% → 1 pt`,
> otherwise `0 pt`. Bucket points are summed. Max heart rate uses the
> Tanaka formula (`205.8 − 0.685 × age`), with age derived from `.env`'s
> `BIRTH_DATE`. A day with heart rate data but no minute in a zone is
> recorded as `0` (no data at all stays empty). Health Auto Export emits
> sparse samples (often hundreds of seconds apart), so strict "1 continuous
> minute" matching as Google Fit specifies is not achievable from this data
> shape — the bucket-mean approximation slightly overcounts compared to the
> official Google Fit value (tracked in issue #13).

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — module-level overview
- [`docs/setup.md`](docs/setup.md) — detailed setup including the Obsidian
  vault and Notion DB
- [`docs/case-study/dataless-icloud.md`](docs/case-study/dataless-icloud.md) —
  debugging silent EDEADLK from iCloud Drive dataless placeholders
- [`docs/ja/README.ja.md`](docs/ja/README.ja.md) — Japanese README
- [`CHANGELOG.md`](CHANGELOG.md)

## Development

```bash
# Run all tests (unittest, no external deps, no network)
PYTHONPATH=src /usr/bin/python3 -m unittest discover tests

# Manual run wrapper (same env as launchd)
./bin/ihealth-run --date 2026-04-22
```

CI: see [`.github/workflows/ci.yml`](.github/workflows/ci.yml). Tests run
on macOS + Linux against Python 3.9 / 3.10 / 3.11 / 3.12 (3.9 and 3.10 on
Linux only, as the macOS arm64 runners no longer provide them).

## Exit Codes

| Code | Class | Cause |
|---|---|---|
| 0 | — | Success |
| 1 | (catch-all) | Unexpected exception |
| 2 | argparse / `--*` validation | CLI argument format error |
| 3 | `ConfigAppError` | `.env` / `config.toml` misconfigured (incl. Obsidian vault / diary directory missing) |
| 4 | `HealthExportDirError` | `HEALTH_EXPORT_DIR` missing or not a directory |
| 5 | `SourceAppError` | `.hae` fetch / decompression failed |
| 6 | `NotionAppError` | Notion API failure / page not found / column-name mismatch |
| 7 | `MarkdownAppError` | Markdown write failed (disk full, permission, …) |
| 8 | `StdoutAppError` | stdout write failed (closed, encoding, …) |
| 9 | `SQLiteAppError` | SQLite I/O / SQL / lock contention |
| 10 | `SlackAppError` | Slack webhook HTTP failure |
| 11 | `ObsidianAppError` | Obsidian: a diary note could not be written, or every source failed for every date |
| 12 | `DiaryPartialFailureError` | Obsidian: some source failed for some dates, a note needs your attention, or `data/diary-state.json` could not be saved |

Exit codes 1 and 3–12 create a Reminders.app reminder so the failure stays
visible until acknowledged (argument errors, exit 2, are only printed). For
`obsidian`, one reminder summarizes the run, and an identical reminder
(including `.env` / `config.toml` errors, exit 3, and unexpected errors,
exit 1) is created at most once per day; `obsidian --dry-run` only logs it.

## License

[MIT](LICENSE) — Atsuro Murata, 2026.
