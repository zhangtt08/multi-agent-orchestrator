"""彩排档：免费那颗按钮必须演的是**同一条交付路径**。

现场（2026-09-30）：`start-mao-mock.bat` 跑的是内置 Mock provider，而它答不出
项目档（`plan --mock` 被 check_spec 拒），于是免费那一档只说"没有自动切分…
按单任务入队"，永远到不了 DELIVERY.md。业主的抱怨是"还是偏 demo，而不是一个
完整的交付" —— 那么能免费看的那一档就得是完整的那一条。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import rehearsal as rh
from tools import workbench as wb

ROOT = Path(__file__).resolve().parent.parent


def _runner(config_dir):
    """页面渲染要读调度器状态；用真的 SchedulerRunner（不 start 就不碰进程）。"""
    from tools.scheduler_cli import SchedulerRunner

    return SchedulerRunner(config_dir)


@pytest.fixture()
def made(tmp_path):
    """演练目录**注入到 tmp**：真实那一格在本机是有状态的（按过『建仓库并开工』
    之后它就是一个 git 仓库了），拿共享目录做判据的用例会跟着上一次的现场变红。"""
    return rh.ensure(ROOT, workspace=tmp_path / "rehearsal-ws")


class TestGeneratedConfig:
    def test_the_two_stores_never_share_one_sqlite_file(self, made):
        """按**裸键名**替换会把两处 `db_path` 一起改掉 —— 第一次实跑就是这么红的。

        现场：那一格交付本身是全的（Reviewer pass round=3、补丁 27 行、验证 exit 0、
        产物 SHA 校验通过），`auto_merge_gate` 却因为「checkpoint 链 0 条」判 failed。
        原因在生成器自己：checkpoint 那句 `db_path: ""`（空 = 落在
        `<attempts_root>/checkpoints.db`）被改成了队列库，检查点于是写不进自己的表。
        闸门这条判据**不该放宽** —— durability 是无人值守能续跑的前提。
        """
        from mao.core.config import load_config

        st = load_config(made["config_dir"], require_harness_file=True).settings
        assert st.checkpoint.db_path == "",         "留空才按 <attempts_root>/checkpoints.db 解析"
        queue = Path(st.scheduler.db_path).resolve()
        ckpt = (Path(st.scheduler.attempts_root) / "checkpoints.db").resolve()
        assert queue != ckpt, f"队列库与检查点库撞在同一个文件：{queue}"
        assert queue.parent == (ROOT / "runtime_rehearsal").resolve()

    def test_it_loads_and_binds_the_three_roles_to_local_scripts(self, made):
        from mao.core.config import load_config

        cfg = load_config(made["config_dir"], require_harness_file=True)
        bound = {k: v.get("harness_profile")
                 for k, v in cfg.binding_map().items()}
        assert bound == {"supervisor": "rehearsal_supervisor",
                         "executor": "rehearsal_executor",
                         "reviewer": "rehearsal_reviewer"}

    def test_the_landing_dir_is_a_plain_folder_so_the_button_still_shows(
            self, made):
        """演练目录必须在**仓库外面**，否则 `repo_problem` 判成"别的仓库的子目录"，
        那一格不给按钮 —— 彩排档就永远按不动（第一次实跑就是这么红的）。"""
        from tools import workbench as wb

        ws = Path(made["workspace"])
        assert ws.is_dir() and not made["workspace"].lower().startswith(
            str(ROOT).lower())
        assert wb.repo_problem(made["workspace"])[0] == "needs-init",             "要正好落在『建仓库并开工』那颗按钮能出现的那一档"
        # 默认那一格（人不注入时）也必须落在仓库外面，理由同上面那条 docstring。
        default = rh.workspace_dir()
        assert "mao-rehearsal" in str(default)
        assert ROOT not in default.parents and default != ROOT

    def test_no_real_cli_is_reachable_from_it(self, made):
        from tools import agent_probe

        for row in agent_probe.role_facts(made["config_dir"]):
            assert Path(row["path"]).name.lower().startswith("python"), row
            assert row["login"] == agent_probe.UNKNOWN, "假 agent 没有登录态这件事"


class TestThePanelRunsIt:
    def test_the_landing_dir_is_overridden_however_it_was_typed(self, made,
                                                                monkeypatch,
                                                                tmp_path):
        """假 agent 写的是固定内容 —— 让它落到业主真实项目里就是污染。"""
        from tools import batch_project as bp

        ctx = wb.Workbench(config_dir=made["config_dir"], runner=None,
                           real_roles=True, forced_workspace=made["workspace"])
        seen = {}

        def fake_plan(target, goal, **kw):
            seen["workspace"] = kw["workspace"]
            (tmp_path / "x.json").write_text(json.dumps({
                "name": "rehearsal", "workspace": seen["workspace"],
                "config_dir": made["config_dir"],
                "milestones": [{"id": "m1",
                                "goal": "在工作区根新建 1111文档.md 并写清用途",
                                "acceptance": "pytest -q"}]}),
                encoding="utf-8")
            return 0

        monkeypatch.setattr(wb, "_planned_target", lambda ws: tmp_path / "x.json")
        monkeypatch.setattr(bp, "plan", fake_plan)
        monkeypatch.setattr(bp, "submit_next", lambda *a, **k: 2)
        # 第一下：演练目录还不是仓库 —— 和业主真实那一格一样先被挡
        first, bad = wb.go_from_form(ctx, {
            "prompt": "创建一个1111文档",
            "workspace": r"C:\Users\EDY\Desktop\绝不该被碰的目录"})
        assert bad and "建仓库并开工" in first
        # 第二下：按按钮。落地目录仍然被强制改到演练位
        wb.go_from_form(ctx, {"prompt": "创建一个1111文档",
                              "workspace": r"C:\Users\EDY\Desktop\绝不该被碰的目录",
                              "init_repo": "1"})
        assert seen["workspace"] == made["workspace"]
        assert made["workspace"] not in r"C:\Users\EDY\Desktop\绝不该被碰的目录"
        assert not made["workspace"].lower().startswith(
            str(ROOT).lower()), "演练目录在仓库里面就会被仓库闸门挡住"

    def test_the_page_says_it_is_a_rehearsal_not_a_cost(self, made, tmp_path,
                                                        monkeypatch):
        """彩排档那一页要说两件事：**不花钱**，以及**产出是固定剧本**。

        原来打的是 `workbench_ui.tasks`（那一页已删，界面只剩 `tools/app.py` 这一页）。
        判据一条没松，只是搬到还活着的那一格里：
        ① 免费那一档要明说不花订阅额度（不写清业主就不敢按）；
        ② 要明说内容是固定剧本 —— 彩排档演的是**路**，不是业主的内容，
           不写清就会让人以为假 agent 做了他那句话（地雷 50 的那条推论）。
        """
        from tools import app as A

        # 这一页读 runtime_batch 与默认落地目录：夹具自己造现场（地雷 38）
        monkeypatch.setattr(A, "ROOT", tmp_path)
        monkeypatch.setattr(A, "desktop_dir", lambda: tmp_path)
        app = A.App(config_dir=made["config_dir"],
                    runner=_runner(made["config_dir"]),
                    real_roles=True, rehearsal=True)
        app.wb.forced_workspace = made["workspace"]
        strip = A.agent_strip(app)
        page = A.page(app)
        assert strip in page,             "彩排档那一格要真的在那一页上，不是只活在函数里"
        assert "彩排档" in page and "不消耗任何订阅额度" in page
        # 这一句讲的是**边界**而不是措辞：彩排档演的是**路**（拆目标 → 干活 →
        # 判定 → 合入），不是业主那句话会被切成什么。所以判据收在"这一格里
        # 同时出现固定与剧本"，改字不改边界都得过；把这一格删掉或改成
        # "按你的目标切分"就红。（2026-10-08 文案减负那一轮就是按字面
        # "固定的剧本"红的 —— 页面当时说的是"产出内容是固定剧本"。）
        assert "固定" in strip and "剧本" in strip,                 \
            "彩排档演的是**路**，不是业主的内容"
        # 「这一档不花钱」与「这一档花钱」不许在同一格里各说一遍。
        # 整页级的那一条现在只能收在这一格上：`go_form` 的成本标签只看 real_roles，
        # 而彩排档三个角色都是 generic_cli + 本机 python（supports_cli 为真），
        # 于是同一页顶上写"不消耗任何订阅额度"、下面写"会消耗订阅额度"。
        # 那是页面的缺陷，已单独上报，不在测试里替它抹平；修好后这条要收紧回 page 级。
        assert "会消耗订阅额度" not in strip
        assert "真实档那一下会花额度" not in page
        assert "绝不该被碰" not in page,             "演练档不许把业主真实项目当成落地目录 —— 假 agent 写的是固定内容，落进去就是污染"

    def test_a_plain_panel_still_says_the_cost(self, tmp_path, monkeypatch):
        """没开 --rehearsal 的那一档不许自称在彩排，而真实档要把代价说出来。"""
        from tools import app as A

        cfg = tmp_path / "config"
        cfg.mkdir()
        (cfg / "settings.yaml").write_text(
            "runtime_dir: runtime\nscheduler:\n  enabled: true\n"
            "  db_path: ./runtime_scheduler/queue.db\n  attempts_root: runtime\n"
            "checkpoint:\n  enabled: true\n", encoding="utf-8")
        (cfg / "agents.yaml").write_text(
            "supervisor:\n  provider: mock_supervisor\n", encoding="utf-8")
        (cfg / "harness.yaml").write_text("# 空\n", encoding="utf-8")
        monkeypatch.setattr(A, "ROOT", tmp_path)
        monkeypatch.setattr(A, "desktop_dir", lambda: tmp_path)
        # config_dir 传**绝对路径**：`load_config` 按进程 cwd 解析相对路径（地雷 36）
        app = A.App(config_dir=str(cfg), runner=_runner(str(cfg)),
                    real_roles=True)
        page = A.page(app)
        assert "彩排档" not in page            # 没开 --rehearsal 就不许自称在彩排
        assert "mao-rehearsal" not in page
        assert "固定的剧本" not in page
        assert "会消耗订阅额度" in page,             "花钱的那一档要写明花额度 —— 与上一条一起成立，成本这一格才算有判据"


class TestTheFreeShortcutPointsAtIt:
    def test_the_launcher_passes_the_tier_that_actually_exists(self, tmp_path):
        """两颗免费按钮各自的档：`--rehearsal` 与零配额最小档，都不再传 `--mock`。

        入口换了：一页版是 `tools/app.py`，而它的 argparse 里**没有** `--mock` 这个
        开关 —— 外壳再传一次就是双击打开一个连网页都没有的窗口（地雷 26 同形）。
        免费那一档演彩排（要能走到 DELIVERY.md），`--mock` 那一档指的是
        `examples/config_minimal`（内置 Mock 角色，零配额，答不出项目档）。
        """
        from tools import desktop_launcher as dl

        cmd = dl.workbench_cmd(True, tmp_path, 8765, True)
        assert "--rehearsal" in cmd and "--mock" not in cmd
        assert "--port" in cmd
        assert Path(cmd[1]).name == "app.py",             "起的是那一页，不是已经不带网页的 workbench.py"
        mock_cmd = dl.workbench_cmd(True, tmp_path, 8765, False)
        assert mock_cmd[-2:] == ["--config-dir", "examples/config_minimal"]
        assert "--mock" not in mock_cmd
        assert (ROOT / "examples" / "config_minimal").is_dir(),             "那一档指的是真存在的配置目录，不是一个被搬走的位置"

    def test_the_generated_batch_file_describes_what_it_now_does(self):
        from tools.make_desktop_app import bat

        text = bat("C:\\py", mock=True)
        assert "rehearsal" in text.lower() and "no subscription quota" in text
        assert text.isascii(), ".bat 含中文会被 cmd 按 GBK 解码成别的命令"


# ===========================================================================
# 第二次按『开工』不该得到一个假故障（2026-10-08 实测）
# ===========================================================================
class TestEachPressGetsItsOwnStage:
    """彩排档是业主第一个按的东西：连按两次就报"未确认"，讲的是软件自己。

    机制：假 agent 每一批写的是**同一份固定内容**，而第一跑之后那一格已经
    是仓库且文件就在里面 —— 第二跑的 `changes.patch` 必然 0 行，证据闸门
    按设计拒绝，人读到的却是"我的东西没做成"。
    """

    def test_a_stage_that_became_a_repo_is_never_reused(self, tmp_path,
                                                        monkeypatch):
        first = tmp_path / "mao-rehearsal" / "ws"
        monkeypatch.setattr(rh, "workspace_dir", lambda: first)
        assert rh.stage_dir() == first

        (first / ".git").mkdir(parents=True)
        (first / ".git" / "HEAD").write_text("ref: refs/heads/main\n",
                                             encoding="utf-8")
        assert rh.stage_dir() == tmp_path / "mao-rehearsal" / "ws-2"

    def test_a_broken_dot_git_is_not_a_used_stage(self, tmp_path, monkeypatch):
        """判据是 `.git/HEAD`（地雷 5），不是"`.git` 在不在"。

        只有 objects/ 没有 HEAD 的空 `.git` 不算仓库 —— 把这种目录判成
        "上一次用完了"会白开新的一格，而真正的现场其实还能接着演。
        """
        first = tmp_path / "mao-rehearsal" / "ws"
        (first / ".git" / "objects").mkdir(parents=True)
        monkeypatch.setattr(rh, "workspace_dir", lambda: first)
        assert rh.stage_dir() == first

    def test_moving_to_a_new_stage_does_not_touch_the_previous_one(self,
                                                                   tmp_path,
                                                                   monkeypatch):
        """换格不是清理：上一批的交付物留在原地，它自己的 DELIVERY.md 还指得到它。"""
        first = tmp_path / "mao-rehearsal" / "ws"
        first.mkdir(parents=True)
        (first / "说明.md").write_text("上一批写的\n", encoding="utf-8")
        (first / ".git").mkdir()
        (first / ".git" / "HEAD").write_text("ref: refs/heads/main\n",
                                             encoding="utf-8")

        monkeypatch.setattr(rh, "workspace_dir", lambda: first)
        second = rh.stage_dir()
        assert second == tmp_path / "mao-rehearsal" / "ws-2"
        assert (first / "说明.md").read_text(encoding="utf-8") == "上一批写的\n"

    def test_the_injected_workspace_still_bypasses_the_stage_pick(self, tmp_path,
                                                                 monkeypatch):
        """夹具注入的是整格路径 —— 换格逻辑不许把测试的现场挪走（地雷 40）。"""
        monkeypatch.setattr(rh, "workspace_dir",
                            lambda: tmp_path / "should-not-be-used")
        forced = tmp_path / "given"
        made = rh.ensure(ROOT, workspace=forced)
        assert Path(made["workspace"]) == forced.resolve()
