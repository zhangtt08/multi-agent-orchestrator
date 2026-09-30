"""业主中途改方向（directives）—— 排队、按轮消耗、跨进程还能查回来。

要证的三件事，每件都对应一种真实失败：
1. 一句话只会被**某一轮**用掉一次（重复注入 = 同一句补充把执行者刷屏）；
2. Reviewer 在崩溃后的新进程里仍然拿得到本轮的补充（内存字段天生为空 → 会按
   旧方向判，AGENTS.md 地雷 16 的同一形状）；
3. 终态任务不收话（收了就没有下一轮会读它，界面上却显示"已排队"—— 假答应）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from mao.core.models import EventType, Task
from mao.core.orchestrator import Orchestrator
from mao.scheduler import (FakeClock, RuntimeStatus, TaskRepository,
                           TaskSubmissionService)
from mao.scheduler.models import SchedulerEventType
from mao.scheduler.scheduler import SchedulerControl


@pytest.fixture
def queue_env(tmp_path: Path):
    clock = FakeClock()
    repo = TaskRepository(tmp_path / "queue.db", clock=clock)
    submission = TaskSubmissionService(repo, clock=clock)
    return clock, repo, submission


def _running(repo, submission, goal: str = "steerable"):
    """提交并手动推到 RUNNING —— 排话不需要真跑一轮，但要有非终态的行。"""
    rt = submission.submit(Task(goal=goal))
    repo.update_status(rt.runtime_task_id, RuntimeStatus.RUNNING)
    return rt


class TestDirectiveQueue:
    def test_pending_then_taken_once_with_round_tag(self, queue_env):
        clock, repo, submission = queue_env
        rt = _running(repo, submission)
        did = repo.add_directive(rt.runtime_task_id, "改用中文演示站")
        clock.advance(1)

        assert [d["id"] for d in repo.pending_directives(rt.runtime_task_id)] == [did]
        taken = repo.take_directives(rt.runtime_task_id, round_no=2)
        assert [d["text"] for d in taken] == ["改用中文演示站"]
        # 取走即消耗：再来一轮不许拿到同一句
        assert repo.take_directives(rt.runtime_task_id, round_no=3) == []
        assert repo.pending_directives(rt.runtime_task_id) == []

    def test_take_is_ordered_by_insertion_not_lexicographic(self, queue_env):
        """FIFO 判据是 rowid；同一秒提交时字典序会把后说的当成先说的。"""
        clock, repo, submission = queue_env
        rt = _running(repo, submission)
        first = repo.add_directive(rt.runtime_task_id, "aaa 后来的方向")
        repo.add_directive(rt.runtime_task_id, "zzz 先说的方向")
        clock.advance(1)
        taken = repo.take_directives(rt.runtime_task_id, round_no=1)
        assert [d["id"] for d in taken] == sorted([first, taken[1]["id"]])
        assert taken[0]["id"] == first

    def test_terminal_task_refuses_a_directive(self, queue_env):
        clock, repo, submission = queue_env
        rt = _running(repo, submission)
        repo.update_status(rt.runtime_task_id, RuntimeStatus.COMPLETED)
        assert repo.add_directive(rt.runtime_task_id, "再来一句") is None
        assert repo.pending_directives(rt.runtime_task_id) == []

    @pytest.mark.parametrize("text", ["", "   ", "\n"])
    def test_blank_text_is_not_queued(self, queue_env, text):
        _, repo, submission = queue_env
        rt = _running(repo, submission)
        assert repo.add_directive(rt.runtime_task_id, text) is None

    def test_applied_round_is_queryable_after_the_fact(self, queue_env):
        clock, repo, submission = queue_env
        rt = _running(repo, submission)
        repo.add_directive(rt.runtime_task_id, "首页要能离线打开")
        clock.advance(1)
        repo.take_directives(rt.runtime_task_id, round_no=3)
        rows = repo.directives_for_round(rt.runtime_task_id, round_no=3)
        assert [r["text"] for r in rows] == ["首页要能离线打开"]
        assert repo.directives_for_round(rt.runtime_task_id, round_no=4) == []
        ledger = repo.directive_ledger(rt.runtime_task_id)
        assert ledger[0]["applied_round"] == 3

    def test_queued_but_never_used_stays_visible(self, queue_env):
        """跑完没用上的话必须还能看见（界面上写"还在排队"，不是"已生效"）。"""
        _, repo, submission = queue_env
        rt = _running(repo, submission)
        repo.add_directive(rt.runtime_task_id, "最后一轮没轮到它")
        ledger = repo.directive_ledger(rt.runtime_task_id)
        assert ledger[0]["applied_round"] == 0
        assert ledger[0]["consumed_at"] == ""

    def test_events_record_both_ends_of_the_line(self, queue_env):
        clock, repo, submission = queue_env
        rt = _running(repo, submission)
        repo.add_directive(rt.runtime_task_id, "补一句")
        clock.advance(1)
        repo.take_directives(rt.runtime_task_id, round_no=1)
        events = [e["event"] for e in repo.events_for(rt.runtime_task_id)]
        assert SchedulerEventType.DIRECTIVE_QUEUED.value in events
        assert SchedulerEventType.DIRECTIVE_APPLIED.value in events


class TestSchedulerControlHandle:
    def _control(self, repo, rt_id):
        return SchedulerControl(repo, rt_id, worker_id="w1", lease_seconds=60.0)

    def test_control_delegates_to_the_repository(self, queue_env):
        clock, repo, submission = queue_env
        rt = _running(repo, submission)
        ctl = self._control(repo, rt.runtime_task_id)
        assert ctl.take_directives(2) == []          # 没话就是没话
        repo.add_directive(rt.runtime_task_id, "方向修正")
        clock.advance(1)
        assert [d["text"] for d in ctl.take_directives(2)] == ["方向修正"]
        assert [d["text"] for d in ctl.directives_for_round(2)] == ["方向修正"]

    def test_control_failure_does_not_kill_the_task(self, queue_env):
        """队列库瞬时故障 → 当作没话。正在跑的一轮不许因为控制面报错而打死。"""
        _, repo, submission = queue_env
        rt = _running(repo, submission)

        class _Broken:
            def get(self, _rid):
                raise RuntimeError("database is locked")

            def add_directive(self, *a, **k):
                raise RuntimeError("database is locked")

            def take_directives(self, *a, **k):
                raise RuntimeError("database is locked")

            def directives_for_round(self, *a, **k):
                raise RuntimeError("database is locked")

        ctl = SchedulerControl(_Broken(), rt.runtime_task_id, worker_id="w1",
                               lease_seconds=60.0)
        assert ctl.take_directives(1) == []
        assert ctl.directives_for_round(1) == []


class TestOrchestratorInjection:
    """Orchestrator 侧的两个取话函数 —— 用 duck-typed self，不建整个 runtime。"""

    def _fake(self, control, round_no: int = 2):
        logged = []
        return SimpleNamespace(
            state=SimpleNamespace(current_round=round_no),
            runtime_control=control,
            _directive_blocks={},
            _log=lambda event, message, payload=None: logged.append(
                (event, message, payload)),
        ), logged

    def test_directive_text_reaches_the_executor_brief(self):
        ctl = SimpleNamespace(take_directives=lambda r: [
            {"id": 1, "text": "只要中文界面"}], directives_for_round=lambda r: [])
        fake, logged = self._fake(ctl)
        block = Orchestrator._take_round_directives(fake)
        assert "只要中文界面" in block
        assert fake._directive_blocks[2] == block
        assert logged and logged[0][0] is EventType.USER_DIRECTIVE_APPLIED
        assert logged[0][2]["directive_ids"] == [1]

    def test_same_directive_is_not_logged_twice_in_one_round(self):
        seen = {"n": 0}

        def take(round_no):
            seen["n"] += 1
            return [] if seen["n"] > 1 else [{"id": 7, "text": "别改 tests"}]

        ctl = SimpleNamespace(take_directives=take,
                              directives_for_round=lambda r: [])
        fake, logged = self._fake(ctl)
        first = Orchestrator._take_round_directives(fake)
        again = Orchestrator._take_round_directives(fake)
        assert first == again
        assert len(logged) == 1

    def test_resumed_process_reads_the_round_from_the_store(self):
        """新进程里没有缓存（_directive_blocks 空）也必须拿得到本轮的补充。"""
        rows = [{"id": 3, "text": "验收要跑 pytest"}]
        ctl = SimpleNamespace(take_directives=lambda r: [],
                              directives_for_round=lambda r: rows)
        fake, logged = self._fake(ctl, round_no=5)
        assert Orchestrator._round_directive_block(fake) == "- 验收要跑 pytest"
        assert fake._directive_blocks[5] == "- 验收要跑 pytest"
        assert logged == []          # 读回不等于再次生效

    def test_no_control_handle_is_zero_overhead(self):
        """Phase 1-7 的形状：没有 runtime_control 时不报错、不改简报。"""
        fake, logged = self._fake(None)
        assert Orchestrator._take_round_directives(fake) == ""
        assert Orchestrator._round_directive_block(fake) == ""
        assert logged == []


class TestSchemaUpgrade:
    def test_old_database_gets_the_directive_table(self, tmp_path):
        """v3 库直接升级：不许要求删 queue DB（§53）。"""
        db = tmp_path / "queue.db"
        clock = FakeClock()
        seed = TaskRepository(db, clock=clock)
        seed.close()
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE schema_version SET version = 3")
        conn.execute("DROP TABLE task_directives")
        conn.commit()
        conn.close()

        reopened = TaskRepository(db, clock=clock)
        try:
            row = reopened._connection().execute(
                "SELECT version FROM schema_version LIMIT 1").fetchone()
            assert row["version"] == 4
            rt = TaskSubmissionService(reopened, clock=clock).submit(
                Task(goal="upgraded"))
            reopened.update_status(rt.runtime_task_id, RuntimeStatus.RUNNING)
            assert reopened.add_directive(rt.runtime_task_id, "升级后还能排话")
            assert [d["text"] for d in
                    reopened.pending_directives(rt.runtime_task_id)] == \
                   ["升级后还能排话"]
        finally:
            reopened.close()
