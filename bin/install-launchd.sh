#!/bin/bash
# ihealth-relay LaunchAgent インストーラ。
#
# 使い方:
#   ./bin/install-launchd.sh                   # 登録 (冪等)。最後に、定期実行に要る macOS の許可を
#                                              #   与える手順を、貼り付けて使えるコマンド付きで表示する
#   ./bin/install-launchd.sh --print-plist     # 登録する plist を標準出力に出すだけ (何も変更しない)
#   ./bin/install-launchd.sh --build-launcher <dir>
#                                              # 専用ランチャーを既存のディレクトリ <dir> にビルドする
#                                              #   だけ (テスト・ビルドの確認用)。定期実行が使うランチャー
#                                              #   (~/Library/Application Support/ihealth-relay) と launchd
#                                              #   には触れず、そのディレクトリを <dir> に指定すると拒否する
#
# 動作:
#   1. 専用ランチャー bin/launcher/ihealth-launcher.c を
#      ~/Library/Application Support/ihealth-relay/ihealth-launcher にビルドし、ad-hoc 署名する。
#      前回ビルドしたときとソース・プロジェクトの場所・ビルド設定が同じで、バイナリも変わって
#      いなければ (横に置く ihealth-launcher.stamp で判定) 作り直さない。同じバイナリのままなので、
#      与えたフルディスクアクセスは有効なまま残る。プロジェクトの場所は実体のパス (symlink を
#      辿り、大文字小文字も実際の名前) で比べるので、symlink 経由や大文字小文字を変えて
#      実行しても作り直さない。
#      ビルドできない (C コンパイラが無い / コンパイル失敗) ときは bin/ihealth-run を直接登録する
#      (非推奨: フルディスクアクセスを /bin/bash に与えることになる)
#   2. $HOME/Library/Logs/ihealth-relay/ を作成 (launchd の StandardOutPath が指す)
#   3. LaunchAgent/com.ihealthrelay.daemon.plist の __PROGRAM__ (1. のランチャー) /
#      __PROJECT_ROOT__ / __HOME__ を実パスに sed 置換
#   4. 起動時刻を .env の IHEALTH_PUBLISHER で決める:
#      obsidian → 毎日 07:00 / 12:00 / 21:00 (今日の暫定値の更新・遅れて届く前日分の取り込み)
#      それ以外 (未設定 = notion を含む) → 毎日 07:00 のみ (従来どおり。Slack の重複投稿や、
#      データ未到着の通知・再試行待ちが 1 日 3 回に増えるのを防ぐ)
#   5. ~/Library/LaunchAgents/ に配置
#   6. 既存ジョブがあれば bootout してから bootstrap (冪等)
#   7. 最後に次の手順 (フルディスクアクセスの付与・試し実行・結果の確認) を表示する
#
# なぜ専用ランチャーか (実機の TCC ログで確認済み):
#   macOS のプライバシー保護 (TCC) は、launchd ジョブのファイルアクセスを ProgramArguments[0]
#   のプログラム (責任プロセス = responsible process) によるものとみなし、exec しても変わらない。
#   bin/ihealth-run (bash スクリプト) を直接登録すると責任プロセスは /bin/bash になり、Health
#   Auto Export の iCloud フォルダを読むには /bin/bash にフルディスクアクセスを与えるしかない
#   (launchd がこのユーザーで起動するすべての bash スクリプトに及ぶ)。ランチャーは、埋め込んだ
#   bin/ihealth-run だけを実行する小さなバイナリなので、許可をこれ 1 つに絞れる。ad-hoc 署名の
#   バイナリへの許可はそのバイナリ (cdhash) に結び付くので、作り直すと以前の許可は効かなくなる。
#   そのため作り直すのは stamp が変わったときだけにし、作り直したら付与し直しを求める。
#
# launchd は引数なしでランチャー → `bin/ihealth-run` を実行する (内部で python3 -m ihealth)。
# ランチャーは引数を受け付けない (引数付きで起動されたら何もせずに 64 で終わる。許可を持つ
# プロセスに --slack-webhook <URL> などで健康データを別の送り先へ出させないため)。
# publisher は .env の IHEALTH_PUBLISHER (未設定なら notion)。Obsidian 日記なら
# IHEALTH_PUBLISHER=obsidian で「昨日から lookback 日分 + 今日の暫定値」。IHEALTH_PUBLISHER を
# 変えたら本スクリプトを再実行する。手動実行は `./bin/ihealth-run`。
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
#   launchctl bootout gui/$(id -u)/com.ihealthrelay.daemon
#   rm ~/Library/LaunchAgents/com.ihealthrelay.daemon.plist
#   rm -rf ~/Library/Application\ Support/ihealth-relay
#   システム設定 › プライバシーとセキュリティ › フルディスクアクセス (と オートメーション) で
#   ihealth-launcher を選んで「−」で削除する

set -euo pipefail

usage() {
    echo "使い方: $0 [--print-plist | --build-launcher <dir>]"
    echo "  (引数なし)              登録する。最後に、許可の付与などの次の手順をコマンド付きで表示する"
    echo "  --print-plist           登録する plist (起動するプログラムと起動時刻を含む) を標準出力に出すだけ。"
    echo "                          何も変更しない (ランチャーもビルドしない)"
    echo "  --build-launcher <dir>  専用ランチャーを既存のディレクトリ <dir> にビルドするだけ (テスト・ビルドの"
    echo "                          確認用。必要なときだけビルド)。定期実行が使うランチャーと launchd には触れない"
}

MODE="install"
BUILD_DIR=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --print-plist)
            NEXT_MODE="print"
            ;;
        --build-launcher)
            # 出力先は必ず指定させる。定期実行が使うランチャー (登録済みのジョブが起動する
            # ファイル) を置き換えると、ジョブが別の clone の ihealth-run を実行するようになり、
            # 与えたフルディスクアクセスも効かなくなる (作り直したバイナリは cdhash が変わる)
            case "${2:-}" in
                "" | -*)
                    echo "エラー: --build-launcher にはビルド先のディレクトリを指定してください" >&2
                    usage >&2
                    exit 2
                    ;;
            esac
            NEXT_MODE="build"
            BUILD_DIR="$2"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "エラー: 不明な引数です: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
    if [ "$MODE" != "install" ] && [ "$MODE" != "$NEXT_MODE" ]; then
        echo "エラー: --print-plist と --build-launcher は同時に指定できません" >&2
        usage >&2
        exit 2
    fi
    MODE="$NEXT_MODE"
    shift
done

# ディレクトリの実体のパス (symlink を辿り、macOS では大文字小文字も実際の名前にそろえる)。
# プロジェクトの場所はランチャーに埋め込み、stamp の run_path にも書くので、インストーラを
# symlink 経由や大文字小文字を変えたパスで実行したときに別の場所とみなすと、そのたびに
# ランチャーを作り直して、与えたフルディスクアクセスが外れる。
# (bash の pwd -P は、大文字小文字を区別しない APFS では打ったとおりの綴りを返すので、
#  macOS 13+ の /bin/realpath で実際の名前にする。無ければ pwd -P の結果のまま)
physical_dir() {
    local dir resolved
    dir="$(cd -P "$1" && pwd -P)" || return 1
    if [ -x /bin/realpath ] && resolved="$(/bin/realpath "$dir" 2>/dev/null)" && [ -n "$resolved" ]; then
        dir="$resolved"
    fi
    printf '%s\n' "$dir"
}

SCRIPT_DIR="$(physical_dir "$(dirname "${BASH_SOURCE[0]}")")"
PROJECT_ROOT="$(physical_dir "$SCRIPT_DIR/..")"
PLIST_LABEL="com.ihealthrelay.daemon"
PLIST_NAME="${PLIST_LABEL}.plist"
PLIST_SRC="$PROJECT_ROOT/LaunchAgent/$PLIST_NAME"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_DST="$LAUNCH_AGENTS_DIR/$PLIST_NAME"
RUN_SCRIPT="$PROJECT_ROOT/bin/ihealth-run"

# 専用ランチャー (bin/launcher/ihealth-launcher.c をビルドしたもの)。
# 定期実行 (plist の ProgramArguments[0]) が使うのは LIVE_LAUNCHER_DIR の中のもの。
LAUNCHER_SRC="$PROJECT_ROOT/bin/launcher/ihealth-launcher.c"
LIVE_LAUNCHER_DIR="$HOME/Library/Application Support/ihealth-relay"
LAUNCHER_DIR="$LIVE_LAUNCHER_DIR"
if [ "$MODE" = "build" ]; then
    if [ ! -d "$BUILD_DIR" ]; then
        echo "エラー: --build-launcher のビルド先のディレクトリがありません: $BUILD_DIR" >&2
        exit 2
    fi
    # 同じディレクトリかは inode で比べる (symlink や大文字小文字の違いでも見逃さない)
    if [ -d "$LIVE_LAUNCHER_DIR" ] && [ "$BUILD_DIR" -ef "$LIVE_LAUNCHER_DIR" ]; then
        echo "エラー: --build-launcher のビルド先が、定期実行が使うランチャーのディレクトリです: $BUILD_DIR" >&2
        echo "       ここのランチャーを置き換えるのは登録 (引数なしの $0) だけです。" >&2
        echo "       ビルドを確かめるときは別のディレクトリを指定してください。" >&2
        exit 2
    fi
    LAUNCHER_DIR="$BUILD_DIR"
fi
LAUNCHER_PATH="$LAUNCHER_DIR/ihealth-launcher"
LAUNCHER_STAMP="$LAUNCHER_DIR/ihealth-launcher.stamp"
# ビルド設定。変えると stamp が変わってランチャーを作り直す (= フルディスクアクセスの付与し直し)。
# -Werror は付けない (将来のコンパイラが警告を増やしてもビルドを止めない。警告ゼロはテストで確かめる)。
LAUNCHER_FLAGS=(-O2 -Wall -Wextra)
if [ "$(uname -s)" = "Darwin" ]; then
    # __RESTRICT セグメントがあると dyld は DYLD_INSERT_LIBRARIES などを無視する。
    # 許可を持つランチャーのプロセスに、他のライブラリを差し込ませないため。
    LAUNCHER_FLAGS+=(-Wl,-sectcreate,__RESTRICT,__restrict,/dev/null)
    LAUNCHER_SIGN_ARGS=(-s - --force --identifier com.ihealthrelay.launcher)
    LAUNCHER_SIGN_DESC="codesign ${LAUNCHER_SIGN_ARGS[*]}"
else
    # macOS 以外 (CI のテスト) では署名しない
    LAUNCHER_SIGN_ARGS=()
    LAUNCHER_SIGN_DESC="none"
fi

# 事前チェック: テンプレートと Python 実行環境
if [ ! -f "$PLIST_SRC" ]; then
    echo "エラー: plist テンプレートが見つかりません: $PLIST_SRC" >&2
    exit 1
fi

# PROJECT_ROOT / HOME に含まれると壊れる文字:
#   '|' '&' '\'          sed の置換 (区切り文字 / マッチ全体 / エスケープ) と plist (XML の '&')
#   '"' と改行などの制御文字  ランチャーに埋め込む C の文字列リテラル (ビルド時はさらに 1 バイト
#                        ずつ 8 進エスケープにする)、sed、行単位の stamp
#   '$' '`' '!'          最後に表示する貼り付け用コマンド (パスをダブルクォートで囲む) の意味が変わる
for var_name in PROJECT_ROOT HOME; do
    var_value="${!var_name}"
    case "$var_value" in
        *"|"* | *"&"* | *"\\"* | *'"'* | *'$'* | *'`'* | *'!'* | *[[:cntrl:]]*)
            echo "エラー: $var_name に使えない文字 (| & \\ \" \$ \` ! または改行などの制御文字) が含まれるためインストールできません: $var_value" >&2
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

# ---------------------------------------------------------------------------
# 専用ランチャー
# ---------------------------------------------------------------------------

# コマンド名かパスを実行ファイルのパスに解決する (見つからなければ何も出さずに失敗)
resolve_command() {
    case "$1" in
        */*) [ -f "$1" ] && [ -x "$1" ] && printf '%s\n' "$1" ;;
        *) command -v "$1" 2>/dev/null ;;
    esac
}

# ランチャーのビルドに使う C コンパイラのパス (無ければ空)。IHEALTH_CC で指定できる
# (存在しないパスを指定すると「コンパイラが無い」ときの動作になる。テスト用)。
find_cc() {
    if [ -n "${IHEALTH_CC:-}" ]; then
        resolve_command "$IHEALTH_CC" || true
    else
        resolve_command /usr/bin/cc || resolve_command cc || true
    fi
}

# ファイルの sha256 (16 進) を標準出力へ。計算できなければ失敗
sha256_of() {
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    elif command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        return 1
    fi
}

# 文字列の各バイトを 8 進エスケープ (\ooo) にして、C の文字列リテラルの中身として出す。
# 空白・日本語・'?' (trigraph) などを含むパスでもリテラルが壊れない。
c_string_octal() {
    local bytes out="" b
    bytes="$(printf '%s' "$1" | od -An -v -to1)" || return 1
    for b in $bytes; do
        out="$out\\$b"
    done
    printf '%s' "$out"
}

# stamp のうちビルドの入力の部分。この内容と、ビルドしたバイナリの sha256 (binary_sha256=) が
# 前回と同じならランチャーを作り直さない。コンパイラ自体のバージョンは入れない
# (Command Line Tools の更新のたびに作り直して許可が外れるのを避ける)。
launcher_stamp_inputs() {
    local source_sha256
    [ -f "$LAUNCHER_SRC" ] || return 1
    source_sha256="$(sha256_of "$LAUNCHER_SRC")" || return 1
    [ -n "$source_sha256" ] || return 1
    printf 'format=1\n'
    printf 'source_sha256=%s\n' "$source_sha256"
    printf 'run_path=%s\n' "$RUN_SCRIPT"
    printf 'cflags=%s\n' "${LAUNCHER_FLAGS[*]}"
    printf 'sign=%s\n' "$LAUNCHER_SIGN_DESC"
}

# ランチャーが前回ビルドしたときのまま使えるなら 0 (何も書き換えない)
launcher_is_current() {
    local expected recorded binary_recorded binary_actual
    if [ ! -f "$LAUNCHER_PATH" ] || [ ! -x "$LAUNCHER_PATH" ] || [ ! -f "$LAUNCHER_STAMP" ]; then
        return 1
    fi
    expected="$(launcher_stamp_inputs)" || return 1
    recorded="$(grep -v '^binary_sha256=' "$LAUNCHER_STAMP")" || return 1
    binary_recorded="$(sed -n 's/^binary_sha256=//p' "$LAUNCHER_STAMP")" || return 1
    binary_actual="$(sha256_of "$LAUNCHER_PATH")" || return 1
    [ "$expected" = "$recorded" ] && [ -n "$binary_recorded" ] && [ "$binary_recorded" = "$binary_actual" ]
}

# ランチャーをビルド・署名して置き換え、stamp を書く。失敗したら 1 (作りかけのファイルは消す)。
# 置き換えは一時ファイル + mv (別 inode)。動いている古いランチャーや、symlink の先の実体を
# 書き換えない (署名済みバイナリを同じ inode のまま上書きすると macOS は実行時に kill する)。
# ビルド結果が今のバイナリとバイト単位で同じ (コメントだけの変更など) なら、ファイルは
# 置き換えずに stamp だけ更新する (同じバイナリなので、与えた許可も有効なまま)。
#   LAUNCHER_BINARY_CHANGED: 1 = バイナリを置き換えた / 0 = 前と同じだったので残した
build_launcher() {
    local cc_path inputs run_path_c build_tmp stamp_tmp binary_sha256 sign_output current_sha256
    LAUNCHER_BINARY_CHANGED=1
    cc_path="$(find_cc)"
    if [ -z "$cc_path" ]; then
        echo "警告: C コンパイラ (cc) が見つからないため、専用ランチャーをビルドできません" \
             "(Xcode Command Line Tools を入れると使える: xcode-select --install)" >&2
        return 1
    fi
    if [ ! -f "$LAUNCHER_SRC" ]; then
        echo "警告: ランチャーのソースが見つかりません: $LAUNCHER_SRC" >&2
        return 1
    fi
    if ! inputs="$(launcher_stamp_inputs)"; then
        echo "警告: sha256 を計算できないため (shasum / sha256sum が無い)、ランチャーをビルドしません" >&2
        return 1
    fi
    # mv は置き換え先がディレクトリ (やその symlink) だと中へ移してしまう
    if [ -d "$LAUNCHER_PATH" ] || [ -d "$LAUNCHER_STAMP" ]; then
        echo "警告: $LAUNCHER_PATH (または .stamp) がディレクトリのため、ランチャーを置けません" >&2
        return 1
    fi
    run_path_c="$(c_string_octal "$RUN_SCRIPT")" || return 1
    mkdir -p "$LAUNCHER_DIR" || return 1
    # リンク先は毎回同じパスにする。リンカは出力先のパスでも LC_UUID を変えるので、
    # 一時ファイル名が毎回違うと、同じソースでもバイナリ (= cdhash) が変わってしまう。
    # 先に rm -f しておけば、symlink が置かれていても辿らずに新しいファイルとして作られる。
    build_tmp="$LAUNCHER_DIR/.ihealth-launcher.build"
    rm -f "$build_tmp" || return 1
    if ! "$cc_path" "${LAUNCHER_FLAGS[@]}" "-DIHEALTH_RUN_PATH=\"$run_path_c\"" \
            -o "$build_tmp" "$LAUNCHER_SRC"; then
        rm -f "$build_tmp"
        echo "警告: ランチャーのコンパイルに失敗しました ($cc_path)" >&2
        return 1
    fi
    chmod 755 "$build_tmp"
    if [ "${#LAUNCHER_SIGN_ARGS[@]}" -gt 0 ]; then
        # リンカの ad-hoc 署名を、識別子付きの ad-hoc 署名に置き換える
        if ! sign_output="$(codesign "${LAUNCHER_SIGN_ARGS[@]}" "$build_tmp" 2>&1)" \
                || ! sign_output="$(codesign --verify --strict "$build_tmp" 2>&1)"; then
            rm -f "$build_tmp"
            echo "警告: ランチャーに署名できませんでした (codesign): $sign_output" >&2
            return 1
        fi
    fi
    if ! binary_sha256="$(sha256_of "$build_tmp")" \
            || ! stamp_tmp="$(/usr/bin/mktemp "$LAUNCHER_DIR/.ihealth-launcher.stamp.XXXXXX")"; then
        rm -f "$build_tmp"
        return 1
    fi
    printf '%s\nbinary_sha256=%s\n' "$inputs" "$binary_sha256" > "$stamp_tmp"
    if [ -f "$LAUNCHER_PATH" ] && [ -x "$LAUNCHER_PATH" ]; then
        current_sha256="$(sha256_of "$LAUNCHER_PATH" 2>/dev/null || true)"
        if [ "$current_sha256" = "$binary_sha256" ]; then
            LAUNCHER_BINARY_CHANGED=0
            rm -f "$build_tmp"
        fi
    fi
    # バイナリ → stamp の順に置き換える (途中で止まっても、次回は binary_sha256 が合わずに作り直す)
    if [ "$LAUNCHER_BINARY_CHANGED" = 1 ] && ! mv -f "$build_tmp" "$LAUNCHER_PATH"; then
        rm -f "$build_tmp" "$stamp_tmp"
        return 1
    fi
    if ! mv -f "$stamp_tmp" "$LAUNCHER_STAMP"; then
        rm -f "$stamp_tmp"
        return 1
    fi
}

# --print-plist 用: 何も作らずに、登録するとしたら使うプログラムを決める。
# (コンパイラがあればビルドできるものとみなす。実際のコンパイル失敗までは予測しない)
plan_program() {
    if launcher_is_current; then
        PROGRAM_PATH="$LAUNCHER_PATH"
    elif [ -n "$(find_cc)" ] && [ -f "$LAUNCHER_SRC" ]; then
        PROGRAM_PATH="$LAUNCHER_PATH"
    else
        PROGRAM_PATH="$RUN_SCRIPT"
        echo "警告: C コンパイラかランチャーのソースが無いため、専用ランチャーを使えません。" \
             "ProgramArguments は bin/ihealth-run (フルディスクアクセスは /bin/bash に必要。非推奨) になります" >&2
    fi
}

# ランチャーを用意 (必要なときだけビルド) して、登録するプログラムを決める。結果の表示は
# 呼び出し側 (登録と --build-launcher で伝えることが違う)。
#   LAUNCHER_STATE: current (前回のまま) / same (作り直したが中身は同じ) /
#                   built (新しいバイナリにした = 許可の付与し直しが必要) / fallback (使えない)
#   PROGRAM_PATH:   plist の ProgramArguments[0]
ensure_launcher() {
    if launcher_is_current; then
        LAUNCHER_STATE="current"
        PROGRAM_PATH="$LAUNCHER_PATH"
    elif build_launcher; then
        PROGRAM_PATH="$LAUNCHER_PATH"
        if [ "$LAUNCHER_BINARY_CHANGED" = 1 ]; then
            LAUNCHER_STATE="built"
        else
            LAUNCHER_STATE="same"
        fi
    else
        LAUNCHER_STATE="fallback"
        PROGRAM_PATH="$RUN_SCRIPT"
    fi
}

render_plist() {
    if [ -n "$SCHEDULE_SED" ]; then
        sed -e "s|__PROGRAM__|$PROGRAM_PATH|g" \
            -e "s|__PROJECT_ROOT__|$PROJECT_ROOT|g" \
            -e "s|__HOME__|$HOME|g" \
            -e "$SCHEDULE_SED" \
            "$PLIST_SRC"
    else
        sed -e "s|__PROGRAM__|$PROGRAM_PATH|g" \
            -e "s|__PROJECT_ROOT__|$PROJECT_ROOT|g" \
            -e "s|__HOME__|$HOME|g" \
            "$PLIST_SRC"
    fi
}

if [ "$MODE" = "print" ]; then
    plan_program
    render_plist
    exit 0
fi

if [ "$MODE" = "build" ]; then
    # ビルド先は定期実行が使うランチャーではないので、許可の付与は求めない
    ensure_launcher
    case "$LAUNCHER_STATE" in
        current) echo "ランチャー: 前回ビルドしたままです (作り直していません): $LAUNCHER_PATH" ;;
        same) echo "ランチャー: 作り直しましたが中身は前回と同じなので、ファイルはそのままです: $LAUNCHER_PATH" ;;
        built) echo "ランチャー: ビルドしました: $LAUNCHER_PATH" ;;
        fallback) echo "警告: ランチャーをビルドできませんでした (このまま登録すると bin/ihealth-run を直接起動します)" >&2 ;;
    esac
    echo "(--build-launcher: 定期実行が使うランチャー ($LIVE_LAUNCHER_DIR) と launchd には触れていません)"
    echo "launcher=$LAUNCHER_STATE"
    echo "program=$PROGRAM_PATH"
    if [ "$LAUNCHER_STATE" = "fallback" ]; then
        exit 1
    fi
    exit 0
fi

if [ ! -x /usr/bin/python3 ]; then
    echo "エラー: /usr/bin/python3 が実行できません。Xcode Command Line Tools を" \
         "インストールしてください (xcode-select --install)" >&2
    exit 1
fi
# plist はランチャー → ラッパー経由で起動するので、ここで実行属性も確認しておく。
# launchctl 登録自体は通ってしまい、07:00 起動の瞬間にだけ exec 失敗するパターンを事前に弾く。
if [ ! -x "$RUN_SCRIPT" ]; then
    echo "エラー: $RUN_SCRIPT が実行可能ではありません" >&2
    echo "        chmod +x bin/ihealth-run で実行属性を付与してください" >&2
    exit 1
fi

ensure_launcher
case "$LAUNCHER_STATE" in
    current)
        echo "ランチャー: 前回ビルドしたままです (作り直していません): $LAUNCHER_PATH"
        ;;
    same)
        echo "ランチャー: 作り直しましたが中身は前回と同じなので、そのまま使います (許可も有効なまま): $LAUNCHER_PATH"
        ;;
    built)
        echo "ランチャー: ビルドしました: $LAUNCHER_PATH"
        echo "  → このファイルにフルディスクアクセスを与える必要があります (前のビルドへの許可は効きません)。手順は最後に表示します。"
        ;;
    fallback)
        echo "警告: 専用ランチャーを用意できないため、launchd は bin/ihealth-run を直接起動します。" \
             "フルディスクアクセスは /bin/bash に与えることになります (非推奨。詳細は最後に表示します)。" >&2
        ;;
esac

# logs/ を先に作っておく (アプリ側 logger.py が出す run.log の親)
mkdir -p "$PROJECT_ROOT/logs"

# launchd の StandardOutPath が指す先 (本 plist では $HOME/Library/Logs/ihealth-relay/)
# を先に作っておかないと launchd が open(O_CREAT) に失敗して EX_CONFIG=78 を返す。
LAUNCHD_LOG_DIR="$HOME/Library/Logs/ihealth-relay"
mkdir -p "$LAUNCHD_LOG_DIR"

mkdir -p "$LAUNCH_AGENTS_DIR"

# __PROGRAM__ / __PROJECT_ROOT__ / __HOME__ を sed 置換 (+ 起動時刻の選択) してコピー。
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
USER_ID="$(id -u)"
LAUNCHD_DOMAIN="gui/$USER_ID"
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

echo ""
echo "登録完了: $PLIST_DST"
echo "自動実行: $SCHEDULE_LABEL (IHEALTH_PUBLISHER=${PUBLISHER_VALUE:-未設定 = notion})"
if [ "$PUBLISHER_VALUE" != "obsidian" ]; then
    echo "       Obsidian 日記 (IHEALTH_PUBLISHER=obsidian) にすると 07:00 / 12:00 / 21:00 の 3 回になります"
    echo "       (.env を変えたら本スクリプトを再実行してください)。"
fi
if [ "$LAUNCHER_STATE" = "fallback" ]; then
    echo "起動するもの: $RUN_SCRIPT (bash スクリプトを直接。専用ランチャーなし)"
else
    echo "起動するもの: $LAUNCHER_PATH → bin/ihealth-run"
fi
echo "手動実行: $RUN_SCRIPT"
echo "launchd ログ: $LAUNCHD_LOG_DIR/launchd.log (起動失敗時はここを確認)"
echo "アプリログ:   $PROJECT_ROOT/logs/run.log (TimedRotatingFileHandler)"
echo ""
echo "launchctl 登録状況:"
launchctl list | grep "$PLIST_LABEL" || {
    echo "警告: launchctl list に表示されません。load に失敗した可能性があります。" >&2
    echo "plist を手で確認: $PLIST_DST" >&2
    exit 1
}

# ---------------------------------------------------------------------------
# 次の手順。上の出力は流れて見落とされるので、やることとコマンドは必ずここ (最後) にまとめる。
# パスはダブルクォートで囲む (bash / zsh / fish のどれに貼り付けても同じ意味になるよう、
# 上で '"' '$' '`' '!' を含むパスを拒否している)。uid は展開済みの数字で出す。
# ---------------------------------------------------------------------------
FDA_SETTINGS_URL="x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
RUN_LOG="$PROJECT_ROOT/logs/run.log"
RULE="======================================================================"

# 結果を確かめるコマンド。
#   launchctl print: 1 段目 (タブ 1 つ) の state と last exit code だけを出す (入れ子の
#     "state = active" などは出さない)
#   awk: run.log のうち最後の実行 (最後の「ihealth-relay 起動」から下) の要約の行だけを出す。
#     日記の実行は 1 日 3〜5 行 × 8 日以上 (未確定の日の再処理があればさらに) 書くので、
#     tail では最後の実行の全体が見えない
# awk の部分はシングルクォートで囲む ($0 などを bash / zsh / fish のどれでも展開させない)
CHECK_STATE_COMMAND="launchctl print $SERVICE_TARGET | grep -E \"^.(state|last exit code) = \""
if [ "$PUBLISHER_VALUE" = "obsidian" ]; then
    CHECK_LOG_PATTERN='ihealth-relay 起動|日記 [0-9-]+:|日記 完了:'
else
    CHECK_LOG_PATTERN='ihealth-relay 起動|集計完了|ERROR'
fi
CHECK_LOG_COMMAND="awk '/ihealth-relay 起動/{n=0} /$CHECK_LOG_PATTERN/{l[++n]=\$0} END{for(i=1;i<=n;i++)print l[i]}' \"$RUN_LOG\""

print_check_steps() {
    # $1 = 試し実行の手順番号, $2 = 結果確認の手順番号
    echo "$1) 今すぐ 1 回実行する (定時と同じ処理。終わるまで 1〜数分):"
    echo "     launchctl kickstart -k $SERVICE_TARGET"
    echo "$2) 少し待ってから結果を確かめる (2 つ目は run.log のうち最後の実行の要約だけを出す):"
    echo "     $CHECK_STATE_COMMAND"
    echo "     $CHECK_LOG_COMMAND"
    if [ "$PUBLISHER_VALUE" = "obsidian" ]; then
        echo "   成功: state = not running、last exit code = 0 で、最後の行 (日記 完了: …) が exit=0。"
        echo "   各日の行は health=complete (今日の分は、まだデータが届いておらず health=nodata のこともある)。"
        echo "   health=failed (exit=12) なら、フルディスクアクセスがまだ効いていない"
        echo "   (run.log に Operation not permitted が出ている)。"
    else
        echo "   成功: state = not running、last exit code = 0 で、「集計完了」があり ERROR が無い。"
        echo "   ERROR に Operation not permitted があれば、フルディスクアクセスがまだ効いていない。"
    fi
    echo "   state = running ならまだ実行中 (last exit code は前の回の値か (never exited))。"
    echo "   少し待ってから同じコマンドで確かめ直す。"
}

echo ""
echo "$RULE"
if [ "$LAUNCHER_STATE" = "fallback" ]; then
    echo " 【要対応】次にやること: 定期実行に要る macOS の許可"
    echo "$RULE"
    echo "専用ランチャーを用意できなかったため、launchd は bash スクリプト"
    echo "bin/ihealth-run を直接起動します。この場合フルディスクアクセスは /bin/bash に"
    echo "与えるしかなく、launchd が起動するすべての bash スクリプトに許可が及ぶので"
    echo "おすすめしません。Xcode Command Line Tools を入れてから、このスクリプトを"
    echo "実行し直してください (専用ランチャーをビルドします):"
    echo "     xcode-select --install"
    echo "     \"$SCRIPT_DIR/install-launchd.sh\""
    echo ""
    echo "それでも /bin/bash に与える場合:"
    echo "1) フルディスクアクセスの設定を開き、「+」→ Cmd+Shift+G → /bin/bash →"
    echo "   「開く」→ オンにする:"
    echo "     open \"$FDA_SETTINGS_URL\""
    print_check_steps 2 3
    echo "4) 定期実行が初めてリマインダーで通知するとき、bash にリマインダーの操作を"
    echo "   許可するか聞かれるので許可する (未許可の通知は残り、次の実行で再送される)。"
else
    if [ "$LAUNCHER_STATE" = "built" ]; then
        echo " 【要対応】次にやること: ランチャーにフルディスクアクセスを与える"
        echo "$RULE"
        echo "ランチャーをビルドしました。与えるまで、定期実行は Health Auto Export の"
        echo "iCloud フォルダを読めません (run.log に Operation not permitted が出ます)。"
        echo "前のビルドに与えた許可は、このファイルには効きません。"
    else
        echo " 次にやること: ランチャーにフルディスクアクセスを与える (付与済みなら 3) から)"
        echo "$RULE"
        echo "ランチャーは前回と同じバイナリです。前回与えた許可はそのまま有効です。"
    fi
    echo "  ランチャー: $LAUNCHER_PATH"
    echo ""
    echo "1) ランチャーを Finder で表示する:"
    echo "     open -R \"$LAUNCHER_PATH\""
    echo "2) フルディスクアクセスの設定を開き、1) の ihealth-launcher をリストへ"
    echo "   ドラッグしてオンにする (「+」→ Cmd+Shift+G でパスを貼り付けてもよい)。"
    echo "   前の ihealth-launcher が残っていたら「−」で消してから追加する:"
    echo "     open \"$FDA_SETTINGS_URL\""
    print_check_steps 3 4
    echo "5) 定期実行が初めてリマインダーで通知するとき、ihealth-launcher に"
    echo "   リマインダーの操作を許可するか聞かれるので許可する"
    echo "   (未許可の通知は残り、次の実行で再送される)。"
    echo "6) 以前 /bin/bash (bash) にフルディスクアクセスを与えていたら、もう不要。"
    echo "   2) の画面で bash を選んで「−」で外してよい"
    echo "   (オートメーションの bash → リマインダーも不要)。"
fi
echo "$RULE"
