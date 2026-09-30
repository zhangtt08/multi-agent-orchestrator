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

    def test_the_tasks_page_says_it_is_a_rehearsal_not_a_cost(self, made):
        from types import SimpleNamespace

        import tools.workbench_ui as ui
        ctx = SimpleNamespace(config_dir=made["config_dir"], real_roles=True,
                              default_strategy="GIT_WORKTREE", last_form={},
                              last_go={}, go_init_hint={},
                              runner=_runner(made["config_dir"]),
                              forced_workspace=made["workspace"])
        page = ui.tasks(ctx)
        assert "彩排档" in page and "一分钱额度都不花" in page
        assert "不会按你这句话做" in page,             "彩排档演的是**路**，不是业主的内容；不写清就会让人以为假 agent 做了他那句话"
        assert "真实档那一下会花额度" not in page
        assert "绝不该被碰" not in page

    def test_a_plain_panel_still_says_the_cost(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        import tools.delivery_view as dv
        import tools.workbench_ui as ui
        cfg = tmp_path / "config"
        cfg.mkdir()
        (cfg / "settings.yaml").write_text(
            "runtime_dir: runtime\nscheduler:\n  enabled: true\n"
            "  db_path: ./runtime_scheduler/queue.db\n  attempts_root: runtime\n"
            "checkpoint:\n  enabled: true\n", encoding="utf-8")
        (cfg / "agents.yaml").write_text(
            "supervisor:\n  provider: mock_supervisor\n", encoding="utf-8")
        (cfg / "harness.yaml").write_text("# 空\n", encoding="utf-8")
        monkeypatch.setattr(ui, "ROOTISH", tmp_path)
        monkeypatch.setattr(dv, "ROOT", tmp_path)
        ctx = SimpleNamespace(config_dir=str(cfg), real_roles=False,
                              default_strategy="COPY", last_form={}, last_go={},
                              go_init_hint={}, runner=_runner(str(cfg)),
                              forced_workspace="")
        page = ui.tasks(ctx)
        assert "彩排档" not in page            # 没开 --rehearsal 就不许自称在彩排
        assert "mao-rehearsal" not in page


class TestTheFreeShortcutPointsAtIt:
    def test_the_launcher_passes_the_rehearsal_flag(self, tmp_path):
        from tools import desktop_launcher as dl

        cmd = dl.workbench_cmd(True, tmp_path, 8765, True)
        assert "--rehearsal" in cmd and "--mock" not in cmd
        assert "--port" in cmd
        assert dl.workbench_cmd(True, tmp_path, 8765, False)[-1] == "--mock"

    def test_the_generated_batch_file_describes_what_it_now_does(self):
        from tools.make_desktop_app import bat

        text = bat("C:\\py", mock=True)
        assert "rehearsal" in text.lower() and "no subscription quota" in text
        assert text.isascii(), ".bat 含中文会被 cmd 按 GBK 解码成别的命令"
