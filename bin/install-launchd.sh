#!/bin/bash
# ihealth-relay LaunchAgent インストーラ。
#
# 使い方:
#   ./bin/install-launchd.sh                 # 登録 (冪等)
#   ./bin/install-launchd.sh --print-plist   # 登録する plist を標準出力に出すだけ (何も変更しない)
#
# 動作:
#   1. $HOME/Library/Logs/ihealth-relay/ を作成 (launchd の StandardOutPath が指す)
#   2. LaunchAgent/com.ihealthrelay.daemon.plist の
#      __PROJECT_ROOT__ / __HOME__ を実パスに sed 置換
#   3. 起動時刻を .env の IHEALTH_PUBLISHER で決める:
#      obsidian → 毎日 07:00 / 12:00 / 21:00 (今日の暫定値の更新・遅れて届く前日分の取り込み)
#      それ以外 (未設定 = notion を含む) → 毎日 07:00 のみ (従来どおり。Slack の重複投稿や、
#      データ未到着の通知・再試行待ちが 1 日 3 回に増えるのを防ぐ)
#   4. ~/Library/LaunchAgents/ に配置
#   5. 既存ジョブがあれば unload してから load (冪等)
#
# launchd は引数なしで `bin/ihealth-run` を実行する (内部で python3 -m ihealth)。publisher は
# .env の IHEALTH_PUBLISHER (未設定なら notion)。Obsidian 日記なら IHEALTH_PUBLISHER=obsidian で
# 「昨日から lookback 日分 + 今日の暫定値」。IHEALTH_PUBLISHER を変えたら本スクリプトを再実行する。
# 手動実行も `./bin/ihealth-run`。
#
# 経緯メモ (2026-04-26 ~ 27 実機検証):
#   リポジトリを外部ボリューム /Volumes/... に置いていると、launchd が
#   WorkingDirectory / StandardOutPath で外部ボリュームへ chdir/open する段階で
#   EX_CONFIG=78 を返して python が起動する前に死ぬ。フルディスクアクセスを
#   /usr/bin/python3 に付与しても launchd 自身の TCC は別物なので解決しない。
#   そのため plist 内の launchd 直接アクセスパスは $HOME 配下に固定し、外部
#   ボリュームへの接触は子プロセス (bin/ihealth-run → python3) に委譲する設計に
#   したのが今のテンプレートの形。
#
# アンインストール:
#   launchctl unload ~/Library/LaunchAgents/com.ihealthrelay.daemon.plist
#   rm ~/Library/LaunchAgents/com.ihealthrelay.daemon.plist

set -euo pipefail

MODE="install"
for arg in "$@"; do
    case "$arg" in
        --print-plist) MODE="print" ;;
        -h|--help)
            echo "使い方: $0 [--print-plist]"
            echo "  --print-plist  登録する plist (起動時刻を含む) を標準出力に出すだけ。何も変更しない"
            exit 0
            ;;
        *)
            echo "エラー: 不明な引数です: $arg (使い方: $0 [--print-plist])" >&2
            exit 2
            ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PLIST_LABEL="com.ihealthrelay.daemon"
PLIST_NAME="${PLIST_LABEL}.plist"
PLIST_SRC="$PROJECT_ROOT/LaunchAgent/$PLIST_NAME"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_DST="$LAUNCH_AGENTS_DIR/$PLIST_NAME"

# 事前チェック: テンプレートと Python 実行環境
if [ ! -f "$PLIST_SRC" ]; then
    echo "エラー: plist テンプレートが見つかりません: $PLIST_SRC" >&2
    exit 1
fi

# sed の区切り文字は `|` を使うので PROJECT_ROOT / HOME に `|` が含まれるとズレる。
# さらに sed の置換文字列では `&` がマッチ全体を意味するため、パスに `&` があると
# 置換が壊れる (plist XML 的にも `&` は不正)。`\` もエスケープ文字なので危険。
for var_name in PROJECT_ROOT HOME; do
    var_value="${!var_name}"
    case "$var_value" in
        *"|"* | *"&"* | *"\\"*)
            echo "エラー: $var_name に '|', '&', '\\' のいずれかが含まれるためインストールできません: $var_value" >&2
            echo "別のパスに移動してから再実行してください。" >&2
            exit 1
            ;;
    esac
done

# launchd が実行する publisher = .env の IHEALTH_PUBLISHER (未設定なら notion)。
# obsidian のときだけ 12:00 / 21:00 の起動を残す (テンプレートの
# IHEALTH_OBSIDIAN_EXTRA_RUNS_BEGIN 〜 END の範囲)。
ENV_FILE="$PROJECT_ROOT/.env"
PUBLISHER_VALUE=""
if [ -f "$ENV_FILE" ]; then
    PUBLISHER_LINE="$(grep -E '^[[:space:]]*IHEALTH_PUBLISHER[[:space:]]*=' "$ENV_FILE" | tail -n 1 || true)"
    if [ -n "$PUBLISHER_LINE" ]; then
        PUBLISHER_VALUE="$(printf '%s' "${PUBLISHER_LINE#*=}" | tr -d "\"' \r\t" | tr '[:upper:]' '[:lower:]')"
    fi
fi
if [ "$PUBLISHER_VALUE" = "obsidian" ]; then
    SCHEDULE_LABEL="毎日 07:00 / 12:00 / 21:00"
    SCHEDULE_SED=""
else
    SCHEDULE_LABEL="毎日 07:00"
    SCHEDULE_SED='/IHEALTH_OBSIDIAN_EXTRA_RUNS_BEGIN/,/IHEALTH_OBSIDIAN_EXTRA_RUNS_END/d'
fi

render_plist() {
    if [ -n "$SCHEDULE_SED" ]; then
        sed -e "s|__PROJECT_ROOT__|$PROJECT_ROOT|g" \
            -e "s|__HOME__|$HOME|g" \
            -e "$SCHEDULE_SED" \
            "$PLIST_SRC"
    else
        sed -e "s|__PROJECT_ROOT__|$PROJECT_ROOT|g" \
            -e "s|__HOME__|$HOME|g" \
            "$PLIST_SRC"
    fi
}

if [ "$MODE" = "print" ]; then
    render_plist
    exit 0
fi

if [ ! -x /usr/bin/python3 ]; then
    echo "エラー: /usr/bin/python3 が実行できません。Xcode Command Line Tools を" \
         "インストールしてください (xcode-select --install)" >&2
    exit 1
fi
# plist の ProgramArguments がラッパー経由になったので、ここで実行属性も確認しておく。
# launchctl 登録自体は通ってしまい、07:00 起動の瞬間にだけ exec 失敗するパターンを事前に弾く。
if [ ! -x "$PROJECT_ROOT/bin/ihealth-run" ]; then
    echo "エラー: $PROJECT_ROOT/bin/ihealth-run が実行可能ではありません" >&2
    echo "        chmod +x bin/ihealth-run で実行属性を付与してください" >&2
    exit 1
fi

# logs/ を先に作っておく (アプリ側 logger.py が出す run.log の親)
mkdir -p "$PROJECT_ROOT/logs"

# launchd の StandardOutPath が指す先 (本 plist では $HOME/Library/Logs/ihealth-relay/)
# を先に作っておかないと launchd が open(O_CREAT) に失敗して EX_CONFIG=78 を返す。
LAUNCHD_LOG_DIR="$HOME/Library/Logs/ihealth-relay"
mkdir -p "$LAUNCHD_LOG_DIR"

mkdir -p "$LAUNCH_AGENTS_DIR"

# __PROJECT_ROOT__ / __HOME__ を sed 置換 (+ 起動時刻の選択) してコピー。
# PLIST_DST が万一テンプレ本体への symlink だった場合、`> "$PLIST_DST"` は symlink を
# 辿って実体 (= LaunchAgent/com.ihealthrelay.daemon.plist) を truncate してしまい、
# テンプレ自体を破壊する。一時ファイルに書いてから mv で atomic に置き換える。
PLIST_TMP="$(/usr/bin/mktemp "${TMPDIR:-/tmp}/${PLIST_NAME}.XXXXXX")"
trap 'rm -f "$PLIST_TMP"' EXIT
render_plist > "$PLIST_TMP"
mv -f "$PLIST_TMP" "$PLIST_DST"
trap - EXIT

# plist 検証: plutil で書式チェック (置換後に再検証、不正なら早期停止)
if ! /usr/bin/plutil -lint -s "$PLIST_DST"; then
    echo "エラー: 置換後 plist の plutil 検証に失敗: $PLIST_DST" >&2
    exit 1
fi

# 冪等化 (現代的な bootstrap/bootout を優先、古い macOS は load/unload にフォールバック):
# - bootstrap/bootout は macOS 10.10+ で導入され Apple 推奨。GUI session domain
#   (`gui/<uid>`) に LaunchAgent として明示的に登録する。
# - load/unload は 10.10 以降 deprecated だが互換性のため温存される。bootstrap が
#   何らかの理由で動かない環境向けの fallback として保持する。
LAUNCHD_DOMAIN="gui/$(id -u)"
SERVICE_TARGET="$LAUNCHD_DOMAIN/$PLIST_LABEL"

# 旧 LaunchAgent (pre-rename: com.amurata.ihealthintegrator) のクリーンアップ.
# rename PR でラベルが変わったため、旧ラベルのまま残った agent と新ラベルが同時に
# 走ると Slack publisher などが二重投稿になる. fail-safe で全部試す.
LEGACY_LABELS=("com.amurata.ihealthintegrator")
for legacy_label in "${LEGACY_LABELS[@]}"; do
    legacy_plist="$LAUNCH_AGENTS_DIR/${legacy_label}.plist"
    legacy_target="$LAUNCHD_DOMAIN/$legacy_label"
    if launchctl print "$legacy_target" >/dev/null 2>&1 \
       || [ -f "$legacy_plist" ]; then
        launchctl bootout "$legacy_target" 2>/dev/null \
            || launchctl unload "$legacy_plist" 2>/dev/null \
            || true
        if [ -f "$legacy_plist" ]; then
            rm -f "$legacy_plist"
            echo "(旧 LaunchAgent ${legacy_label} を削除しました)"
        fi
    fi
done

# 新ラベルの既存ジョブを bootout (見つからなければ非 0 でも無視)。fallback: legacy unload。
if ! launchctl bootout "$SERVICE_TARGET" 2>/dev/null; then
    launchctl unload "$PLIST_DST" 2>/dev/null || true
fi

# bootstrap で読み込む。bootstrap が動かない環境では load -w にフォールバック。
if launchctl bootstrap "$LAUNCHD_DOMAIN" "$PLIST_DST" 2>/dev/null; then
    # bootstrap 成功後は launchctl enable で過去 disable 状態を解除する.
    # (bootstrap 単独では disabled-on-disk な service を再有効化しない. 旧 `load -w`
    # 相当の `enable + bootstrap` パターンに揃える)
    launchctl enable "$SERVICE_TARGET" 2>/dev/null || true
else
    echo "(launchctl bootstrap が利用できない環境のため legacy 'launchctl load -w' にフォールバック)"
    launchctl load -w "$PLIST_DST"
fi

echo "登録完了: $PLIST_DST"
echo "自動実行: $SCHEDULE_LABEL (IHEALTH_PUBLISHER=${PUBLISHER_VALUE:-未設定 = notion})"
if [ "$PUBLISHER_VALUE" != "obsidian" ]; then
    echo "       Obsidian 日記 (IHEALTH_PUBLISHER=obsidian) にすると 07:00 / 12:00 / 21:00 の 3 回になります"
    echo "       (.env を変えたら本スクリプトを再実行してください)。"
fi
echo "手動実行: $PROJECT_ROOT/bin/ihealth-run"
echo "launchd ログ: $LAUNCHD_LOG_DIR/launchd.log (起動失敗時はここを確認)"
echo "アプリログ:   $PROJECT_ROOT/logs/run.log (TimedRotatingFileHandler)"
echo ""
echo "launchctl 登録状況:"
launchctl list | grep "$PLIST_LABEL" || {
    echo "警告: launchctl list に表示されません。load に失敗した可能性があります。" >&2
    echo "plist を手で確認: $PLIST_DST" >&2
    exit 1
}
