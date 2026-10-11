# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-10-12

Obsidian daily notes. `--publisher obsidian` (or `IHEALTH_PUBLISHER=obsidian`
in `.env`) writes health metrics, JMA weather and GitHub activity into the
YAML frontmatter of existing `YYYY-MM-DD.md` diary notes, for a window of
days per run. The single-day sinks keep working as before.

### Added

- **Obsidian diary pipeline** (`src/ihealth/diary.py`): for every date in the
  window, health, weather and GitHub are fetched independently; a failing
  source never blocks the others or other dates. Default window: yesterday
  back `IHEALTH_LOOKBACK_DAYS` (7) days plus today, whose values are
  provisional and overwritten by later runs.
- **Ledger of unsettled values** `data/diary-state.json` (gitignored): every
  key that has not settled yet is recorded per date with its reason, the
  time it was first seen and, for a provisional value, the value written.
  Reasons: `provisional` (today's health, `アクティビティ詳細` and `Github`; a
  day whose automation file was written before the day ended; night sleep
  without the previous day's file), `withheld` (AutoSync `.hae` written
  before the day ended), `weather_pending` / `weather_incomplete`,
  `github_incomplete`, and `unwritable` (a provisional value in an entry the
  tool keeps because of a comment or a duplicate key). Scheduled runs (no
  date arguments) re-process those days for up to 30 days, also after they
  leave the 7-day window (outside the window only empty keys are filled and
  the tool's own unchanged provisional values replaced); `--fill-empty-only`
  also replaces such a value as long as it is unchanged (values you edited
  are kept). A key leaves the ledger when its final value is written, the
  note holds a value of yours the tool will not overwrite, the source's
  final data has no value for it, its source is removed from
  `IHEALTH_SOURCES`, or the note is gone. Keys still unsettled more than 3
  days after they were first seen are added to the run's reminder as 要確認
  (once per day; `withheld` and `weather_incomplete` only once); days still
  unsettled after 30 days are dropped with one last reminder; unsettled keys
  on dates older than 30 days are not tracked but reported once. Health and
  weather fetch failures are not tracked (they are failures on every run).
  `--dry-run` never writes the ledger.
- **Obsidian publisher** (`publishers/obsidian.py`, `frontmatter.py`,
  `records.py`): finds notes recursively by exact name `YYYY-MM-DD.md`;
  byte-preserving frontmatter upsert (only its own keys change — body, other
  keys, comments, line endings, BOM untouched; quoted and plain keys both
  match); one of its keys whose entry has a comment (end of the key line, a
  comment line inside the entry, end of a continuation line, or a `|` / `>`
  header), or that appears more than once (plain / quoted / NFC / NFD
  spellings included), is not rewritten: it is reported as `kept` with a
  `comment present` / `duplicate key` warning while the other keys are
  written; refuses frontmatter it cannot read safely (e.g. no closing
  `---`); an independent pre-write check (`verify_edit_invariants`, a
  separate splitter that shares no code with the editor) compares the old
  and new bytes before the backup and the temporary file are created, also
  in `--dry-run` — body after the closing `---`, the `---` lines and every
  frontmatter line outside the written keys must be byte-identical and in
  order, otherwise nothing is written (`EditInvariantError`, exit 11);
  backup before every overwrite in
  `data/obsidian-backup/<UTC date>/<note>.<UTC time>.md` (0600, 30 days);
  concurrent-edit detection right before `os.replace`; the note is read back
  after the replace (or creation) and, if another program changed it right
  after, the values are applied again to the latest content — both retries
  share 3 attempts, and a write that is never confirmed fails with the
  backup paths (exit 11);
  optional creation of missing notes from a Templater template (limited
  expansion: leading `<%* … %>`, `<% dateStr %>`, `<% weekday %>`), never for
  today or yesterday (`OBSIDIAN_CREATE_MIN_AGE_DAYS`, default 2), placed next
  to the nearest existing note; `--dry-run` logs a diff and touches nothing.
- **Health Auto Export automation JSON** as the primary health source
  (`automation.py`, `automation_parser.py`, `health_loader.py`): discovers
  `HealthAutoExport-YYYY-MM-DD.json` in any automation folder, prefers the
  newest file per day (when it cannot be read, an older candidate is used
  only if it is final), and supplements missing fields from AutoSync `.hae`.
  A day is final only when its file was written after the day ended —
  midnight at the records' own UTC offset (`IHEALTH_TZ_OFFSET` when they
  carry none), compared as absolute time, independent of the Mac's time
  zone. A file written earlier (today's, or a mid-day export of a past day)
  is written as provisional and tracked until Health Auto Export re-exports
  the day. Values from an AutoSync `.hae` written before the end of a past
  day are withheld (AutoSync never re-exports them).
- **`アクティビティ詳細`** (activity detail), on the automation and AutoSync
  paths alike: `瞑想N分` (mindfulness of at least 1 minute), then the day's
  workouts by name when the automation exports workouts (**Include
  Workouts**) and the day has any of at least 1 minute (shorter ones count
  as none), otherwise Apple Exercise Time as `エクササイズN分` (at least 1
  minute), joined with `、`. `なし` for a day with none of these but with
  heart-rate data (Watch worn; Health Auto Export leaves Apple Exercise Time
  out on days without exercise). Not written without heart-rate data (even
  if Apple Exercise Time reports 0 minutes), nor, even with heart-rate data,
  when an input could not be read.
- **JMA weather** (`weather.py`): 気温 / 湿度 / 気圧 / 天気 / 日照時間 / 降水量
  from the hourly page (`hourly_s1.php`) in the predecessor's exact format;
  station via `JMA_PREC_NO` / `JMA_BLOCK_NO` (default Tokyo 44 / 47662);
  incomplete days and keys with gaps are not written; at least 1 s between
  requests.
- **GitHub activity** (`github_activity.py`): issues closed, PRs merged and
  default-branch commits with line counts via GraphQL, in the predecessor's
  format (`該当なし` when there was none); scope = your repositories plus
  your organizations' (`GITHUB_OWNERS` / `GITHUB_EXCLUDE_REPOS` to narrow);
  fork commits counted only when authored by you; written only when the
  collection is complete. Token from `GITHUB_TOKEN` or `gh auth token`,
  never logged.
- CLI: `--publisher obsidian`, `--from` / `--to`, `--sources`,
  `--fill-empty-only` (keep values that are already filled; for backfills).
- `.env` keys (all optional except `OBSIDIAN_VAULT_DIR` for obsidian):
  `IHEALTH_PUBLISHER`, `OBSIDIAN_VAULT_DIR`, `OBSIDIAN_DIARY_DIR`,
  `OBSIDIAN_TEMPLATE`, `OBSIDIAN_CREATE_MISSING`,
  `OBSIDIAN_CREATE_MIN_AGE_DAYS`, `IHEALTH_LOOKBACK_DAYS`,
  `IHEALTH_INCLUDE_TODAY`, `IHEALTH_SOURCES`, `IHEALTH_TZ_OFFSET`,
  `IHEALTH_NETWORK_GRACE_SEC`, `JMA_PREC_NO`, `JMA_BLOCK_NO`,
  `GITHUB_TOKEN`, `GITHUB_OWNERS`, `GITHUB_EXCLUDE_REPOS`.
- `config.toml`: `[publishers.obsidian] date_property` and
  `[publishers.obsidian.fields.<name>] property` overrides; two fields mapped
  to one key are a configuration error (exit 3).
- Run report `logs/diary-report-<UTC>.json` for every Obsidian run
  (including `--dry-run`; 30 days): created when the run starts
  (`status: "running"`, `planned_dates`, `revisit_dates`); each date's
  result is appended and fsync'd to the journal
  `logs/diary-report-<UTC>.jsonl` as soon as the date is done; at the end
  the report is completed (`finished`, or `interrupted` with
  `remaining_dates`; `processed`, `exit_code`) and the journal deleted. Per
  date: note status, source statuses, the keys still unsettled and, per key,
  the action and reason (plus `warning`, `resolved`, `resolution`);
  `provisional` holds the ledger after the run (`unresolved`, `overdue`,
  `expired`, `untracked`). If the report cannot be created no note is
  touched; if a date cannot be appended the remaining dates are not
  processed; if the final report cannot be written the journal is kept —
  each is exit 12 with a reminder.
- Notifications: one Reminders.app reminder per run; an identical set of
  failures at most once per day (`logs/.failure_notified`), and the same
  for configuration errors (exit 3) and unexpected errors (exit 1); data
  freshness at most once per day (`logs/.stale_notified`): "automation
  stopped" when the newest automation file is older than today − 3 days,
  "automation output not found" when there is none at all and no day got
  health data, and 要確認 for values unsettled more than 3 days after they
  were first seen (`日記の暫定値が確定していません (要確認)` /
  `日記の値が確定していません (要確認)`). One-time items (the single 要確認 for
  `withheld` / `weather_incomplete`, days dropped after 30 days, unsettled
  keys on dates older than 30 days) are sent once, remembered by content in
  `logs/.freshness_notified`; dropped days and old dates whose reminder
  could not be created wait in the ledger's `unreported` section for the
  next run. A reminder that could not be created is not recorded as sent
  (the next run retries; `notify_failure` now returns `True` / `False`).
  `--dry-run` only logs the reminders it would create.
- Exit codes 11 (`ObsidianAppError`: a note could not be written — including
  a write that could not be confirmed and a write refused by the pre-write
  check — or every source failed for every date) and 12
  (`DiaryPartialFailureError`: some source failed, a note needs attention,
  or the run report or ledger could not be saved).
- Network grace after wake (`IHEALTH_NETWORK_GRACE_SEC`, default 60 s):
  connection errors that clear within it are retried, not reported.
- Synthetic test fixtures `tests/fixtures/automation/`, `tests/fixtures/jma/`
  and `tests/fixtures/e2e/`; `tests/test_e2e_diary.py` runs the whole diary
  pipeline (real loader, JMA parser, GitHub collector, publisher, report and
  ledger) with only external I/O replaced, for a normal day, the JST
  midnight boundary, a mid-day export and missing data.
- `exercise_minutes` health field (Apple Exercise Time → `Exercise Minutes` /
  `アクティビティ時間 (分)`), so there are now 15 health fields.

### Changed

- **launchd schedule**: `install-launchd.sh` picks the times from
  `IHEALTH_PUBLISHER` in `.env`: 07:00 / 12:00 / 21:00 for `obsidian`,
  07:00 only (unchanged) for every other publisher. Re-run the installer
  after changing `IHEALTH_PUBLISHER`; `--print-plist` shows the plist
  without installing it.
- Under launchd (stderr not a terminal) only warnings and errors are
  written to stderr, i.e. to the unrotated `launchd.log`; `logs/run.log`
  keeps everything. `--verbose` and dry runs (`--dry-run`, or the `dryrun`
  publisher) keep stderr at INFO, so a dry run's output also shows when
  redirected to a file or piped.
- `config.toml`: the `[publishers.obsidian]` section is validated for every
  publisher (a broken config file stops every publisher with exit 3, as
  documented), and a file that is not valid UTF-8 is a configuration error
  (exit 3) instead of an unexpected error (exit 1).
- The default publisher can be set with `IHEALTH_PUBLISHER`
  (`--publisher` still wins; `notion` remains the fallback).
  `NOTION_SECRET` / `DATABASE_ID` are validated only when Notion is actually
  used.
- `HEALTH_EXPORT_DIR` may also point at the Health Auto Export `Documents`
  folder for `obsidian`; the single-day sinks still read AutoSync `.hae`
  only and need the `AutoSync` folder. When it points at `AutoSync`,
  `obsidian` searches the parent folder for automation files (also after
  `AutoSync/HealthMetrics` has been removed).
- `Config` no longer shows `NOTION_SECRET`, the Slack webhook URL or the
  GitHub token in its `repr`.
- Exercise Intensity Score: a day with heart rate data but no minute in a
  zone is recorded as `0` instead of being left empty (no heart rate data
  still leaves it empty).
- The iCloud materialize timeout for dataless `.hae` files is 120 s instead
  of 60 s, adjustable with the process environment variable
  `IHEALTH_MATERIALIZE_TIMEOUT_SEC` (up to 600 s).
- Package version `ihealth.__version__` is `0.2.0`.
- `.env.example`, `config.example.toml` and the docs cover the Obsidian
  pipeline.

### Fixed

- AutoSync `.hae` files written as an **HAE1 container** (`HAE1` +
  length-prefixed LZFSE segments) are now decoded; they previously failed
  to decompress.
- Health value validation (`parser.drop_invalid_fields`): an out-of-range
  value now drops only its own field. Previously, fields processed before it
  could be dropped as well. `heart_rate_avg > heart_rate_max` drops both.
- AutoSync sleep: a session crossing midnight that appears in both the
  previous-day and the current-day `sleep_analysis` file is counted once
  (was double-counted when the two files were merged).

### Notes for diaries migrated from the predecessor

Values written by the predecessor (GoogleFitNotionIntegration, via Notion)
are left alone, but new values are computed slightly differently:

- **Weather — 1時 row**: the predecessor skipped the 1時 row (00:00–01:00)
  because of an off-by-one in its header check (`if i > 2`). ihealth-relay
  uses all 24 rows, so weather averages (mainly the 1-6時 pressure and the
  night temperature / humidity) can differ by about 0.1–0.8 from older
  notes; max / min, night precipitation and, rarely, the pressure marks can
  differ too when the 1時 row matters. Setting `weather._FIRST_HOUR = 2`
  reproduces the predecessor exactly. Missing data is no longer written as
  `0℃` / `0%`.
- **平均心拍数 (bpm)**: from the automation JSON it is the mean of
  per-minute averages (each minute with data weighs the same). AutoSync and
  older notes averaged individual heart rate records, which weights densely
  sampled periods (e.g. workouts) more, so values are not directly
  comparable.
- **睡眠時間 (分)**: with the automation's sleep aggregation it is Health
  Auto Export's daily `totalSleep`, which includes naps that ended before
  18:00 (sleep ending after 18:00 counts toward the next day). AutoSync
  values were the main night sleep only (ending 0:00–14:00), with naps in
  昼寝時間. 昼寝時間 is now written only when naps can be separated (the
  same day's AutoSync `.hae`, or non-aggregated sleep in the automation).
- **アクティビティ詳細**: the predecessor listed workouts by name
  (e.g. `…、ウォーキング31分`). Without **Include Workouts** in the
  automation (and on days computed from AutoSync `.hae` alone) the tool
  writes Apple Exercise Time as `エクササイズN分` instead, and `なし` on a day
  with heart-rate data but no activity. Within the default window
  (yesterday back 7 days plus today) the computed value, `なし` and
  `エクササイズN分` included, replaces whatever the key holds, so workout
  names and activity details typed by hand for those days can be replaced
  (an entry with a comment is kept as it is). Turn on Include Workouts to
  write workout names. Older notes are only changed by `--date` / `--from`
  runs (`--fill-empty-only` keeps them).

### Tests

- 1,400+ unit tests (no network, no real vault / iCloud / `gh`, independent
  of the machine's time zone), including the end-to-end diary test, on
  `/usr/bin/python3` 3.9.6

## [0.1.0] - 2026-04-29

Initial public OSS release. ihealth-relay reads Apple Health exports
produced by the iPhone app **Health Auto Export — JSON+CSV** (AutoSync,
`.hae` LZFSE-compressed JSON in iCloud Drive), aggregates them into 14
daily fields (date + 13 metrics), and pushes the result to **pluggable
sinks**:

### Added

- **Pluggable Publisher architecture**: new `Publisher` Protocol in
  `src/ihealth/publishers/base.py` and shared payload validation layer
  (`_payload.py`) used by every sink for type-strict, fail-fast value
  coercion.
- **Notion publisher**: updates a daily journal page in Notion via
  `urllib.request` (no external HTTP library). Retry with exponential
  backoff on HTTP 429 / 5xx. Column-name overrides via `config.toml`.
- **Markdown publisher**: writes one Obsidian-compatible YAML frontmatter +
  body file per day. Atomic write via `tempfile.mkstemp` + `os.fsync` +
  parent-directory `fsync`. Process-safe under concurrent daemon launches.
- **Stdout publisher**: emits a single newline-terminated JSON line on
  stdout for Unix-pipe composability
  (`python3 -m ihealth --publisher stdout | jq '.step_count'`).
  `BrokenPipeError` is silenced cleanly via `os.dup2(/dev/null, sys.stdout)`.
- **SQLite publisher**: UPSERTs one row per day into a `daily_health`
  table. `BEGIN IMMEDIATE` + `COALESCE` for partial-update safety.
  Schema-drift detection.
- **Slack publisher**: posts a daily summary via Incoming Webhook. Strict
  URL validator (host allow-list: `hooks.slack.com` / `hooks.slack-gov.com`,
  path must be `/services/{T}/{B}/{token}`, no userinfo / non-443 port /
  query / fragment / non-ASCII / control char). Secret token never leaks
  in logs / errors / `__cause__`.
- **`config.toml` external configuration**: map the internal field names
  (`step_count`, `distance_km`, `sleep_hours`, ...) to arbitrary Notion
  property names. Custom minimal TOML 1.0 subset parser
  (`src/ihealth/_toml.py`) — no external dependencies.
- **English property names by default**: `Step Count` / `Distance (km)` /
  `Date` etc. Notion DBs with non-English column names (e.g., Japanese
  legacy DBs) remain supported via `cp config.example.toml config.toml`.
- **dataless `.hae` auto-materialize**: detects iCloud Drive's `SF_DATALESS`
  flag (`0x40000000`) before decompressing, triggers `brctl download`,
  polls until materialized, then proceeds. Fixes silent EDEADLK in
  launchd autoruns when iCloud has evicted the file.
- **`AppError` hierarchy**: every failure path maps to a deterministic exit
  code (3=Config, 4=HealthExportDir, 5=Source, 6=Notion, 7=Markdown,
  8=Stdout, 9=SQLite, 10=Slack). launchd / Reminders.app receive a
  user-friendly title + body for each.
- **MIT LICENSE**, **CHANGELOG**, **GitHub Actions CI** (this release).

### Changed

- **Notion property name defaults** switched from Japanese (`歩数 (歩)` etc.)
  to English Title Case + units. Users with non-English column names
  (e.g., legacy Japanese DBs) must run `cp config.example.toml config.toml`
  and edit the override mapping before the next launchd run.
- `NotionClient.DATE_PROPERTY_NAME` constant retained as a backward-compat
  alias of `DEFAULT_DATE_PROPERTY_NAME`.

### Compatibility

- macOS 14+ (Sonoma); Apple Silicon
- Python 3.9+ (`/usr/bin/python3`); standard library only
- iPhone-side: Health Auto Export — JSON+CSV (App ID 1115567069), Premium
  buy-once license

### Tests

- 545+ unit tests, no external HTTP / Notion / Slack contact during testing
- macOS `/usr/bin/python3` 3.9.6 verified

[Unreleased]: https://github.com/arkatom/ihealth-relay/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/arkatom/ihealth-relay/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/arkatom/ihealth-relay/releases/tag/v0.1.0
