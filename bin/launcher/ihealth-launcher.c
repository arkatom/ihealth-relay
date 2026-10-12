/*
 * ihealth-launcher: launchd の定期実行専用ランチャー。
 *
 * なぜ必要か:
 *   macOS のプライバシー保護 (TCC) は、launchd ジョブのファイルアクセスを、ジョブの
 *   ProgramArguments[0] に書かれたプログラム (責任プロセス = responsible process) に
 *   よるものとみなす。この帰属は exec() しても子プロセスに引き継がれる。bash スクリプト
 *   bin/ihealth-run を直接登録すると責任プロセスは /bin/bash になり、Health Auto Export の
 *   iCloud フォルダを読むには /bin/bash にフルディスクアクセスを与えるしかない (その許可は
 *   launchd がこのユーザーで起動するすべての bash スクリプトに及ぶ)。
 *   このバイナリを ProgramArguments[0] にすると責任プロセスはこのバイナリになるので、
 *   フルディスクアクセスはこのバイナリ 1 つに与えれば足りる。
 *
 * やること:
 *   /bin/bash <IHEALTH_RUN_PATH> を引数なしで execve() するだけ。
 *   - 実行するスクリプトは、コンパイル時に埋め込んだ IHEALTH_RUN_PATH (= <プロジェクト>/
 *     bin/ihealth-run の絶対パス) だけ。実行時の引数で別のスクリプトを選ぶ手段は作らない
 *     (許可を別のスクリプトの実行に使い回させない)。
 *   - 引数は受け付けない。launchd は引数なしで起動する (plist の ProgramArguments は
 *     このランチャー 1 つだけ)。引数を ihealth-run (→ python3 -m ihealth) に渡すと、
 *     許可を持つこのプロセスに --publisher slack --slack-webhook <URL> や
 *     --markdown-out <パス> を指定して、読んだ健康データを任意の送り先・書き込み先へ
 *     出させることができてしまう。引数付きで起動されたら何も実行せずに 64 (EX_USAGE) で
 *     終わる (黙って無視すると、--dry-run のつもりの実行が本番の書き込みになる)。
 *     手で実行するときは bin/ihealth-run を直接使う。
 *   - bash / python / dyld に別のコードを読み込ませられる環境変数 (BASH_ENV、export された
 *     関数 BASH_FUNC_*、SHELLOPTS + PS4、PYTHON*、DYLD_* など) は渡さない。どれも
 *     ihealth-run には不要 (PYTHONPATH は ihealth-run が設定し直す)。
 *   - execve() に失敗したら、標準エラー (launchd.log) に理由を書いて 126 で終わる。
 *
 * ビルドと署名は bin/install-launchd.sh が行う (cc -DIHEALTH_RUN_PATH=... → ad-hoc 署名)。
 * macOS では __RESTRICT セグメント付きでリンクし、dyld に DYLD_INSERT_LIBRARIES などを
 * 無視させる (許可を持つこのプロセスに、他のライブラリを差し込ませない)。
 * ソース・プロジェクトの場所・ビルド設定が変わったときだけ作り直し、結果が前と同じ
 * バイト列なら古いファイルを残す。中身の変わったバイナリには以前のフルディスクアクセスの
 * 許可が効かないので、そのときだけ付与し直しが必要になる。
 */
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifndef IHEALTH_RUN_PATH
#error "IHEALTH_RUN_PATH (bin/ihealth-run の絶対パスの文字列リテラル) を -D で指定してください"
#endif

/* exec するシェル。テストで exec の失敗を確かめるためだけに、コンパイル時に差し替えられる
 * (実行時に変える手段は無い)。 */
#ifndef IHEALTH_BASH_PATH
#define IHEALTH_BASH_PATH "/bin/bash"
#endif

/* シェルの「見つかったが実行できない」と同じ終了コード */
#define EXIT_CANNOT_EXECUTE 126
/* 引数付きで起動された (sysexits.h の EX_USAGE。ihealth の終了コード 0〜12 と重ならない) */
#define EXIT_USAGE 64

extern char **environ;

/* 名前が一致したら渡さない環境変数 */
static const char *const blocked_names[] = {
    "BASH_ENV",      /* 非対話の bash がスクリプトより先に読み込むファイル */
    "ENV",           /* sh 互換モードの bash が読み込むファイル */
    "SHELLOPTS",     /* xtrace などを有効にできる (PS4 の展開でコマンドが走る) */
    "BASHOPTS",
    "PS4",
    "BASH_XTRACEFD",
    "CDPATH",
    "GLOBIGNORE",
    NULL,
};

/* 名前がこれで始まったら渡さない環境変数 */
static const char *const blocked_prefixes[] = {
    "BASH_FUNC_",    /* export された bash 関数 (dirname などのコマンドを置き換えられる) */
    "PYTHON",        /* PYTHONHOME / PYTHONUSERBASE / PYTHONWARNINGS など */
    "DYLD_",         /* 動的リンカへの指示 (ライブラリの差し込み) */
    NULL,
};

/* entry ("名前=値") を子プロセスに渡さないなら 1 */
static int is_blocked(const char *entry)
{
    const char *eq = strchr(entry, '=');
    size_t name_len;
    size_t i;

    if (eq == NULL) {
        return 1; /* 「名前=値」の形でないものは渡さない */
    }
    name_len = (size_t)(eq - entry);
    for (i = 0; blocked_names[i] != NULL; i++) {
        if (strlen(blocked_names[i]) == name_len
            && strncmp(entry, blocked_names[i], name_len) == 0) {
            return 1;
        }
    }
    for (i = 0; blocked_prefixes[i] != NULL; i++) {
        size_t prefix_len = strlen(blocked_prefixes[i]);
        if (name_len >= prefix_len && strncmp(entry, blocked_prefixes[i], prefix_len) == 0) {
            return 1;
        }
    }
    /* 古い形式の関数 export (値が "() {" で始まる) */
    if (strncmp(eq + 1, "() {", 4) == 0) {
        return 1;
    }
    return 0;
}

int main(int argc, char *argv[])
{
    /* execve() の argv は char * の配列なので、書き換え可能な配列に入れて渡す (キャスト不要) */
    static char bash_path[] = IHEALTH_BASH_PATH;
    static char run_path[] = IHEALTH_RUN_PATH;
    /* bash + スクリプト + NULL。受け取った引数は渡さない (上のコメント参照) */
    char *child_argv[] = { bash_path, run_path, NULL };
    size_t n_env = 0;
    size_t i;
    size_t kept = 0;
    char **child_env;
    int saved_errno;

    (void)argv;
    if (argc > 1) {
        fprintf(stderr,
                "ihealth-launcher: 引数は受け付けません (%d 個渡されました)。launchd が引数なしで"
                "起動するためのプログラムです。手で実行するときは %s を使ってください\n",
                argc - 1, IHEALTH_RUN_PATH);
        return EXIT_USAGE;
    }

    while (environ != NULL && environ[n_env] != NULL) {
        n_env++;
    }
    child_env = calloc(n_env + 1, sizeof *child_env);
    if (child_env == NULL) {
        fprintf(stderr, "ihealth-launcher: メモリを確保できません\n");
        return EXIT_CANNOT_EXECUTE;
    }

    for (i = 0; i < n_env; i++) {
        if (!is_blocked(environ[i])) {
            child_env[kept++] = environ[i];
        }
    }
    child_env[kept] = NULL;

    execve(IHEALTH_BASH_PATH, child_argv, child_env);
    saved_errno = errno;
    fprintf(stderr, "ihealth-launcher: %s で %s を起動できません: %s\n",
            IHEALTH_BASH_PATH, IHEALTH_RUN_PATH, strerror(saved_errno));
    return EXIT_CANNOT_EXECUTE;
}
