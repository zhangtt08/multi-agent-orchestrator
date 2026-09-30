"""交付检视器 HTML 渲染的守卫。

`render_html` 是面板运行详情页真正渲染的东西，之前**一条直接测试都没有**
（只被 test_release_cli / test_workbench_ui 间接碰过）。这里先补上 loop 里
最容易被忘掉的那条腿：验收 Agent 交回给执行 Agent 的返工提示词。
"""
from __future__ import annotations

import pytest

from tools import delivery_view as dv


def _view(review=None, **over):
    """一份照着 `collect()` 真实返回形状搭的 view（子字段形状取自线上一条运行）。

    键缺一个就会以 KeyError 通过测试掩盖掉真判据，所以这里按 collect 的形状给全。
    """
    v = {
        "runtime_task_id": "rt-test", "task_id": "task-test",
        "status": "COMPLETED", "failure_class": "", "config_dir": "config",
        "attempt": 1, "max_attempts": 3, "resume_epoch": 0,
        "goal": "做一个能打开的首页", "constraints": [],
        "workspace_strategy": "COPY",
        "plan": {"acceptance_criteria": [], "verification_commands": [],
                 "tasks": [], "executor_prompt": ""},
        "review": review if review is not None else {},
        "execution": {"summary": "我建了 index.html", "changed_files": [],
                      "commands_run": [], "tests": [], "errors": [],
                      "artifacts": [], "remaining_issues": [],
                      "evidence": {}, "status": "COMPLETED", "round": 1},
        "state": {"current_round": 1, "current_state": "COMPLETED",
                  "last_error": None, "max_rounds": 2, "attempts": [],
                  "plan_round": 1},
        "checkpoint": {"db": "", "chain": 1, "committed": 1, "hanging": [],
                       "broken": [], "fingerprints": [], "store": "ok"},
        "lease": {"active": False, "expired": False, "detail": ""},
        "ws_result": {"workspace_strategy": "COPY",
                      "execution_workspace_path": "", "base_revision": "",
                      "git_status": "", "changes_patch": "",
                      "changed_files": []},
        "calls": [], "patch": "", "patch_lines": 0, "attempt_dirs": [],
        "result_md": "", "execution_workspace_path": "",
        "workspace_path": "", "base_revision": "",
    }
    v.update(over)
    return v


_VERDICT = {"delivered": True, "stable": True, "delivery_label": "已交付",
            "stability_label": "稳定", "delivery": [], "stability": [],
            "conflicts": []}


class TestReplanPromptLeg:
    def test_next_prompt_is_shown_verbatim_and_labeled_as_reviewer(self):
        page = dv.render_html(
            _view({"status": "fail", "round": 1,
                   "next_prompt": "index.html 缺 <title>，补上后再交"}),
            _VERDICT)
        assert "验收 Agent 交回给执行 Agent 的话" in page
        assert "index.html 缺 &lt;title&gt;，补上后再交" in page
        assert "round=1" in page

    def test_absent_next_prompt_says_so_instead_of_printing_an_empty_box(self):
        page = dv.render_html(_view({"status": "pass", "round": 2}), _VERDICT)
        assert "没有留下返工提示词" in page

    def test_a_rework_round_only_present_in_a_snapshot_is_still_shown(self):
        """先 FAIL 后修好的运行：最终那份 review.json 里没有 next_prompt，
        FAIL 那一轮的原话只活在 attempt/checkpoint 快照里。

        旧代码只读最终 review.json，于是对着这样一次运行说"这一轮没有返工提示词"，
        并且写了一句"有返工的运行会在上面显示原文" —— 那句话是假的。
        真实档实测：rt-50aac87b2412（FAILED）round 2 的 brief 就只在快照里。
        """
        v = _view({"status": "pass", "round": 3, "next_prompt": None})
        v["rework_prompts"] = [
            {"round": 2, "next_prompt": "先让路由回滚完成再 resolve close",
             "source": "runtime/example/rt-50aac87b2412/attempt1/task_1/review.json"}]
        page = dv.render_html(v, _VERDICT)
        assert "先让路由回滚完成再 resolve close" in page
        assert "round=2" in page
        assert "attempt1/task_1/review.json" in page
        assert "没有留下返工提示词" not in page

    def test_next_prompt_is_escaped_not_executed(self):
        page = dv.render_html(
            _view({"status": "fail", "next_prompt": "<script>bad()</script>"}),
            _VERDICT)
        assert "<script>bad()</script>" not in page
        assert "&lt;script&gt;" in page

    def test_long_prompt_is_truncated_rather_than_dropped(self):
        body = "x" * 2000
        page = dv.render_html(_view({"status": "fail", "next_prompt": body}),
                              _VERDICT)
        assert "x" * 1200 in page and "x" * 1300 not in page


class TestFailedButDeliverable:
    """判 FAILED 而采集器拿到了一整份改动 —— 这个矛盾必须说出来。

    实测（rt-2575d7a28865）：执行者把页面建出来了、`changes.patch` 227 行，
    却因为自述信封不合格整格 FAILED。此前检视器 conflicts 是**空的**，
    于是"有货但判红"和"什么都没干"长得一模一样。
    """

    def _v(self, status, patch_lines, files):
        v = _view({"status": "pass" if status == "COMPLETED" else "fail",
                   "round": 1}, status=status)
        v["patch_lines"] = patch_lines
        v["ws_result"]["changed_files"] = files
        return v

    def test_a_failed_slice_with_real_changes_is_flagged(self):
        verdict = dv.judge(self._v("FAILED", 227, ["index.html", "styles.css"]))
        hits = [c for c in verdict["conflicts"] if "可交接" in c]
        assert len(hits) == 1
        assert "227 行" in hits[0] and "index.html" in hits[0]

    def test_reporting_the_conflict_does_not_upgrade_the_verdict(self):
        """把 FAILED 说成已交付才是作弊 —— 这里只报告矛盾。"""
        verdict = dv.judge(self._v("FAILED", 227, ["index.html"]))
        assert verdict["delivery_label"] == "未确认"
        assert verdict["delivered"] is False

    def test_a_failed_slice_with_nothing_collected_is_not_flagged(self):
        verdict = dv.judge(self._v("FAILED", 0, []))
        assert not [c for c in verdict["conflicts"] if "可交接" in c]

    def test_a_completed_slice_is_not_called_a_conflict(self):
        verdict = dv.judge(self._v("COMPLETED", 190, ["index.html"]))
        assert not [c for c in verdict["conflicts"] if "可交接" in c]

    def test_the_conflict_reaches_the_rendered_page(self):
        page = dv.render_html(self._v("FAILED", 227, ["index.html"]),
                              dv.judge(self._v("FAILED", 227, ["index.html"])))
        assert "事实冲突" in page and "可交接" in page


class TestBaselineIntegrity:
    """执行者改不改考卷 —— 这条判据只能问 git，不能按字节哈希。

    实测（2026-09-28 真实批次）：本机 core.autocrlf 让 worktree checkout 成 CRLF，
    源仓库工作树是 LF，逐字节比三个验收文件全部 DIFF —— 按哈希写守卫的话，
    一次干净交付会被判成作弊。
    """

    @pytest.fixture()
    def repo(self, tmp_path):
        import subprocess

        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    assert 1\n",
                                                      encoding="utf-8")
        (tmp_path / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
        for args in (["init", "-q", "."], ["add", "-A"],
                     ["-c", "user.name=t", "-c", "user.email=t@t",
                      "commit", "-q", "-m", "base"]):
            subprocess.run(["git", *args], cwd=str(tmp_path), check=True,
                           capture_output=True, text=True)
        return tmp_path

    def _v(self, repo):
        v = _view({"status": "pass", "round": 1})
        v["workspace_strategy"] = "GIT_WORKTREE"
        v["execution_workspace_path"] = str(repo)
        v["plan"] = {"verification_commands": [
            {"command": "pytest tests/test_x.py -q"}],
            "acceptance_criteria": []}
        return v

    def test_command_tokens_become_the_baseline_list(self, repo):
        assert dv.acceptance_files_of(self._v(repo)) == ["tests/test_x.py"]

    def test_argv_lists_are_read_as_lists_not_as_str_repr(self, repo):
        """框架采集的 `command` 就是 argv 列表（真实档实测）。

        按 `str()` 切会把 `"['pytest', 'tests/x.py']"` 里的 `'tests/x.py',`
        当成路径 —— 守卫于是查一个不存在的文件，并报告"未改动"：假的平安。
        """
        v = self._v(repo)
        v["plan"]["verification_commands"] = [
            {"command": ["pytest", "tests/test_x.py", "-q"]}]
        assert dv.acceptance_files_of(v) == ["tests/test_x.py"]
        assert dv.baseline_touched(v) == []
        (repo / "tests" / "test_x.py").write_text("def test_x():\n    pass\n",
                                                  encoding="utf-8")
        assert dv.baseline_touched(v) == ["tests/test_x.py"]

    def test_a_clean_workspace_reports_the_check_ran(self, repo):
        verdict = dv.judge(self._v(repo))
        assert verdict["baseline_touched"] == []
        line = [t for ok, t in verdict["stability"] if "验收基线" in t]
        assert line and line[0].startswith("验收基线未被执行者改动")
        assert [ok for ok, t in verdict["stability"] if "验收基线" in t] == [True]

    def test_a_modified_baseline_is_refused_as_undelivered(self, repo):
        (repo / "tests" / "test_x.py").write_text(
            "def test_x():\n    assert 1  # 自己给自己打分\n", encoding="utf-8")
        verdict = dv.judge(self._v(repo))
        assert verdict["baseline_touched"] == ["tests/test_x.py"]
        assert verdict["stable"] is False
        assert any("考卷" in c for c in verdict["conflicts"])

    def test_creating_a_new_baseline_file_is_not_tampering(self, repo):
        """按设计，先建考卷的那一格就是要新增 —— `??` 不算改。"""
        (repo / "tests" / "test_new.py").write_text("def test_n():\n    pass\n",
                                                    encoding="utf-8")
        v = self._v(repo)
        v["plan"]["verification_commands"] = [
            {"command": "pytest tests/test_new.py -q"}]
        assert dv.baseline_touched(v) == []

    def test_a_dead_worktree_pointer_does_not_claim_safety(self, repo):
        """采不到判据 ≠ 没被改。取不到时不许写成"未改动"。"""
        v = self._v(repo)
        v["execution_workspace_path"] = str(repo / "nope")
        verdict = dv.judge(v)
        assert verdict["baseline_touched"] is None
        line = [t for ok, t in verdict["stability"] if "验收基线" in t]
        assert line and "无法判定" in line[0]
        assert [ok for ok, t in verdict["stability"] if "验收基线" in t] == [False]
        assert verdict["stable"] is False


class TestVerificationEvidenceIsTheFrameworksOwn:
    """验收判定只问**框架跑过的那一份**，执行者自述的探索命令不算证据。

    真实那一跑（2026-09-30）：Codex 为了看文件内容跑了四次
    `git diff --no-index -- NUL <文件>`，那条命令在"确实有差异"时退出 1。
    这些被记在 `execution.commands_run`（自述）里，而闸门把它们当成
    "框架实际跑了 9 条验证命令，非零退出 4 条" —— 于是框架自己那条
    `python -m unittest discover ...`（exit 0）的合格交付被判 FAILED。
    """

    def _self_report_noise(self, extra_verif):
        v = _view({"status": "pass", "round": 1,
                   "passed_checks": [{"criterion_id": "ac_1",
                                      "satisfied": True}]},
                  plan={"acceptance_criteria": [{"criterion_id": "ac_1",
                                                 "required": True}],
                        "verification_commands": [], "tasks": [],
                        "executor_prompt": ""})
        v["execution"]["commands_run"] = [
            {"command": "git -c core.quotePath=false diff --no-index -- NUL a.md",
             "exit_code": 1},
            {"command": "git -c core.quotePath=false diff --no-index -- NUL b.md",
             "exit_code": 1},
        ]
        v["execution"]["evidence"] = {"extra": {"verification": extra_verif}}
        return v

    def test_self_reported_nonzero_does_not_veto_a_passed_delivery(self):
        verdict = dv.judge(self._self_report_noise(
            [{"name": "acceptance-baseline", "exit_code": 0,
              "command_display": "python -m unittest discover -s tests -v"}]))
        assert any("框架实际跑了 1 条验证命令，非零退出 0 条" in text
                   for ok, text in verdict["delivery"]), verdict["delivery"]
        assert not [c for c in verdict["conflicts"]
                    if "非零退出" in c], verdict["conflicts"]

    def test_the_self_report_is_still_surfaced_just_not_as_a_verdict(self):
        """自述不判生死，但也不能藏起来 —— 它是供述，读的人有权看到。"""
        verdict = dv.judge(self._self_report_noise(
            [{"name": "x", "exit_code": 0}]))
        line = [text for _ok, text in verdict["delivery"] if "自述" in text]
        assert line and "2 条" in line[0] and "不参与验收判定" in line[0], line

    def test_a_framework_command_that_really_fails_still_refuses(self):
        """"别用自述判"不等于"放宽"：框架那条真红了，判据必须照红。"""
        verdict = dv.judge(self._self_report_noise(
            [{"name": "acceptance-baseline", "exit_code": 1,
              "command_display": "pytest -q"}]))
        assert any("非零退出 1 条" in text for _ok, text in verdict["delivery"])
        assert any("非零退出" in c for c in verdict["conflicts"]), verdict["conflicts"]

    def test_only_a_self_report_means_there_is_no_mechanical_evidence(self):
        """框架一条都没记录时，不许拿自述凑成"已验证"。"""
        v = self._self_report_noise([])
        v["execution"]["evidence"] = {}
        verdict = dv.judge(v)
        assert any("只有执行者自述" in text for _ok, text in verdict["delivery"]), \
            verdict["delivery"]


class TestSnapshotCountsTheFrameworksCommands:
    def test_the_board_uses_the_persisted_framework_record(self, tmp_path,
                                                            monkeypatch):
        """看板那句"! 框架验证 X/Y"以前数的是自述 —— 同一个错，另一个入口。"""
        import json

        cfg = tmp_path / "config"
        cfg.mkdir()
        (cfg / "settings.yaml").write_text(
            "runtime_dir: runtime\nscheduler:\n  attempts_root: runtime\n",
            encoding="utf-8")
        task = tmp_path / "runtime" / "rt-x" / "attempt1" / "task_t1"
        (task / "artifacts").mkdir(parents=True)
        (task / "execution.json").write_text(json.dumps({
            "commands_run": [{"command": "git diff --no-index", "exit_code": 1}],
            "evidence": {"extra": {"verification": [
                {"name": "acc", "exit_code": 0}]}}}), encoding="utf-8")
        monkeypatch.setattr(dv, "ROOT", tmp_path)
        snap = dv.snapshot("config", {"runtime_task_id": "rt-x",
                                      "task_id": "task_t1"})
        assert snap["verification_ran"] == 1, snap
        assert snap["verification_failed"] == 0, snap
