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
  back `IHEALTH_LOOKBACK_DAYS` (7) days with final values, plus today with
  provisional values that later runs overwrite.
- **Provisional ledger** `data/diary-state.json` (gitignored): every key
  written as provisional (today's health, `アクティビティ詳細`, `Github`) is
  recorded with its value until a final value replaces it. Scheduled runs
  (no date arguments) re-process those days for up to 30 days, also after
  they leave the 7-day window (outside the window only empty keys are
  filled and the tool's own provisional values replaced); `--fill-empty-only`
  replaces such a value
  as long as it is unchanged (values you edited are kept); a day still
  provisional after 3 days is reminded once per day. `--dry-run` never
  writes the ledger.
- **Obsidian publisher** (`publishers/obsidian.py`, `frontmatter.py`,
  `records.py`): finds notes recursively by exact name `YYYY-MM-DD.md`;
  byte-preserving frontmatter upsert (only its own keys change — body, other
  keys, comments, line endings, BOM untouched; quoted and plain keys both
  match); refuses frontmatter it cannot read safely (e.g. no closing
  `---`); backup before every overwrite in
  `data/obsidian-backup/<UTC date>/<note>.<UTC time>.md` (0600, 30 days);
  concurrent-edit detection right before `os.replace` with up to 3 attempts;
  optional creation of missing notes from a Templater template (limited
  expansion: leading `<%* … %>`, `<% dateStr %>`, `<% weekday %>`), never for
  today or yesterday (`OBSIDIAN_CREATE_MIN_AGE_DAYS`, default 2), placed next
  to the nearest existing note; `--dry-run` logs a diff and touches nothing.
- **Health Auto Export automation JSON** as the primary health source
  (`automation.py`, `automation_parser.py`, `health_loader.py`): discovers
  `HealthAutoExport-YYYY-MM-DD.json` in any automation folder, prefers the
  newest file per day and falls back to the next candidate when one is
  broken, supplements missing fields from AutoSync `.hae`, and treats a day
  as final only when its file was written after the day ended.
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
- Run report `logs/diary-report-<UTC>.json` after every Obsidian run
  (including `--dry-run`; 30 days): per date the note status, source
  statuses and, per key, the action and reason.
- Notifications: one Reminders.app reminder per run; an identical set of
  failures at most once per day (`logs/.failure_notified`), and the same
  for configuration errors (exit 3) and unexpected errors (exit 1); data
  freshness at most once per day (`logs/.stale_notified`): "automation
  stopped" when the newest automation file is older than today − 3 days,
  "automation output not found" when there is none at all and no day got
  health data, "provisional values not final" after 3 days. A reminder
  that could not be created is not recorded as sent (the next run retries;
  `notify_failure` now returns `True` / `False`). `--dry-run` only logs the
  reminders it would create.
- Exit codes 11 (`ObsidianAppError`: a note could not be written, or every
  source failed for every date) and 12 (`DiaryPartialFailureError`: some
  source failed, or a note needs attention).
- Network grace after wake (`IHEALTH_NETWORK_GRACE_SEC`, default 60 s):
  connection errors that clear within it are retried, not reported.
- Synthetic test fixtures `tests/fixtures/automation/` and
  `tests/fixtures/jma/`.

### Changed

- **launchd schedule**: `install-launchd.sh` picks the times from
  `IHEALTH_PUBLISHER` in `.env`: 07:00 / 12:00 / 21:00 for `obsidian`,
  07:00 only (unchanged) for every other publisher. Re-run the installer
  after changing `IHEALTH_PUBLISHER`; `--print-plist` shows the plist
  without installing it.
- Under launchd (stderr not a terminal) only warnings and errors are
  written to stderr, i.e. to the unrotated `launchd.log`; `logs/run.log`
  keeps everything (`--verbose` keeps stderr verbose).
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
- **アクティビティ詳細**: written only when the automation JSON contains
  workouts (`data.workouts`, i.e. the automation exports workouts). Without
  them, and on days computed from AutoSync `.hae` alone, the key is left
  as it is — writing `なし` or only `瞑想N分` would erase workouts that
  migrated notes already list.

### Tests

- 1,200+ unit tests (no network, no real vault / iCloud / `gh`), on
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
