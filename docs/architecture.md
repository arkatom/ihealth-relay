# Architecture

This document explains the module layout and runtime data flow of
ihealth-relay. Read this after the top-level [README](../README.md) to
understand *how* the code is organized; read the
[setup guide](setup.md) to understand *what* you have to configure.

There are two pipelines behind one CLI:

- **Obsidian diary pipeline** (`--publisher obsidian`, `ihealth.diary`):
  a window of days × three independent sources (health, JMA weather, GitHub)
  → frontmatter of existing diary notes.
- **Single-day pipeline** (every other publisher, `ihealth.workflow`):
  one day of AutoSync `.hae` → one sink.

## Module layout

```
src/ihealth/
├── __main__.py         # CLI entry point: argparse → load_config → diary or workflow
├── config.py           # .env reader (no external deps); PUBLISHER_KINDS, diary settings
├── config_file.py      # config.toml reader (uses _toml.parse); per-publisher overrides
├── _toml.py            # in-tree minimal TOML 1.0 subset parser
├── errors.py           # AppError hierarchy → exit code mapping
├── logger.py           # logging setup (stderr + rotating file)
├── models.py           # DailyHealthData (frozen dataclass, date + 15 health fields)
├── notifier.py         # AppleScript → Reminders.app (failure persistence)
├── parser.py           # AutoSync .hae metric aggregators + drop_invalid_fields
├── source.py           # AutoSync .hae discovery, brctl materialize, LZFSE / HAE1 decompress
├── workflow.py         # single-day run_pipeline + composition root (build_default_deps,
│                       #   build_obsidian_publisher)
├── diary.py            # Obsidian diary pipeline: dates, sources, ledger, run report, notifications
├── automation.py       # Health Auto Export automation JSON discovery + loading
├── automation_parser.py# automation JSON (ExportVersion.v2) → DailyHealthData + activity detail
├── health_loader.py    # per-day health: automation JSON first, AutoSync .hae supplement
├── weather.py          # JMA hourly_s1 page → 気温 / 湿度 / 気圧 / 天気 / 日照時間 / 降水量
├── github_activity.py  # GitHub GraphQL → issues closed / PRs merged / default-branch commits
├── records.py          # DiaryRecord (one day of values for the diary)
├── frontmatter.py      # byte-preserving YAML frontmatter upsert (pure functions; commented /
│                       #   duplicate target entries are kept, not rewritten)
└── publishers/
    ├── __init__.py
    ├── base.py         # Publisher Protocol (publish(health_data) -> None)
    ├── _payload.py     # FIELD_SPECS + coerce_int / coerce_float / build_payload
    ├── notion.py       # NotionClient + NotionPublisher + DryRun helpers
    ├── dryrun.py       # DryRunPublisher (logs the Notion payload)
    ├── markdown.py     # MarkdownPublisher (generated files, atomic write)
    ├── stdout.py       # StdoutPublisher (JSON line + BrokenPipe-safe)
    ├── sqlite.py       # SQLitePublisher (UPSERT with BEGIN IMMEDIATE)
    ├── slack.py        # SlackPublisher (Incoming Webhook + URL allow-list)
    └── obsidian.py     # ObsidianPublisher (diary note frontmatter, backups, conflicts,
                        #   independent pre-write check, post-write verification)
```

Tests mirror the source structure under `tests/`; fixtures for the new
sources are synthetic (`tests/fixtures/automation/`, `tests/fixtures/jma/`).
`tests/test_e2e_diary.py` runs the whole diary pipeline on synthetic
fixtures (`tests/fixtures/e2e/`) with only external I/O replaced (HTTP, `gh`,
iCloud materialization, clock, Reminders): a normal day, the JST midnight
boundary, a mid-day export and missing data.

## Runtime data flow

### 1. iPhone → iCloud Drive

Health Auto Export writes two kinds of files to its iCloud Drive folder
(`~/Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents`):

```
<automation name>/HealthAutoExport-YYYY-MM-DD.json       # automation: one JSON per day
AutoSync/HealthMetrics/{metric}/YYYYMMDD.hae             # AutoSync: one file per metric per day
```

An automation JSON holds every exported metric of one day (1-minute
aggregates, a daily sleep summary, optionally workouts). Each `.hae` is
**LZFSE-compressed UTF-8 JSON**, either a single LZFSE stream or an `HAE1`
container (`b"HAE1"` + repeated `[uint32 big-endian length][LZFSE stream]`
segments whose `data` arrays are concatenated). Both kinds are usually iCloud
*dataless* placeholders and are materialized with `brctl download` before
they are read.

### 2. `__main__`

1. Parses `argparse` flags (`--date`, `--dry-run`, `--publisher`, `--from`,
   `--to`, `--sources`, `--fill-empty-only`, `--markdown-out`,
   `--sqlite-path`, `--slack-webhook`).
2. Loads `.env` via `config.py:load_config` without requiring Notion keys,
   then resolves the publisher: `--publisher` → `IHEALTH_PUBLISHER` →
   `notion`. Only when the effective publisher is `notion` are
   `NOTION_SECRET` / `DATABASE_ID` required.
3. Configures the logger (stderr + `logs/run.log`). When stderr is not a
   terminal (launchd, whose `launchd.log` is never rotated), stderr only
   gets warnings and errors (`_quiet_stderr_when_not_tty`), unless
   `--verbose` is given or the run is a dry run (`--dry-run`, or the
   `dryrun` publisher): a dry run's log (the start line and the changes it
   would make) is its output, so it stays at INFO when redirected or piped.
4. `obsidian` → `diary.resolve_dates` + `diary.run_diary` (without date
   arguments, `revisit_unresolved` also adds the days the ledger still
   tracks). Anything else → `workflow.build_default_deps` +
   `workflow.run_pipeline`.
5. Maps any `AppError` subclass to its exit code + Reminders notification.
   Configuration errors (exit 3) and unexpected errors (exit 1) are
   reminded at most once per day with the same content
   (`diary.notify_once_per_day`); `obsidian --dry-run` only logs them.

### 3a. `diary.run_diary` (Obsidian)

```
load data/diary-state.json (ledger of unsettled keys) → add its days (≤ 30 days old) to the window
build_obsidian_publisher (vault / diary dir / config.toml checks → exit 3 on error)
  → start the run report: logs/diary-report-<UTC>.json (status "running", planned_dates,
    revisit_dates) + journal logs/diary-report-<UTC>.jsonl
    (cannot create → stop before touching any note: exit 12)
  → NetworkGate.wait (up to IHEALTH_NETWORK_GRACE_SEC after wake)
  → collect_github_activity once for the whole window → journal line
  → for each date:
       HealthLoader.load(d)   (automation JSON → AutoSync supplement / fallback)
       fetch_weather(d)       (JMA, ≥ 1 s between requests; today is skipped)
       GitHubActivity.text_for(d)
       → DiaryRecord → ObsidianPublisher.publish_record
         (days added from the ledger: fill-only + replaceable;
          --fill-empty-only: keys still holding the ledger's provisional value are replaceable)
       → unsettled keys of the day: reason, first seen, provisional value
         (settled / user-valued / final-without-value / deselected keys dropped)
       → journal line, fsync'd (cannot append → remaining dates not processed: exit 12)
  → weather persistence check, stale / missing automation check
  → save the ledger (re-read first; not in --dry-run): overdue (> 3 days since first seen),
    expired (> 30 days), untracked (unsettled keys on dates older than 30 days)
  → final run report (finished / interrupted + remaining_dates; journal deleted;
    cannot write → journal kept, exit 12) → one Reminders notification
```

Every source result carries a completeness status — `complete`, `partial`,
`failed` or `nodata` — and only values that are certain reach the
publisher; anything else is left out, so the note keeps its existing value:

- **Health**: an automation file is *final* for a day only if it was written
  after that day ended — midnight at the UTC offset of the day's own records
  (`automation_parser.record_day_end`; `IHEALTH_TZ_OFFSET` when they carry
  none), compared with the file's mtime as absolute time, so the Mac's time
  zone plays no part. An earlier file (today's, or a past day exported
  mid-day) is passed as provisional. Values from an AutoSync `.hae` written
  before the end of a day that has ended are withheld (`withheld_fields`),
  because a stopped AutoSync never re-exports them. Segment-format night
  sleep that lacks the previous day's file is provisional for `sleep_hours`
  only (`provisional_fields`). A broken best candidate falls back only to an
  older candidate that is final; `None` fields are supplemented field by
  field from AutoSync `.hae`. The activity detail follows
  `automation_parser.compose_activity_detail` on both paths: mindfulness,
  then the day's workouts (when the automation exports them and the day has
  any of at least 1 minute) or Apple Exercise Time (`エクササイズN分`); with
  none of these, `なし` when the day has heart-rate data (`has_heart_rate`,
  judged on the final values after supplementing and validation: the Watch
  was worn), otherwise not written — nor whenever an input it depends on
  could not be read (failed, dropped as implausible, or an unreadable /
  withheld supplement), even with heart-rate data.
- **Unsettled keys** are recorded per date and key in
  `data/diary-state.json` (format 2: reason, first-seen time, and the value
  for a provisional value; format 1 is still read). Reasons: `provisional`
  (a value that can still change was written), `withheld`,
  `weather_pending`, `weather_incomplete`, `github_incomplete` and
  `unwritable` (a provisional value in an entry the editor keeps because of
  a comment or a duplicate key). Scheduled runs re-process those days for up
  to 30 days; a day added from the ledger (outside the window) is written
  fill-only: empty keys are filled and unchanged provisional values
  replaced. A key leaves the ledger once its final value is written, the
  note holds a user value the tool will not overwrite, the source's final
  data has no value for it (`no_value_final`), its source is removed from
  `IHEALTH_SOURCES` (`source_deselected`), or the note is gone. Health and
  weather fetch failures are not tracked (they are failures on every run).
- **Weather**: written only for complete days (24時 row with temperature and
  sea-level pressure, at least one weather observation); a key whose source
  series has a gap is left out. An incomplete page (even one with no rows)
  for a day past the 2-day publication grace is `partial`.
- **GitHub**: written only when the whole collection was complete;
  `該当なし` only for complete days without activity.

Failures are collected as `DiaryFailure(date, source, message, kind)` and
decide the exit code: write failure or everything failed → 11, anything
else (source failures, notes needing attention, run report or ledger not
saved) → 12.
One reminder per run: an identical failure set is reminded at most once per
day (`logs/.failure_notified`), and data freshness — a stale automation
(newest file older than today − 3 days), no automation output at all, or
keys still unsettled more than 3 days after they were first seen (要確認) —
at most once per day (`logs/.stale_notified`); both go into one reminder
when due together. One-time items (the single 要確認 for `withheld` /
`weather_incomplete`, a day dropped after 30 days, unsettled keys on dates
older than 30 days) are sent once, tracked by content in
`logs/.freshness_notified`; dropped days and old dates that could not be
delivered wait in the ledger's `unreported` section for the next run. A
reminder that could not be created (`notify_failure` returned `False`) is
not recorded, so the next run retries; `--dry-run` creates none.

### 3b. `ObsidianPublisher.publish_record`

```
find_note (exact YYYY-MM-DD.md, recursive, hidden dirs skipped)
  → missing: create from template only if allowed (create_missing, create_min_age_days),
             in the folder of the nearest existing note within ±7 days, exclusive create
  → existing: snapshot (bytes + mtime/size/inode)
       → frontmatter.upsert_frontmatter (only owned keys, byte-preserving; an owned entry
         with a comment, or an owned key that appears twice → kept + warning)
       → unchanged? done
       → verify_edit_invariants(old, new, written keys): independent splitter, also in
         --dry-run; anything outside the written keys changed → EditInvariantError,
         nothing written
       → backup (data/obsidian-backup/<UTC date>/<note>.<UTC ts>.md, 0600)
       → hidden tmp in the same dir + fsync, permission and birthtime carried over
       → re-snapshot: changed since read? discard tmp, start over
       → os.replace + parent dir fsync
       → read back: differs / unreadable? the write is not confirmed → start over
         on the latest content
  (the re-snapshot and read-back retries share 3 attempts in total; a write that is
   never confirmed → ObsidianPublishError listing the backups, exit 11)
```

New notes and the initialisation of empty notes go through the same
pre-write check and read-back. `--dry-run` runs the edit and the pre-write
check, then stops before writing or creating any file and logs
`[dry-run] <path>: <key>: <old> -> <new>`.

### 3c. `workflow.run_pipeline` (single-day sinks)

```
HEALTH_EXPORT_DIR check
  → fetch_all_metrics (source.py): per-metric .hae discovery + brctl + decompress
  → parse_all (parser.py): metric aggregators → DailyHealthData
  → publisher.publish(DailyHealthData): one of the sinks
```

If any step raises an `AppError` subclass it bubbles up to `__main__`. If a
`Publisher`-specific exception (`MarkdownPublishError`,
`SlackPublishError`, ...) bubbles up, `run_pipeline` rewraps it into the
matching `AppError`.

### 4. Sinks

All single-day publishers implement the `Publisher` Protocol:

```python
class Publisher(Protocol):
    def publish(self, health_data: DailyHealthData) -> None: ...
```

Each sink validates its own boundary and raises a publisher-specific error
on failure. The `_payload.build_payload` helper provides a single source of
truth for type-strict value coercion (no silent `int(True) → 1`,
`int(1.9) → 1`, NaN/Inf, or bool-as-number). `ObsidianPublisher` also
implements `publish` (health only), but the CLI drives it through
`publish_record` with a full `DiaryRecord`.

### 5. Failure path

Every non-zero exit except argument errors (exit 2):

```
AppError.exit_code → process exit code
AppError.title + body → notifier.notify_failure → AppleScript → Reminders.app
```

Reminders persist until you acknowledge them (unlike Notification Center
banners that auto-dismiss).

## Key design decisions

- **Standard library only** so the daemon survives macOS upgrades that
  occasionally invalidate `pip`-installed environments.
- **`config.toml` separate from `.env`**: secrets stay in `.env`,
  structured config (Notion property names, Obsidian frontmatter keys, date
  key names) goes into `config.toml`. Both are gitignored.
- **`AppError` instead of `sys.exit`**: every internal failure raises a
  typed exception with `exit_code`, `title`, `body`. The CLI surface
  (`__main__`) is the only place that maps these to OS-level effects.
- **Never touch what you don't own**: the frontmatter upsert replaces only
  the entries of its keys, and not even those when they carry a comment or
  the key is duplicated; a frontmatter Obsidian itself would not read the
  same way (missing closing `---`, unusual syntax) is refused rather than
  "repaired". A second, independently written check compares every write
  with the original before anything touches the disk.
- **Missing is not zero**: a source that could not deliver a certain value
  leaves the key alone; unfinished days are provisional, and every value
  that has not settled is tracked in the ledger until it is final (or
  reported when it does not settle).
- **Atomic writes**: Markdown and Obsidian publishers write via a temp file
  + `os.fsync` + `os.replace` + parent dir fsync for POSIX-durability across
  power loss. SQLite publisher uses `BEGIN IMMEDIATE` so `CREATE TABLE` +
  UPSERT roll back together.
- **Secret hygiene**: Slack webhook URLs are masked to `hooks.slack.com`
  in every error message; `__cause__` is suppressed via `raise ... from None`
  so `traceback` doesn't leak the secret either. The GitHub token only ever
  appears in the `Authorization` header, and `Config.__repr__` hides
  `NOTION_SECRET`, the Slack webhook and the GitHub token.
- **Pluggable Sinks**: adding a new sink (e.g., InfluxDB, RSS) only
  requires implementing `Publisher` and listing the kind in
  `config.PUBLISHER_KINDS` (re-exported as `workflow.PUBLISHER_KINDS`).

## Where to look next

- The dataless-file workaround is non-obvious; see
  [`docs/case-study/dataless-icloud.md`](case-study/dataless-icloud.md).
- For the AutoSync aggregation rules (e.g., why `sleep_analysis` reads two
  days at a time), see `src/ihealth/parser.py` and
  `src/ihealth/workflow.py:_build_merged_sleep_payload`.
- For the automation JSON rules (sleep summary vs. segments, workouts), see
  `src/ihealth/automation_parser.py`; for the "final" rule and the AutoSync
  supplement, `src/ihealth/health_loader.py`.
- The JMA and GitHub modules document their deliberate deviations from the
  predecessor in their module docstrings (`weather.py`, `github_activity.py`).
