"""桌面打包：从 git ref 生成应用目录，可复现、升级不丢用户状态。

判据不是"我看了一眼觉得对"，而是这四条能机械核对的性质：
1. 本体只来自 `git archive <ref>`，工作树里未提交的东西进不去；
2. .bat 是 ASCII + CRLF，.ps1 带 BOM —— 中文进 cmd 会被 GBK 解码成别的命令；
3. 升级（--replace）保住 app/ 里新档没有的文件（队列、证据、.env）以及面板会写的配置；
4. 半途失败不留半个 app。

测试用一个自己造的临时 git 仓库，不碰真仓库、也不依赖 HEAD 里有没有某个文件。
"""
from __future__ import annotations

import io
import struct
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import make_desktop_app as mda      # noqa: E402


def sh(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture()
def fake_repo(tmp_path: Path) -> Path:
    """一个最小可打包的仓库：workbench 在位、VERSION 在、启动器在、有 .gitignore。"""
    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    (repo / "config").mkdir()
    (repo / "runtime_scheduler").mkdir()
    (repo / ".gitignore").write_text("/runtime_*/\n.env\n", encoding="utf-8")
    (repo / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    (repo / "tools" / "workbench.py").write_text("print('panel')\n", encoding="utf-8")
    (repo / "tools" / "desktop_launcher.py").write_text(
        "'''launcher'''\nprint('desktop')\n", encoding="utf-8")
    (repo / "config" / "agents.yaml").write_text(
        "supervisor:\n  profile: codex-high   # 注释要保住\n", encoding="utf-8")
    (repo / "runtime_scheduler" / "should_not_ship.db").write_text("x", encoding="utf-8")
    sh(repo, "init", "-q")
    sh(repo, "add", "-A")
    sh(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return repo


def build(repo: Path, dest: Path, **kw):
    kw.setdefault("python_exe", str(sys.executable))
    return mda.build(ref="HEAD", dest=dest, root=repo, quiet=True, **kw)


class TestArchiveIsTheSource:
    def test_uncommitted_work_tree_changes_do_not_ship(self, fake_repo, tmp_path):
        (fake_repo / "tools" / "workbench.py").write_text(
            "print('in-progress edit by someone else')\n", encoding="utf-8")
        dest = tmp_path / "desk"
        build(fake_repo, dest)
        shipped = (dest / "app" / "tools" / "workbench.py").read_text(encoding="utf-8")
        assert shipped == "print('panel')\n"

    def test_gitignored_files_do_not_ship(self, fake_repo, tmp_path):
        (fake_repo / ".env").write_text("CLAUDE_CLI_PATH=secret\n", encoding="utf-8")
        dest = tmp_path / "desk"
        build(fake_repo, dest)
        assert not (dest / "app" / ".env").exists()
        assert not (dest / "app" / "runtime_scheduler" / "should_not_ship.db").exists()

    def test_ref_without_launcher_refuses_instead_of_building_a_dead_app(
            self, fake_repo, tmp_path):
        sh(fake_repo, "rm", "-q", "tools/desktop_launcher.py")
        sh(fake_repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
           "-m", "drop launcher")
        with pytest.raises(mda.BuildError) as exc:
            build(fake_repo, tmp_path / "desk")
        assert "装不成桌面版" in str(exc.value)
        assert not (tmp_path / "desk" / "app").exists()

    def test_existing_install_needs_an_explicit_replace(self, fake_repo, tmp_path):
        dest = tmp_path / "desk"
        build(fake_repo, dest)
        with pytest.raises(mda.BuildError) as exc:
            build(fake_repo, dest)
        assert "--replace" in str(exc.value)


class TestWrapperEncoding:
    def test_bats_are_ascii_with_crlf_and_start_the_launcher(self, fake_repo, tmp_path):
        dest = tmp_path / "desk"
        build(fake_repo, dest)
        real = (dest / "start-mao.bat").read_bytes()
        mock = (dest / "start-mao-mock.bat").read_bytes()
        assert real.isascii() and mock.isascii()
        assert b"\r\n" in real and b"\n" not in real.replace(b"\r\n", b"")
        assert b'MAO-Desktop.py"\r\n' in real
        assert b'MAO-Desktop.py" mock\r\n' in mock
        assert b"PYDIR=" in real

    def test_bat_does_not_spend_quota_silently(self, fake_repo, tmp_path):
        dest = tmp_path / "desk"
        build(fake_repo, dest)
        real = (dest / "start-mao.bat").read_text(encoding="ascii")
        assert "does NOT spend quota" in real
        # 花钱那一步的**名字**必须写对。以前这里锁 "start scheduler"，可是 v1.9 之后
        # 点任务页那颗『开始』就已经花掉一次（验收 agent 现场切分），不必等到启动调度器。
        # 外壳上写一个界面上不存在的按钮，等于把人往错的那一格引。
        assert '"start" button' in real and "split" in real

    def test_ps1_carries_a_bom_so_powershell_5_1_stops_guessing(self, fake_repo,
                                                                tmp_path):
        dest = tmp_path / "desk"
        build(fake_repo, dest, shortcuts=True)
        for name in ("close-window.ps1", "install-shortcuts.ps1"):
            head = (dest / name).read_bytes()[:3]
            assert head == b"\xef\xbb\xbf", f"{name} 没有 BOM，PS5.1 会按 ANSI 读"
        ps = (dest / "install-shortcuts.ps1").read_text(encoding="utf-8-sig")
        assert "MAO 工作台（真实档）" in ps
        assert '`"' in ps, "路径里有空格时未加引号会断"

    def test_launcher_written_verbatim_from_the_ref(self, fake_repo, tmp_path):
        dest = tmp_path / "desk"
        build(fake_repo, dest)
        assert (dest / "MAO-Desktop.py").read_text(
            encoding="utf-8") == "'''launcher'''\nprint('desktop')\n"


class TestDesktopFile:
    """业主说"封装为桌面文件"：桌面上要的是**一个**双击就开的东西。"""

    def test_the_desktop_entry_uses_the_shell_folder_not_a_guessed_path(self):
        ps = mda.shortcuts_ps1(Path("C:/x/MAO-Workbench"), "C:/py/pythonw.exe",
                               "9.9.9", desktop=True)
        assert 'GetFolderPath("Desktop")' in ps        # OneDrive 重定向过的桌面也认得
        assert "MAO 工作台.lnk" in ps
        assert "不花额度" in ps
        assert "{real}" not in ps and "{dest}" not in ps and "{version}" not in ps

    def test_desktop_entry_is_only_the_real_tier(self):
        """两条同名不同档的条目放桌面上就是第二个重复入口 —— 演示档留在开始菜单与 .bat。"""
        ps = mda.shortcuts_ps1(Path("C:/x/MAO-Workbench"), "C:/py/pythonw.exe",
                               "9.9.9", desktop=True)
        desktop_block = ps.split('GetFolderPath("Desktop")')[1]
        assert "mock" not in desktop_block

    def test_no_desktop_entry_when_not_asked(self):
        ps = mda.shortcuts_ps1(Path("C:/x/MAO-Workbench"), "C:/py/pythonw.exe",
                               "9.9.9")
        assert 'GetFolderPath("Desktop")' not in ps

    def test_generating_is_not_installing(self, fake_repo, tmp_path, monkeypatch):
        """带 --desktop-file 但不能碰真人桌面/开始菜单：只有 --install-shortcuts 才执行。

        这条边界是硬的：测试自己就带 desktop_file 跑，如果生成即执行，
        跑一次测试往用户桌面写一个 .lnk，没人会看见，也没人会想到要删。
        """
        seen = []
        real_run = mda.subprocess.run

        def spy(args, **kw):
            if str(args[0]).lower().startswith("powershell"):
                seen.append(list(args))
                return subprocess.CompletedProcess(args, 0, "", "")
            return real_run(args, **kw)

        monkeypatch.setattr(mda.subprocess, "run", spy)
        build(fake_repo, tmp_path / "a", desktop_file=True)
        assert seen == []
        build(fake_repo, tmp_path / "b", install=True)
        assert len(seen) == 1


class TestAppIcon:
    """桌面文件的图标必须是自己的设计，不是浏览器/解释器的通用快捷方式图标。"""

    @staticmethod
    def _pixels(png: bytes, size: int):
        import struct
        import zlib
        idat = png.split(b"IDAT")[1].split(b"IEND")[0][:-4]   # 去掉尾部 CRC
        raw = zlib.decompress(idat)
        stride = size * 4
        assert raw[0] == 0, "第一行的 filter 字节应是 0（测试按未压缩行解析）"
        return raw, stride

    def test_png_is_a_real_png_at_the_requested_size(self):
        size = 48
        png = mda.render_logo_png(size)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        ihdr = png.split(b"IHDR")[1][:13]
        w, h = struct.unpack(">II", ihdr[:8])
        depth, ctype = ihdr[8], ihdr[9]
        assert (w, h, depth, ctype) == (size, size, 8, 6)   # 8bit RGBA

    def test_the_three_role_nodes_are_the_brand_colors(self):
        size = 64
        raw, stride = self._pixels(mda.render_logo_png(size), size)
        def at(fx, fy):
            px, py = int(fx * size / 24), int(fy * size / 24)
            off = py * (stride + 1) + 1 + px * 4      # 每行前面还有一个 filter 字节
            return tuple(raw[off:off + 4])
        assert at(12, 4.4)[:3] == (0x25, 0x63, 0xEB)        # 验收
        assert at(4.9, 17.4)[:3] == (0x0F, 0x76, 0x6E)      # 执行
        assert at(19.1, 17.4)[:3] == (0xB4, 0x53, 0x09)     # 评审
        assert at(1, 1)[3] == 0, "角落必须是透明的，否则桌面上是一块白底"

    def test_ico_container_is_valid(self):
        png = mda.render_logo_png(64)
        ico = mda.wrap_ico(png, 64)
        reserved, kind, count = struct.unpack("<HHH", ico[:6])
        assert (reserved, kind, count) == (0, 1, 1)
        entry = ico[6:22]
        assert entry[0] == entry[1] == 64                   # 宽高（像素）
        size, offset = struct.unpack("<II", entry[8:16])
        assert (size, offset) == (len(png), 22)
        assert ico[offset:] == png, "ICO 内嵌的就是那份 PNG（Vista+ 允许）"

    def test_shortcuts_use_the_icon_when_given_and_shell_icon_otherwise(self):
        with_icon = mda.shortcuts_ps1(Path("C:/x"), "C:/p/pythonw.exe", "1.0",
                                      desktop=True, icon=r"C:\x\mao.ico,0")
        without = mda.shortcuts_ps1(Path("C:/x"), "C:/p/pythonw.exe", "1.0",
                                    desktop=True)
        assert with_icon.count(r"C:\x\mao.ico,0") == 2      # 脚本里两处（foreach 那条 + 桌面那条）
        assert "shell32.dll,220" not in with_icon
        assert "shell32.dll,220" in without

    def test_icon_failure_degrades_to_the_generic_icon_instead_of_aborting(
            self, tmp_path, monkeypatch):
        real = mda.Path

        class Boom(real):
            def write_bytes(self, data):
                raise OSError("disk full")

        monkeypatch.setattr(mda, "Path", Boom)
        assert mda.write_app_icon(Boom(str(tmp_path / "x"))) == ""


class TestUpgradeKeepsUserState:
    def test_a_release_file_that_moved_upstream_is_not_resurrected(self, fake_repo,
                                                                   tmp_path):
        """真出过一次：`git mv` 把历史报告移进 docs/history/ 之后，
        升级规则把旧 app 里那 18 份又搬回顶层 —— "新档里没有"不等于"用户产生的"。"""
        dest = tmp_path / "desk"
        build(fake_repo, dest)
        (dest / "app" / "PHASE2_REPORT.md").write_text("发布内容，不是用户数据\n",
                                                       encoding="utf-8")
        (dest / "app" / "runtime_batch").mkdir()
        (dest / "app" / "runtime_batch" / "x.json").write_text("{}", encoding="utf-8")
        manifest = build(fake_repo, dest, replace=True)
        assert not (dest / "app" / "PHASE2_REPORT.md").exists()
        assert (dest / "app" / "runtime_batch" / "x.json").is_file()
        carried = {p for p, _ in manifest["preserved"]}
        assert "runtime_batch/x.json" in carried
        assert "PHASE2_REPORT.md" not in carried


    def test_replace_preserves_queue_env_and_wiring(self, fake_repo, tmp_path):
        dest = tmp_path / "desk"
        build(fake_repo, dest)
        app = dest / "app"
        (app / ".env").write_text("CLAUDE_CLI_PATH=C:\\some\\claude.exe\n",
                                  encoding="utf-8")
        (app / "config" / "agents.yaml").write_text(
            "supervisor:\n  profile: claude-max   # 我在面板里换的\n", encoding="utf-8")
        (app / "runtime_scheduler").mkdir(parents=True, exist_ok=True)
        (app / "runtime_scheduler" / "queue.db").write_text("队列与交付现场",
                                                            encoding="utf-8")

        manifest = build(fake_repo, dest, replace=True)
        preserved = dict(manifest["preserved"])
        assert (app / ".env").read_text(encoding="utf-8").startswith("CLAUDE_CLI_PATH=")
        assert "我在面板里换的" in (app / "config" / "agents.yaml").read_text(
            encoding="utf-8")
        assert (app / "runtime_scheduler" / "queue.db").read_text(
            encoding="utf-8") == "队列与交付现场"
        assert set(preserved) >= {".env", "config/agents.yaml",
                                  "runtime_scheduler/queue.db"}
        assert not any(p.name.startswith(".app-previous") for p in dest.iterdir())

    def test_tracked_release_files_still_come_from_the_ref(self, fake_repo, tmp_path):
        dest = tmp_path / "desk"
        build(fake_repo, dest)
        (dest / "app" / "VERSION").write_text("旧的\n", encoding="utf-8")
        build(fake_repo, dest, replace=True)
        assert (dest / "app" / "VERSION").read_text(encoding="utf-8") == "9.9.9\n"

    def test_bytecode_is_not_user_state(self, fake_repo, tmp_path):
        """实跑里第一条"升级保住"是 __pycache__ 里的 .pyc，把该看的行挤没了。"""
        dest = tmp_path / "desk"
        build(fake_repo, dest)
        stale = dest / "app" / "tools" / "__pycache__" / "workbench.cpython-313.pyc"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"stale")
        manifest = build(fake_repo, dest, replace=True)
        assert not stale.exists()
        assert not any("__pycache__" in p for p, _ in manifest["preserved"])


class TestTarSafety:
    def _tar(self, **members: bytes) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return buf.getvalue()

    def test_paths_that_escape_are_refused(self, tmp_path):
        with pytest.raises(mda.BuildError):
            mda.extract_tar(self._tar(**{"../evil.txt": b"x"}), tmp_path / "app")
        with pytest.raises(mda.BuildError):
            mda.extract_tar(self._tar(**{"/abs/evil.txt": b"x"}), tmp_path / "app")

    def test_links_are_refused(self, tmp_path):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo("payload")
            info.type = tarfile.SYMTYPE
            info.linkname = "C:/Windows"
            tar.addfile(info)
        with pytest.raises(mda.BuildError):
            mda.extract_tar(buf.getvalue(), tmp_path / "app")

    def test_bytes_survive_byte_for_byte(self, tmp_path):
        payload = b"line one\nline two\n"      # 发布档就是 LF，不许被换行策略改动
        mda.extract_tar(self._tar(**{"a/b.py": payload}), tmp_path / "app")
        assert (tmp_path / "app" / "a" / "b.py").read_bytes() == payload


class TestReadmeTellsTheTruth:
    def test_readme_names_the_ref_and_denies_being_a_new_gui(self, fake_repo, tmp_path):
        dest = tmp_path / "desk"
        build(fake_repo, dest)
        text = (dest / "README.txt").read_text(encoding="utf-8")
        assert "9.9.9" in text
        assert "不是另外" in text                      # 不假装是新做的界面
        assert "不花额度" in text
        assert "不用 API key" in text
        assert "127.0.0.1:8765" in text

    def test_readme_describes_the_flow_that_the_software_actually_has(
            self, fake_repo, tmp_path):
        """桌面版第一屏读到的那段话必须与 v1.9 的行为一致。

        它一直写着"每格出补丁后停下等你人工核查；你说「合」才合入" —— 那句话从
        v1.8 起就是错的，而业主正是照着它找不到下一步的人。
        """
        text = (build_readme(fake_repo, tmp_path)).read_text(encoding="utf-8")
        assert "停下等你人工核查" not in text
        assert "中间不问你" in text
        assert "只填两格" in text and "落地目录" in text
        assert "建仓库并开工" in text, "落地目录不必自己 git init 这件事要写在第一屏"
        assert "用哪个 agent" in text and "codex login" in text
        assert "DELIVERY.md" in text

    def test_readme_carries_no_invented_metric(self, fake_repo, tmp_path):
        text = (build_readme(fake_repo, tmp_path)
                ).read_text(encoding="utf-8")
        for banned in ("Active 3", "98%", "5/5 tests", "Online", "T-1040"):
            assert banned not in text

    def test_readme_points_at_the_reproducible_command(self, fake_repo, tmp_path):
        text = build_readme(fake_repo, tmp_path).read_text(encoding="utf-8")
        assert "make_desktop_app.py" in text and "--replace" in text


def build_readme(repo: Path, tmp_path: Path) -> Path:
    dest = tmp_path / "desk-r"
    build(repo, dest)
    return dest / "README.txt"


class TestDesktopLauncherLayout:
    """启动器在两种摆放位置都要能找到本体 —— 装机形态是拷出去的那一份。"""

    def test_finds_repo_root_when_run_in_place(self, tmp_path):
        f = tmp_path / "tools" / "desktop_launcher.py"
        f.parent.mkdir(parents=True)
        f.write_text("", encoding="utf-8")
        (tmp_path / "tools" / "workbench.py").write_text("", encoding="utf-8")
        from tools import desktop_launcher as dl
        assert dl.app_dir(f) == tmp_path

    def test_finds_app_folder_when_copied_out(self, tmp_path):
        entry = tmp_path / "MAO-Desktop.py"
        entry.write_text("", encoding="utf-8")
        (tmp_path / "app" / "tools").mkdir(parents=True)
        (tmp_path / "app" / "tools" / "workbench.py").write_text("", encoding="utf-8")
        from tools import desktop_launcher as dl
        assert dl.app_dir(entry) == tmp_path / "app"

    def test_missing_body_is_a_clear_error_not_a_traceback(self, tmp_path):
        entry = tmp_path / "whatever.py"
        entry.write_text("", encoding="utf-8")
        from tools import desktop_launcher as dl
        with pytest.raises(SystemExit) as exc:
            dl.app_dir(entry)
        assert "tools/workbench.py" in str(exc.value)

    def test_port_flag_parsed(self):
        from tools import desktop_launcher as dl
        assert dl.port_of([]) == dl.PORT
        assert dl.port_of(["--port", "8790"]) == 8790
        assert dl.port_of(["--port=8791"]) == 8791
        assert dl.port_of(["--port", "not-a-number"]) == dl.PORT

    def test_the_parsed_port_reaches_the_server_process(self, tmp_path,
                                                       monkeypatch):
        """解析出来不等于传到了 —— 差这一段时，`--port` 是一个自毁的开关。

        服务会起在默认 8765，启动器等的是 8770，25 秒后超时，然后把**已经好好
        跑着的那个服务** terminate 掉，报"工作台没能在 25 秒内起来"。
        上面那条测试只测 `port_of`，所以它一直是绿的：判据要落在被传下去的参数上。
        """
        from tools import desktop_launcher as dl
        cmd = dl.workbench_cmd(False, tmp_path, 8790)
        assert "--port" in cmd and cmd[cmd.index("--port") + 1] == "8790"
        assert dl.workbench_cmd(True, tmp_path, 8790)[-1] == "--mock"
        seen = {}

        def fake_popen(argv, **kw):
            seen["argv"] = list(argv)
            raise OSError("别真起进程")

        monkeypatch.setattr(dl.subprocess, "Popen", fake_popen)
        with pytest.raises(OSError):
            dl.start_server(False, tmp_path, tmp_path / "x.log", 8790)
        assert seen["argv"][seen["argv"].index("--port") + 1] == "8790"
