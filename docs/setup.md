# Setup Guide

End-to-end setup for a fresh macOS machine. Follow the sections in order;
sections marked *Obsidian* or *Notion* only apply to that publisher.

## 1. Prerequisites

- macOS 14+ (Sonoma) on Apple Silicon or Intel
- Python 3.9+ (`/usr/bin/python3` is bundled with macOS as 3.9.x — verify
  with `/usr/bin/python3 --version`)
- An iPhone signed in to the same iCloud account as the Mac
- *Obsidian*: an Obsidian vault with a diary folder of `YYYY-MM-DD.md` notes,
  and for the GitHub part either a GitHub token or the
  [GitHub CLI](https://cli.github.com/) (`brew install gh`)

## 2. iPhone-side: Health Auto Export

1. Install **Health Auto Export — JSON+CSV** from the App Store
   (App ID 1115567069).
2. Buy the **Premium one-time license** (no subscription).
3. Grant HealthKit permissions for these 11 metrics:
   - Step Count
   - Walking + Running Distance
   - Active Energy
   - Apple Exercise Time
   - Heart Rate
   - Resting Heart Rate
   - Blood Oxygen Saturation
   - Sleep Analysis
   - Mindful Minutes
   - Weight & Body Mass
   - Body Fat Percentage
4. Set up the export the publisher you use reads:
   - **Obsidian** (`--publisher obsidian`) — create an **automation** that
     exports **Health Metrics** (at least the 11 metrics above) to
     **iCloud Drive** as **JSON**, export version 2, with data aggregation on
     grouped by **minutes** (the exercise intensity score counts minutes),
     sleep aggregation on, and **one file per day**.
     Leave the export period at its default (recent days are re-written on
     every run, which is how yesterday's values become final).
     `アクティビティ詳細` is built from mindfulness and Apple Exercise Time
     (`瞑想10分、エクササイズ21分`); turn on **Include Workouts** in the same
     automation to list the day's workouts by name instead
     (`瞑想10分、ウォーキング31分`; days without a workout keep
     `エクササイズN分`). Labels differ
     slightly between app versions. Files appear under
     `iCloud Drive › Health Auto Export › <automation name> ›
     HealthAutoExport-YYYY-MM-DD.json`; the folder name is up to you.
     AutoSync is optional here: if it is (or was) enabled, its `.hae` files
     supplement missing values and cover days before the automation existed.
   - **Notion / Markdown / stdout / SQLite / Slack** — enable **AutoSync**
     (Premium feature) to **iCloud Drive**. Tap *Sync now* once to confirm
     files appear under `iCloud Drive › Health Auto Export › AutoSync ›
     HealthMetrics › ...`. Syncing every hour is plenty.
5. iOS runs exports only when the app gets background time. If data lags
   (e.g. after a night with the phone locked), open Health Auto Export once.

## 3. macOS-side: clone the repository

```bash
git clone https://github.com/arkatom/ihealth-relay.git ~/src/ihealth-relay
cd ~/src/ihealth-relay
```

## 4. Configure `.env`

```bash
cp .env.example .env
chmod 600 .env
$EDITOR .env
```

`.env.example` documents every key with comments. Comments must be on their
own line: `KEY=value # note` makes `# note` part of the value.

| Key | Required when | Example |
|---|---|---|
| `HEALTH_EXPORT_DIR` | Always | see below |
| `BIRTH_DATE` | Always | `1990-01-01` |
| `IHEALTH_PUBLISHER` | Optional (default publisher, `notion` if unset) | `obsidian` |
| `OBSIDIAN_VAULT_DIR` | Obsidian publisher | `~/obsidian/<vault>` |
| `OBSIDIAN_DIARY_DIR` … `GITHUB_EXCLUDE_REPOS` | Optional (Obsidian) | see [README ▸ `.env` keys for Obsidian](../README.md#env-keys-for-obsidian) |
| `NOTION_SECRET` | Notion publisher | `secret_aaaa…` |
| `DATABASE_ID` | Notion publisher | 32-char DB UUID from the page URL |
| `MARKDOWN_OUTPUT_DIR` | Markdown publisher | `~/notes/health` |
| `SQLITE_DB_PATH` | SQLite publisher | `~/health.db` |
| `SLACK_WEBHOOK_URL` | Slack publisher | `https://hooks.slack.com/services/T0/B0/secret` |
| `LOG_LEVEL` | Optional | `info` (default) / `debug` / `warning` / `error` |

`HEALTH_EXPORT_DIR` is the Health Auto Export folder in iCloud Drive:

- `~/Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents`
  — recommended for Obsidian (automation JSON is searched below it, and
  `Documents/AutoSync` is used as a supplement when present);
- `.../Documents/AutoSync` — required by the Notion / Markdown / stdout /
  SQLite / Slack publishers, which read AutoSync `.hae` only. Obsidian works
  with this value too (it searches the parent folder for automation files,
  also after AutoSync's `HealthMetrics` folder has been removed).

Paths accept a leading `~/`; otherwise they must be absolute. The `~` inside
`iCloud~com~ifunography~HealthExport` is part of the folder name.

## 5. Obsidian vault setup (only if using `--publisher obsidian`)

1. **Vault and diary folder**: set `OBSIDIAN_VAULT_DIR` and, if your diary
   folder is not `日記`, `OBSIDIAN_DIARY_DIR` (vault-relative, e.g.
   `Journal/Daily`). Notes are matched by exact file name `YYYY-MM-DD.md`
   anywhere below that folder (subfolders such as `2025/` work; hidden
   folders and `.trash` are skipped).
2. **Frontmatter keys**: by default the tool writes Japanese keys
   (`歩数 (歩)`, `気温`, `Github`, ... — full list in
   [README ▸ What it writes](../README.md#what-it-writes)). Keys that are not
   in a note yet are appended before the closing `---`. To use other names,
   `cp config.example.toml config.toml` and add
   `[publishers.obsidian.fields.<internal name>] property = "..."` entries.
3. **Missing notes** (optional): set `OBSIDIAN_TEMPLATE` to your daily-note
   template to let the tool create notes for days that have none (only days
   at least `OBSIDIAN_CREATE_MIN_AGE_DAYS` = 2 days old; today's and
   yesterday's notes are left to you so Obsidian Sync never sees two
   different "new" notes for the same day). Only `<%* … %>` at the very top
   (removed), `<% dateStr %>` and `<% weekday %>` are expanded; a template
   with any other Templater syntax is not used.
4. **GitHub**: `gh auth login` once (the tool runs `gh auth token`, looking
   for `gh` in `/opt/homebrew/bin`, `/usr/local/bin`, then `PATH`), or set
   `GITHUB_TOKEN`. Optionally restrict with `GITHUB_OWNERS` /
   `GITHUB_EXCLUDE_REPOS`. Scope: [README ▸ GitHub scope](../README.md#github-scope).
5. **Weather station** (optional): `JMA_PREC_NO` / `JMA_BLOCK_NO`, default
   Tokyo. See [README ▸ Changing the JMA station](../README.md#changing-the-jma-station).
6. **Make it the default**: `IHEALTH_PUBLISHER=obsidian`, so launchd runs
   (which pass no arguments) update the diary.

## 6. Notion DB setup (only if using `--publisher notion`)

1. Create a Notion database with **at least these columns**, using one of:

   - **English defaults** (recommended for new users): `Date` (date type),
     `Step Count` (number), `Distance (km)` (number), …
     — see [README ▸ Collected Data](../README.md#collected-data) for the
     full list.
   - **Legacy Japanese** (legacy column-name compat): `日付` (date), `歩数 (歩)`
     (number), `移動距離 (km)` (number), … — and run
     `cp config.example.toml config.toml` to map internal field names to
     these column names.

2. Create a Notion **internal integration** at
   <https://www.notion.so/my-integrations>, copy the secret into
   `NOTION_SECRET`, and **share the database with that integration** via
   the database's "Connections" menu.

3. Copy the DB UUID from the database URL into `DATABASE_ID` (the 32
   hex chars, no dashes).

## 7. First run (dry-run)

*Obsidian*:

```bash
./bin/ihealth-run --publisher obsidian --dry-run --verbose
```

This reads `.env` and `config.toml`, collects health / weather / GitHub for
the default window (yesterday back `IHEALTH_LOOKBACK_DAYS` days, plus
today), and **logs each planned change as
`[dry-run] <note path>: <key>: <old> -> <new>` without touching the vault**.
It also writes a run report to `logs/diary-report-<UTC>.json` (created at the
start; while the run is in progress, each date's result is appended to the
`.jsonl` journal next to it). Reminders are only logged in a dry run (none is
created, nothing is marked as notified) and `data/diary-state.json` is not
changed.
Check a few notes, then run without `--dry-run`. To fill older days once:

```bash
./bin/ihealth-run --publisher obsidian --from 2026-01-01 --to 2026-09-30 --fill-empty-only
```

`--fill-empty-only` keeps every value that is already filled in, except
provisional values the tool wrote itself that are still unchanged (they are
replaced by the final values). Weather is fetched at most once per second
per day, so long ranges take a while. Values that cannot be settled yet on
dates older than 30 days are not tracked for re-processing; they are listed
once in a reminder and in the run report (`provisional.untracked`).

*Single-day sinks*:

```bash
PYTHONPATH=src /usr/bin/python3 -m ihealth --dry-run --verbose --date 2026-04-22
```

This:

- Reads `.env` and `config.toml` (if present).
- Looks up `2026-04-22` worth of `.hae` files under `HEALTH_EXPORT_DIR`.
- `brctl download`s any dataless files (see
  [case study](case-study/dataless-icloud.md)).
- Decompresses with `/usr/bin/compression_tool` (plain LZFSE and the
  `HAE1` multi-segment container).
- Aggregates into `DailyHealthData`.
- **Logs the would-be Notion payload without making any HTTP request.**

If the dry-run succeeds you are ready for a real run.

## 8. Pick a sink

| Goal | Command |
|---|---|
| Update Obsidian daily notes | `./bin/ihealth-run --publisher obsidian` (or `IHEALTH_PUBLISHER=obsidian`) |
| Update Notion daily | `PYTHONPATH=src /usr/bin/python3 -m ihealth` (default `--publisher notion`) |
| Write Markdown files (one generated file per day, Obsidian-compatible YAML frontmatter) | `... --publisher markdown --markdown-out ~/notes/health` |
| Pipe into `jq` | `... --publisher stdout \| jq '.step_count'` |
| Append to local SQLite | `... --publisher sqlite --sqlite-path ~/health.db` |
| Post Slack summary | `... --publisher slack --slack-webhook https://hooks.slack.com/services/...` |

You can also configure paths via `.env` so plain `python3 -m ihealth
--publisher markdown` works without CLI overrides. Note the difference
between `markdown` (writes its own files in a dedicated folder, rewriting
them as a whole) and `obsidian` (edits only its keys in your existing diary
notes).

## 9. Schedule with launchd (optional)

```bash
./bin/install-launchd.sh
```

Installs `LaunchAgent/com.ihealthrelay.daemon.plist` to
`~/Library/LaunchAgents/` and registers it. It runs `bin/ihealth-run`
without arguments, so the publisher comes from `IHEALTH_PUBLISHER`
(default `notion`), and the installer picks the times from the same key:

- `obsidian`: **07:00, 12:00 and 21:00**. Each run writes yesterday back 7
  days plus today (provisional), so late data is filled in by a later run;
  days whose values have not settled yet (provisional values, pending or
  incomplete weather, an incomplete GitHub collection, …) are re-processed
  for up to 30 days.
- any other publisher: **07:00 only** (as before). Each run processes
  yesterday; Slack posts once a day.

Re-run the installer after changing `IHEALTH_PUBLISHER`.
`./bin/install-launchd.sh --print-plist` prints the plist it would install
without changing anything.

Runs missed while the Mac sleeps are run once after wake; the Obsidian
pipeline then waits up to `IHEALTH_NETWORK_GRACE_SEC` (60) seconds for the
network.

To verify, or to run immediately:

```bash
launchctl list | grep ihealthrelay
launchctl kickstart -k "gui/$(id -u)/com.ihealthrelay.daemon"
```

To unregister:

```bash
launchctl bootout "gui/$(id -u)/com.ihealthrelay.daemon" 2>/dev/null \
    || launchctl unload ~/Library/LaunchAgents/com.ihealthrelay.daemon.plist
```

## 10. Daily operations

- **Failures**: a Reminder appears in Reminders.app with title and body.
  Reminders persist until acknowledged (unlike Notification Center banners
  which auto-dismiss). The Obsidian pipeline sends at most one per run and
  repeats an identical reminder (including configuration errors) at most
  once per day; a reminder that could not be created is retried by the next
  run. Values that have not settled more than 3 days after they were first
  seen are added to that reminder as 要確認.
- **Logs**: rotating daily under `logs/run.log` in the project root,
  30-day retention. launchd's own stdout/stderr go to
  `~/Library/Logs/ihealth-relay/launchd.log` (start-up failures, warnings and
  errors only; look there if a run never started).
- **Unsettled values**: `data/diary-state.json` (the ledger) lists, per day
  and key, values that have not settled yet — provisional values the tool
  wrote (with the value), withheld health values, pending or incomplete
  weather, an incomplete GitHub collection, provisional values it cannot
  rewrite because of a comment or a duplicate key — with the reason and when
  they were first seen. Scheduled runs re-process those days for up to 30
  days (see
  [README ▸ Values that have not settled yet](../README.md#values-that-have-not-settled-yet)).
  Deleting the file only stops the re-processing and the 要確認 reminders
  for what it lists.
- **Obsidian run reports**: `logs/diary-report-<UTC>.json` for every run
  (30 days) — created at the start with the planned dates, per-date results
  appended to `logs/diary-report-<UTC>.jsonl` as each date finishes, and
  completed at the end (`status` `finished` / `interrupted`,
  `remaining_dates`; the journal is then deleted). Per date and key it shows
  what was inserted, updated, kept or skipped and why, and which keys are
  still unsettled. Useful as a ledger after a backfill; if a run stopped
  half-way, the `.jsonl` journal still holds the dates it finished.
- **Obsidian backups**: every overwritten note is copied first to
  `data/obsidian-backup/<UTC date>/<note>.<UTC time>.md` (0600, 30 days).
- **Re-run a specific date**: `python3 -m ihealth --date 2026-04-22`
  (add `--publisher obsidian` for the diary). Re-running is idempotent
  (Notion overwrites, SQLite UPSERTs, Markdown rewrites, Slack reposts,
  Obsidian notes are rewritten only when a value changes).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `HealthExportDirError` (exit 4) | `HEALTH_EXPORT_DIR` typo or iCloud not synced | verify path under `~/Library/Mobile Documents/...` |
| `SourceAppError` (exit 5) — "data not arrived" | iPhone hasn't synced yet | open Health Auto Export on the iPhone and tap *Sync now* |
| `SourceAppError` (exit 5) — `HealthMetrics` not found | `HEALTH_EXPORT_DIR` points at `Documents`, but single-day sinks need the `AutoSync` folder | append `/AutoSync` to `HEALTH_EXPORT_DIR` |
| `SourceAppError` — "EDEADLK" / "Resource deadlock avoided" | iCloud Drive evicted the file (dataless) | usually auto-recovered by the in-tree `brctl download` step; see [dataless case study](case-study/dataless-icloud.md) |
| `NotionAppError` — "page not found" (exit 6) | no Notion page exists for that date | create the daily page in Notion first |
| `NotionAppError` — "column name mismatch" (exit 6) | DB columns differ from `PROPERTY_MAP` | run `cp config.example.toml config.toml` and edit |
| `MarkdownAppError` (exit 7) | `--markdown-out` is a file (not a dir) or not writable | provide an existing/creatable directory path |
| `SQLiteAppError` (exit 9) — "database is locked" | another process has an exclusive lock | wait or close the other tool; the publisher uses SQLite's connection-level `timeout=10.0` so brief contention auto-resolves, longer contention fails fast |
| `SlackAppError` (exit 10) — "host not in allow-list" | webhook URL host is not `hooks.slack.com` / `hooks.slack-gov.com` | regenerate webhook from Slack |
| `ConfigAppError` (exit 3) — "Obsidian vault 未設定" / "Obsidian 設定不備" | `OBSIDIAN_VAULT_DIR` unset, vault or diary folder missing, or two fields mapped to one key in `config.toml` | fix `.env` / `config.toml` |
| Exit 12 — "日記の一部取得失敗" | a source failed (GitHub token, JMA unreachable, unreadable health file); its keys were left untouched | see [README ▸ Failures and recovery](../README.md#failures-and-recovery) |
| Exit 12 — "日記ノートの確認が必要" | a note without frontmatter still contains Templater `<% … %>`, or `OBSIDIAN_TEMPLATE` cannot be used | apply the template in Obsidian / fix the template path, then re-run that date |
| Exit 11 — "日記の書き込み失敗" | a note could not be written (permissions, broken frontmatter, edited continuously during the write), the read-back after writing kept showing other content (another program saved the note right after; the reminder lists the backups), or the pre-write check found a change outside the tool's keys (nothing was written; please report it as a bug) | check the note (restore from the backup if needed) and re-run that date |
| Exit 12 — "日記の実行レポートを保存できません" / "日記の未確定の値の台帳を保存できません" | `logs/` or `data/` cannot be written (disk full, permissions); if the report could not be created, no note was touched | free up space / fix permissions, then re-run |
| Reminder "HAE オートメーション停止の疑い" | newest automation file is more than 3 days old | check the automation on the iPhone, then run with `--from <first missing day> --sources health` |
| Reminder "HAE オートメーション出力が見つかりません" | no `HealthAutoExport-YYYY-MM-DD.json` under `HEALTH_EXPORT_DIR` (or its parent when it points at `AutoSync`), and no day got health data | point `HEALTH_EXPORT_DIR` at the Health Auto Export `Documents` folder; check the automation on the iPhone |
| Reminder "日記の暫定値が確定していません (要確認)" / "日記の値が確定していません (要確認)" | values have not settled more than 3 days after they were first seen; the reminder lists the keys, reasons and what to check | for provisional values, open Health Auto Export on the iPhone, run the automation and check its export range covers recent days; for the other reasons follow the hint in the reminder ([README ▸ Failures and recovery](../README.md#failures-and-recovery)). Days are re-processed automatically for 30 days |
| Obsidian: yesterday's health values still provisional (`provisional` in the run report) | yesterday's latest automation file was written before midnight | usually nothing: scheduled runs replace the values once Health Auto Export re-exports the day (open the app to speed it up); if not, the 要確認 reminder arrives after 3 days |
| Obsidian: a health key not written, `not_final` in the run report | the value would come from an AutoSync `.hae` written before the day ended, which a stopped AutoSync never re-exports | fill the value in by hand if you need it (that also ends its tracking) |
| Obsidian: a key reported as `kept` with `comment present …` / `duplicate key …` | the key's entry in the note has a comment, or the key appears twice; the tool does not rewrite it | move the comment to its own line above the key, or merge the duplicates |
| Obsidian: `アクティビティ詳細` not written | no mindfulness, workout or Apple Exercise Time that day and no heart-rate data either (Watch not worn; a 0-minute Apple Exercise Time alone does not count), or an input could not be read (then it is not written even with heart-rate data) | nothing to do; check that the automation exports Mindful Minutes, Apple Exercise Time and Heart Rate |
| Obsidian: `アクティビティ詳細` shows `エクササイズN分` instead of workout names | the automation does not export workouts (with workouts exported, a day without any workout of at least 1 minute also shows `エクササイズN分`) | turn on **Include Workouts** in the Health Auto Export automation |
| Obsidian: an `アクティビティ詳細` you typed was replaced (e.g. by `なし` or `エクササイズN分`) | within the default window (yesterday back 7 days plus today) the tool writes this key like its other keys; `なし` means heart-rate data but no mindfulness, workout or exercise record that day | to keep your value, add a comment to its line (`アクティビティ詳細: "ヨガ30分" # 手入力`; the entry is then `kept`); for workout names, turn on **Include Workouts** |
| Works by hand, `Operation not permitted` only under launchd | the vault or export folder sits in a privacy-protected location (e.g. `~/Documents`, `~/Desktop`) | grant Full Disk Access to `/usr/bin/python3` (System Settings › Privacy & Security), or keep the vault elsewhere |
| Reminder shows `想定外エラー` ("Unexpected error", exit 1) | unhandled exception | check `logs/run.log`, file an issue |

## Uninstalling

```bash
launchctl bootout "gui/$(id -u)/com.ihealthrelay.daemon" 2>/dev/null \
    || launchctl unload ~/Library/LaunchAgents/com.ihealthrelay.daemon.plist
rm ~/Library/LaunchAgents/com.ihealthrelay.daemon.plist
rm -rf ~/src/ihealth-relay   # or wherever you cloned it
```

`data/` (health archives, Obsidian backups, the ledger `diary-state.json`)
and `logs/` (logs, run reports) inside the clone hold personal data; deleting
the clone removes them too.
