"""发布轮的 CLI 边界回归测试。

这些用例锁的不是新功能，而是"新用户第一次踩到时会怎样"：
提交入口的 JSON 任务文件、版本单一来源、生产配置的默认值。
每一条都对应本轮实际发现过的失败形态。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mao.scheduler import SubmissionError  # noqa: E402
from tools.scheduler_cli import (SUBMIT_ENTRY_KEYS,  # noqa: E402
                                 _submission_entries)


def _args(**overrides):
    """一个与 `queue submit` 真实 flag 集同形的 Namespace。"""
    base = {
        "goal": None, "constraint": None, "workspace": None,
        "max_rounds": None, "priority": "NORMAL", "max_attempts": None,
        "strategy": None, "from_json": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _write(tmp_path, payload):
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


class TestSubmissionEntries:
    def test_plain_flags_still_work(self):
        entries = _submission_entries(_args(goal="do the thing"))
        assert len(entries) == 1
        assert entries[0]["goal"] == "do the thing"
        assert entries[0]["priority"] == "NORMAL"

    def test_goal_is_required_without_file(self):
        with pytest.raises(SubmissionError) as exc:
            _submission_entries(_args())
        assert "--goal" in str(exc.value)

    def test_single_object_file(self, tmp_path):
        path = _write(tmp_path, {
            "goal": "fix multiply",
            "constraints": ["只改 calc.py"],
            "workspace_path": "examples/calculator",
            "max_rounds": 3,
            "priority": "HIGH",
            "max_attempts": 2,
            "strategy": "GIT_WORKTREE",
        })
        entries = _submission_entries(_args(from_json=path))
        assert len(entries) == 1
        one = entries[0]
        assert one["goal"] == "fix multiply"
        assert one["constraints"] == ["只改 calc.py"]
        assert one["priority"] == "HIGH"
        assert one["strategy"] == "GIT_WORKTREE"

    def test_array_file_submits_one_entry_per_task(self, tmp_path):
        path = _write(tmp_path, [
            {"goal": "a", "priority": "HIGH"},
            {"goal": "b"},
            {"goal": "c", "priority": "LOW"},
        ])
        entries = _submission_entries(_args(from_json=path))
        assert [e["goal"] for e in entries] == ["a", "b", "c"]
        assert [e["priority"] for e in entries] == ["HIGH", "NORMAL", "LOW"]

    def test_file_wins_for_keys_it_declares(self, tmp_path):
        """规则必须可预测：文件是任务定义，缺的键才回落到 flag。"""
        path = _write(tmp_path, {"goal": "from file"})
        entries = _submission_entries(_args(from_json=path, goal="from cli",
                                            priority="LOW"))
        assert entries[0]["goal"] == "from file"      # 文件里有 -> 以文件为准
        assert entries[0]["priority"] == "LOW"        # 文件里没有 -> 用命令行

    def test_unknown_key_is_rejected_with_the_allowed_list(self, tmp_path):
        path = _write(tmp_path, {"goal": "x", "prioriy": "HIGH"})
        with pytest.raises(SubmissionError) as exc:
            _submission_entries(_args(from_json=path))
        message = str(exc.value)
        assert "prioriy" in message            # 拼错的键被点名
        for key in SUBMIT_ENTRY_KEYS:
            assert key in message              # 并且告诉用户能用哪些

    def test_missing_goal_in_entry_is_rejected(self, tmp_path):
        path = _write(tmp_path, [{"goal": "ok"}, {"constraints": []}])
        with pytest.raises(SubmissionError) as exc:
            _submission_entries(_args(from_json=path))
        assert "goal" in str(exc.value)

    def test_entry_must_be_object(self, tmp_path):
        path = _write(tmp_path, ["just a string"])
        with pytest.raises(SubmissionError) as exc:
            _submission_entries(_args(from_json=path))
        assert "不是 JSON 对象" in str(exc.value)

    def test_missing_file_is_a_message_not_a_traceback(self, tmp_path):
        with pytest.raises(SubmissionError) as exc:
            _submission_entries(_args(from_json=str(tmp_path / "nope.json")))
        assert "读不了" in str(exc.value)

    def test_invalid_json_points_at_the_line(self, tmp_path):
        path = tmp_path / "tasks.json"
        path.write_text('{"goal": "x",,}', encoding="utf-8")
        with pytest.raises(SubmissionError) as exc:
            _submission_entries(_args(from_json=str(path)))
        assert "合法 JSON" in str(exc.value)

    def test_none_in_file_does_not_erase_a_flag_value(self, tmp_path):
        """文件里显式 null = 没填，不该把命令行给的值覆盖成 None。"""
        path = _write(tmp_path, {"goal": "g", "max_rounds": None})
        entries = _submission_entries(_args(from_json=path, max_rounds=4))
        assert entries[0]["max_rounds"] == 4


class TestResultMarkdown:
    """RESULT.md：交付说明必须可读，且不能假装已经合并（§46/§47）。"""

    def _attempt_dir(self, tmp_path, *, with_patch: bool):
        import json

        attempt = tmp_path / "runtime" / "rt-1" / "attempt1"
        task_dir = attempt / "task_t1"
        artifacts = attempt / "artifacts"
        task_dir.mkdir(parents=True)
        artifacts.mkdir(parents=True)
        (task_dir / "task.json").write_text(json.dumps(
            {"task_id": "task_t1", "goal": "fix multiply"}), encoding="utf-8")
        (task_dir / "state.json").write_text(json.dumps({
            "task_id": "task_t1", "current_state": "completed",
            "current_round": 1, "max_rounds": 3,
            "attempts": [{"round": 1, "execution_status": "success",
                          "review_status": "pass", "review_reason": "ok",
                          "summary": "changed calc.py"}]}), encoding="utf-8")
        (task_dir / "execution.json").write_text(json.dumps({
            "changed_files": ["calc.py", "claimed_extra.py"],
            "evidence": {"test_result": "2 passed",
                         "git_diff_stat": {"calc.py": 2}}}), encoding="utf-8")
        (task_dir / "review.json").write_text(json.dumps({
            "status": "pass", "reason": "all criteria satisfied", "round": 1,
            "passed_checks": [{"criterion_id": "ac1", "description": "multiply works",
                               "satisfied": True, "detail": "pytest green"}],
            "failed_checks": []}), encoding="utf-8")
        (artifacts / "workspace_result.json").write_text(json.dumps({
            "workspace_strategy": "GIT_WORKTREE" if with_patch else "COPY",
            "execution_workspace_path": str(tmp_path / "worktree"),
            "base_revision": "deadbeef" if with_patch else "",
            "changed_files": ["calc.py"]}), encoding="utf-8")
        if with_patch:
            (artifacts / "changes.patch").write_text(
                "--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-a\n+b\n",
                encoding="utf-8")
        return attempt

    def test_patch_branch_documents_manual_apply(self, tmp_path):
        from mao.result_report import write_result_md

        attempt = self._attempt_dir(tmp_path, with_patch=True)
        out = write_result_md(attempt, outcome="COMPLETED", error="")
        assert out and out.exists()
        text = out.read_text(encoding="utf-8")
        assert "COMPLETED" in text
        assert "changes.patch" in text
        assert "git -C <你的项目目录> apply" in text
        assert "程序不会替你执行" in text
        # 框架采集的那份与 Agent 自述的那份必须分开呈现
        assert "claimed_extra.py" in text and "框架那份" in text
        assert "✓ ac1" in text

    def test_copy_branch_says_there_is_no_patch(self, tmp_path):
        from mao.result_report import write_result_md

        attempt = self._attempt_dir(tmp_path, with_patch=False)
        out = write_result_md(attempt, outcome="FAILED", error="review rejected")
        text = out.read_text(encoding="utf-8")
        assert "本次没有 changes.patch" in text
        assert "FAILED" in text and "review rejected" in text

    def test_missing_artifacts_do_not_raise(self, tmp_path):
        from mao.result_report import write_result_md

        assert write_result_md(tmp_path / "nothing", outcome="X") is None


class TestRunBoard:
    """看板与实时追踪：只读是它的立身之本 —— 观测面不该变成副作用源。"""

    def _config(self, tmp_path, monkeypatch, *, db="./q.db"):
        import textwrap

        from tools import delivery_view as dv

        cfg = tmp_path / "config"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "settings.yaml").write_text(textwrap.dedent(f"""
            runtime_dir: runtime
            scheduler:
              enabled: true
              db_path: {db}
              attempts_root: runtime
            checkpoint:
              enabled: true
        """), encoding="utf-8")
        (cfg / "agents.yaml").write_text(
            "supervisor:\n  provider: mock_supervisor\n", encoding="utf-8")
        monkeypatch.setattr(dv, "ROOT", tmp_path)
        return tmp_path

    def test_missing_db_is_reported_absent_not_created(self, tmp_path, monkeypatch):
        from tools import delivery_view as dv

        self._config(tmp_path, monkeypatch)
        assert dv.queue_rows("config") == []
        assert not (tmp_path / "q.db").exists(), \
            "看板把不存在的队列库建出来了 —— 观测面变成了写入方"

    def test_reading_a_run_never_modifies_the_queue_db(self, tmp_path, monkeypatch):
        from mao.core.models import Task
        from mao.scheduler import SystemClock, TaskRepository, TaskSubmissionService
        from tools import delivery_view as dv

        root = self._config(tmp_path, monkeypatch)
        repo = TaskRepository(root / "q.db", clock=SystemClock())
        try:
            rt = TaskSubmissionService(repo, clock=repo.clock).submit(
                Task(goal="<script>别把我当 HTML</script>", max_rounds=1))
            rt_id = rt.runtime_task_id
        finally:
            repo.close()
        before = (root / "q.db").read_bytes()

        rows = dv.queue_rows("config")
        snap = dv.snapshot("config", rows[0])
        after = (root / "q.db").read_bytes()

        assert snap["runtime_task_id"] == rt_id
        assert snap["status"] == "QUEUED"
        assert before == after, "只读连接不该改动队列库一个字节"

    def test_two_configs_on_one_db_are_one_run_not_two(self, tmp_path, monkeypatch):
        from mao.core.models import Task
        from mao.scheduler import SystemClock, TaskRepository, TaskSubmissionService
        from tools import delivery_view as dv

        root = self._config(tmp_path, monkeypatch)
        repo = TaskRepository(root / "q.db", clock=SystemClock())
        try:
            TaskSubmissionService(repo, clock=repo.clock).submit(
                Task(goal="shared db", max_rounds=1))
        finally:
            repo.close()
        second = root / "config_p8"
        second.mkdir(exist_ok=True)
        (second / "settings.yaml").write_text(
            "scheduler:\n  enabled: true\n  db_path: ./q.db\n", encoding="utf-8")
        (second / "agents.yaml").write_text("supervisor:\n  provider: x\n",
                                            encoding="utf-8")

        rows = dv.board(["config", "config_p8"])
        assert len(rows) == 1, "同一份库里的同一条任务被数成了两条运行"
        assert rows[0]["also_in"] == ["config_p8"]

    def test_board_html_escapes_user_supplied_text(self, tmp_path, monkeypatch):
        from mao.core.models import Task
        from mao.scheduler import SystemClock, TaskRepository, TaskSubmissionService
        from tools import delivery_view as dv

        root = self._config(tmp_path, monkeypatch)
        repo = TaskRepository(root / "q.db", clock=SystemClock())
        try:
            TaskSubmissionService(repo, clock=repo.clock).submit(
                Task(goal="<script>alert(1)</script>", max_rounds=1))
        finally:
            repo.close()
        page = dv.render_board_html(dv.board(["config"]), refresh=5)
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;" in page or "alert" not in page
        assert "http-equiv='refresh'" in page

    def test_latest_committed_uses_insertion_order(self, tmp_path, monkeypatch):
        """按 rowid 取最新，不按 id 字典序 —— 那个坑踩过三次（AGENTS.md 地雷 2）。"""
        import sqlite3

        from tools import delivery_view as dv

        root = self._config(tmp_path, monkeypatch)
        (root / "runtime").mkdir(exist_ok=True)
        db = root / "runtime" / "checkpoints.db"
        con = sqlite3.connect(str(db))
        con.execute(
            "CREATE TABLE checkpoint_records (checkpoint_id TEXT PRIMARY KEY,"
            " task_id TEXT, runtime_task_id TEXT, attempt INTEGER, round_no INTEGER,"
            " stage TEXT, status TEXT, created_at TEXT, committed_at TEXT)")
        # 故意让"旧"记录的 id 字典序更大：插入顺序才是真相
        con.execute("INSERT INTO checkpoint_records VALUES (?,?,?,?,?,?,?,?,?)",
                    ("zz-later-id", "task_a", "rt_a", 1, 0, "STALE_ONE",
                     "COMMITTED", "", ""))
        con.execute("INSERT INTO checkpoint_records VALUES (?,?,?,?,?,?,?,?,?)",
                    ("aa-newer-id", "task_a", "rt_a", 1, 1, "FRESH_ONE",
                     "COMMITTED", "", ""))
        con.execute("INSERT INTO checkpoint_records VALUES (?,?,?,?,?,?,?,?,?)",
                    ("mm-preparing", "task_a", "rt_a", 1, 1, "HANGING",
                     "PREPARING", "", ""))
        con.commit()
        con.close()

        assert dv.latest_committed("config", "task_a", "rt_a") == "FRESH_ONE"


class TestBoardCellSemantics:
    """看板单元格的一个记号就是一句结论 —— 说错状态等于伪造判据。"""

    def test_missing_patch_is_flagged_only_for_completed_runs(self):
        from tools import delivery_view as dv

        row = {"patch_lines": 0, "strategy": "GIT_WORKTREE"}
        # 取消的运行本就没有可交接物，标 ! 是噪声
        assert "!" not in dv._patch_cell(dict(row, status="CANCELLED"))
        assert "!" in dv._patch_cell(dict(row, status="COMPLETED"))
        # 有补丁就不该标；DIRECT 策略本就不产 changes.patch
        assert "!" not in dv._patch_cell(dict(row, status="COMPLETED",
                                              patch_lines=12))
        assert "!" not in dv._patch_cell(dict(row, status="COMPLETED",
                                              strategy="DIRECT"))

    def test_last_error_on_a_live_run_is_labelled_history_not_error(self):
        from tools import delivery_view as dv

        notes = dv._note_rows({"status": "COMPLETED", "last_error": "resume from "
                                                                    "checkpoint"})
        assert len(notes) == 1 and notes[0].startswith("·")
        assert not notes[0].startswith("!")

        failed = dv._note_rows({"status": "FAILED", "last_error": "boom"})
        assert failed[0].startswith("!")

    def test_orphan_worktree_pointer_is_the_named_reason_for_zero_changes(
            self, tmp_path):
        """.git 存在不等于仓库有效（AGENTS.md 地雷 5）：断了的指针要说出来，
        否则"框架采集 0 个改动"读起来就像 Agent 什么都没做。"""
        from tools import delivery_view as dv

        ws = tmp_path / "wt"
        ws.mkdir()
        view = {"execution_workspace_path": str(ws), "patch": "",
                "workspace_strategy": "GIT_WORKTREE"}

        (ws / ".git").write_text("gitdir: %s\n" % (tmp_path / "gone"),
                                 encoding="utf-8")
        assert dv._orphan_worktree(view) is True

        live = tmp_path / "live"
        live.mkdir()
        (live / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (ws / ".git").write_text("gitdir: %s\n" % live, encoding="utf-8")
        assert dv._orphan_worktree(view) is False
        # DIRECT 策略本就没有 worktree，不许顺带标成损坏
        assert dv._orphan_worktree(dict(view, workspace_strategy="DIRECT")) is False

    def test_where_marks_only_paths_that_are_actually_there(self):
        """现场那栏是按目录布局推出来的 —— 推出来的路径得说它在不在。"""
        from tools import delivery_view as dv

        assert dv._where("") == "（无）"
        assert dv._where(__file__) == __file__
        assert dv._where(str(__file__) + ".not-there").endswith("（不在本机）")

    def test_board_and_detail_view_share_one_error_judgement(self):
        """判据写两遍就是这个项目里缺陷的形状（AGENTS.md「判据归属」）。"""
        from tools import delivery_view as dv

        for status in ("COMPLETED", "FAILED", "BLOCKED", "CANCELLED"):
            row = {"status": status, "last_error": "stale lease -> resume"}
            assert dv._note_rows(row)[0].startswith("!") == \
                dv._is_live_failure(row), f"{status} 两处说法不一致"

    def test_absent_workspace_keeps_the_end_that_identifies_it(self):
        """旧机器路径的区分度在头部；两头砍法一样，两行就渲染成了同一行。"""
        from tools import delivery_view as dv

        old = (r"C:\Users\Administrator\Desktop\mao\runtime_p10\offline"
               r"\demo_source")
        here = (r"C:\Users\EDY\Desktop\mao\runtime_p10\offline"
                r"\demo_source")
        row = {"config_dir": "config", "runtime_task_id": "rt_1",
               "status": "COMPLETED", "stage": "", "calls": {},
               "also_in": [], "patch_lines": 0, "strategy": "DIRECT",
               "review_status": "", "verification_failed": 0,
               "verification_ran": 0, "last_error": ""}
        page = dv.render_board_text(
            [dict(row, workspace=old, workspace_here=False),
             dict(row, workspace=here, workspace_here=True)])
        assert r"C:\Users\Administrator" in page, \
            "旧机器路径被截掉了头，看不出来自哪台机器"
        assert page.count("（不在本机）") == 1
        assert "…trator" not in page
