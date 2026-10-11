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
├── diary.py            # Obsidian diary pipeline: dates, sources, report, notifications
├── automation.py       # Health Auto Export automation JSON discovery + loading
├── automation_parser.py# automation JSON (ExportVersion.v2) → DailyHealthData + activity detail
├── health_loader.py    # per-day health: automation JSON first, AutoSync .hae supplement
├── weather.py          # JMA hourly_s1 page → 気温 / 湿度 / 気圧 / 天気 / 日照時間 / 降水量
├── github_activity.py  # GitHub GraphQL → issues closed / PRs merged / default-branch commits
├── records.py          # DiaryRecord (one day of values for the diary)
├── frontmatter.py      # byte-preserving YAML frontmatter upsert (pure functions)
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
    └── obsidian.py     # ObsidianPublisher (diary note frontmatter, backups, conflicts)
```

Tests mirror the source structure under `tests/`; fixtures for the new
sources are synthetic (`tests/fixtures/automation/`, `tests/fixtures/jma/`).

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
   gets warnings and errors unless `--verbose` is given.
4. `obsidian` → `diary.resolve_dates` + `diary.run_diary` (without date
   arguments, `revisit_unresolved` also adds days that still hold
   provisional values). Anything else → `workflow.build_default_deps` +
   `workflow.run_pipeline`.
5. Maps any `AppError` subclass to its exit code + Reminders notification.
   Configuration errors (exit 3) and unexpected errors (exit 1) are
   reminded at most once per day with the same content
   (`diary.notify_once_per_day`); `obsidian --dry-run` only logs them.

### 3a. `diary.run_diary` (Obsidian)

```
load data/diary-state.json (provisional ledger) → add unresolved days (≤ 30 days) to the window
build_obsidian_publisher (vault / diary dir / config.toml checks → exit 3 on error)
  → NetworkGate.wait (up to IHEALTH_NETWORK_GRACE_SEC after wake)
  → collect_github_activity once for the whole window
  → for each date:
       HealthLoader.load(d)   (automation JSON → AutoSync supplement / fallback)
       fetch_weather(d)       (JMA, ≥ 1 s between requests; today is skipped)
       GitHubActivity.text_for(d)
       → DiaryRecord → ObsidianPublisher.publish_record
         (--fill-empty-only: keys still holding the ledger's provisional value are replaceable)
       → ledger entry: provisional keys kept with their values, final / user-edited keys dropped
  → weather persistence check, stale / missing automation check
  → save the ledger (re-read first; not in --dry-run), overdue (> 3 days) / expired (> 30 days)
  → run report (logs/diary-report-<UTC>.json) → one Reminders notification
```

Every source result carries a completeness status — `complete`, `partial`,
`failed` or `nodata` — and only values that are certain reach the
publisher; anything else is left out, so the note keeps its existing value:

- **Health**: an automation file is *final* for a day only if it was written
  after that day ended; for a past day, values from an earlier write are
  withheld, and today's values are passed as provisional. Broken candidate
  files fall back to the next candidate; `None` fields are supplemented field
  by field from AutoSync `.hae`. The activity detail is written only when
  the automation JSON contains `data.workouts` (never from AutoSync alone).
- **Provisional values** (today's health, activity detail and GitHub) are
  recorded per date and key in `data/diary-state.json` and re-processed by
  scheduled runs until a final value replaces them (up to 30 days). A day
  added from the ledger (outside the window) is written fill-only: empty
  keys are filled and unchanged provisional values replaced.
- **Weather**: written only for complete days (24時 row with temperature and
  sea-level pressure, at least one weather observation); a key whose source
  series has a gap is left out. An incomplete page (even one with no rows)
  for a day past the 2-day publication grace is `partial`.
- **GitHub**: written only when the whole collection was complete;
  `該当なし` only for complete days without activity.

Failures are collected as `DiaryFailure(date, source, message, kind)` and
decide the exit code: write failure or everything failed → 11, anything
else (source failures, notes needing attention, ledger not saved) → 12.
One reminder per run: an identical failure set is reminded at most once per
day (`logs/.failure_notified`), and data freshness — a stale automation
(newest file older than today − 3 days), no automation output at all, or
provisional values still not final after 3 days — at most once per day
(`logs/.stale_notified`); both go into one reminder when due together. A
reminder that could not be created (`notify_failure` returned `False`) is
not recorded, so the next run retries; `--dry-run` creates none.

### 3b. `ObsidianPublisher.publish_record`

```
find_note (exact YYYY-MM-DD.md, recursive, hidden dirs skipped)
  → missing: create from template only if allowed (create_missing, create_min_age_days),
             in the folder of the nearest existing note within ±7 days, exclusive create
  → existing: snapshot (bytes + mtime/size/inode)
       → frontmatter.upsert_frontmatter (only owned keys, byte-preserving)
       → unchanged? done
       → backup (data/obsidian-backup/<UTC date>/<note>.<UTC ts>.md, 0600)
       → hidden tmp in the same dir + fsync, permission and birthtime carried over
       → re-snapshot: changed since read? discard tmp, start over (3 attempts in total)
       → os.replace + parent dir fsync → read back and warn if it differs
```

`--dry-run` stops before any file operation and logs
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
  the entries of its keys; a frontmatter Obsidian itself would not read the
  same way (missing closing `---`, unusual syntax) is refused rather than
  "repaired".
- **Missing is not zero**: a source that could not deliver a certain value
  leaves the key alone; unfinished days are provisional and are tracked
  until they get overwritten with final values.
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
