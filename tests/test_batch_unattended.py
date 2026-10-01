"""无人值守那一路的守卫：合入不再等人，但判据一条都不许少。

业主把"验收的人"换成了 Agent，所以这一层要锁的是两件事：
1. 证据齐 → 不问人就合入并继续下一格（老形状跑一格要人敲一次 run，是 demo）；
2. 证据缺任何一条 → 判失败、列出原因、停下，**绝不因为没人回答就放过**。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools import batch_project as bp
from tools import delivery_view as dv

PATCH_BODY = ("--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n"
              " def multiply(a, b):\n"
              "-    return a + b\n+    return a * b\n")


def git(path, *args):
    return subprocess.run(["git", *args], cwd=str(path), check=True,
                          capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q", ".")
    git(tmp_path, "config", "user.name", "tester")
    git(tmp_path, "config", "user.email", "tester@example.com")
    (tmp_path / "calc.py").write_text("def multiply(a, b):\n    return a + b\n",
                                      encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "init")
    monkeypatch.setattr(bp, "STATE_DIR", tmp_path / "runtime_batch")
    return tmp_path


def make_spec(repo, milestones, **over):
    spec = {"name": "unattended", "workspace": str(repo), "strategy": "COPY",
            "milestones": milestones}
    spec.update(over)
    p = repo / "project.json"
    p.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    return bp.load_spec(p)


def one_cell_ready(repo, mid="m1", mode=None):
    """一格跑完、补丁与哈希都在现场的状态 —— 与人点头前那一刻完全同形。"""
    milestones = [{"id": mid, "goal": "把 multiply 改成返回 a * b",
                   "acceptance": "pytest test_calc.py -q"}]
    spec = make_spec(repo, milestones, **({"mode": mode} if mode else {}))
    state = bp.load_state(spec)
    patch = repo / "runtime_batch" / "changes.patch"
    patch.parent.mkdir(exist_ok=True)
    patch.write_text(PATCH_BODY, encoding="utf-8")
    ms = bp.milestone_state(state, mid)
    ms.update(status="awaiting-merge", runtime_task_id="rt-x",
              base_before=bp.head(str(repo)), patch=str(patch),
              patch_sha256=hashlib.sha256(patch.read_bytes()).hexdigest())
    bp.save_state(spec, state)
    return spec, state, ms


def evidence(review_status="pass", delivered=True, stable=True,
             touched=(), conflicts=(), patch="x"):
    """一份"框架采集都说得过去"的检视数据；要哪一条坏就单独传哪一条。"""
    view = {"review": {"status": review_status, "round": 1},
            "patch": patch, "patch_lines": 5}
    verdict = {"delivered": delivered, "stable": stable,
               "baseline_touched": list(touched), "conflicts": list(conflicts),
               "delivery": [(delivered, "交付判据样例")],
               "stability": [(stable, "稳定性判据样例")],
               "delivery_label": "已交付" if delivered else "未确认",
               "stability_label": "稳定" if stable else "需人工确认"}
    return view, verdict


class TestGateRefusals:
    def test_each_missing_piece_is_its_own_reason(self, repo):
        spec, state, ms = one_cell_ready(repo)
        target = spec["milestones"][0]
        view, good = evidence()
        assert bp.auto_merge_gate(spec, state, ms, view, good) == []

        cases = [
            ({"patch": ""}, "没有可交接的补丁"),
            ({"patch_sha256": ""}, "补丁哈希没记进状态文件"),
            (None, "Reviewer 没有判 pass"),
            (None, "检视数据读不到"),
        ]
        # 补丁缺失 / 哈希缺失
        for over, needle in cases[:2]:
            broken = dict(ms, **over)
            assert any(needle in r for r in
                       bp.auto_merge_gate(spec, state, broken, view, good)), over
        # Reviewer 不是 pass
        bad_view, bad_verdict = evidence(review_status="fail")
        assert any("Reviewer 没有判 pass" in r for r in
                   bp.auto_merge_gate(spec, state, ms, bad_view, bad_verdict))
        # 读不到检视数据
        assert any("检视数据读不到" in r for r in
                   bp.auto_merge_gate(spec, state, ms, None, None))
        # 交付/稳定性/考卷/冲突各自都要留话
        for kwargs, needle in (
                ({"delivered": False}, "交付判据未全部成立"),
                ({"stable": False}, "稳定性判据未全部成立"),
                ({"touched": ["tests/test_m1.py"]}, "改了自己的考卷"),
                ({"conflicts": ["终态与结论不一致"]}, "事实冲突")):
            v, vd = evidence(**kwargs)
            assert any(needle in r for r in
                       bp.auto_merge_gate(spec, state, ms, v, vd)), kwargs

    def test_a_patch_that_drifted_after_recording_is_refused(self, repo):
        spec, state, ms = one_cell_ready(repo)
        Path(ms["patch"]).write_text(PATCH_BODY + "\n# 事后又动过\n",
                                     encoding="utf-8")
        view, good = evidence()
        reasons = bp.auto_merge_gate(spec, state, ms, view, good)
        assert any("被改过" in r for r in reasons), reasons


class TestAutoMergeWithoutAHuman:
    def test_complete_evidence_merges_and_continues(self, repo, monkeypatch,
                                                    capsys):
        """这一条是整件事的核心：没有人回答，合入照样发生，且只发生一次。"""
        def never_ask(*a, **k):
            raise AssertionError("无人值守不该问人")

        monkeypatch.setattr("builtins.input", never_ask)
        spec, state, ms = one_cell_ready(repo)
        # 检视数据里的补丁路径要是真的那一份 —— _report_ready 会用它覆盖状态里
        # 记下的路径，给个假路径就等于让闸门去找不到文件。
        monkeypatch.setattr(dv, "collect",
                            lambda rt, cd: (evidence(patch=ms["patch"])[0], ""))
        monkeypatch.setattr(dv, "judge", lambda view: evidence()[1])
        before = bp.head(str(repo))
        capsys.readouterr()

        bp._report_ready(spec, state, spec["milestones"][0], ms, print)

        assert bp.head(str(repo)) != before, "证据齐了却没合入"
        assert (repo / "calc.py").read_text(encoding="utf-8").count("a * b") == 1
        assert ms["status"] == "done" and ms["accepted_by"] == "agent-review"
        assert ms["accepted_gate"] == []
        out = capsys.readouterr().out
        assert "同意就一条命令" not in out and "直接合入" in out

    def test_incomplete_evidence_fails_the_cell_instead_of_asking(
            self, repo, monkeypatch, capsys):
        def never_ask(*a, **k):
            raise AssertionError("无人值守不该问人")

        monkeypatch.setattr("builtins.input", never_ask)
        spec, state, ms = one_cell_ready(repo)
        bad_view, bad_verdict = evidence(delivered=False, patch=ms["patch"])
        monkeypatch.setattr(dv, "collect", lambda rt, cd: (bad_view, ""))
        monkeypatch.setattr(dv, "judge", lambda view: bad_verdict)
        before = bp.head(str(repo))
        capsys.readouterr()

        bp._report_ready(spec, state, spec["milestones"][0], ms, print)

        assert bp.head(str(repo)) == before, "判据不齐却合进去了"
        assert ms["status"] == "failed" and "交付判据未全部成立" in ms["detail"]
        out = capsys.readouterr().out
        assert "拒绝合入" in out and "交付判据未全部成立" in out

    def test_human_mode_still_stops_for_the_command_line(self, repo, monkeypatch,
                                                         capsys):
        monkeypatch.setattr(dv, "collect", lambda rt, cd: (evidence()[0], ""))
        monkeypatch.setattr(dv, "judge", lambda view: evidence()[1])
        spec, state, ms = one_cell_ready(repo, mode="human")
        before = bp.head(str(repo))

        bp._report_ready(spec, state, spec["milestones"][0], ms, print)

        assert bp.head(str(repo)) == before
        assert ms["status"] == "awaiting-merge"
        assert "accept --project" in capsys.readouterr().out


class TestDriveRunsTheWholeBatch:
    def test_every_milestone_is_submitted_without_another_human_call(
            self, repo, monkeypatch, capsys):
        calls = []

        def fake_submit_next(spec, state, **kw):
            target, _why = bp.next_step(spec, state)
            ms = bp.milestone_state(state, str(target["id"]))
            ms.update(status="done", commit="deadbeef1234",
                      patch_sha256="ab" * 32, accepted_by="agent-review",
                      runtime_task_id=f"rt-{target['id']}")
            bp.save_state(spec, state)
            calls.append(str(target["id"]))
            return 0

        monkeypatch.setattr(bp, "submit_next", fake_submit_next)
        spec = make_spec(repo, [
            {"id": "m1", "goal": "第一格：把首页做成中文",
             "acceptance": "pytest test_m1.py -q"},
            {"id": "m2", "goal": "第二格：补上离线可用",
             "acceptance": "pytest test_m2.py -q"},
        ], final_acceptance={"name": "总验收",
                             "command": [sys.executable, "-c",
                                         "print('final ok')"]})
        capsys.readouterr()
        assert bp.drive(spec, bp.load_state(spec)) == 0
        assert calls == ["m1", "m2"], "无人值守应当自己走完两格"

        delivery = bp.STATE_DIR / "unattended" / "DELIVERY.md"
        text = delivery.read_text(encoding="utf-8")
        assert "项目完成" in text
        assert "deadbeef1234" in text and ("ab" * 6) in text
        assert "final ok" in text
        assert "98%" not in text and "Online" not in text
        # demo 没声明就写"没有记录"，不编一行退出码
        assert "没有记录" in text

    def test_a_failed_cell_stops_the_batch_instead_of_being_skipped(
            self, repo, monkeypatch, capsys):
        calls = []

        def fake_submit_next(spec, state, **kw):
            target, _why = bp.next_step(spec, state)
            ms = bp.milestone_state(state, str(target["id"]))
            ms.update(status="failed", detail="证据闸门拒绝：补丁哈希漂移")
            bp.save_state(spec, state)
            calls.append(str(target["id"]))
            return 1

        monkeypatch.setattr(bp, "submit_next", fake_submit_next)
        spec = make_spec(repo, [
            {"id": "m1", "goal": "第一格：把首页做成中文",
             "acceptance": "pytest test_m1.py -q"},
            {"id": "m2", "goal": "第二格：补上离线可用",
             "acceptance": "pytest test_m2.py -q"}])
        capsys.readouterr()
        assert bp.drive(spec, bp.load_state(spec)) == 1
        assert calls == ["m1"], "失败格之后不许再提交下一格"
        assert "批次停在 m1" in capsys.readouterr().out

    def test_the_cli_reason_survives_into_the_delivery_note(self, repo,
                                                           monkeypatch,
                                                           capsys):
        """失败那句 CLI 的原因必须原样落在 DELIVERY.md 上。

        2026-09-30 真实那一跑的形状：`codex exec` 退出码 1、stdout 是空的，
        而 `detail` 在写状态文件时被 `[:200]` 切掉 —— 就算 adapter 已经把
        CLI 的 stderr 接进错误消息（地雷 49），人也还是读不到
        "you've hit your usage limit … try again at Oct 4th"，
        只能再花一次额度去复现一个本来写清楚了的失败。
        """
        reason = ("AgentExecutionError: agent exited with code 1, allowed=[0]"
                  " | stderr: error: you've hit your usage limit;"
                  " upgrade to Pro or visit codex/settings/usage to purchase more"
                  " credits, or try again at Oct 4th, 2026 6:58 AM."
                  + " 诊断提示：本条来自 CLI 自己的 stderr，不是框架的猜测。" * 14
                  + " 结尾标记：这一句在 400 字之后。")

        def fake_submit_next(spec, state, **kw):
            target, _why = bp.next_step(spec, state)
            ms = bp.milestone_state(state, str(target["id"]))
            ms.update(status="failed", detail=reason)
            bp.save_state(spec, state)
            return 1

        monkeypatch.setattr(bp, "submit_next", fake_submit_next)
        spec = make_spec(repo, [{"id": "m1", "goal": "第一格：把首页做成中文",
                                 "acceptance": "pytest test_m1.py -q"}])
        capsys.readouterr()
        assert bp.drive(spec, bp.load_state(spec)) == 1
        # `drive` 自己只停批不写文档 —— 写 DELIVERY.md 的是 `run`/`ship`
        # （地雷 45 的②：以前只有 ship 会重写，run --retry 之后人翻到的是上一轮的原因）。
        bp.write_delivery(spec, bp.load_state(spec))

        text = (bp.STATE_DIR / "unattended" / "DELIVERY.md").read_text(
            encoding="utf-8")
        assert "usage limit" in text, text
        assert "Oct 4th, 2026" in text, text
        # 尾巴断言：这一句在 1000 字之外，旧的 `[:400]` 与 `[:900]` 都会把它切掉。
        # 变异检查做过：把显示上限改回 400，这条必红（第一次写的时候它就在 400 内，
        # 于是"看着像断言"其实没有判据 —— 那句记录留在注释里）。
        assert "结尾标记" in text, text[-300:]

    def test_once_flag_keeps_the_old_one_step_shape(self, repo, monkeypatch):
        seen = {}

        def spy(spec, state, **kw):
            seen.update(kw)
            return 0

        monkeypatch.setattr(bp, "submit_next", spy)
        spec = make_spec(repo, [
            {"id": "m1", "goal": "第一格：把首页做成中文",
             "acceptance": "pytest test_m1.py -q"},
            {"id": "m2", "goal": "第二格：补上离线可用",
             "acceptance": "pytest test_m2.py -q"}])
        assert bp.drive(spec, bp.load_state(spec), once=True) == 0
        assert "once" not in seen          # 走的是 submit_next，不是循环
        # human 档同样一次一格
        spec2 = make_spec(repo, [{"id": "m1", "goal": "第一格：把首页做成中文",
                                  "acceptance": "pytest test_m1.py -q"}],
                          mode="human", name="humanbatch")
        calls = []
        monkeypatch.setattr(bp, "submit_next",
                            lambda s, st, **kw: calls.append(1) or 0)
        bp.drive(spec2, bp.load_state(spec2))
        assert calls == [1]


class TestRunAlsoLeavesADeliveryNote:
    """`run` 也要留一份能读的交付说明 —— 以前只有 ship 写。

    真实那一跑就是这么被误读的：`run --retry` 跑完停在失败格，人翻开
    `DELIVERY.md` 看到的还是**上一轮**那一段原因（IllegalStateTransition），
    而状态文件里这一格的 detail 早已是别的东西。文档没坏，是它不再更新。
    """

    def test_run_writes_the_note_after_the_drive(self, repo, monkeypatch):
        written = []
        spec_path = repo / "project.json"
        make_spec(repo, [{"id": "m1", "goal": "第一格：把首页做成中文",
                          "acceptance": "pytest test_m1.py -q"}])
        monkeypatch.setattr(bp, "drive", lambda *a, **k: 0)
        monkeypatch.setattr(
            bp, "write_delivery",
            lambda spec, state: written.append(str(spec["workspace"]))
            or Path(spec_path).parent / "DELIVERY.md")
        rc = bp.main(["run", "--project", str(spec_path),
                      "--config-dir", "examples/config_minimal"])
        assert rc == 0
        assert written == [str(repo)], "run 之后交付说明必须重写一遍（读的人看的就这一份）"

    def test_ship_still_writes_it(self, repo, monkeypatch, capsys):
        written = []
        spec_path = repo / "project.json"
        make_spec(repo, [{"id": "m1", "goal": "第一格：把首页做成中文",
                          "acceptance": "pytest test_m1.py -q"}])
        monkeypatch.setattr(bp, "drive", lambda *a, **k: 0)
        monkeypatch.setattr(
            bp, "write_delivery",
            lambda spec, state: written.append(1) or Path("DELIVERY.md"))
        bp.main(["ship", "--project", str(spec_path),
                 "--config-dir", "examples/config_minimal"])
        assert written == [1]
        assert "交付说明：" in capsys.readouterr().out


class TestCollectedListIsFileGranular:
    """无人值守把"事实冲突"当拒绝理由，所以采集清单必须与自述清单同粒度。

    实测形状：执行者新建一整个目录（src/hooks/…）时，porcelain 会把未跟踪的
    新目录折叠成 `src/hooks/` 一项 —— 于是"改动清单不一致"这条冲突每一格都会
    触发，任何新增目录的交付都永远合不进去。补丁按文件生成，清单也必须按文件。
    """

    def test_new_directory_is_collected_as_files_not_as_a_directory(self, repo):
        from mao.workspaces import WorkspaceStrategy, WorkspaceStrategyManager

        mgr = WorkspaceStrategyManager(worktree_root=repo / "wt2")
        base = mgr.validate_for_submission(repo, WorkspaceStrategy.GIT_WORKTREE)
        plan = mgr.prepare(runtime_task_id="rt-gran", source_path=repo,
                           strategy=WorkspaceStrategy.GIT_WORKTREE,
                           base_revision=base["base_revision"], now_iso="now")
        exec_ws = Path(plan.execution_workspace_path)
        (exec_ws / "src" / "hooks").mkdir(parents=True)
        (exec_ws / "src" / "hooks" / "useEscapeKey.ts").write_text(
            "export const useEscapeKey = () => {};\n", encoding="utf-8")

        result = mgr.collect_result(plan, repo / "arts" / "rt-gran")

        assert "src/hooks/" not in result["changed_files"], result["changed_files"]
        assert "src/hooks/useEscapeKey.ts" in result["changed_files"]
        patch = Path(result["changes_patch"]).read_text(encoding="utf-8")
        assert "src/hooks/useEscapeKey.ts" in patch
        # 同一条冲突判据在 delivery_view 里必须不再触发
        gate = bp.auto_merge_gate(
            {"name": "x", "workspace": str(repo)}, {},
            {"patch": result["changes_patch"],
             "patch_sha256": hashlib.sha256(
                 Path(result["changes_patch"]).read_bytes()).hexdigest()},
            {"review": {"status": "pass"}},
            {"delivered": True, "stable": True, "baseline_touched": [],
             "conflicts": [], "delivery": [(True, "ok")],
             "stability": [(True, "ok")]})
        assert gate == []
class TestBatchLevelSteering:
    """中途改方向要跨格生效：只改正在跑的那一格，下一格又会变回旧方向。"""

    def _two(self, repo):
        return make_spec(repo, [
            {"id": "m1", "goal": "第一格：把首页做成中文",
             "acceptance": "pytest test_m1.py -q"},
            {"id": "m2", "goal": "第二格：补上离线可用",
             "acceptance": "pytest test_m2.py -q"}])

    def test_a_directive_lands_in_later_milestones_prompts(self, repo):
        spec = self._two(repo)
        state = bp.load_state(spec)
        before = bp.goal_for(spec, spec["milestones"][1], state)
        assert bp.steer_batch(spec, state, "标题一律用中文，不要 emoji") == 0
        after = bp.goal_for(spec, spec["milestones"][1],
                            bp.load_state(spec))
        assert "标题一律用中文，不要 emoji" in after
        assert "业主中途补充" in after
        assert after != before
        # 项目档本身不被改：改的是交给执行者的那段话
        assert "directives" not in json.loads(
            (repo / "project.json").read_text(encoding="utf-8"))

    def test_the_directive_is_persisted_not_just_memory(self, repo):
        spec = self._two(repo)
        state = bp.load_state(spec)
        bp.steer_batch(spec, state, "页脚加一行版本号", echo=lambda _s: None)
        reloaded = bp.load_state(spec)
        assert [d["text"] for d in bp.project_directives(reloaded)] == \
               ["页脚加一行版本号"]

    def test_it_also_queues_into_the_running_cell(self, repo, monkeypatch):
        import tools.scheduler_cli as sc
        import mao.core.config as core_config

        seen = {}

        class FakeRepo:
            def add_directive(self, rt, text):
                seen["call"] = (rt, text)
                return 7

            def close(self):
                pass

        monkeypatch.setattr(core_config, "load_config",
                            lambda d, **k: object())
        monkeypatch.setattr(sc, "build_repo_from_config", lambda c: FakeRepo())
        spec = self._two(repo)
        state = bp.load_state(spec)
        bp.milestone_state(state, "m1").update(status="running",
                                               runtime_task_id="rt-live")
        out = []
        assert bp.steer_batch(spec, state, "先别动 tests/", echo=out.append) == 0
        assert seen["call"] == ("rt-live", "先别动 tests/")
        assert any("下一个轮次边界" in line for line in out), out

    def test_a_concurrent_writer_cannot_lose_the_direction_change(self, repo):
        """推进器手里那份状态没有这句话，它整份覆盖时也不许把它弄丢。

        这一条是 e2e 抓出来的：跑中途 steer 的那句没进第二格的简报，因为
        推进器随后用它那份旧状态写了同一个文件。
        """
        spec = self._two(repo)
        steered = bp.load_state(spec)
        stale = bp.load_state(spec)             # 推进器在 steer 之前读的那份
        bp.steer_batch(spec, steered, "整站改成深色主题", echo=lambda _s: None)
        bp.save_state(spec, stale)              # 它之后整份覆盖
        assert [d["text"] for d in
                bp.project_directives(bp.load_state(spec))] == ["整站改成深色主题"]
        # 而且这句话真的会进后面那一格交给执行者的话
        assert "整站改成深色主题" in bp.goal_for(
            spec, spec["milestones"][1], bp.load_state(spec))

    def test_blank_direction_changes_nothing(self, repo):
        spec = self._two(repo)
        state = bp.load_state(spec)
        assert bp.steer_batch(spec, state, "   ") == 2
        assert bp.project_directives(bp.load_state(spec)) == []

    def test_delivery_report_records_the_direction_change(self, repo):
        spec = self._two(repo)
        state = bp.load_state(spec)
        bp.steer_batch(spec, state, "整站改成深色主题", echo=lambda _s: None)
        for m in spec["milestones"]:
            bp.milestone_state(state, str(m["id"]))["status"] = "done"
        text = bp.write_delivery(spec, state).read_text(encoding="utf-8")
        assert "中途改过的方向" in text and "整站改成深色主题" in text


class TestModeIsPartOfTheSpec:
    def test_absent_mode_means_auto(self, repo):
        spec = make_spec(repo, [{"id": "m1", "goal": "把首页做成中文并且能离线打开",
                                 "acceptance": "pytest test_m1.py -q"}])
        assert bp.batch_mode(spec) == "auto"

    def test_a_made_up_mode_is_rejected_by_the_same_validator(self, repo):
        with pytest.raises(bp.BatchError) as exc:
            make_spec(repo, [{"id": "m1", "goal": "把首页做成中文并且能离线打开",
                              "acceptance": "pytest test_m1.py -q"}],
                      mode="maybe")
        assert "auto 或 human" in str(exc.value)

    def test_no_interactive_prompt_survives_in_the_auto_path(self):
        """accept 里那句 input() 只在 human 档可达 —— auto 档不经过它。"""
        src = Path(bp.__file__).read_text(encoding="utf-8")
        assert src.count("input(prompt)") == 1
        guard = [ln for ln in src.splitlines()
                 if 'authorized_by == "human"' in ln]
        assert guard, "没有把交互提问限定在 human 档 —— 无人值守会卡在 input 上"


class TestDriveTakesOverADeadMilestone:
    """drive() 对"已经入队的一格"必须先问**有没有人在跑**，再决定等还是接管。

    现场（2026-09-30 真实那一跑，AGENTS.md 地雷 42）：驱动方那一侧被进程树回收，
    m1 留在 RUNNING 而租约早就过期。drive() 里 `queued/running` 那一段是直接
    `watch_one(...)`（不带调度器），于是推进器活着、日志里只有 RUNNING，
    这一格永远不会再被动 —— 业主那边的症状还是"跑到一半没了动静"。
    """

    class _RT:
        def __init__(self, sink):
            self.sink = sink

        @property
        def log_path(self):
            return "runtime_batch/config/scheduler.log"

        def running(self):
            return "start" in self.sink

        def start(self):
            self.sink.append("start")
            return True, "已启动"

        def stop(self):
            self.sink.append("stop")
            return False, "已停止"

    def test_drive_asks_the_gate_instead_of_only_waiting(self, repo, monkeypatch):
        seen = {}

        def spy_wait(spec, state, target, ms, **kw):
            seen.update(kw)
            seen["milestone"] = target["id"]
            return 0

        monkeypatch.setattr(bp, "wait_for_milestone", spy_wait)
        spec = make_spec(repo, [{"id": "m1", "goal": "第一格：把首页做成中文",
                                 "acceptance": "pytest test_m1.py -q"}])
        state = bp.load_state(spec)
        bp.milestone_state(state, "m1").update(status="queued",
                                               runtime_task_id="rt-dead")
        # drive() 每一轮都从盘上重读状态（两个写者那条，地雷 34），
        # 所以这一格"已经交出去了"必须落到状态文件里，测试测的才是同一条路。
        bp.save_state(spec, state)
        bp.drive(spec, state)
        assert seen.get("milestone") == "m1", seen
        assert seen.get("serve") is True, \
            "把 serve 丢了就等于承认：没人跑也只用等"

    def test_nobody_home_means_a_scheduler_gets_started(self, repo, monkeypatch):
        sink = []
        monkeypatch.setattr(bp, "worker_state", lambda cd, rt: "reclaimable")
        monkeypatch.setattr(bp, "watch_one",
                            lambda *a, **k: sink.append("watch") or 0)
        spec = make_spec(repo, [{"id": "m1", "goal": "第一格：把首页做成中文",
                                 "acceptance": "pytest test_m1.py -q"}])
        state = bp.load_state(spec)
        ms = bp.milestone_state(state, "m1")
        ms.update(status="queued", runtime_task_id="rt-dead")
        rc = bp.wait_for_milestone(spec, state, spec["milestones"][0], ms,
                                   serve=True, runner_factory=lambda cd: self._RT(sink))
        assert rc == 0
        assert sink == ["start", "watch", "stop"], sink

    def test_somebody_home_means_wait_only_and_never_grabs(self, repo, monkeypatch):
        """判据反过来也得成立 —— 否则这条改动只是"永远起一个调度器"。"""
        sink = []
        monkeypatch.setattr(bp, "worker_state", lambda cd, rt: "running")
        monkeypatch.setattr(bp, "watch_one",
                            lambda *a, **k: sink.append("watch") or 0)
        spec = make_spec(repo, [{"id": "m1", "goal": "第一格：把首页做成中文",
                                 "acceptance": "pytest test_m1.py -q"}])
        state = bp.load_state(spec)
        ms = bp.milestone_state(state, "m1")
        ms.update(status="queued", runtime_task_id="rt-live")
        bp.wait_for_milestone(spec, state, spec["milestones"][0], ms, serve=True,
                              runner_factory=lambda cd: self._RT(sink))
        assert sink == ["watch"], sink
